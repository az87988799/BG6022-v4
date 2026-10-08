"""Current delivery facts, independent of model prose; offline production paths."""

import json
from datetime import timedelta

import pytest

from orca_agent.delivery import collect_delivery_snapshot, collect_goal_facts
from orca_agent.goals import validate_goal_evidence
from orca_agent.models import (
    BudgetLimits,
    Check,
    EvidenceRef,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
    TerminalReceipt,
    ToolCall,
)
from orca_agent.report import build_report, render_report
from orca_agent.store import Store
from orca_agent.tools.analysis import SamplingCandidate, SamplingParameters
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_dispatch import MANIFEST, PROJECT, REVIEW, archived_energy
from tests.unit.test_report_extended import source as source


def sampling_run(tmp_path, window_id="left"):
    """Read repository reference outputs and run deterministic analysis only."""
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    window = next(w for w in MANIFEST["windows"] if w["id"] == window_id)
    candidates, inputs, authorized = [], {}, []
    for item in window["candidates"]:
        geometry = store.import_artifact(PROJECT / item["path"], "sampling_candidate")
        candidates.append(SamplingCandidate(id=item["id"], artifact_id=geometry.id, sha256=geometry.sha256,
            declared_r_angstrom=item["declared_r_angstrom"], required_initial=item["required_initial"]))
        if item["required_initial"]:
            origin, energy = archived_energy(store, PROJECT / "tests/fixtures/phase_b/independent" / item["reference_id"])
            inputs[item["id"]] = EvidenceRef(run_id=origin.id, result_id=energy.id, attempt_id=energy.attempt_id)
            authorized.append(energy.id)
    parameters = SamplingParameters(target_width_angstrom=window["target_width_angstrom"],
        energy_threshold_eh=REVIEW["sampling"]["threshold_eh"], fixed_bond_angstrom=MANIFEST["source"]["r02_angstrom"],
        fixed_angle_degrees=MANIFEST["source"]["angle_degrees"])
    goal = Goal(id="sampling", port="sampling", minimum_check_version="finite-sampling-1",
        original_text="Obtain evidence satisfying the finite sampling criterion.", conditions={
        "sampling": parameters.model_dump(mode="json"), "candidates": [c.model_dump(mode="json") for c in candidates]})
    step = Step(id="analyze", logical_id="analyze", tool="analysis.finite_sampling", parameters={"goal_id": goal.id}, inputs=inputs)
    request = Request(goals=[goal])
    plan = Plan(request_id=request.id, steps=[step], goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)})
    run = store.create_run(request, plan, PermissionSnapshot(allowed_tools=[step.tool], artifact_writes=True,
        artifact_ids=[c.artifact_id for c in candidates], result_ids=authorized),
        BudgetLimits(orca_starts=0, extra_orca_starts=0, evidence_reads=8, analysis_executions=4))
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    request, plan = store.load_request(run), store.load_plan(run)
    run.goal_status[goal.id] = "satisfied" if validate_goal_evidence(store, run, request, goal, result) else "insufficient_evidence"
    store.save_run(run)
    return store, run, request, plan, result


def by_kind(snapshot, kind):
    return next(item["value"] for item in snapshot["facts"] if item["kind"] == kind)


def test_failed_span_retains_numbers_required_roles_and_all_zero_budget_blocks(tmp_path):
    store, run, request, plan, result = sampling_run(tmp_path)
    snapshot = collect_delivery_snapshot(store, run, request, plan)
    criterion = by_kind(snapshot, "criterion")
    assert criterion["neighbor_span_angstrom"] == 0.16000000000042536
    assert criterion["target_width_angstrom"] == 0.12
    assert criterion["acceptance_criteria"]["distance_tolerance_angstrom"] == 1e-8
    assert criterion["reason"] == "span_too_wide" and criterion["goal_satisfied"] is False
    assert criterion["rule_version"] == "finite-sampling-1"
    members = by_kind(snapshot, "members")
    assert sum(m["required"] and m["status"] == "qualified" for m in members) == 3
    assert sum(not m["required"] and m["status"] == "missing" for m in members) == 2
    assert result.operation_status == "completed" and not result.qualified_outputs
    assert not snapshot["goals"][0]["goal_complete"]
    codes = {item["code"] for item in snapshot["blockers"]}
    assert {"scientific_execution_not_authorized", "additional_science_not_authorized",
            "orca_starts_exhausted", "extra_orca_starts_exhausted"} <= codes
    assert "missing_required_member" not in json.dumps(snapshot)
    report = build_report(store, run)
    assert report["delivery"] == snapshot and not report["user_goal_complete"]
    text = render_report(report)
    for required in ("0.16000000000042536 Å", "0.12 Å + 1e-08 Å", "span_too_wide",
                     "scientific_execution_not_authorized", "extra_orca_starts_exhausted", "required=False"):
        assert required in text
    assert report["permission"]["scientific_execution"] is False
    assert run.usage.orca_starts_actual == 0


