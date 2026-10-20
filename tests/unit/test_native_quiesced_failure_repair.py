"""Offline archived receipts only; no model, container or ORCA execution."""

import copy
import json

import pytest

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
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


def case(tmp_path, monkeypatch, defect=None):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _: None)
    config = Config(data_root=tmp_path / "data")
    store = Store(config.data_root, environment_root=tmp_path / "environment",
                  configuration=config.run_configuration())
    codes = []
    for index in range(2):
        source = tmp_path / f"input-{index}.inp"
        source.write_text(f"# synthetic input {index}; never executed\n")
        artifact = store.import_artifact(source, "native_input")
        codes.append(CodeRef(artifact_id=artifact.id, sha256=artifact.sha256, language="orca"))
    request = Request(original_text="Retain raw evidence and bounded failures.", goals=[
        Goal(id="raw", port="artifacts", minimum_check_version="generic-observation-1",
             evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    step = Step(id="native", logical_id="native", tool="orca.run", parameters={"input": codes[0]})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(scientific_execution=True,
        artifact_writes=True, allowed_tools=["orca.run"], artifact_ids=[code.artifact_id for code in codes]),
        BudgetLimits(orca_starts=2, extra_orca_starts=1, attempts_per_step=2, plan_revisions=2))
    attempt = store.reserve_attempt(run, step)
    handle = {"backend": "linux-container", "kind": "orca", "launch_phase": "gated_launcher",
              "container_id": "offline-container", "launch_id": "offline-launch",
              "workload_identity": {"pid": 42, "start_ticks": "1234"}}
    attempt.execution_handle = copy.deepcopy(handle)
    outcome = {
        "state": "failed", "reason": "orca_actual_start_unconfirmed",
        "actual_start_status": "unconfirmed", "not_started": False, "exit_code": 11,
        "handle": handle, "resource_usage": {"active_processes": 0, "complete": False},
    }
    if defect == "unknown":
        outcome["state"] = "unknown"
    elif defect == "active":
        outcome["resource_usage"]["active_processes"] = 1
    elif defect == "boolean_active":
        outcome["resource_usage"]["active_processes"] = False
    elif defect == "handle_mismatch":
        attempt.execution_handle["container_id"] = "another-container"
    elif defect in {"zero_exit", "boolean_exit"}:
        outcome["exit_code"] = 0 if defect == "zero_exit" else True
    elif defect == "wrong_reason":
        outcome["reason"] = "container_termination_unconfirmed"
    elif defect == "wrong_phase":
        handle["launch_phase"] = "executed"
        attempt.execution_handle = copy.deepcopy(handle)
    elif defect == "invalid_identity":
        handle["workload_identity"]["pid"] = False
        attempt.execution_handle = copy.deepcopy(handle)
    elif defect in {"invalid_ticks", "zero_ticks", "integer_ticks"}:
        handle["workload_identity"]["start_ticks"] = {
            "invalid_ticks": "unknown", "zero_ticks": "0", "integer_ticks": 1234}[defect]
        attempt.execution_handle = copy.deepcopy(handle)
    elif defect in {"control_action", "reconciliation", "collection_reconciliation"}:
        outcome[defect] = {"state": "unknown"}
    elif defect and defect.startswith("missing:"):
        path = defect.split(":", 1)[1].split(".")
        target = outcome
        for part in path[:-1]:
            target = target[part]
        target.pop(path[-1])
    store.save_run(run)
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, step, attempt, outcome)
    store.save_result(result)
    runner._settle(store, run, attempt, result, outcome)
    repaired = step.model_copy(deep=True, update={"id": "repaired"})
    repaired.parameters.input = codes[1]
    revised = plan.model_copy(deep=True, update={"version": 2, "steps": [repaired]})
    revised.goal_map["raw"].step_id = repaired.id
    return store, run, plan, revised, result


def test_quiescent_unconfirmed_failure_allows_changed_input_without_rewriting_history(tmp_path, monkeypatch):
    store, run, plan, revised, result = case(tmp_path, monkeypatch)
    previous = run.attempts[0].model_copy(deep=True)
    budget = run.budget.model_copy(deep=True)
    before_result = store.load_result(run.id, result.id)
    assert previous.state == "failed" and not previous.started
    assert run.usage.orca_starts_reserved == 1 and run.usage.orca_starts_actual == 0
    assert store.environment_lease() is None
    assert not result.qualified_outputs
    with pytest.raises(StoreError, match="unchanged failed input"):
        store.reserve_attempt(run, plan.steps[0])
    agent._repair_evidence(store, run, revised)
    updated = store.commit_revision(run, revised, decision_id="bounded_input_repair",
        basis=current_basis(store, run), related_results=[result.id])
    second = store.reserve_attempt(updated, revised.steps[0])
    assert second.number == 2 and second.logical_id == previous.logical_id
    assert second.input_fingerprint != previous.input_fingerprint
    assert updated.usage.orca_starts_reserved == updated.budget.orca_starts == 2
    assert updated.usage.extra_orca_starts_reserved == 1
    assert updated.usage.orca_starts_actual == 0 and updated.usage.model_calls == 0
    assert updated.budget == budget and updated.attempts[0] == previous
    assert store.load_result(run.id, result.id) == before_result
    assert json.loads(store.path(f"{previous.directory}/execution.json").read_text()) == result.source["execution"]


@pytest.mark.parametrize("defect", [
    "unknown", "active", "boolean_active", "handle_mismatch", "zero_exit", "boolean_exit",
    "wrong_reason", "wrong_phase", "invalid_identity", "invalid_ticks", "zero_ticks", "integer_ticks",
    "control_action",
    "reconciliation", "collection_reconciliation",
    "missing:actual_start_status", "missing:not_started", "missing:exit_code",
    "missing:resource_usage.active_processes", "missing:handle.container_id",
    "missing:handle.launch_id", "missing:handle.workload_identity",
])
def test_uncertain_or_incomplete_execution_cannot_use_the_quiescent_exception(tmp_path, monkeypatch, defect):
    store, run, _, revised, _ = case(tmp_path, monkeypatch, defect)
    before = run.model_copy(deep=True)
    with pytest.raises(StoreError, match="repair|reconciled"):
        agent._repair_evidence(store, run, revised)
    assert run == before
    assert run.usage.orca_starts_reserved == 1 and run.usage.orca_starts_actual == 0


def test_quiescent_exception_is_not_a_generic_backend_failure_allowance(tmp_path, monkeypatch):
    _, run, _, _, result = case(tmp_path, monkeypatch)
    attempt = run.attempts[0].model_copy(deep=True)
    execution = copy.deepcopy(result.source["execution"])
    execution["handle"]["backend"] = "windows"
    attempt.execution_handle = copy.deepcopy(execution["handle"])
    assert not agent._quiescent_unconfirmed_native_failure(attempt, execution)


@pytest.mark.parametrize("defect", ["source_hash", "receipt", "collection"])
def test_quiescent_exception_still_requires_original_archive_integrity(tmp_path, monkeypatch, defect):
    store, run, _, revised, result = case(tmp_path, monkeypatch)
    if defect == "source_hash":
        result.source["code_sha256"] = "f" * 64
    elif defect == "receipt":
        result.source["execution"]["exit_code"] = 12
    else:
        result.observations["evidence_collection_complete"] = False
    monkeypatch.setattr(store, "load_result", lambda *args: result)
    with pytest.raises(StoreError, match="repair|receipt"):
        agent._repair_evidence(store, run, revised)
    assert run.usage.orca_starts_reserved == 1 and run.usage.orca_starts_actual == 0
