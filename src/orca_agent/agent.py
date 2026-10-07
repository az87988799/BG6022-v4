"""One feedback loop, with model and deterministic selection strategies."""

import importlib
import json
import os

import psutil

from orca_agent import runner
from orca_agent.backends import local
from orca_agent.context import build_context
from orca_agent.llm import DeepSeekTransport
from orca_agent.model_usage import current_basis, send_model
from orca_agent.models import EvidenceRef, Proposal, fingerprint, utc_now
from orca_agent.proposals import materialize_plan, valid_call_tool_parameters
from orca_agent.store import BudgetExceeded, ControlChanged, EnvironmentBusy, StoreError
from orca_agent.tools.dispatch import _source_hashes, execute_call
from orca_agent.tools.registry import get_tool


def _ready(plan, results):
    return [s for s in plan.steps if s.id not in results
            and all(dep in results for dep in s.depends_on)] if plan else []


def _science(store, config, run, step, results, batch, fault):
    from orca_agent.natural import ensure_scientific_environment
    ensure_scientific_environment(store, config, run)
    reference = step.geometry
    if reference.artifact_id:
        geometry_id = reference.artifact_id
    else:
        output = results[reference.producer_step_id].qualified_outputs.get(reference.port)
        if output is None or not output.artifact_id:
            raise StoreError("required geometry did not pass scientific checks")
        required = get_tool(step.tool).required_input_checks.get(reference.port)
        if not required or any(check.rule_version != required for check in output.checks):
            raise StoreError("producer geometry rule does not meet the consumer requirement")
        geometry_id = output.artifact_id
    runner._validate_execution_rules(store, run)
    if run.batch_category and not batch:
        raise StoreError("acceptance Run requires its shared batch accounting")
    ticket = None

    def reserve(draft):
        nonlocal ticket
        ticket = batch.reserve_science(run, step, geometry_id, draft_attempt=draft)

    attempt = store.reserve_attempt(run, step, geometry_id, before_reserve=reserve if batch else None)
    attempt.execution_handle = {"job_name": local.new_job_name(), "coordinator_pid": os.getpid(),
                                "coordinator_create_time": psutil.Process().create_time()}
    store.update_lease_handle(run.id, attempt.id, attempt.execution_handle)
    store.save_run(run)
    if fault:
        fault("after_intent_saved")
    module, name = get_tool(step.tool).implementation.rsplit(".", 1)
    implementation = getattr(importlib.import_module(module), name)
    result, outcome = implementation(store, run, step, attempt, config, fault)
    store.save_result(result)
    if fault:
        fault("after_result_saved")
    runner._settle(store, run, attempt, result, outcome)
    if batch:
        batch.settle_science(run, ticket, attempt)
    if fault:
        fault("after_run_updated")
    return result, outcome


def _mark_decision(store, run, ticket, proposal, basis, *, action=None):
    with store.control_lock(run.id):
        if current_basis(store, run) != basis or store.read_signal(run.id):
            raise StoreError("stale proposal after user/control change")
        if any(d["id"] == ticket for d in run.decisions):
            return
        run.decisions.append({"id": ticket, "basis": basis, "action": action or proposal.action,
                              "related_results": proposal.related_results,
                              "reason": proposal.reason, "parameters": proposal.parameters})
        run.processed_feedback = list(dict.fromkeys(run.processed_feedback + proposal.related_results))
        if not any(m["id"] not in run.processed_messages for m in store.read_control(run.id)["messages"]):
            run.control_generation = basis["control_generation"]
        store.save_run(run)


