"""Offline mock receipts only: no provider, shell, container, Python task or ORCA."""

import copy
import json

import pytest

from orca_agent import agent, context, runner
from orca_agent.config import Config
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.model_usage import current_basis, send_model, terminal_delivery_due
from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store, StoreError
from orca_agent.tools import managed
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_mechanical_progression import feedback_case


class MockTransport(ScriptedTransport):
    """Synthetic decisions through the real reservation/settlement callbacks."""


def native_case(tmp_path, monkeypatch):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _: None)
    monkeypatch.setattr(agent, "require_execution_available", lambda _: None)
    config = Config(data_root=tmp_path / "data")
    store = Store(config.data_root, environment_root=tmp_path / "environment",
                  configuration=config.run_configuration())
    codes = []
    for number in range(2):
        path = tmp_path / f"input-{number}.inp"
        path.write_text(f"# synthetic unexecuted native input {number}\n")
        artifact = store.import_artifact(path, "native_input")
        codes.append(CodeRef(artifact_id=artifact.id, sha256=artifact.sha256, language="orca"))
    request = Request(original_text="Return raw evidence, including any bounded failure.",
        conditions={"explain_results": True}, goals=[Goal(id="raw", port="artifacts",
            minimum_check_version="generic-observation-1",
            evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    step = Step(id="native", logical_id="native_work", tool="orca.run", parameters={"input": codes[0]})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(model_execution=True,
        scientific_execution=True, artifact_writes=True, allowed_tools=["orca.run", "evidence.text"],
        artifact_ids=[code.artifact_id for code in codes]),
        BudgetLimits(orca_starts=3, extra_orca_starts=2, attempts_per_step=3, plan_revisions=4,
                     decision_rounds=16, corrections_per_proposal=1, evidence_reads=4,
                     model_calls=8, model_tokens=250000, input_tokens=48000, output_tokens=2000))
    run.agent_enabled = True
    store.save_run(run)
    return store, config, run, plan, codes


def settle_native(store, run, step, *, state="failed", started=False, control=None):
    attempt = store.reserve_attempt(run, step)
    outcome = {"state": state, "reason": "synthetic input validation failure",
               "not_started": not started, "handle": {"offline": True} if started else None,
               "exit_code": 1 if started else None, "resource_usage": {}}
    if control:
        outcome["control_action"] = control
        store.signal(run.id, control)
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, step, attempt, outcome)
    store.save_result(result)
    runner._settle(store, run, attempt, result, outcome)
    return result, outcome


def test_prelaunch_failure_is_settled_in_live_context_and_stale_choice_can_correct(tmp_path, monkeypatch):
    store, config, run, plan, _ = native_case(tmp_path, monkeypatch)
    seen = []

    def science(store, config, run, step, *args):
        seen.append(step.id)
        return settle_native(store, run, step)

    monkeypatch.setattr(agent, "_science", science)

    def stale_choice(wire):
        assert wire["CONTROL"]["pending_step_ids"] == []
        assert "revise_plan" in wire["AUTHORITY"]["decision_purpose"]["allowed_actions"]
        return {"action": "call_tool", "parameters": {"step_id": plan.steps[0].id}}

    def correction(wire):
        error = wire["CONTROL"]["validation_error"]["requirement"]
        assert error["expected_ready_step_ids"] == []
        assert "settled Result" in error["requirement"]
        return {"action": "stop", "parameters": {}}

    transport = MockTransport(stale_choice, correction)
    ended = agent.execute(store, config, run.id, transport=transport)
    assert seen == ["native"] and len(ended.attempts) == 1
    assert ended.attempts[0].state == "failed" and not ended.attempts[0].started
    assert ended.usage.orca_starts_reserved == 1 and ended.usage.orca_starts_actual == 0
    assert ended.usage.model_calls == 2 and len(ended.terminal_deliveries) == 1
    assert ended.delivery_status == "partial" and ended.goal_status["raw"] == "insufficient_evidence"
    assert agent._ready(store.load_plan(ended), runner._step_results(store, ended)) == []
    assert store.environment_lease() is None


def test_settled_prelaunch_failure_still_rejects_identical_input_at_store(tmp_path, monkeypatch):
    store, _, run, plan, _ = native_case(tmp_path, monkeypatch)
    settle_native(store, run, plan.steps[0])
    with pytest.raises(StoreError, match="unchanged failed input"):
        store.reserve_attempt(run, plan.steps[0])
    assert len(run.attempts) == 1 and run.usage.orca_starts_reserved == 1


def test_native_failure_revision_executes_only_new_input_and_preserves_both_receipts(tmp_path, monkeypatch):
    store, config, run, plan, codes = native_case(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_science", lambda store, config, run, step, *args:
        settle_native(store, run, step))

    def repair(wire):
        assert wire["CONTROL"]["pending_step_ids"] == []
        assert len(wire["AUTHORITY"]["related_results"]) == 1
        parameters = plan.steps[0].parameters.model_dump(mode="json")
        parameters["input"] = codes[1].model_dump(mode="json")
        return {"action": "revise_plan", "parameters": {
            "steps": [{"key": "fixed_input", "logical_key": plan.steps[0].logical_id,
                       "tool": plan.steps[0].tool, "parameters": parameters}],
            "goal_map": {"raw": {"step_key": "fixed_input", "port": "artifacts"}}}}

    transport = MockTransport(repair, {"action": "stop", "parameters": {}})
    ended = agent.execute(store, config, run.id, transport=transport)
    assert len(ended.attempts) == 2 and ended.usage.model_calls == 2, ended.diagnostics
    assert [item.number for item in ended.attempts] == [1, 2]
    assert len({item.logical_id for item in ended.attempts}) == 1
    assert len({item.input_fingerprint for item in ended.attempts}) == 2
    assert all(item.state == "failed" and not item.started for item in ended.attempts)
    assert len(ended.result_ids) == 2 and set(ended.result_ids) <= set(ended.processed_feedback)
    assert ended.usage.orca_starts_actual == 0 and ended.usage.orca_starts_reserved == 2
    assert ended.usage.extra_orca_starts_reserved == 1 and ended.delivery_status == "partial"
    assert len(ended.terminal_deliveries) == 1 and not store.environment_lease()


@pytest.mark.parametrize("control,state,expected", [
    ("pause", "cancelled", "paused"), (None, "unknown", "unknown")])
def test_control_pause_and_unknown_keep_existing_reconciliation_semantics(
        tmp_path, monkeypatch, control, state, expected):
    store, config, run, plan, _ = native_case(tmp_path, monkeypatch)
    monkeypatch.setattr(agent, "_science", lambda store, config, run, step, *args:
        settle_native(store, run, step, state=state, control=control))
    transport = MockTransport()
    ended = agent.execute(store, config, run.id, transport=transport)
    assert ended.state == expected and not transport.sent
    assert ended.usage.orca_starts_actual == 0 and len(ended.attempts) == 1
    if control == "pause":
        assert ended.attempts[0].state == "not_started"
        assert runner._step_results(store, ended) == {}
        assert agent._ready(plan, {}) == plan.steps
        assert store.environment_lease() is None
    else:
        assert ended.attempts[0].finished_at is None
        assert store.environment_lease()["attempt_id"] == ended.attempts[0].id
        with pytest.raises(StoreError, match="unfinished attempts"):
            agent.execute(store, config, run.id, transport=MockTransport())


@pytest.mark.parametrize("started", [False, True])
def test_native_changed_input_uses_bound_failure_for_same_logical_repair(tmp_path, monkeypatch, started):
    store, _, run, plan, codes = native_case(tmp_path, monkeypatch)
    result, _ = settle_native(store, run, plan.steps[0], started=started)
    before = copy.deepcopy(run.attempts[0])
    step = plan.steps[0].model_copy(deep=True, update={"id": "repaired_native"})
    step.parameters.input = codes[1]
    revised = plan.model_copy(deep=True, update={"version": 2, "steps": [step]})
    revised.goal_map["raw"].step_id = step.id
    agent._repair_evidence(store, run, revised)
    updated = store.commit_revision(run, revised, decision_id="native_repair",
        basis=current_basis(store, run), related_results=[result.id])
    next_attempt = store.reserve_attempt(updated, step)
    assert next_attempt.logical_id == before.logical_id and next_attempt.number == 2
    assert updated.usage.orca_starts_reserved == 2 and updated.usage.extra_orca_starts_reserved == 1
    assert updated.usage.orca_starts_actual == int(started)  # Synthetic accounting only.
    assert updated.attempts[0] == before and not next_attempt.started
    assert not result.qualified_outputs


@pytest.mark.parametrize("defect", ["unknown", "cancelled", "unfinished", "foreign_result", "wrong_hash",
    "reconciliation", "missing_collection", "missing_receipt", "altered_receipt", "control"])
def test_native_repair_rejects_unsettled_or_unbound_evidence(tmp_path, monkeypatch, defect):
    store, _, run, plan, codes = native_case(tmp_path, monkeypatch)
    result, _ = settle_native(store, run, plan.steps[0])
    attempt = run.attempts[0]
    if defect in {"unknown", "cancelled"}:
        attempt.state = defect
    elif defect == "unfinished":
        attempt.finished_at = None
    elif defect == "foreign_result":
        result.run_id = "other_run"
    elif defect == "wrong_hash":
        result.source["code_sha256"] = "f" * 64
    elif defect in {"reconciliation", "control"}:
        result.source["execution"]["reconciliation" if defect == "reconciliation" else "control_action"] = "pause"
    elif defect == "missing_collection":
        result.observations["evidence_collection_complete"] = False
    elif defect == "missing_receipt":
        del result.source["files"]["execution.json"]
    else:
        result.source["execution"]["reason"] = "changed after receipt"
    monkeypatch.setattr(store, "load_result", lambda *args: result)
    step = plan.steps[0].model_copy(deep=True, update={"id": "repair"})
    step.parameters.input = codes[1]
    candidate = plan.model_copy(deep=True, update={"steps": [step]})
    before = run.model_copy(deep=True)
    with pytest.raises(StoreError, match="repair|reconciled"):
        agent._repair_evidence(store, run, candidate)
    assert run == before


@pytest.mark.parametrize("dimension", ["calls", "rounds"])
def test_last_slot_forces_partial_terminal_before_more_approved_work(tmp_path, dimension):
    store, run, _, _ = feedback_case(tmp_path)
    if dimension == "calls":
        run.usage.model_calls = run.budget.model_calls - 1
    else:
        run.usage.decision_rounds = run.budget.decision_rounds - 1
    store.save_run(run)
    before_budget = run.budget.model_copy(deep=True)
    transport = MockTransport({"action": "stop", "parameters": {}})
    ended = agent.execute(store, Config(), run.id, transport=transport)
    assert len(transport.sent) == 1 and len(ended.calls) == 1
    assert transport.sent[0]["AUTHORITY"]["decision_purpose"] == {
        "kind": "terminal", "allowed_actions": ["stop"]}
    assert ended.delivery_status == "partial" and ended.goal_status["report"] == "insufficient_evidence"
    assert ended.state == "failed" and len(ended.terminal_deliveries) == 1
    assert ended.budget == before_budget and ended.usage.orca_starts_actual == 0


def test_zero_calls_keeps_deterministic_partial_report_without_more_work(tmp_path):
    store, run, _, _ = feedback_case(tmp_path)
    run.usage.model_calls = run.budget.model_calls
    store.save_run(run)
    transport = MockTransport()
    ended = agent.execute(store, Config(), run.id, transport=transport)
    assert ended.state == "budget_exhausted" and ended.delivery_status == "partial"
    assert not transport.sent and len(ended.calls) == 1
    assert ended.usage.model_calls == run.budget.model_calls and not ended.terminal_deliveries
    assert ended.fallback_report_receipts


def test_last_terminal_reply_cannot_authorize_work_or_an_extra_correction_call(tmp_path):
    store, run, plan, _ = feedback_case(tmp_path)
    run.usage.model_calls = run.budget.model_calls - 1
    store.save_run(run)
    transport = MockTransport({"action": "call_tool", "parameters": {"step_id": plan.steps[1].id}})
    ended = agent.execute(store, Config(), run.id, transport=transport)
    assert len(transport.sent) == 1 and len(ended.calls) == 1
    assert ended.usage.model_calls == ended.budget.model_calls
    assert ended.state == "failed" and ended.delivery_status == "partial"
    assert not ended.terminal_deliveries and ended.fallback_report_receipts
    rejection = next(row for row in ended.decisions if row["action"] == "rejected")
    assert rejection["parameters"]["requirement"]["code"] == "decision_purpose_action"
    assert rejection["parameters"]["requirement"]["allowed_actions"] == ["stop"]


def test_last_slot_never_bypasses_new_user_message_normalization(tmp_path):
    store, run, plan, _ = feedback_case(tmp_path)
    run.usage.model_calls = run.budget.model_calls - 1
    request = store.load_request(run)
    assert terminal_delivery_due(request, run)
    assert context.current_decision_purpose(request, run, plan, pending_messages=True).kind == "normalization"
    request.conditions.pop("explain_results")
    assert not terminal_delivery_due(request, run)


def test_send_lock_rechecks_terminal_budget_basis_without_reservation(tmp_path):
    store, run, plan, results = feedback_case(tmp_path)
    run.usage.model_calls = run.budget.model_calls - 2
    store.save_run(run)
    request = store.load_request(run)
    snapshot = collect_delivery_snapshot(store, run, request, plan, results)
    prepared = context.build_context(request, run, plan, results=list(results.values()),
        feedback={"pending_step_ids": [plan.steps[1].id]}, delivery_snapshot=snapshot)
    # A stale planning projection cannot spend the reserved terminal call.
    run.usage.model_calls += 1
    store.save_run(run)
    before = run.model_copy(deep=True)
    transport = MockTransport()
    with pytest.raises(StoreError, match="decision purpose"):
        send_model(store, run, prepared, transport, basis=current_basis(store, run),
            logical_id="stale_terminal", delivery_snapshot=snapshot)
    assert run == before and not transport.sent and not run.model_records
    assert json.loads(prepared.body()["messages"][-1]["content"])["AUTHORITY"]["decision_purpose"]["kind"] == "planning"
