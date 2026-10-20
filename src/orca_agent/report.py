"""Deterministic, read-only delivery when a model is unavailable or exhausted.

The report does not replace scientific tools or goal judgment. It distinguishes
recorded goal decisions, currently verifiable qualified outputs, and observations.
No result is selected by recency and no file is converted, generated, or repaired.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from orca_agent.delivery import (
    collect_goal_selections,
    delivery_communication,
    delivery_snapshot,
    goal_fact_rows,
)
from orca_agent.models import Run

_SECRET_KEYS = {"authorization", "api_key", "apikey", "api-key", "access_token", "password",
                "secret", "credential", "credentials", "headers"}
_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+\S+", re.I)
_MAX_RESULTS = 128


def _safe(value: Any, *, depth=0, bounded=True) -> Any:
    """Bound untrusted descriptive content and omit credential-bearing fields."""
    if bounded and depth > 10:
        return "[bounded report: deeper data omitted]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {_SECRET.sub("[redacted]", str(k)): "[redacted]" if str(k).lower() in _SECRET_KEYS
                else _safe(v, depth=depth + 1, bounded=bounded)
                for k, v in (list(value.items())[:80] if bounded else value.items())}
    if isinstance(value, (list, tuple)):
        items = [_safe(v, depth=depth + 1, bounded=bounded) for v in (value[:80] if bounded else value)]
        return items + (["[bounded report: additional items omitted]"] if bounded and len(value) > 80 else [])
    if isinstance(value, str):
        cleaned = _SECRET.sub("[redacted]", value)
        return cleaned[:4096] + ("[truncated]" if len(cleaned) > 4096 else "") if bounded else cleaned
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return "[unsupported descriptive value]"


def _references(value, found, *, depth=0):
    if depth > 12:
        raise ValueError("source binding nesting exceeds report bound")
    if isinstance(value, dict):
        if isinstance(value.get("artifact_id"), str) and isinstance(value.get("sha256"), str):
            found.setdefault(value["artifact_id"], set()).add(value["sha256"])
        if isinstance(value.get("artifact_hashes"), dict):
            for artifact_id, digest in value["artifact_hashes"].items():
                found.setdefault(artifact_id, set()).add(digest)
        for child in value.values():
            _references(child, found, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _references(child, found, depth=depth + 1)
    if len(found) > 512:
        raise ValueError("source binding count exceeds report bound")


def _artifact_report(store, artifact_id, expected=()):
    try:
        artifact = store.load_artifact(artifact_id)
        if expected and set(expected) != {artifact.sha256}:
            raise ValueError("source hash binding differs")
        path = store.artifact_path(artifact_id)
        return {"artifact_id": artifact.id, "sha256": artifact.sha256,
                "path": str(path.resolve()), "role": artifact.role,
                "run_id": artifact.run_id, "attempt_id": artifact.attempt_id,
                "status": "verified"}
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return {"artifact_id": artifact_id, "status": "unverified"}


def _result_report(store, result):
    references = {artifact_id: set() for artifact_id in result.artifact_ids}
    failures, artifacts = [], []
    try:
        _references(result.source, references)
    except (TypeError, ValueError):
        failures.append("source_binding_unreadable")
    for artifact_id, expected in references.items():
        artifact = _artifact_report(store, artifact_id, expected)
        if artifact["status"] != "verified":
            failures.append(f"source_unverified:{artifact_id}")
        artifacts.append(artifact)
    integrity_failed = bool(failures)
    qualified, output_integrity = {}, {}
    for port, output in result.qualified_outputs.items():
        own_references, own_errors = {}, []
        try:
            _references(output.source, own_references)
            if output.artifact_id:
                own_references.setdefault(output.artifact_id, set())
        except (TypeError, ValueError):
            own_errors.append("source_binding_unreadable:" + port)
        for artifact_id, expected in own_references.items():
            artifact = _artifact_report(store, artifact_id, expected)
            if artifact["status"] != "verified":
                own_errors.append(f"source_unverified:{artifact_id}")
            if artifact not in artifacts:
                artifacts.append(artifact)
        failures.extend(own_errors)
        output_integrity[port] = not integrity_failed and not own_errors
        checks = result.checks.get(port)
        valid = bool(output_integrity[port] and artifacts and result.operation_status == "completed"
                     and checks == output.checks and checks
                     and all(c.status == "passed" for c in checks))
        if not valid:
            failures.append(f"scientific_output_withheld:{port}")
            continue
        # Numeric values come solely from the qualified output, never observations.
        qualified[port] = _safe(output)
    output_validation = []
    for port in sorted(result.checks.keys() | result.qualified_outputs.keys()):
        checks = result.checks.get(port, [])
        verified = output_integrity.get(port, not integrity_failed)
        status = ("passed" if port in qualified else "source_unverified" if not verified
                  else "failed" if any(c.status == "failed" for c in checks) else "unverified")
        output_validation.append({"name": port,
            "kind": "qualified_output" if port in result.qualified_outputs else "checked_output",
            "status": status, "scientific_qualification": port in qualified,
            "checks": _safe(checks), "source_integrity": "verified" if verified else "unverified"})
    observation_records = []
    for observed in result.observation_records:
        record = _safe(observed, bounded=False)
        sources = [_artifact_report(store, source.artifact_id, [source.sha256]) for source in observed.sources]
        sources.extend(_artifact_report(store, identifier) for identifier in getattr(observed, "artifact_ids", [])
                       if identifier not in {item["artifact_id"] for item in sources})
        known_ids = {item["artifact_id"] for item in artifacts}
        artifacts.extend(item for item in sources if item["artifact_id"] not in known_ids)
        verified = bool(sources) and all(item["status"] == "verified" for item in sources)
        record.update(source_integrity="verified" if verified else "unverified", scientific_qualification=False)
        observation_records.append(record)
        output_validation.append({"name": observed.name, "kind": "observation",
            "status": observed.validation_status, "scientific_qualification": False,
            "checks": _safe(observed.checks), "source_integrity": record["source_integrity"]})
        if not verified:
            failures.append("observation_source_unverified:" + observed.name)
    scientific_status = ("passed" if qualified and all(item["scientific_qualification"] for item in output_validation)
                         else "partially_checked" if qualified else "no_currently_verified_scientific_output")
    observations = {"classification": "evidence_observation_only", "scientific_qualification": False,
                    "data": _safe(result.observations)}
    return {
        "run_id": result.run_id, "result_id": result.id, "step_id": result.step_id,
        "attempt_id": result.attempt_id, "call_id": getattr(result, "call_id", None),
        "operation_status": result.operation_status,
        "scientific_status": scientific_status, "output_validation": output_validation,
        "qualified_outputs": qualified, "checks": _safe(result.checks),
        "observations": observations, "observation_records": observation_records, "artifacts": artifacts,
        "conditions": _safe(result.source.get("conditions", {})),
        "source": _safe(result.source), "diagnostics": _safe(result.diagnostics),
        "gaps": failures,
    }


def _delivery_index(store, run, request, results):
    """Index saved artifacts, including initial geometries and every code attempt."""
    index = {item["artifact_id"]: {**item, "purposes": ["result_evidence"]}
             for result in results for item in result.get("artifacts", [])}

    def include(identifier, purpose, expected=()):
        if not identifier:
            return
        if identifier not in index:
            index[identifier] = {**_artifact_report(store, identifier, expected), "purposes": []}
        elif expected and set(expected) != {index[identifier].get("sha256")}:
            index[identifier]["status"] = "unverified"
        if purpose not in index[identifier]["purposes"]:
            index[identifier]["purposes"].append(purpose)

    if request:
        include(request.geometry_artifact_id, "initial_geometry")
        for system in request.systems:
            include(system.geometry_artifact_id, "initial_geometry")
    for attempt in run.attempts:
        include(attempt.geometry_artifact_id, "attempt_geometry")
        step = attempt.frozen_step
        if step:
            for field in ("code", "input"):
                ref = getattr(step.parameters, field, None)
                if ref:
                    include(ref.artifact_id, "generated_script" if ref.language == "python" else "actual_input", [ref.sha256])
    for call in run.internal_calls:
        if call.code:
            include(call.code.artifact_id, "actual_input", [call.code.sha256])
        for identifier in call.artifact_ids:
            include(identifier, "internal_call_evidence")
    for item in index.values():
        if item.get("role") in {"python", "code"} and "generated_script" not in item["purposes"]:
            item["purposes"].append("generated_script")
    return list(index.values())


def _execution_report(run):
    fields = ("request_key", "purpose", "retry_of", "state", "attempt_id", "parent_attempt_id",
              "input_fingerprint", "conditions", "actual_conditions", "goal", "resources",
              "work_units", "started", "result_id", "artifact_ids", "resource_usage")
    calls = [{key: _safe(getattr(item, key), bounded=False) for key in fields} for item in run.internal_calls]
    return {"internal_calls": calls, "checkpoints": _safe(run.task_checkpoints, bounded=False),
        "progress": {"total": len(calls), "completed": sum(item["state"] == "completed" for item in calls),
            "failed": sum(item["state"] in {"failed", "cancelled", "timed_out"} for item in calls),
            "running": sum(item["state"] in {"reserved", "running", "unknown"} for item in calls)}}


def _stop_reason(run):
    stopped = run.state in {"completed", "failed", "budget_exhausted", "cancelled", "paused", "unknown", "waiting_user"}
    diagnostics = [{key: item[key] for key in ("category", "message", "error_category", "model_id") if key in item}
                   for item in run.diagnostics if isinstance(item, dict)]
    model_errors = [{key: item[key] for key in ("id", "status", "error_category") if key in item}
                    for item in run.model_records if item.get("error_category")]
    categories = list(dict.fromkeys(item["category"] for item in diagnostics if item.get("category")))
    if stopped:
        categories.insert(0, run.state)
    return _safe({"state": run.state, "categories": categories, "diagnostics": diagnostics,
                  "model_errors": model_errors}, bounded=False)


def _money(records):
    """Price actual known tokens at frozen peak rates; provider charges are unknown."""
    fields = ("id", "logical_id", "request_hash", "prompt_version", "status", "input_reserved",
              "output_reserved", "cost_reserved_usd", "input_tokens", "output_tokens", "total_tokens",
              "cost_known_usd", "error_category", "response_hash", "request_id", "latency_seconds")
    sanitized = [{key: _safe(record[key]) for key in fields if key in record} for record in records]
    known, occupied = [], []
    incomplete = False
    for record in records:
        key = "cost_known_usd" if record.get("status") == "known" else "cost_reserved_usd"
        value = record.get(key)
        try:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise InvalidOperation
            amount = Decimal(str(value))
            if not amount.is_finite() or amount < 0:
                raise InvalidOperation
            (known if key == "cost_known_usd" else occupied).append(amount)
        except InvalidOperation:
            incomplete = True
    return {"currency": "USD" if records else None,
            "known_cost": str(sum(known, Decimal("0"))) if known else None,
            "unsettled_reservation": str(sum(occupied, Decimal("0"))) if occupied else None,
            "cost_basis": "frozen_peak_uncached_upper_bound_from_known_tokens",
            "provider_billing_queried": False, "provider_charged_amount": None,
            "cost_records_complete": not incomplete,
            "known_call_count": sum(r.get("status") == "known" for r in records),
            "unsettled_call_count": sum(r.get("status") != "known" for r in records),
            "records": sanitized}


def _explanation_status(request, run, communication):
    rejected = any(item.get("category") == "terminal_explanation_rejected"
        and item.get("request_version") == run.request_version
        and item.get("control_generation") == run.control_generation for item in run.diagnostics)
    if rejected:
        return "rejected"
    if (not request or request.conditions.get("explain_results") is not True
            or (communication["delivery_scope"] == "registration_only" and not communication["awaiting_reply"])):
        return "not_requested"
    if run.state in {"completed", "failed", "budget_exhausted", "cancelled"}:
        return "unavailable"
    return "pending"


def build_report(store, run: Run | str) -> dict[str, Any]:
    """Build a reproducible report without changing any source or calling a model."""
    run = store.load_run(run) if isinstance(run, str) else run
    gaps, results = [], {}
    request = plan = None
    try:
        request = store.load_request(run)
    except (ValueError, OSError, RuntimeError):
        gaps.append("request_unavailable")
    if run.plan_id:
        try:
            plan = store.load_plan(run)
        except (ValueError, OSError, RuntimeError):
            gaps.append("plan_unavailable")

    def read_result(run_id, result_id):
        key = (run_id, result_id)
        if key not in results:
            try:
                value = store.load_result(run_id, result_id)
                if value.run_id != run_id or value.id != result_id:
                    raise ValueError("result identity differs")
                results[key] = _result_report(store, value)
            except (ValueError, OSError, RuntimeError):
                gaps.append(f"result_unavailable:{result_id}")
                results[key] = {"run_id": run_id, "result_id": result_id,
                                "operation_status": "unavailable", "qualified_outputs": {},
                                "gaps": ["result_unavailable"]}
        return results[key]

    for result_id in run.result_ids[:_MAX_RESULTS]:
        read_result(run.id, result_id)
    if len(run.result_ids) > _MAX_RESULTS:
        gaps.append("result_count_exceeds_report_bound")
    selections = collect_goal_selections(store, run, request, plan) if request else {}
    facts = goal_fact_rows(request, run, selections) if request else []
    by_goal = {row["goal_id"]: row for row in facts}
    goals = []
    for goal in request.goals if request else []:
        row = by_goal[goal.id]
        goal_gaps = list(row["gaps"])
        selection = selections[goal.id]
        binding = selection["binding"]
        selected = None
        if selection["result"]:
            raw = selection["result"]
            selected = read_result(raw.run_id, raw.id)
            integrity = selection.get("source_integrity", {})
            if integrity.get("status") == "unverified":
                selected["gaps"] = list(dict.fromkeys([*selected.get("gaps", []), *integrity.get("gaps", [])]))
                selected["qualified_outputs"].pop(goal.port, None)
                selected["scientific_status"] = "partially_checked" if selected["qualified_outputs"] else "source_unverified"
                for output in selected.get("output_validation", []):
                    if output["name"] == goal.port:
                        output.update(status="source_unverified", source_integrity="unverified", scientific_qualification=False)
        support = "insufficient_evidence"
        if row["answer"] and row["answer"]["kind"] == "qualified_scientific_output":
            support = "scientific_output_verified"
        elif row["answer"] and goal.evidence_contract_version == "generic-observation-1":
            support = "generic_observation_reported_with_limitations"
        elif row["answer"]:
            support = "query_evidence_read_verified"
        applicability = selection["assessment"]
        if selected and row["current_evidence_status"] != "passed":
            goal_gaps.append("source_not_applicable_to_current_goal")
        recorded = run.goal_status.get(goal.id, "insufficient_evidence")
        satisfied = row["goal_complete"]
        if not satisfied and not goal_gaps:
            goal_gaps.append("qualified_evidence_or_recorded_goal_decision_missing")
        goals.append({"goal_id": goal.id, "port": goal.port, "required": goal.required,
                      "original_text": _safe(goal.original_text), "recorded_status": recorded,
                      "report_status": "satisfied" if satisfied else "insufficient_evidence",
                      "evidence_status": support, "minimum_check_version": goal.minimum_check_version,
                      "conditions": _safe(goal.conditions), "minimum_evidence": _safe(goal.minimum_evidence),
                      "applicability": _safe(applicability),
                      "binding": _safe(binding), "result_id": selected.get("result_id") if selected else None,
                      "run_id": selected.get("run_id") if selected else None,
                      "gaps": _safe(goal_gaps)})
        if row.get("verification_basis") == "reported_delivery":
            goals[-1].update(verification_basis="reported_delivery", scientific_qualification=False,
                unverified_user_obligations=_safe([entry for entry in row.get("minimum_evidence", [])
                    if entry.get("status") == "unverified"]))
    complete = bool(goals and not gaps and request and not request.unresolved
                    and all(g["report_status"] == "satisfied" for g in goals if g["required"]))
    unverified_obligations = any(goal.get("unverified_user_obligations")
                                for goal in goals if goal["required"])
    attempts = []
    previous = {}
    for attempt in run.attempts:
        step = getattr(attempt, "frozen_step", None)
        parameters = step.parameters.model_dump(mode="json") if step else None
        old = previous.get(attempt.logical_id)
        changes = ({key: {"before": old.get(key), "after": value}
                    for key, value in parameters.items() if old.get(key) != value}
                   if old is not None and parameters is not None else {})
        attempts.append({"attempt_id": attempt.id, "step_id": attempt.step_id,
                         "logical_id": attempt.logical_id, "number": attempt.number,
                         "state": attempt.state, "started": attempt.started,
                         "result_id": attempt.result_id, "input_fingerprint": attempt.input_fingerprint,
                         "geometry_artifact_id": attempt.geometry_artifact_id,
                         "request_version": getattr(attempt, "request_version", None),
                         "plan_version": getattr(attempt, "plan_version", None),
                         "parameters": _safe(parameters), "parameter_changes": _safe(changes)})
        if parameters is not None:
            previous[attempt.logical_id] = parameters
    report = {
        "schema_version": 1, "run_id": run.id, "run_state": run.state,
        "request_id": run.request_id, "request_version": run.request_version,
        "plan_id": run.plan_id, "plan_version": run.plan_version,
        "recorded_delivery_status": run.delivery_status,
        "report_delivery_status": "complete" if complete else "partial",
        "user_goal_complete": complete and not unverified_obligations,
        "request": {"original_text": _safe(request.original_text),
                    "conditions": {key: getattr(request, key) for key in (
                        "method", "basis", "charge", "multiplicity")},
                    "additional_conditions": _safe(request.conditions),
                    "condition_sources": _safe(request.conditions_source),
                    "systems": _safe(request.systems), "unresolved": _safe(request.unresolved)}
                   if request else None,
        "goals": goals, "results": list(results.values()), "attempts": attempts,
        "goal_facts": _safe(facts, bounded=False),
        "permission": run.permission.model_dump(mode="json"),
        "budget": {"limits": run.budget.model_dump(mode="json"),
                   "usage": run.usage.model_dump(mode="json"), "deadline": run.deadline.isoformat(),
                   "unresolved_scientific_attempt_ids": [a.id for a in run.attempts
                                                          if a.state in {"intent", "running", "unknown"}],
                   "model_cost": _money(run.model_records)},
        "diagnostics": _safe(run.diagnostics), "gaps": gaps,
        "execution": _execution_report(run), "stop_reason": _stop_reason(run),
        "limitations": ["Evidence reading, scientific output qualification, and user goal completion are distinct.",
                        "Electronic energy and its difference are not free energy or thermochemical corrections.",
                        "Finite sampling describes only the sampled discrete geometries.",
                        "An optimized geometry alone does not establish vibrational stability or a global minimum."],
    }
    if any(goal.get("verification_basis") == "reported_delivery" for goal in goals):
        report.update(reported_delivery_complete=complete,
            user_obligations_verification="unverified" if unverified_obligations else "checked")
    report["artifact_index"] = _delivery_index(store, run, request, report["results"])
    report["generated_scripts"] = [item for item in report["artifact_index"] if "generated_script" in item["purposes"]]
    snapshot = delivery_snapshot(request, run, selections,
        control_generation=store.read_control(run.id)["generation"]) if request else None
    if snapshot:
        # Mandatory goals/facts are never silently dropped by the legacy audit
        # preview bounds. Credential redaction still applies to descriptive text.
        report["delivery"] = _safe(snapshot, bounded=False)
    receipt = run.terminal_deliveries[-1] if getattr(run, "terminal_deliveries", []) else None
    communication = delivery_communication(run)
    explanation_status = _explanation_status(request, run, communication)
    report["model_explanation"] = {"status": explanation_status, "current_status": explanation_status, "current": False,
                                   "free_reason_role": "audit_only"}
    if receipt:
        report["model_explanation"].update({"status": receipt.contract_status,
            "current": bool(snapshot and snapshot["fingerprint"] == receipt.snapshot_fingerprint
                            and receipt.decision_id not in run.reopened_terminal_ids),
            "decision_id": receipt.decision_id, "contract_version": receipt.contract_version,
            "snapshot_fingerprint": receipt.snapshot_fingerprint,
            "report_status": receipt.report_status, "report_sha256": receipt.report_sha256})
        if report["model_explanation"]["current"]:
            report["model_explanation"]["current_status"] = "passed"
    publication = receipt if receipt and report["model_explanation"]["current"] else None
    if publication is None and snapshot:
        from orca_agent.terminal import report_publication_id
        identity = report_publication_id(run, snapshot)
        publication = next((item for item in run.fallback_report_receipts
                            if item.publication_id == identity), None)
    report["report_artifact"] = {"status": "not_recorded", "current": False}
    if publication:
        report["report_artifact"] = {"status": publication.report_status, "current": True,
            "version": publication.report_version, "snapshot_fingerprint": publication.snapshot_fingerprint,
            "path": publication.report_path, "sha256": publication.report_sha256,
            "error": publication.report_error}
        if publication.report_status == "rendered":
            from orca_agent.store import sha256_file
            try:
                if (not publication.report_path or not publication.report_sha256
                        or sha256_file(store.path(publication.report_path)) != publication.report_sha256):
                    raise ValueError("report artifact hash mismatch")
            except (ValueError, OSError, RuntimeError):
                report["report_artifact"].update(status="failed", current=False,
                                                 error="report_source_unverified")
    if any("delivery_scope" in item.get("semantics", {}) and item.get("request_version") == run.request_version
           for item in run.decisions):
        report["communication"] = _safe({key: communication.get(key) for key in (
            "notices", "questions", "question_gaps", "delivery_scope", "awaiting_reply")})
        report["communication"]["registration_complete"] = (
            communication.get("delivery_scope") == "registration_only"
            and communication.get("awaiting_reply") is False)
    return report


def _cell(value, *, bounded=True):
    return str(_safe(value, bounded=bounded)).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_report(report: dict[str, Any]) -> str:
    """Render a compact standalone Markdown report without model interpretation."""
    lines = [f"Run `{_cell(report['run_id'])}` — {_cell(report['run_state'])}", "",
             f"交付：{_cell(report['report_delivery_status'])}；用户目标完成："
             + ("是。" if report["user_goal_complete"] else "尚未验证。"
                if report.get("user_obligations_verification") == "unverified" else "否。"), "",
             "| 目标 | 物理量/查询 | 必需 | 记录状态 | 当前证据 |",
             "| --- | --- | --- | --- | --- |"]
    stop = report.get("stop_reason", {})
    if stop.get("categories"):
        lines.insert(4, "停止/当前状态原因：" + _cell(stop["categories"]) + "。")
        if stop.get("diagnostics") or stop.get("model_errors"):
            lines.insert(5, "停止诊断：" + _cell({"diagnostics": stop["diagnostics"], "model_errors": stop["model_errors"]}) + "。")
    for goal in report["goals"]:
        lines.append("| " + " | ".join(_cell(goal[key]) for key in (
            "goal_id", "port", "required", "recorded_status", "evidence_status")) + " |")
    for goal in report["goals"]:
        if goal["gaps"]:
            lines.append(f"\n目标 `{_cell(goal['goal_id'])}` 缺口：{_cell('; '.join(goal['gaps']))}。\n")
        if goal.get("verification_basis") == "reported_delivery":
            state = "仅完成有来源观察的交付" if goal["report_status"] == "satisfied" else "尚未完成观察交付"
            lines.append(f"\n目标 `{_cell(goal['goal_id'])}` {state}，不授予科学资格。")
            for entry in goal.get("unverified_user_obligations", []):
                lines.append("- 原文义务尚未由程序验证：" + _cell(entry["requested"], bounded=False)
                             + "；来源：" + _cell(entry.get("source", "unknown")) + "。")
    for row in report.get("goal_facts", []):
        lines.append(f"\n目标 `{_cell(row['goal_id'])}` 当前条件：{_cell(row['current_conditions'])}。")
        if row["source_conditions"]:
            lines.append("来源实际条件：" + _cell(row["source_conditions"]) + "。")
        if row.get("geometry_relation"):
            lines.append("请求几何关系：" + _cell(row["geometry_relation"]) + "。")
        answer = row.get("answer")
        if answer and answer["kind"] in {"qualified_scientific_output", "qualified_input_artifact"}:
            displayed = ("产物 " + _cell(answer["artifact_id"]) if answer.get("artifact_id") and answer["value"] is None
                         else _cell(answer["value"]) + " " + _cell(answer["unit"] if answer["unit"] is not None else "unknown"))
            label = ("本目标已检查输入（不宣称科学资格）" if answer["kind"] == "qualified_input_artifact"
                     else "本目标合格输出")
            lines.append(f"{label}：{displayed}；"
                         f"检查版本：{_cell(answer['check_versions'])}。")
        elif answer:
            lines.append("本目标观察（不宣称科学资格）：" + _cell(answer["observation"], bounded=False)
                         + "；单位：" + _cell(answer["unit"] if answer["unit"] is not None else "unknown") + "。")
    delivery = report.get("delivery", {})
    for fact in delivery.get("facts", []):
        if fact["kind"] != "criterion":
            continue
        value = fact["value"]
        lines.append(f"\n目标 `{_cell(fact['goal_ref'])}` 已保存的判据：{_cell(value.get('reason', 'unknown'))}；"
                     f"规则 {_cell(value.get('rule_version', 'unknown'))}。")
        span = value.get("neighbor_span_angstrom")
        target = value.get("target_width_angstrom")
        tolerance = value.get("acceptance_criteria", {}).get("distance_tolerance_angstrom")
        if span is not None and target is not None:
            lines.append(f"邻点跨度：{_cell(span)} Å；允许阈值：{_cell(target)} Å + "
                         f"{_cell(tolerance) if tolerance is not None else 'unknown'} Å（距离容差）。")
        if value.get("energy_threshold_eh") is not None:
            lines.append(f"能量区分阈值：{_cell(value['energy_threshold_eh'])} Eh。")
        if value.get("limitation"):
            lines.append("判据适用范围：" + _cell(value["limitation"]))
    if delivery.get("blockers"):
        lines.append("\n当前并列阻断（不授予新增执行许可）：")
        for blocker in delivery["blockers"]:
            lines.append(f"- {_cell(blocker['code'])}：{_cell(blocker['value'])}；{_cell(blocker['text'])}")
    if delivery.get("resources"):
        permission = delivery["resources"]["permission"]
        remaining = delivery["resources"]["remaining"]
        lines.append("\n科学执行许可：" + _cell(permission["scientific_execution"])
            + "；追加科学许可：" + _cell(permission["allow_additional_science"])
            + "；ORCA 剩余额度：" + _cell(remaining["orca_starts"])
            + "；追加 ORCA 剩余额度：" + _cell(remaining["extra_orca_starts"]) + "。")
    explanation = report.get("model_explanation", {})
    if explanation:
        lines.append("\n模型解释合同：" + _cell(explanation.get("status"))
            + "；适用于当前快照：" + _cell(explanation.get("current"))
            + "；当前解释状态：" + _cell(explanation.get("current_status"))
            + "。自由 reason 仅为审计正文，不代表已核验交付。")
    communication = report.get("communication", {})
    if communication.get("registration_complete"):
        lines.append("\n需求登记已完成；科学目标状态见上表。")
    for notice in communication.get("notices") or []:
        lines.append("告知：" + _cell(notice))
    if communication.get("awaiting_reply"):
        for question in communication.get("questions") or []:
            lines.append("待答问题：" + _cell(question))
    request = report.get("request")
    if request:
        lines.extend(["", "用户目标：" + _cell(request["original_text"]),
                      "请求条件：" + _cell(request["conditions"]),
                      "条件来源：" + _cell(request["condition_sources"])])
        if request["additional_conditions"]:
            lines.append("附加条件：" + _cell(request["additional_conditions"]))
        if request["unresolved"]:
            lines.append("待明确事项：" + _cell(request["unresolved"]))
    for result in report["results"]:
        lines.extend(["", f"Result `{_cell(result['result_id'])}`：操作 {_cell(result['operation_status'])}；"
                      f"科学输出 {_cell(result.get('scientific_status', 'unavailable'))}。"])
        if result.get("attempt_id"):
            lines.append(f"来源 Run `{_cell(result['run_id'])}` / Attempt `{_cell(result['attempt_id'])}`。")
        if result.get("conditions"):
            lines.append("结果条件：" + _cell(result["conditions"]) + "。")
        for output in result.get("output_validation", []):
            lines.append(f"- 输出 `{_cell(output['name'])}`：{_cell(output['kind'])}；"
                         f"验证状态 {_cell(output['status'])}；科学资格 {_cell(output['scientific_qualification'])}；"
                         f"来源 {_cell(output['source_integrity'])}；检查 {_cell(output['checks'])}。")
        for observed in result.get("observation_records", []):
            lines.append(f"\n通用观察 `{_cell(observed['name'])}`（不宣称科学资格）："
                         + _cell(observed["value"], bounded=False) + "；单位 "
                         + _cell(observed["unit"] if observed["unit"] is not None else "unknown") + "。")
            lines.append("验证覆盖：" + _cell(observed["validation_status"]) + "；来源完整性："
                         + _cell(observed["source_integrity"]) + "；检查：" + _cell(observed["checks"]) + "。")
            lines.append("实际条件：" + _cell(observed["actual_conditions"], bounded=False) + "；脚本 SHA256："
                         + _cell(observed["code_sha256"]) + "。")
            if observed.get("formula"):
                lines.append("公式：" + _cell(observed["formula"]) + "。")
            for reference in observed["sources"]:
                lines.append("来源：" + _cell(reference, bounded=False) + "。")
            if observed.get("artifact_ids"):
                lines.append("派生产物：" + _cell(observed["artifact_ids"], bounded=False) + "。")
            if observed["limitations"]:
                lines.append("验证限制：" + _cell(observed["limitations"], bounded=False) + "。")
        for port, output in result.get("qualified_outputs", {}).items():
            value = output.get("value")
            if value is not None:
                label = "ΔE = E(B) − E(A)" if port == "energy_difference" else port
                lines.append(f"- 已验证 {label}：{value:.15g} {_cell(output.get('unit'))}。")
                if port == "energy_difference":
                    for member in ("A", "B"):
                        source = output.get("source", {}).get(member, {})
                        lines.append(f"  - {member}：Run `{_cell(source.get('run_id'))}` / "
                                     f"Attempt `{_cell(source.get('attempt_id'))}` / "
                                     f"Result `{_cell(source.get('result_id'))}`。")
            elif output.get("artifact_id"):
                lines.append(f"- 已验证 {port}：Artifact `{_cell(output['artifact_id'])}`。")
        data = result.get("observations", {}).get("data", {})
        if data:
            lines.append("证据观察（读取事实，不代表科学通过）：")
            summary = data.get("analysis", data) if isinstance(data, dict) else data
            members = summary.get("members", []) if isinstance(summary, dict) else []
            if isinstance(members, list) and members:
                for member in members:
                    if isinstance(member, dict):
                        status = member.get("status")
                        if result.get("gaps") and status == "qualified":
                            status = "source_unverified"
                        lines.append(f"- 成员 `{_cell(member.get('member_id', member.get('id')))}`："
                                     f"required={_cell(member.get('required'))}；"
                                     f"status={_cell(status)}；"
                                     f"missing={_cell(member.get('missing_reason'))}。")
                        context = {key: value for key, value in member.items()
                                   if key not in {"member_id", "id", "required", "status", "energy_eh", "missing_reason"}}
                        if context:
                            lines.append("  成员条件、结构关系与排除记录：" + _cell(context, bounded=False) + "。")
                        energy = member.get("energy_eh")
                        if isinstance(energy, (int, float)) and not isinstance(energy, bool) and math.isfinite(energy):
                            if result.get("gaps"):
                                lines.append("  成员来源未核验，电子能数值不展示。")
                            else:
                                lines.append(f"  成员电子能观察：{energy:.15g} Eh；该成员值不代表整体分析目标通过。")
                if isinstance(summary.get("goal_satisfied"), bool):
                    assessment = ("未核验" if result.get("gaps") else
                                  "通过" if summary["goal_satisfied"] else "未通过")
                    lines.append("有限采样目标检查：" + assessment + "。")
                if summary.get("reason"):
                    lines.append("分析判据：" + _cell(summary["reason"]) + "。")
                if summary.get("limitation"):
                    lines.append(_cell(summary["limitation"]))
                context = {key: value for key, value in summary.items() if key != "members"}
                if context:
                    lines.append("已保存的比较条件与搜索范围：" + _cell(context, bounded=False) + "。")
            else:
                lines.append(f"- {_cell(data)}")
        for artifact in result.get("artifacts", []):
            if artifact["status"] == "verified":
                lines.append(f"- 原始证据 [{_cell(artifact['artifact_id'])}](<{artifact['path']}>)；"
                             f"SHA256 `{artifact['sha256']}`。")
        if result.get("gaps"):
            lines.append("未验证/缺口：" + _cell("; ".join(result["gaps"])) + "。")
        if result.get("diagnostics"):
            lines.append("结果诊断：" + _cell(result["diagnostics"]))
    if report["attempts"]:
        lines.extend(["", "执行尝试与修复：", ""])
        for attempt in report["attempts"]:
            lines.append(f"- Attempt `{_cell(attempt['attempt_id'])}`：{_cell(attempt['state'])}；"
                         f"Request v{_cell(attempt['request_version'])} / Plan v{_cell(attempt['plan_version'])}；"
                         f"输入指纹 `{_cell(attempt['input_fingerprint'])}`。")
            if attempt["parameter_changes"]:
                lines.append("  参数变化：" + _cell(attempt["parameter_changes"]))
    execution = report.get("execution", {})
    if execution.get("internal_calls"):
        lines.append("\n逐计算进度：" + _cell(execution["progress"]) + "。")
        for call in execution["internal_calls"]:
            lines.append("- 内部计算记录：" + _cell(call, bounded=False))
    if execution.get("checkpoints"):
        lines.append("\n已保存检查点：" + _cell(execution["checkpoints"], bounded=False))
    if report.get("artifact_index"):
        lines.append("\n文件交付索引（包含初始结构、各次脚本、输入和结果）：")
        for artifact in report["artifact_index"]:
            if artifact["status"] == "verified":
                lines.append(f"- [{_cell(artifact['artifact_id'])}](<{artifact['path']}>)；"
                             f"用途 {_cell(artifact['purposes'])}；SHA256 `{artifact['sha256']}`。")
            else:
                lines.append(f"- `{_cell(artifact['artifact_id'])}`：来源未核验；用途 {_cell(artifact['purposes'])}。")
    usage = report["budget"]["usage"]
    money = report["budget"]["model_cost"]
    lines.extend(["", f"累计成本：ORCA 已预约 {usage['orca_starts_reserved']}，"
                  f"已知实际启动 {usage['orca_starts_actual']}；模型 HTTP {usage['model_calls']}；"
                  f"已知 token {usage['model_tokens_used']}，未知占用 token {usage['model_tokens_unknown']}。",
                  f"模型按冻结最高价估算费用上界：已知 token 对应 {_cell(money['known_cost'])}；未结算预约 "
                  f"{_cell(money['unsettled_reservation'])} {_cell(money['currency'])}。"
                  "实际扣费金额未查询；未知费用未按零处理。",
                  f"计算资源：已记录耗时 {usage['elapsed_seconds']} s，CPU {usage['cpu_seconds']} s；"
                  f"资源记录完整={usage['resource_usage_complete']}；"
                  f"执行未决尝试={_cell(report['budget']['unresolved_scientific_attempt_ids'])}。", ""])
    if report["gaps"]:
        lines.append("交付缺口：" + _cell("; ".join(report["gaps"])) + "。")
    if report["diagnostics"]:
        lines.append("Run 诊断：" + _cell(report["diagnostics"]))
    lines.extend(report["limitations"])
    return "\n".join(lines) + "\n"
