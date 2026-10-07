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

from orca_agent.goals import current_goal_evidence
from orca_agent.models import Run

_SECRET_KEYS = {"authorization", "api_key", "apikey", "api-key", "access_token", "password",
                "secret", "credential", "credentials", "headers"}
_SECRET = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}|\bBearer\s+\S+", re.I)
_MAX_RESULTS = 128


def _safe(value: Any, *, depth=0) -> Any:
    """Bound untrusted descriptive content and omit credential-bearing fields."""
    if depth > 10:
        return "[bounded report: deeper data omitted]"
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {_SECRET.sub("[redacted]", str(k)): "[redacted]" if str(k).lower() in _SECRET_KEYS
                else _safe(v, depth=depth + 1)
                for k, v in list(value.items())[:80]}
    if isinstance(value, (list, tuple)):
        items = [_safe(v, depth=depth + 1) for v in value[:80]]
        return items + (["[bounded report: additional items omitted]"] if len(value) > 80 else [])
    if isinstance(value, str):
        cleaned = _SECRET.sub("[redacted]", value)
        return cleaned[:4096] + ("[truncated]" if len(cleaned) > 4096 else "")
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


def _result_report(store, result):
    references = {artifact_id: set() for artifact_id in result.artifact_ids}
    failures, artifacts = [], []
    try:
        _references(result.source, references)
        for output in result.qualified_outputs.values():
            _references(output.source, references)
            if output.artifact_id:
                references.setdefault(output.artifact_id, set())
    except (TypeError, ValueError):
        failures.append("source_binding_unreadable")
    for artifact_id, expected in references.items():
        try:
            artifact = store.load_artifact(artifact_id)
            if expected and expected != {artifact.sha256}:
                raise ValueError("source hash binding differs")
            path = store.artifact_path(artifact_id)
            artifacts.append({"artifact_id": artifact.id, "sha256": artifact.sha256,
                              "path": str(path.resolve()), "role": artifact.role,
                              "run_id": artifact.run_id, "attempt_id": artifact.attempt_id,
                              "status": "verified"})
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            failures.append(f"source_unverified:{artifact_id}")
            artifacts.append({"artifact_id": artifact_id, "status": "unverified"})
    integrity_failed = bool(failures)
    qualified = {}
    for port, output in result.qualified_outputs.items():
        checks = result.checks.get(port)
        valid = bool(not integrity_failed and artifacts and result.operation_status == "completed"
                     and checks == output.checks and checks
                     and all(c.status == "passed" for c in checks))
        if not valid:
            failures.append(f"scientific_output_withheld:{port}")
            continue
        # Numeric values come solely from the qualified output, never observations.
        qualified[port] = _safe(output)
    observations = {"classification": "evidence_observation_only", "scientific_qualification": False,
                    "data": _safe(result.observations)}
    return {
        "run_id": result.run_id, "result_id": result.id, "step_id": result.step_id,
        "attempt_id": result.attempt_id, "call_id": getattr(result, "call_id", None),
        "operation_status": result.operation_status,
        "scientific_status": "passed" if qualified else "no_currently_verified_scientific_output",
        "qualified_outputs": qualified, "checks": _safe(result.checks),
        "observations": observations, "artifacts": artifacts,
        "conditions": _safe(result.source.get("conditions", {})),
        "source": _safe(result.source), "diagnostics": _safe(result.diagnostics),
        "gaps": failures,
    }


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
    goals = []
    for goal in request.goals if request else []:
        goal_gaps = list(goal.unresolved)
        selection = current_goal_evidence(store, run, request, goal, plan)
        binding = selection["binding"]
        goal_gaps.extend(selection["gaps"])
        selected = None
        if selection["result"]:
            raw = selection["result"]
            selected = read_result(raw.run_id, raw.id)
        output = selected.get("qualified_outputs", {}).get(goal.port) if selected else None
        support = "insufficient_evidence"
        if output and all(c["rule_version"] == goal.minimum_check_version for c in output["checks"]):
            support = "scientific_output_verified"
        elif goal.minimum_check_version == "evidence-read-1" and selected:
            checks = selected.get("checks", {}).get(goal.port, [])
            if (selected.get("operation_status") == "completed" and not selected.get("gaps")
                    and checks and all(c["status"] == "passed" and c["rule_version"] == "evidence-read-1"
                                       for c in checks)):
                support = "query_evidence_read_verified"
        if goal_gaps or request.unresolved:
            support = "insufficient_evidence"
        applicability = None
        if selected:
            applicability = selection["assessment"]
            purpose_valid = applicability is not None and applicability["status"] == "passed"
            if not purpose_valid:
                support = "insufficient_evidence"
                goal_gaps.append("source_not_applicable_to_current_goal")
                if applicability:
                    goal_gaps.extend(applicability["reasons"])
        recorded = run.goal_status.get(goal.id, "insufficient_evidence")
        satisfied = recorded == "satisfied" and support != "insufficient_evidence"
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
    complete = bool(goals and not gaps and request and not request.unresolved
                    and all(g["report_status"] == "satisfied" for g in goals if g["required"]))
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
        "user_goal_complete": complete,
        "request": {"original_text": _safe(request.original_text),
                    "conditions": {key: getattr(request, key) for key in (
                        "method", "basis", "charge", "multiplicity")},
                    "additional_conditions": _safe(request.conditions),
                    "condition_sources": _safe(request.conditions_source),
                    "systems": _safe(request.systems), "unresolved": _safe(request.unresolved)}
                   if request else None,
        "goals": goals, "results": list(results.values()), "attempts": attempts,
        "budget": {"limits": run.budget.model_dump(mode="json"),
                   "usage": run.usage.model_dump(mode="json"), "deadline": run.deadline.isoformat(),
                   "unresolved_scientific_attempt_ids": [a.id for a in run.attempts
                                                          if a.state in {"intent", "running", "unknown"}],
                   "model_cost": _money(run.model_records)},
        "diagnostics": _safe(run.diagnostics), "gaps": gaps,
        "limitations": ["Evidence reading, scientific output qualification, and user goal completion are distinct.",
                        "Electronic energy and its difference are not free energy or thermochemical corrections.",
                        "Finite sampling describes only the sampled discrete geometries.",
                        "An optimized geometry alone does not establish vibrational stability or a global minimum."],
    }
    communication = next((item["semantics"] for item in reversed(run.decisions)
        if item.get("request_version") == run.request_version
        and "delivery_scope" in item.get("semantics", {})), None)
    if communication is not None:
        report["communication"] = _safe({key: communication.get(key) for key in (
            "notices", "questions", "question_gaps", "delivery_scope", "awaiting_reply")})
        report["communication"]["registration_complete"] = (
            communication.get("delivery_scope") == "registration_only"
            and communication.get("awaiting_reply") is False)
    return report


def _cell(value):
    return str(_safe(value)).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_report(report: dict[str, Any]) -> str:
    """Render a compact standalone Markdown report without model interpretation."""
    lines = [f"Run `{_cell(report['run_id'])}` — {_cell(report['run_state'])}", "",
             f"交付：{_cell(report['report_delivery_status'])}；用户目标完成："
             + ("是。" if report["user_goal_complete"] else "否。"), "",
             "| 目标 | 物理量/查询 | 必需 | 记录状态 | 当前证据 |",
             "| --- | --- | --- | --- | --- |"]
    for goal in report["goals"]:
        lines.append("| " + " | ".join(_cell(goal[key]) for key in (
            "goal_id", "port", "required", "recorded_status", "evidence_status")) + " |")
    for goal in report["goals"]:
        if goal["gaps"]:
            lines.append(f"\n目标 `{_cell(goal['goal_id'])}` 缺口：{_cell('; '.join(goal['gaps']))}。\n")
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
