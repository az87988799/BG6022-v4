"""Offline context projections and scripted reservations; no HTTP or science."""

import copy
from datetime import timedelta

import pytest

from orca_agent import context, model_usage
from orca_agent.decision_purpose import prepared_decision_purpose
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import (
    Attempt,
    BudgetLimits,
    Check,
    EvidenceRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    QualifiedOutput,
    Request,
    Result,
    Step,
    fingerprint,
)
from orca_agent.store import BudgetExceeded, Store, StoreError
from tests.unit.test_context import objects, payload
from tests.unit.test_model_usage import ScriptedTransport


def historical_results():
    request, run = objects()
    steps, results = [], []
    for index in range(5):
        step = Step(id=f"step_{index}", logical_id=f"logical_{index}", tool="orca.sp",
                    geometry=InputRef(artifact_id=f"geometry_{index}"))
        checks = [Check(name="synthetic_evidence", status="passed", rule_version="test-only-1")]
        result = Result(run_id=run.id, step_id=step.id, attempt_id=f"attempt_{index}",
            operation_status="completed",
            qualified_outputs={"energy": QualifiedOutput(value=-float(index), unit="Eh", checks=checks)},
            observations={"reported": {"detail": f"original body {index}"}},
            source={"geometry_artifact_id": f"geometry_{index}", "sha256": str(index) * 64})
        run.attempts.append(Attempt(id=result.attempt_id, step_id=step.id, logical_id=step.logical_id,
            number=1, tool=step.tool, input_fingerprint=f"input_{index}", directory=f"attempts/{index}",
            frozen_step=step, state="completed", result_id=result.id))
        steps.append(step)
        results.append(result)
    pending = Step(id="pending", logical_id="pending", tool="orca.sp",
        geometry=InputRef(artifact_id="geometry_pending"),
        inputs={"report": EvidenceRef(producer_step_id=steps[3].id, port="energy"),
                "value": EvidenceRef(producer_step_id=steps[4].id, port="energy")},
        depends_on=[steps[3].id, steps[4].id])
    results[3].qualified_outputs = {}  # Pending consumer needs the raw report, not a fabricated qualified value.
    plan = Plan(request_id=request.id, steps=[*steps, pending],
                goal_map={"energy": OutputBinding(step_id=steps[0].id, port="energy")})
    run.result_ids = [result.id for result in results]
    return run, plan, results


def test_projection_preserves_bound_goal_fresh_failure_and_unqualified_pending_input():
    run, plan, results = historical_results()
    results[1].operation_status = "failed"
    results[1].qualified_outputs = {}
    results[1].diagnostics = [{"message": "current raw failure, not a success"}]
    results[1].checks = {"energy": [Check(name="syntax", status="failed", rule_version="test-only-1",
                                        detail="invalid native input")]}
    before = copy.deepcopy((run, plan, results))
    shown, refs = context._decision_result_references(
        run, plan, results, [context._result(result, 256) for result in results],
        {"new_result_ids": [results[2].id]})
    bodies = {row["result_id"]: row for row in shown}
    assert set(bodies) == {result.id for result in results[:4]}
    assert bodies[results[1].id]["operation_status"] == "failed"
    assert bodies[results[1].id]["untrusted_diagnostics"] == results[1].diagnostics
    assert bodies[results[1].id]["checks"]["energy"]["nonpassing"] == {"syntax": "failed"}
    # The selected Goal is the oldest, not whichever result was most recent.
    assert bodies[results[0].id]["qualified_outputs"]["energy"]["value"] == 0.0
    assert bodies[results[3].id]["unqualified_observations"]["reported"] == results[3].observations["reported"]
    assert len(refs) == 1 and refs[0]["result_id"] == results[4].id
    assert refs[0]["record_sha256"] == fingerprint(results[4])
    assert refs[0]["qualified_outputs"]["energy"]["value"] == -4.0
    assert refs[0]["source"] == context._source_summary(results[4].source)
    assert (run, plan, results) == before


def test_unchecked_qualified_history_cannot_be_laundered_into_a_reference():
    _, _, results = historical_results()
    results[4].qualified_outputs["energy"].checks[0].status = "failed"
    with pytest.raises(ValueError, match="qualification"):
        context._result(results[4], 256)


def test_external_result_reference_keeps_its_source_run_instead_of_defaulting_to_current():
    run, plan, results = historical_results()
    external = results[-1].model_copy(update={
        "id": "external_result", "run_id": "source_run", "step_id": "source_step"})
    run.permission.result_ids.append(external.id)
    originals = [*results, external]
    _, refs = context._decision_result_references(
        run, plan, originals, [context._result(result, 256) for result in originals], {})
    by_id = {row["result_id"]: row for row in refs}
    assert by_id[external.id]["run_id"] == "source_run"
    assert by_id[external.id]["record_sha256"] == fingerprint(external)
    assert "run_id" not in by_id[results[-1].id]