def test_positive_sampling_completion_is_not_replaced_by_permission_failure(tmp_path):
    store, run, request, plan, _ = sampling_run(tmp_path, "stop")
    snapshot = collect_delivery_snapshot(store, run, request, plan)
    assert snapshot["goals"][0]["goal_complete"]
    assert snapshot["blockers"] == []
    assert snapshot["explanations"][0]["relation"] == "current_goal_satisfied"
    assert snapshot["next_actions"][0]["kind"] == "deliver"
    assert build_report(store, run)["user_goal_complete"]


def test_failed_analysis_rechecks_upstream_result_fingerprint_before_showing_numbers(tmp_path):
    store, run, request, plan, result = sampling_run(tmp_path)
    assert by_kind(collect_delivery_snapshot(store, run, request, plan), "criterion")["reason"] == "span_too_wide"
    binding = next(value for value in result.source["consumption"].values()
                   if isinstance(value, dict) and "result_fingerprint" in value)
    upstream = store.load_result(binding["run_id"], binding["result_id"])
    upstream.qualified_outputs["energy"].value += 1
    # Intentional corruption in this test's temporary Store, never an archival edit.
    store.path(f"runs/{upstream.run_id}/results/{upstream.id}.json").write_text(upstream.model_dump_json(), encoding="utf-8")
    snapshot = collect_delivery_snapshot(store, run, request, plan)
    assert "criterion" not in {fact["kind"] for fact in snapshot["facts"]}
    assert "source_unverified" in {item["code"] for item in snapshot["blockers"]}
    assert not snapshot["goals"][0]["goal_complete"]
    report = build_report(store, run)
    assert report["results"][0]["scientific_status"] == "source_unverified"
    assert "成员来源未核验" in render_report(report)


@pytest.mark.parametrize("partial", [False, True])
def test_raw_read_keeps_unknown_units_and_partial_status_separate_from_science(source, partial):
    source.request.goals = [Goal(id="g_energy", port="field_observation", minimum_check_version="evidence-read-1")]
    source.plan.goal_map = {"g_energy": OutputBinding(step_id="step_one", port="field_observation")}
    source.result.qualified_outputs = {}
    source.result.checks = {"field_observation": [Check(name="read", status="passed", rule_version="evidence-read-1")]}
    source.result.observations = {"field_observation": {"status": "partial" if partial else "observed",
                                                       "value": [1, 2], "units": None}}
    call = ToolCall(tool="evidence.value", parameters={"artifact_id": source.artifact.id}, request_version=1, state="completed")
    source.run.calls.append(call)
    source.result.call_id = call.id
    snapshot = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    goal = snapshot["goals"][0]
    assert goal["goal_complete"] is (not partial)
    if not partial:
        assert goal["answer"]["kind"] == "evidence_observation"
        assert goal["answer"]["unit"] is None
        assert goal["answer"]["scientific_qualification"] is False
    else:
        assert goal["answer"] is None
    assert not any("science" in item["code"] or "orca" in item["code"] for item in snapshot["blockers"])


def test_requested_bounded_read_answer_is_not_replaced_by_hash_summary(source):
    source.request.goals = [Goal(id="g_energy", port="search_hits", minimum_check_version="evidence-read-1")]
    source.plan.goal_map = {"g_energy": OutputBinding(step_id="step_one", port="search_hits")}
    source.result.qualified_outputs = {}
    source.result.checks = {"search_hits": [Check(name="read", status="passed", rule_version="evidence-read-1")]}
    observation = {"status": "observed", "matches": [{"line": 1, "text": "x" * 900}],
                   "scientific_status": "unverified", "unit": None, "partial": False}
    source.result.observations = {"search_hits": observation}
    call = ToolCall(tool="evidence.search", parameters={"artifact_id": source.artifact.id, "query": "x"},
                    request_version=1, state="completed")
    source.run.calls.append(call)
    source.result.call_id = call.id
    snapshot = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    answer = by_kind(snapshot, "answer")
    assert answer["observation"] == observation
    assert answer["scientific_qualification"] is False and answer["unit"] is None


def test_stable_fingerprint_excludes_own_model_settlement_and_terminal_state(source):
    first = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    source.run.usage.model_calls += 1
    source.run.usage.model_tokens_used += 100
    source.run.usage.model_tokens_unknown += 900
    source.run.usage.decision_rounds += 1
    source.run.usage.elapsed_seconds += 30
    source.run.deadline += timedelta(seconds=10)
    source.run.state, source.run.delivery_status = "failed", "partial"
    source.run.model_records.append({"status": "known", "cost_known_usd": "0.01"})
    source.run.decisions.append({"action": "stop", "reason": "wrong free text remains audit-only"})
    source.run.applied_decisions.append("decision_one")
    source.run.processed_feedback.append(source.result.id)
    second = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    assert first == second


@pytest.mark.parametrize("change", ["condition", "permission", "science_budget", "control", "result_value", "source_hash"])
def test_current_purpose_or_source_changes_invalidate_fingerprint(source, change):
    first = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    if change == "condition":
        source.request.multiplicity = None
    elif change == "permission":
        source.run.permission.scientific_execution = True
    elif change == "science_budget":
        source.run.usage.extra_orca_starts_reserved += 1
    elif change == "control":
        source.run.control_generation += 1
    elif change == "result_value":
        source.result.qualified_outputs["energy"].value = -74
    else:
        source.path.write_bytes(b"changed")
    second = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    assert first["fingerprint"] != second["fingerprint"]
    if change == "source_hash":
        assert second["goals"][0]["answer"] is None
        assert "source_unverified" in {item["code"] for item in second["blockers"]}


