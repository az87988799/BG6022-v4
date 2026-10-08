"""Validate and persist local terminal delivery within the existing Run.

This module checks declared relations, not arbitrary natural-language reasoning.
Source selection and scientific judgments belong to delivery/goals and Tools.
"""

from __future__ import annotations

import hashlib
import json

from orca_agent.model_usage import current_basis
from orca_agent.models import ReportReceipt, TerminalReceipt, fingerprint
from orca_agent.proposals import ContractStopParameters, ProposalError
from orca_agent.store import ControlChanged, StoreError, atomic_write

CONTRACT_VERSION = "terminal-delivery-1"


def _reject(code, path, **detail):
    raise ProposalError("Choose only the current goal/fact/relation/action combination.",
                        code=code, path=path, **detail)


def validate_terminal_explanation(parameters, snapshot):
    """Require complete, scoped facts and the program's compatible relations."""
    from orca_agent.proposals import validate_action_parameters

    validate_action_parameters("stop", parameters, terminal_required=True)
    delivery = ContractStopParameters.model_validate(parameters).delivery
    if snapshot.get("version") != CONTRACT_VERSION:
        _reject("terminal_contract_version", ["parameters", "delivery", "version"])
    goals = {row["ref"]: row for row in snapshot["goals"]}
    facts = {row["ref"]: row for row in snapshot["facts"]}
    blockers = {row["ref"]: row for row in snapshot["blockers"]}
    explanations = {row["ref"]: row for row in snapshot["explanations"]}
    actions = {row["ref"]: row for row in snapshot["next_actions"]}
    refs = [item.goal_ref for item in delivery.goal_explanations]
    required = {key for key, goal in goals.items() if goal["required"]}
    if len(refs) != len(set(refs)) or not required <= set(refs) or not set(refs) <= set(goals):
        _reject("terminal_goal_coverage", ["parameters", "delivery", "goal_explanations"],
                required_goals=sorted(required))
    for index, item in enumerate(delivery.goal_explanations):
        path = ["parameters", "delivery", "goal_explanations", index]
        goal = goals[item.goal_ref]
        chosen_facts, chosen_blockers = set(item.fact_refs), set(item.blocker_refs)
        if (len(chosen_facts) != len(item.fact_refs)
                or not set(goal["required_fact_refs"]) <= chosen_facts
                or any(ref not in facts or facts[ref].get("goal_ref") != item.goal_ref
                       for ref in chosen_facts)):
            _reject("terminal_fact_scope", [*path, "fact_refs"],
                    required_refs=goal["required_fact_refs"])
        if (len(chosen_blockers) != len(item.blocker_refs)
                or not set(goal["required_blocker_refs"]) <= chosen_blockers
                or any(ref not in blockers or item.goal_ref not in blockers[ref].get("goal_refs", [])
                       for ref in chosen_blockers)):
            _reject("terminal_blocker_coverage", [*path, "blocker_refs"],
                    required_refs=goal["required_blocker_refs"])
        explanation = explanations.get(item.explanation_ref)
        if (item.explanation_ref not in goal["explanation_refs"] or not explanation
                or explanation.get("goal_ref") != item.goal_ref
                or not set(explanation.get("fact_refs", [])) <= chosen_facts):
            _reject("terminal_incompatible_relation", [*path, "explanation_ref"],
                    allowed_refs=goal["explanation_refs"])
        action = actions.get(item.next_action_ref)
        if (item.next_action_ref not in goal["next_action_refs"] or not action
                or item.goal_ref not in action.get("goal_refs", [])
                or not set(action.get("blocker_refs", [])) <= chosen_blockers):
            _reject("terminal_incompatible_next_action", [*path, "next_action_ref"],
                    allowed_refs=goal["next_action_refs"])
    return delivery.model_dump(mode="json")


def read_delivery_snapshot(store, run, record):
    """Read the immutable snapshot that accompanied this actual HTTP request."""
    digest = record.get("delivery_snapshot_sha256")
    if not digest:
        _reject("terminal_snapshot_missing", ["parameters", "delivery"])
    path = store.path(f"runs/{run.id}/model/{record['id']}.delivery.json")
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise StoreError("terminal delivery snapshot missing or outside its size bound")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise StoreError("terminal delivery snapshot changed")
    snapshot = json.loads(raw)
    from orca_agent.model_usage import _request_body

    body = _request_body(store, run, record)
    payload = json.loads(body["messages"][-1]["content"])
    authority = payload["AUTHORITY"]
    if (authority.get("delivery_snapshot_fingerprint") != snapshot.get("fingerprint")
            or authority.get("contract_required") is not True):
        raise StoreError("terminal snapshot differs from the transmitted authority")
    return snapshot