def planning_run(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    request = Request(original_text="Prepare an evidence report; include incomplete work.",
        conditions={"explain_results": True}, goals=[Goal(id="report", port="artifacts",
            minimum_check_version="generic-observation-1",
            evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    run = store.create_run(request, None,
        PermissionSnapshot(artifact_writes=True, model_execution=True, allowed_tools=["workspace.write"]),
        BudgetLimits(model_calls=8, decision_rounds=12, model_tokens=48000,
                     input_tokens=12000, output_tokens=2000))
    run.usage.model_calls = run.usage.decision_rounds = 1
    run.goal_status["report"] = "insufficient_evidence"
    store.save_run(run)
    return store, run, request


@pytest.mark.parametrize("pressure", ["tokens", "unknown_tokens", "time"])
def test_remaining_terminal_envelope_selects_partial_before_planning_reservation(tmp_path, monkeypatch, pressure):
    store, run, request = planning_run(tmp_path)
    now = run.created_at
    if pressure == "tokens":
        run.usage.model_tokens_used = 33000
    elif pressure == "unknown_tokens":
        run.usage.model_tokens_unknown = 33000
    else:
        now = run.deadline - timedelta(seconds=30)
    monkeypatch.setattr(model_usage, "utc_now", lambda: now)
    store.save_run(run)
    snapshot = collect_delivery_snapshot(store, run, request)
    before = run.model_copy(deep=True)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot, now=now)
    purpose = prepared_decision_purpose(prepared)
    assert purpose.kind == "terminal" and purpose.allowed_actions == ("stop",)
    assert set(purpose.allowed_actions) <= set(context.current_decision_purpose(request, run).allowed_actions)
    wire = payload(prepared)
    assert any(fact["kind"] == "goal_status" and fact["value"]["complete"] is False
               for fact in wire["DATA"]["delivery"]["facts"])
    margin = model_usage.validate_delivery_margin(request, run, purpose=purpose, prepared=prepared, now=now)
    assert margin["future_answer_calls"] == margin["future_answer_tokens"] == 0
    assert run == before and run.usage.model_calls < run.budget.model_calls - 1
    if pressure == "time":
        assert prepared.timeout_seconds == 27
    transport = ScriptedTransport()
    model_usage.send_model(store, run, prepared, transport, basis=model_usage.current_basis(store, run),
                           logical_id=f"terminal_{pressure}", delivery_snapshot=snapshot)
    assert transport.sends == 1 and run.usage.model_calls == 2
    assert run.goal_status == before.goal_status and run.budget == before.budget
    assert run.usage.model_tokens_used == before.usage.model_tokens_used + 15
    assert run.usage.model_tokens_unknown == before.usage.model_tokens_unknown


def test_sufficient_two_envelopes_continue_planning_and_explanation_opt_out_is_respected(tmp_path, monkeypatch):
    store, run, request = planning_run(tmp_path)
    now = run.created_at
    monkeypatch.setattr(model_usage, "utc_now", lambda: now)
    run.usage.model_tokens_used = 22000  # 26K remaining: 14K decision + 12K final.
    snapshot = collect_delivery_snapshot(store, run, request)
    assert not model_usage.terminal_delivery_due(request, run, now=now)
    assert prepared_decision_purpose(context.build_context(request, run, delivery_snapshot=snapshot, now=now)).kind == "planning"
    request.conditions["explain_results"] = False
    run.usage.model_tokens_used = 47000
    assert not model_usage.terminal_delivery_due(request, run, now=run.deadline - timedelta(seconds=1))


def test_terminal_selection_does_not_bypass_actual_token_or_zero_call_guard(tmp_path, monkeypatch):
    store, run, request = planning_run(tmp_path)
    monkeypatch.setattr(model_usage, "utc_now", lambda: run.created_at)
    run.usage.model_tokens_unknown = run.budget.model_tokens - 1
    store.save_run(run)
    snapshot = collect_delivery_snapshot(store, run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot, now=run.created_at)
    assert prepared_decision_purpose(prepared).kind == "terminal"
    before = run.model_copy(deep=True)
    transport = ScriptedTransport()
    with pytest.raises(BudgetExceeded, match="token"):
        model_usage.send_model(store, run, prepared, transport, basis=model_usage.current_basis(store, run),
                               logical_id="insufficient", delivery_snapshot=snapshot)
    assert transport.sends == 0 and run == before
    store, run, request = planning_run(tmp_path / "calls")
    run.usage.model_calls = run.budget.model_calls
    store.save_run(run)
    snapshot = collect_delivery_snapshot(store, run, request)
    with pytest.raises(ValueError, match="call"):
        context.build_context(request, run, delivery_snapshot=snapshot, now=run.created_at)


def test_send_lock_rejects_stale_planning_after_time_crosses_terminal_boundary(tmp_path, monkeypatch):
    store, run, request = planning_run(tmp_path)
    now = run.deadline - timedelta(seconds=180)
    monkeypatch.setattr(model_usage, "utc_now", lambda: now)
    snapshot = collect_delivery_snapshot(store, run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot, now=now)
    assert prepared_decision_purpose(prepared).kind == "planning"
    monkeypatch.setattr(model_usage, "utc_now", lambda: run.deadline - timedelta(seconds=30))
    transport = ScriptedTransport()
    with pytest.raises(StoreError, match="purpose"):
        model_usage.send_model(store, run, prepared, transport, basis=model_usage.current_basis(store, run),
                               logical_id="stale", delivery_snapshot=snapshot)
    assert transport.sends == 0 and run.usage.model_calls == 1 and not run.model_records
