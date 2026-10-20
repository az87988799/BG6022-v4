"""Real file Tools and scripted model receipts; no network or scientific process."""

import copy

import pytest
from test_agent import ScriptedTransport

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.mechanical_progression import select_mechanical_step
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import BudgetExceeded, Store, StoreError
from orca_agent.tools.dispatch import execute_call


def case(tmp_path, enabled=True):
    config = Config(data_root=tmp_path / "data", mechanical_progression=enabled)
    store = Store(config.data_root, environment_root=tmp_path / "environment",
                  configuration=config.run_configuration())
    request = Request(conditions={"explain_results": True}, goals=[Goal(id="report", port="artifacts",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])
    steps = [Step(id=f"write_{i}", logical_id=f"write_{i}", tool="workspace.write",
                  depends_on=[f"write_{i-1}"] if i else [],
                  parameters={"name": f"part_{i}.txt", "content": f"part {i}", "role": "text"})
             for i in range(3)]
    plan = Plan(request_id=request.id, steps=steps,
                goal_map={"report": OutputBinding(port="artifacts", gap="Only preparatory files; analysis absent.")})
    run = store.create_run(request, plan, PermissionSnapshot(artifact_writes=True, model_execution=True,
        allowed_tools=["workspace.write"]), BudgetLimits(analysis_executions=3, model_calls=8,
        model_tokens=250000, input_tokens=48000, output_tokens=2000, decision_rounds=12))
    run.agent_enabled = True
    store.save_run(run)
    return store, config, run, plan


@pytest.mark.parametrize("enabled,expected_calls", [(False, 3), (True, 1)])
def test_three_approved_files_use_one_terminal_model_call_with_policy(tmp_path, enabled, expected_calls):
    store, config, run, plan = case(tmp_path, enabled)
    scripts = ([] if enabled else [{"action": "call_tool", "parameters": {"step_id": step.id}}
                                   for step in plan.steps[1:]])
    transport = ScriptedTransport(*scripts, {"action": "stop", "parameters": {}})
    ended = agent.execute(store, config, run.id, transport=transport)
    assert len(ended.calls) == 3 and all(call.state == "completed" for call in ended.calls), ended.diagnostics
    assert len(transport.sent) == ended.usage.model_calls == expected_calls
    assert ended.usage.decision_rounds == 3
    assert ended.usage.orca_starts_actual == 0
    assert ended.delivery_status == "partial"  # Writing files is no scientific/Goal pass.
    assert ended.usage.file_writes == 3 and ended.usage.analysis_executions == 0
    assert len([row for row in ended.decisions if row.get("selection") == "mechanical-feedback-1"]) == (2 if enabled else 0)


def feedback_case(tmp_path):
    store, config, run, plan = case(tmp_path)
    first = plan.steps[0]
    result = execute_call(store, run, first.tool, first.parameters.model_dump(), step=first)
    return store, run, plan, {first.id: result}


@pytest.mark.parametrize("blocker", ["goals", "signal", "message", "failed", "changed_step", "unknown_call", "disabled"])
def test_changed_or_unsettled_feedback_keeps_model_control(tmp_path, blocker):
    store, run, plan, results = feedback_case(tmp_path)
    if blocker == "signal":
        store.signal(run.id, "pause")
    elif blocker == "message":
        store.enqueue_message(run.id, "Different conditions requested.")
    elif blocker == "failed":
        results[plan.steps[0].id] = results[plan.steps[0].id].model_copy(update={"operation_status": "failed"})
    elif blocker == "changed_step":
        plan = plan.model_copy(deep=True)
        plan.steps[0].parameters.content = "changed"
    elif blocker == "unknown_call":
        run.calls[0].state = "unknown"
    elif blocker == "disabled":
        run.configuration = run.configuration.model_copy(update={"mechanical_progression": False})
    before = copy.deepcopy(run)
    assert select_mechanical_step(store, run, plan, results, [plan.steps[1]], goals_changed=blocker == "goals") is None
    assert run == before


def test_budget_exhaustion_and_corrupt_artifact_never_advance(tmp_path, monkeypatch):
    store, run, plan, results = feedback_case(tmp_path)
    before = copy.deepcopy(run)
    def corrupt(*args, **kwargs):
        raise StoreError("artifact content hash changed")
    with monkeypatch.context() as patch:
        patch.setattr(store, "artifact_path", corrupt)
        with pytest.raises(StoreError, match="hash changed"):
            select_mechanical_step(store, run, plan, results, [plan.steps[1]])
    assert run == before
    run.usage.decision_rounds = run.budget.decision_rounds
    store.save_run(run)
    with pytest.raises(BudgetExceeded):
        select_mechanical_step(store, run, plan, results, [plan.steps[1]])
    assert not run.decisions and not run.processed_feedback


def test_saved_mechanical_decision_recovers_without_duplicate_writes(tmp_path):
    store, config, run, _ = case(tmp_path)
    fired = False
    def interrupt(point):
        nonlocal fired
        if point == "after_mechanical_decision" and not fired:
            fired = True
            raise KeyboardInterrupt
    first = agent.execute(store, config, run.id, fault=interrupt, transport=ScriptedTransport())
    assert fired and len(first.calls) == 1
    transport = ScriptedTransport({"action": "stop", "parameters": {}})
    ended = agent.execute(store, config, run.id, resume=True, transport=transport)
    assert len(ended.calls) == len({call.step_id for call in ended.calls}) == 3
    assert ended.usage.model_calls == len(transport.sent) == 1
    assert ended.usage.decision_rounds == 3 and ended.usage.file_writes == 3
    assert ended.usage.analysis_executions == 0
    assert len(runner._step_results(store, ended)) == 3


def test_missing_historical_policy_stays_disabled():
    from orca_agent.models import RunConfiguration
    historical = RunConfiguration(version=1).model_dump(exclude={"mechanical_progression"})
    assert RunConfiguration.model_validate(historical).mechanical_progression is False
    assert Config().run_configuration().mechanical_progression is True


@pytest.mark.parametrize("remaining", ["round", "calls", "tokens", "time"])
def test_final_explanation_reserve_is_not_consumed_by_mechanical_work(tmp_path, remaining, monkeypatch):
    from datetime import timedelta

    from orca_agent import model_usage
    store, run, plan, results = feedback_case(tmp_path)
    if remaining == "round":
        run.usage.decision_rounds = run.budget.decision_rounds - 1
    elif remaining == "calls":
        run.usage.model_calls = run.budget.model_calls
    elif remaining == "tokens":
        run.usage.model_tokens_unknown = run.budget.model_tokens - 100
    else:
        monkeypatch.setattr(model_usage, "utc_now", lambda: run.deadline - timedelta(seconds=1))
    store.save_run(run)
    before = copy.deepcopy(run)
    assert select_mechanical_step(store, run, plan, results, [plan.steps[1]]) is None
    assert run == before