def _decision(store, run, plan, results, transport, batch, fault):
    from dataclasses import asdict
    from datetime import datetime, timedelta
    from time import sleep

    from orca_agent.model_usage import read_model_reply

    basis = current_basis(store, run)
    feedback = [rid for rid in run.result_ids if rid not in run.processed_feedback]
    logical_id = "decision_" + fingerprint({"basis": basis, "feedback": feedback})[:24]
    rejection = None
    records = [r for r in run.model_records if r.get("logical_id", "").startswith(logical_id + "_")]
    max_requests = 1 + run.budget.corrections_per_proposal + run.budget.transport_retries

    def recovery_kind(reply):
        category = reply.get("error_category")
        if category in {"authentication", "permission", "token_bound_exceeded", "credential_in_response",
                        "response_missing", "redirect_rejected", "response_encoding_rejected"}:
            return "fatal"
        if category in {"rate_limit", "timeout", "connection", "transport_error", "http_error"}:
            return "transport" if reply.get("retryable") else "fatal"
        return "correction"

    def rejected_state():
        """Reconstruct independent limits from this logical request's durable receipts."""
        by_id = {item["id"]: item for item in records}
        counts = {"transport": 0, "correction": 0, "fatal": 0}
        previous = None
        for decision in run.decisions:
            if decision["id"] not in by_id or decision.get("action") != "rejected":
                continue
            reply = asdict(read_model_reply(store, run, by_id[decision["id"]])[0])
            counts[recovery_kind(reply)] += 1
            previous = decision.get("parameters", {})
        if (counts["fatal"] or counts["transport"] > run.budget.transport_retries
                or counts["correction"] > run.budget.corrections_per_proposal):
            raise StoreError("bounded proposal correction or transport retry exhausted")
        return previous

    while len(records) <= max_requests:
        previous_rejection = rejected_state()
        if previous_rejection:
            rejection = {"category": previous_rejection.get("error_category", "rejected"),
                         "requirement": previous_rejection.get("requirement", "prior proposal rejected")}
        handled = {d["id"] for d in run.decisions}
        pending = [r for r in records if r["id"] not in handled]
        record = pending[0] if pending else None
        if record:
            path = store.path(f"runs/{run.id}/model/{record['id']}.response.json")
            if not path.exists():
                raise StoreError("unknown HTTP outcome retained; no implicit resend")
            reply = asdict(read_model_reply(store, run, record)[0])
        else:
            if (len(records) >= max_requests or run.usage.decision_rounds >= run.budget.decision_rounds
                    or run.usage.model_calls >= run.budget.model_calls
                    or run.usage.model_tokens_used + run.usage.model_tokens_unknown >= run.budget.model_tokens):
                raise BudgetExceeded("decision/correction budget exhausted")
            # The provider's bounded delay is persisted with its rejection so a
            # pause/crash cannot reset either the wait or its associated budget.
            retry_at = (previous_rejection or {}).get("retry_not_before")
            if retry_at:
                retry_at = datetime.fromisoformat(retry_at)
                if retry_at >= run.deadline:
                    raise BudgetExceeded("transport retry delay exceeds the remaining Run deadline")
                while (delay := (retry_at - utc_now()).total_seconds()) > 0:
                    if current_basis(store, run) != basis or store.read_signal(run.id):
                        return run, "stale", None
                    sleep(min(delay, 0.2))
            run.usage.decision_rounds += 1
            store.save_run(run)
            control = store.read_control(run.id)
            data = {"new_result_ids": feedback, "validation_error": rejection,
                    "pending_step_ids": [s.id for s in _ready(plan, results)],
                    "allowed_repairs": run.permission.allowed_repairs}
            selected = [store.load_result(run.id, rid) for rid in run.result_ids]
            from orca_agent.goals import goal_evidence_assessment
            request = store.load_request(run)
            current_use = []
            for goal in request.goals:
                binding = plan.goal_map.get(goal.id) if plan else None
                source = results.get(binding.step_id) if binding else None
                reference = run.goal_evidence.get(goal.id) or (binding.evidence if binding else None)
                if reference:
                    if reference.run_id != run.id and reference.result_id not in run.permission.result_ids:
                        raise StoreError("goal evidence is outside the permission snapshot")
                    source = store.load_result(reference.run_id, reference.result_id)
                    if reference.attempt_id and source.attempt_id != reference.attempt_id:
                        raise StoreError("goal evidence Attempt differs")
                if source is not None:
                    assessment = goal_evidence_assessment(store, run, request, goal, source)
                    current_use.append({"goal_id": goal.id, "result_id": source.id, **assessment})
            if current_use:
                data["current_goal_use"] = current_use
            relevant_tools = []
            for name in run.permission.allowed_tools:
                effects = set(get_tool(name).effects)
                if "execute_orca" in effects and not run.permission.scientific_execution:
                    continue
                if (effects & {"create_artifact", "write_artifact", "copy_external_artifact",
                               "import_artifact", "write_analysis"} and not run.permission.artifact_writes):
                    continue
                relevant_tools.append(name)
            from orca_agent.semantic import action_parameters
            has_pending_messages = any(m["id"] not in run.processed_messages for m in control["messages"])
            prepared = build_context(store.load_request(run), run, plan, results=selected,
                                     feedback=data, relevant_tools=[] if has_pending_messages else relevant_tools,
                                     control_generation=basis["control_generation"],
                                     user_messages=control["messages"],
                                     action_parameters=action_parameters(relevant_tools, request=request)
                                     if has_pending_messages else None)
            if run.batch_category and not batch:
                raise StoreError("acceptance model calls require shared batch accounting")
            response = send_model(store, run, prepared, transport, basis=basis,
                                  logical_id=f"{logical_id}_{len(records) + 1}", batch=batch, fault=fault)
            record = run.model_records[-1]
            records.append(record)
            reply = asdict(response)
        ticket = record["id"]
        if current_basis(store, run) != basis or store.read_signal(run.id):
            run.diagnostics.append({"category": "stale_model_response", "model_id": ticket})
            store.save_run(run)
            return run, "stale", None
        try:
            if reply.get("error_category") or not reply.get("proposal"):
                raise ValueError(reply.get("error_category") or "missing_proposal")
            proposal = Proposal.model_validate(reply["proposal"])
            if {name: getattr(proposal, name) for name in basis} != basis:
                raise StoreError("proposal version basis differs from the transmitted context")
            if set(proposal.related_results) != set(feedback):
                from orca_agent.proposals import ProposalError
                raise ProposalError("Copy AUTHORITY.related_results exactly. External source Result references "
                                    "belong in Step.inputs, not related_results.",
                                    path=["related_results"], expected=list(feedback))
            goals_complete = runner._goals(store, run, plan, results)
            if goals_complete and proposal.action != "stop" and not any(
                    m["id"] not in run.processed_messages for m in store.read_control(run.id)["messages"]):
                raise StoreError("completed goals permit a final explanation only, not additional execution")
            pending_user = [m for m in store.read_control(run.id)["messages"]
                            if m["id"] not in run.processed_messages]
            if pending_user and proposal.action not in {"normalize_request", "clarify", "stop"}:
                raise StoreError("unprocessed user conditions require clarification before execution")
            if proposal.action == "normalize_request":
                from orca_agent.semantic import commit_candidate
                run = commit_candidate(store, run, proposal.parameters, decision_id=ticket,
                                       basis=basis, related_results=feedback, fault=fault)
                request = store.load_request(run)
                return run, ("stop" if request.normalization_status == "clarification"
                             else "normalized"), None
            if proposal.action in {"initial_plan", "revise_plan"}:
                if (proposal.action == "initial_plan") != (plan is None):
                    raise StoreError("plan action does not match the current Plan")
                saved = store.path(f"runs/{run.id}/decisions/{ticket}.json")
                if saved.exists():
                    from orca_agent.models import Plan
                    candidate = Plan.model_validate(json.loads(saved.read_text())["plan"])
                else:
                    candidate = materialize_plan(store, run, proposal.parameters)
                _repair_evidence(store, run, candidate)
                run = store.commit_revision(run, candidate, decision_id=ticket, basis=basis,
                                            related_results=proposal.related_results, fault=fault)
                return run, "plan", None
            if proposal.action == "call_tool":
                values = proposal.parameters
                ready = _ready(plan, results)
                if not valid_call_tool_parameters(values):
                    readonly = [name for name in run.permission.allowed_tools
                                if get_tool(name).effects == ["read_registered_artifact"]
                                and run.usage.evidence_reads < run.budget.evidence_reads]
                    if not ready and not readonly:
                        from orca_agent.proposals import ProposalError
                        plan_action = "revise_plan" if plan else "initial_plan"
                        raise ProposalError(
                            f"No ready Step/read Tool; only {plan_action} (new Steps), clarify, or stop.",
                            path=["action"])
                    raise StoreError("call_tool parameters must be {tool,parameters} or {step_id}; no action wrapper")
                if "step_id" in values:
                    step = next((s for s in ready if s.id == values["step_id"]), None)
                    if step is None:
                        from orca_agent.proposals import ProposalError
                        raise ProposalError(
                            "Choose an exact ID from expected_ready_step_ids. A Step with a settled Result "
                            "cannot be executed again; a Step whose dependencies are not ready cannot execute.",
                            path=["parameters", "step_id"], expected_ready_step_ids=[s.id for s in ready])
                    _mark_decision(store, run, ticket, proposal, basis)
                    return run, "step", (step, ticket)
                if get_tool(values["tool"]).effects != ["read_registered_artifact"]:
                    raise StoreError("this Tool imports/writes/executes and requires a planned Step: "
                                     "use initial_plan or revise_plan; only read_registered_artifact is immediate")
                store.reserve_call(run, values["tool"], values["parameters"], validate_only=True)
                # Record action before the Tool reservation; replay never silently duplicates it.
                _mark_decision(store, run, ticket, proposal, basis)
                return run, "query", (values, ticket)
            if proposal.action == "clarify":
                if set(proposal.parameters) != {"questions", "unresolved"} or not all(
                        isinstance(proposal.parameters[k], list) and proposal.parameters[k]
                        and len(proposal.parameters[k]) <= 5
                        and all(isinstance(v, str) and 0 < len(v) <= 1000 for v in proposal.parameters[k])
                        for k in ("questions", "unresolved")):
                    from orca_agent.proposals import ProposalError
                    raise ProposalError(
                        "clarify parameters must contain exactly questions and unresolved. "
                        "Each must be a list of 1..5 nonempty strings, with each string at most 1000 characters. "
                        "Combine related unresolved fields when more than five items are needed.",
                        path=["parameters"], required_fields=["questions", "unresolved"],
                        min_items=1, max_items=5, min_string_length=1, max_string_length=1000)
                if pending_user:
                    from orca_agent.semantic import VERSION, commit_candidate
                    run = commit_candidate(store, run, {
                        "schema_version": VERSION, "message_ids": [m["id"] for m in pending_user],
                        "kind": "clarify", "text_basis": pending_user[0]["text"][:1000],
                        **proposal.parameters}, decision_id=ticket, basis=basis,
                        related_results=feedback, fault=fault)
                else:
                    _mark_decision(store, run, ticket, proposal, basis)
                    if fault:
                        fault("after_clarification_saved")
                run.diagnostics.append({"category": "clarification", **proposal.parameters})
                run.state = "waiting_user"
                store.save_run(run)
                return run, "stop", None
            if (set(proposal.parameters) not in (set(), {"reason"}) or
                    "reason" in proposal.parameters and not isinstance(proposal.parameters["reason"], str)):
                raise ValueError("invalid stop parameters")
            _mark_decision(store, run, ticket, proposal, basis)
            run.state = "completed" if goals_complete else "failed"
            store.save_run(run)
            return run, "stop", None
        except (ValueError, KeyError, TypeError) as exc:
            # Error values and full provider text never enter diagnostics.
            category = reply.get("error_category") or type(exc).__name__
            from pydantic import ValidationError

            from orca_agent.proposals import ProposalError
            detail = (exc.detail if isinstance(exc, ProposalError) else
                      [{"type": e["type"], "loc": list(e["loc"])} for e in exc.errors()]
                      if isinstance(exc, ValidationError) else
                      str(exc)[:300] if isinstance(exc, (StoreError, ValueError)) else category)
            rejection = {"category": category, "requirement": detail}
            metadata = {"error_category": category, "recovery_kind": recovery_kind(reply),
                        "requirement": detail}
            if metadata["recovery_kind"] == "transport":
                metadata["retry_not_before"] = (utc_now() + timedelta(
                    seconds=reply.get("retry_after_seconds") or 0)).isoformat()
            rejected = Proposal(action="stop", **basis, related_results=[], reason="rejected",
                                parameters=metadata)
            _mark_decision(store, run, ticket, rejected, basis, action="rejected")
            run.diagnostics.append({"category": "proposal_rejected", "model_id": ticket,
                                    "error_category": category})
            store.save_run(run)
            rejected_state()
    raise BudgetExceeded("model request budget exhausted")