def test_control_generation_override_and_every_goal_short_reference_binding(source):
    source.request.goals.append(Goal(id="another", port="free_energy", minimum_check_version="unresolved-1"))
    snapshot = collect_delivery_snapshot(source.store, source.run, source.request, source.plan, control_generation=7)
    assert snapshot["basis"]["control_generation"] == 7 and source.run.control_generation == 0
    assert [goal["ref"] for goal in snapshot["goals"]] == ["g1", "g2"]
    for goal in snapshot["goals"]:
        assert goal["required_fact_refs"]
        facts = [item for item in snapshot["facts"] if item["ref"] in goal["required_fact_refs"]]
        assert all(item["goal_ref"] == goal["ref"] for item in facts)
        explanation = next(item for item in snapshot["explanations"] if item["ref"] in goal["explanation_refs"])
        assert explanation["fact_refs"] == goal["required_fact_refs"]
        assert explanation["blocker_refs"] == goal["required_blocker_refs"]
        assert explanation["next_action_refs"] == goal["next_action_refs"]


def test_registration_scope_has_no_invented_wait_or_permission_request(source):
    source.request.conditions["explain_results"] = True
    source.run.goal_status = {"g_energy": "insufficient_evidence"}
    source.run.decisions.append({"request_version": source.request.version, "semantics": {
        "delivery_scope": "registration_only", "awaiting_reply": False, "questions": [], "notices": ["Registered."]}})
    snapshot = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    assert snapshot["explanations"][0]["relation"] == "registration_complete_science_unmet"
    assert snapshot["next_actions"][0]["kind"] == "stop_after_registration"
    assert not snapshot["communication"]["awaiting_reply"]
    report = build_report(source.store, source.run)
    assert report["communication"]["registration_complete"]
    assert report["model_explanation"]["status"] == "not_requested"
    assert not report["user_goal_complete"]


def test_report_uses_selected_facts_without_independent_goal_selection(source, monkeypatch):
    import orca_agent.delivery as module

    calls, original = [], module.current_goal_evidence

    def selected(*args, **kwargs):
        calls.append(args[3].id)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "current_goal_evidence", selected)
    report = build_report(source.store, source.run)
    assert calls == ["g_energy"]
    assert report["goal_facts"] == collect_goal_facts(source.store, source.run, source.request, source.plan)


def test_receipt_contract_and_report_status_are_distinct_and_rechecked(source):
    snapshot = collect_delivery_snapshot(source.store, source.run, source.request, source.plan)
    source.run.terminal_deliveries.append(TerminalReceipt(decision_id="decision_one", contract_version=snapshot["version"],
        basis=snapshot["basis"], snapshot_fingerprint=snapshot["fingerprint"], snapshot=snapshot,
        explanation={}, terminal_state="completed"))
    report = build_report(source.store, source.run)
    assert report["model_explanation"]["status"] == "passed"
    assert report["model_explanation"]["current"]
    assert report["model_explanation"]["report_status"] == "pending"
    source.path.write_bytes(b"changed source")
    report = build_report(source.store, source.run)
    assert not report["model_explanation"]["current"]
    assert report["model_explanation"]["status"] == "passed"  # Historical fact, not current applicability.
    assert not report["user_goal_complete"]
    assert source.run.terminal_deliveries[0].snapshot_fingerprint == snapshot["fingerprint"]


def test_deterministic_fallback_preserves_goals_beyond_model_capacity(source):
    source.request.goals.extend(Goal(id=f"unknown_{index}", port="unresolved", minimum_check_version="unresolved-1")
                                for index in range(80))
    report = build_report(source.store, source.run)
    assert len(report["goals"]) == len(report["goal_facts"]) == len(report["delivery"]["goals"]) == 81
    assert "unknown_79" in render_report(report)


@pytest.mark.parametrize("requested,state,category,version,expected", [
    (False, "ready", None, 1, "not_requested"),
    (True, "ready", None, 1, "pending"),
    (True, "failed", None, 1, "unavailable"),
    (True, "budget_exhausted", None, 1, "unavailable"),
    (True, "ready", "proposal_rejected", 1, "pending"),
    (True, "failed", "terminal_explanation_rejected", 1, "rejected"),
    (True, "failed", "terminal_explanation_rejected", 2, "unavailable"),
])
def test_unverified_explanation_states_are_not_confused_with_planning_or_old_rejections(
        source, requested, state, category, version, expected):
    source.request.conditions["explain_results"] = requested
    source.run.state = state
    if category:
        source.run.diagnostics.append({"category": category, "request_version": version,
                                       "control_generation": source.run.control_generation})
    report = build_report(source.store, source.run)
    assert report["model_explanation"]["status"] == expected
    assert report["model_explanation"]["current_status"] == expected
    assert source.run.terminal_deliveries == []