def accept_terminal(store, run, record, proposal, basis, *, fault=None):
    """One atomic Run update accepts a stop and its applied terminal state."""
    from orca_agent.delivery import collect_delivery_snapshot
    from orca_agent.runner import _goals, _step_results

    with store.control_lock(run.id):
        if current_basis(store, run) != basis or store.read_signal(run.id):
            raise ControlChanged("stale terminal proposal after user/control change")
        existing = next((item for item in run.terminal_deliveries
                         if item.decision_id == record["id"]), None)
        if existing:
            return run
        snapshot = read_delivery_snapshot(store, run, record)
        request, plan = store.load_request(run), store.load_plan(run)
        results = _step_results(store, run)
        complete = _goals(store, run, plan, results)
        current = collect_delivery_snapshot(store, run, request, plan, results,
                                            control_generation=basis["control_generation"])
        if current["fingerprint"] != snapshot["fingerprint"]:
            _reject("terminal_snapshot_stale", ["parameters", "delivery", "snapshot_ref"])
        explanation = validate_terminal_explanation(proposal.parameters, snapshot)
        if fault:
            fault("after_terminal_validated")
        if current_basis(store, run) != basis or store.read_signal(run.id):
            raise ControlChanged("terminal basis changed before atomic acceptance")
        # Source files are external to the control lock. Re-read their hashes
        # after validation before publishing an accepted receipt; a previously
        # valid snapshot cannot grant acceptance after its evidence changes.
        current = collect_delivery_snapshot(store, run, request, plan,
                                            control_generation=basis["control_generation"])
        if current["fingerprint"] != snapshot["fingerprint"]:
            _reject("terminal_snapshot_stale", ["parameters", "delivery", "snapshot_ref"])
        ticket = record["id"]
        if any(item["id"] == ticket for item in run.decisions):
            raise StoreError("terminal decision already recorded without its atomic receipt")
        run.state = "completed" if complete else "failed"
        run.decisions.append({"id": ticket, "basis": basis, "action": "stop",
                              "related_results": proposal.related_results,
                              "reason": proposal.reason, "parameters": proposal.parameters})
        run.processed_feedback = list(dict.fromkeys(run.processed_feedback + proposal.related_results))
        run.control_generation = basis["control_generation"]
        run.applied_decisions.append(ticket)
        run.terminal_deliveries.append(TerminalReceipt(
            decision_id=ticket, contract_version=CONTRACT_VERSION, basis=basis,
            snapshot_fingerprint=snapshot["fingerprint"], snapshot=snapshot,
            explanation=explanation, terminal_state=run.state))
        store.save_run(run)
        if fault:
            fault("after_terminal_saved")
    return run


def active_terminal(run):
    return next((item for item in reversed(run.terminal_deliveries)
                 if item.decision_id not in run.reopened_terminal_ids), None)


def report_publication_id(run, snapshot):
    """Stable identity includes audit changes without granting execution rights."""
    return "report_" + fingerprint({"snapshot": snapshot["fingerprint"], "state": run.state,
        "usage": run.usage.model_dump(mode="json"), "decisions": run.decisions,
        "diagnostics": run.diagnostics, "model_records": run.model_records})[:24]


def publish_terminal_report(store, run, *, fault=None):
    """Publish one stable report identity; failures never erase accepted facts."""
    receipt = active_terminal(run)
    from orca_agent.delivery import collect_delivery_snapshot
    from orca_agent.report import build_report, render_report

    with store.control_lock(run.id):
        current = collect_delivery_snapshot(store, run, store.load_request(run), store.load_plan(run),
                                            control_generation=current_basis(store, run)["control_generation"])
        if receipt is None:
            # Registration, clarification and failed explanations still have a
            # deterministic report. Its receipt is never used by active_terminal.
            publication_id = report_publication_id(run, current)
            receipt = next((item for item in run.fallback_report_receipts
                            if item.publication_id == publication_id), None)
            if receipt is None:
                receipt = ReportReceipt(publication_id=publication_id, basis=current_basis(store, run),
                    snapshot_fingerprint=current["fingerprint"], snapshot=current)
                run.fallback_report_receipts.append(receipt)
                store.save_run(run)
        else:
            publication_id = receipt.decision_id
        if current["fingerprint"] != receipt.snapshot_fingerprint:
            # Keep a previously published receipt intact as historical evidence.
            if receipt.report_status != "rendered":
                receipt.report_status = "failed"
                receipt.report_error = "source_or_basis_unverified"
                store.save_run(run)
            return run
        target = f"runs/{run.id}/deliveries/{publication_id}.md"
        path = store.path(target)
        try:
            if path.exists():
                raw = path.read_bytes()
                if not receipt.report_sha256 or hashlib.sha256(raw).hexdigest() != receipt.report_sha256:
                    raise StoreError("published terminal report changed")
            else:
                raw = render_report(build_report(store, run)).encode("utf-8")
                # Publish intent/hash first; a crash after the file write cannot
                # make an arbitrary existing file into a trusted report.
                receipt.report_path = target
                receipt.report_sha256 = hashlib.sha256(raw).hexdigest()
                store.save_run(run)
                atomic_write(path, raw, immutable=True)
            if fault:
                fault("after_terminal_report_written")
            receipt.report_status = "rendered"
            receipt.report_path = target
            receipt.report_sha256 = hashlib.sha256(raw).hexdigest()
            receipt.report_error = None
            store.save_run(run)
        except (OSError, ValueError, RuntimeError):
            receipt.report_status = "failed"
            receipt.report_error = "report_publication_failed"
            store.save_run(run)
    return run