def _repair_evidence(store, run, plan):
    """SCF candidates apply only to explicit real SCF failure evidence."""
    for step in plan.steps:
        if any(a.step_id == step.id for a in run.attempts):
            continue
        prior = [a for a in run.attempts if a.logical_id == step.logical_id and a.step_id != step.id]
        if not prior:
            continue
        attempt = prior[-1]
        if not attempt.result_id or attempt.state in ("unknown", "running", "intent"):
            raise StoreError("repair requires reconciled failure evidence")
        result = store.load_result(run.id, attempt.result_id)
        current_request = store.load_request(run)
        if (attempt.request_version is not None and attempt.request_version < run.request_version
                and current_request.messages and all(m.get("source") == "user"
                                                      for m in current_request.messages)):
            continue
        if not any(d.get("category") == "scf_not_converged" for d in result.diagnostics):
            raise StoreError("SCF repair is not applicable to this failure")
        # Explicit SCF failure can coexist with a more serious collection,
        # resource or execution failure. Those facts take precedence over a
        # MaxIter proposal; they do not establish an applicable SCF repair.
        ordinary_scf_diagnostics = {"scf_not_converged", "abnormal_or_incomplete_output",
                                    "optimization_not_converged", "failed"}
        execution = result.source.get("execution", {})
        if (attempt.state not in {"completed", "failed"}
                or result.operation_status not in {"completed", "failed"}
                or not isinstance(execution, dict)
                or execution.get("state") != result.operation_status
                or execution.get("reason") not in {"nonzero_exit_code", "process_tree_exited"}
                or execution.get("budget_violations") or execution.get("postprocess_starts_detected")
                or any(d.get("category") not in ordinary_scf_diagnostics for d in result.diagnostics)
                or any(not isinstance(d.get("source", {}), dict) or d.get("source", {}).get("conflict")
                       for d in result.diagnostics)
                or "energy" in result.qualified_outputs):
            raise StoreError("SCF repair requires uncontested failure evidence; reconcile the other failure first")
        required = {"input_integrity", "orca_version", "method_and_electronic_state",
                    "initial_geometry", "parser_consistency", "scf_converged"}
        checks = [c for c in result.checks.get("energy", []) if c.name in required]
        from orca_agent.versions import CURRENT_CHECK_VERSION
        if (len(checks) != len(required) or {c.name for c in checks} != required
                or any(c.rule_version != CURRENT_CHECK_VERSION or c.status != (
                    "failed" if c.name == "scf_converged" else "passed") for c in checks)):
            raise StoreError("SCF repair lacks checked input, source or explicit SCF failure evidence")
        _source_hashes(store, run.id, result)


def _bind_query_goals(store, run, result):
    from orca_agent.goals import validate_goal_evidence
    request = store.load_request(run)
    for goal in request.goals:
        if validate_goal_evidence(store, run, request, goal, result):
            run.goal_evidence[goal.id] = EvidenceRef(
                run_id=run.id, result_id=result.id, port=goal.port,
                rule_version=goal.minimum_check_version)


def execute(store, config, run_id, *, resume=False, fault=None, transport=None, batch=None):
    with store.run_lock(run_id):
        run = store.load_run(run_id)
        plan = store.load_plan(run)
        if resume:
            before = store.read_control(run.id)
            if batch:
                batch.reconcile_science(run)
            if not runner._recover(store, config, run, plan) or not store.recover_calls(run):
                return run
            if batch:
                batch.reconcile_science(run)
            from orca_agent.model_usage import recover_models
            recovered = recover_models(store, run, batch=batch)
            if any(reply.error_category == "response_missing" for reply in recovered.values()):
                run.state = "unknown"
                store.save_run(run)
                return run
            with store.control_lock(run.id):
                unchanged_control = before == store.read_control(run.id)
                if unchanged_control and store.read_signal(run.id) == "pause":
                    store.signal(run.id, None)
                control = store.read_control(run.id)
                if not control["action"]:
                    if not any(m["id"] not in run.processed_messages for m in control["messages"]):
                        run.control_generation = control["generation"]
                    if unchanged_control and run.state == "paused":
                        run.state = "ready" if plan else "waiting_user"
                    store.save_run(run)
        elif any(a.state in ("intent", "running", "unknown") for a in run.attempts):
            raise StoreError("unfinished attempts require explicit resume and reconciliation")
        results = runner._step_results(store, run)
        try:
            while True:
                plan = store.load_plan(run)
                if run.state in {"paused", "cancelled"}:
                    if store.read_signal(run.id) == "cancel":
                        run.state = "cancelled"
                    elif run.state == "paused":
                        from orca_agent.semantic import CANCEL, deterministic_message
                        pending_control = [m for m in store.read_control(run.id)["messages"]
                                           if m["id"] not in run.processed_messages]
                        if (len(pending_control) == 1 and "update" not in pending_control[0]
                                and pending_control[0]["text"].strip().casefold() in CANCEL):
                            run, _ = deterministic_message(store, run, fault=fault)
                    break
                pending_messages = [m for m in store.read_control(run.id)["messages"]
                                    if m["id"] not in run.processed_messages]
                if pending_messages and not store.read_signal(run.id) and run.state != "cancelled":
                    from orca_agent.semantic import deterministic_message
                    run, message_action = deterministic_message(store, run, fault=fault)
                    if message_action == "status":
                        break
                    if message_action:
                        continue
                signal = store.read_signal(run.id)
                persisted_questions = (store.load_request(run).normalization_status == "clarification"
                    and any(d.get("request_version") == run.request_version and d.get("semantics")
                            for d in run.decisions))
                if not pending_messages and not signal and (store.active_clarification(run) or persisted_questions):
                    run.state = "waiting_user"
                    break
                feedback = set(run.result_ids) - set(run.processed_feedback)
                explanation_pending = (run.agent_enabled and feedback
                    and store.load_request(run).conditions.get("explain_results") is True)
                if (runner._goals(store, run, plan, results) and not pending_messages
                        and not explanation_pending):
                    run.state = "completed"
                    break
                if signal:
                    run.state = "paused" if signal == "pause" else "cancelled"
                    break
                if utc_now() >= run.deadline:
                    raise BudgetExceeded("run deadline exhausted")
                feedback = set(run.result_ids) - set(run.processed_feedback)
                need_model = run.agent_enabled and (plan is None or feedback or pending_messages)
                step = None
                decision_id = None
                pending_decisions = [d for d in run.decisions if d.get("action") == "call_tool"
                                     and d["id"] not in run.applied_decisions]
                recovered_action = None
                if pending_decisions:
                    decision = pending_decisions[0]
                    if decision["basis"] != current_basis(store, run) or pending_messages:
                        run.applied_decisions.append(decision["id"])
                        run.diagnostics.append({"category": "stale_pending_action", "id": decision["id"]})
                        store.save_run(run)
                        continue
                    values = decision["parameters"]
                    prior_calls = [c for c in run.calls if c.consumption.get("_decision_id") == decision["id"]]
                    if prior_calls or values.get("step_id") in results:
                        if any(c.state in ("reserved", "unknown") for c in prior_calls):
                            raise StoreError("pending action requires explicit result reconciliation")
                        if "step_id" not in values:
                            for call in prior_calls:
                                if call.result_id:
                                    _bind_query_goals(store, run, store.load_result(run.id, call.result_id))
                        run.applied_decisions.append(decision["id"])
                        store.save_run(run)
                        continue
                    if "step_id" in values:
                        chosen = next((s for s in _ready(plan, results) if s.id == values["step_id"]), None)
                        if not chosen:
                            raise StoreError("durable Tool decision no longer has a ready Step")
                        recovered_action = ("step", (chosen, decision["id"]))
                    else:
                        recovered_action = ("query", (values, decision["id"]))
                if need_model or recovered_action:
                    if recovered_action:
                        action, value = recovered_action
                    else:
                        run, action, value = _decision(store, run, plan, results,
                                                      transport or DeepSeekTransport(), batch, fault)
                    if action == "stale":
                        signal = store.read_signal(run.id)
                        run.state = ("paused" if signal == "pause" else "cancelled"
                                     if signal == "cancel" else "waiting_user")
                        break
                    if action == "stop":
                        break
                    if action in {"plan", "normalized"}:
                        continue
                    elif action == "step":
                        step, decision_id = value
                    elif action == "query":
                        value, decision_id = value
                        result = execute_call(store, run, value["tool"], value["parameters"], fault=fault,
                                              decision_id=decision_id)
                        _bind_query_goals(store, run, result)
                        run.applied_decisions.append(decision_id)
                        store.save_run(run)
                        continue
                elif not run.agent_enabled and any(a.finished_at and a.state not in (
                        "completed", "not_started") for a in run.attempts):
                    run.state = "failed"
                    break
                ready = _ready(plan, results)
                step = step or (ready[0] if ready else None)
                if step is None:
                    run.state = "failed"
                    break
                run.state = "running"
                store.save_run(run)
                if "execute_orca" in get_tool(step.tool).effects:
                    result, outcome = _science(store, config, run, step, results, batch, fault)
                    if not outcome.get("not_started"):
                        results[step.id] = result
                    if outcome["state"] in ("unknown", "cancelled"):
                        run.state = ("waiting_user" if outcome.get("control_action") == "message"
                                     else "paused" if store.read_signal(run.id) == "pause" else outcome["state"])
                        break
                else:
                    result = execute_call(store, run, step.tool, step.parameters.model_dump(),
                                          step=step, results=results, fault=fault, decision_id=decision_id)
                    results[step.id] = result
                if decision_id and decision_id not in run.applied_decisions:
                    run.applied_decisions.append(decision_id)
                    store.save_run(run)
                # Loop head always reevaluates goals and new feedback before a next Tool.
        except ControlChanged as exc:
            run.state = "waiting_user"
            run.diagnostics.append({"category": "control_changed", "message": str(exc)})
        except KeyboardInterrupt:
            run = store.load_run(run_id)
            results = runner._step_results(store, run)
            if run.state not in {"paused", "cancelled"}:
                run.state = "unknown"
            run.diagnostics.append({"category": "interrupted", "message": "Explicit resume required"})
        except (ValueError, OSError, RuntimeError) as exc:
            from pydantic import ValidationError

            from orca_agent.llm import _has_credential
            message = ("Input validation rejected the operation" if isinstance(exc, ValidationError)
                       else str(exc)[:1000])
            if _has_credential(message):
                message = "Operation failed; credential-bearing diagnostic withheld"
            unfinished = any(a.finished_at is None for a in run.attempts)
            run.state = ("unknown" if unfinished else
                         "budget_exhausted" if isinstance(exc, BudgetExceeded) else "failed")
            run.diagnostics.append({"category": type(exc).__name__,
                                    "message": message,
                                    "environment_occupied": isinstance(exc, EnvironmentBusy)})
        runner._goals(store, run, store.load_plan(run), results)
        if run.state == "unknown":
            run.usage.resource_usage_complete = False
        store.save_run(run)
        return run
