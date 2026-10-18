"""General attempts share durable budgets and one environment lease."""

import json

import pytest

from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    InputManifest,
    InternalCall,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import BudgetExceeded, EnvironmentBusy, Store, StoreError


def make_task(tmp_path, monkeypatch, *, tool="python.execute", permission_changes=None,
              budget_changes=None):
    # Reservation-only unit tests. No mocked process/isolation success is claimed.
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda name: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source = tmp_path / ("task.py" if tool == "python.execute" else "job.inp")
    source.write_text("print('task')" if tool == "python.execute" else "! HF STO-3G\n")
    artifact = store.import_artifact(source, "code")
    parameter_name = "code" if tool == "python.execute" else "input"
    code = CodeRef(artifact_id=artifact.id, sha256=artifact.sha256,
                   language="python" if tool == "python.execute" else "orca")
    request = Request(goals=[Goal(id="report", port="artifacts",
        minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    step = Step(id="task", logical_id="task", tool=tool, parameters={parameter_name: code})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"report": OutputBinding(step_id=step.id, port="artifacts")})
    permission = PermissionSnapshot(**{"scientific_execution": True, "code_execution": True,
        "artifact_writes": True, "artifact_ids": [artifact.id],
        "allowed_tools": ["python.execute", "orca.run"], **(permission_changes or {})})
    budget = BudgetLimits(**{"internal_work_units": 1, **(budget_changes or {})})
    run = store.create_run(request, plan, permission, budget)
    return store, run, step, artifact


def native_input(store, run, parent, tmp_path):
    source = tmp_path / "internal.inp"
    source.write_text("! HF STO-3G\n* xyz 0 1\nH 0 0 0\nH 0 0 .74\n*\n")
    artifact = store.import_artifact(source, "native_input", run_id=run.id, attempt_id=parent.id)
    return CodeRef(artifact_id=artifact.id, sha256=artifact.sha256, language="orca")


def test_python_reservation_and_settlement_do_not_charge_orca(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch, budget_changes={"orca_starts": 0})
    attempt = store.reserve_attempt(run, step)
    assert attempt.execution_kind == "python"
    assert attempt.geometry_artifact_id is None
    assert run.usage.orca_starts_reserved == 0
    assert store.environment_lease()["attempt_id"] == attempt.id
    store.finish_attempt(run, attempt.id, state="completed", started=True,
                         elapsed_seconds=2, cpu_seconds=1, termination_confirmed=True)
    assert run.usage.orca_starts_actual == 0
    assert (run.usage.elapsed_seconds, run.usage.cpu_seconds) == (2, 1)
    assert store.environment_lease() is None


def test_native_reservation_uses_code_inputs_and_charges_orca(tmp_path, monkeypatch):
    store, run, step, artifact = make_task(tmp_path, monkeypatch, tool="orca.run")
    attempt = store.reserve_attempt(run, step)
    assert attempt.execution_kind == "orca"
    assert attempt.consumption["code"]["sha256"] == artifact.sha256
    assert run.usage.orca_starts_reserved == 1
    store.finish_attempt(run, attempt.id, state="completed", started=True,
                         termination_confirmed=True)
    assert run.usage.orca_starts_actual == 1


@pytest.mark.parametrize("permission,reason", [
    ({"code_execution": False}, "code execution"),
    ({"artifact_writes": False}, "artifact creation"),
    ({"max_cores": 1}, "resources exceed"),
])
def test_rejected_python_reservation_has_no_budget_or_lease(tmp_path, monkeypatch, permission, reason):
    store, run, step, _ = make_task(tmp_path, monkeypatch, permission_changes=permission)
    with pytest.raises(StoreError, match=reason):
        store.reserve_attempt(run, step)
    assert store.environment_lease() is None
    assert store.load_run(run.id).attempts == []


def test_internal_reservation_shares_parent_lease_and_persists_before_launch(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    lease = store.environment_lease()
    receipt = store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    persisted = store.load_run(run.id)
    assert persisted.internal_calls == [receipt]
    assert persisted.usage.orca_starts_reserved == 1
    assert persisted.usage.internal_work_units == 1
    assert persisted.usage.orca_starts_actual == 0
    assert store.environment_lease() == lease
    assert len(run.attempts) == 1
    intent = json.loads(store.path(f"{parent.directory}/internal/single/intent.json").read_text())
    assert intent["call"]["input_fingerprint"] == "a" * 64
    handle = {"pid": 123, "create_time": 1}
    store.mark_internal_call_started(run, parent, "single", handle)
    store.mark_internal_call_started(run, parent, "single", handle)
    assert run.usage.orca_starts_actual == 1
    store.finish_internal_call(run, parent, "single", state="completed",
                               resource_usage={"user_cpu_seconds": 0.5})
    assert run.usage.cpu_seconds == 0
    assert store.reserve_internal_call(run, parent, "single", "a" * 64, code=code) == receipt
    assert run.usage.orca_starts_reserved == 1
    store.finish_attempt(run, parent.id, state="completed", started=True,
                         cpu_seconds=1, termination_confirmed=True)
    assert run.usage.orca_starts_actual == 1
    assert run.usage.cpu_seconds == 1


@pytest.mark.parametrize("budgets,reason", [
    ({"orca_starts": 0}, "ORCA launch budget"),
    ({"internal_work_units": 0}, "work unit budget"),
])
def test_internal_budget_rejection_keeps_parent_and_no_new_receipt(tmp_path, monkeypatch, budgets, reason):
    store, run, step, _ = make_task(tmp_path, monkeypatch, budget_changes=budgets)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    with pytest.raises(BudgetExceeded, match=reason):
        store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    assert not run.internal_calls
    assert store.environment_lease()["attempt_id"] == parent.id
    assert run.usage.orca_starts_reserved == 0


def test_same_key_changed_payload_unknown_retry_and_second_call_rejected(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    with pytest.raises(StoreError, match="changed input"):
        store.reserve_internal_call(run, parent, "single", "b" * 64, code=code)
    with pytest.raises(StoreError, match="reconcile"):
        store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    with pytest.raises(StoreError, match="only one"):
        store.reserve_internal_call(run, parent, "second", "a" * 64, code=code)
    assert run.usage.orca_starts_reserved == 1


def test_receipt_history_and_process_identity_cannot_be_reset(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    store.mark_internal_call_started(run, parent, "single", {"pid": 123})
    changed = store.load_run(run.id)
    changed.internal_calls[0].started = False
    with pytest.raises(StoreError, match="process identity"):
        store.save_run(changed)
    changed = store.load_run(run.id)
    changed.internal_calls.clear()
    with pytest.raises(StoreError, match="history"):
        store.save_run(changed)
    changed = store.load_run(run.id)
    changed.attempts[0].execution_kind = "orca"
    with pytest.raises(StoreError, match="execution_kind"):
        store.save_run(changed)


def test_internal_input_must_be_authorized_or_owned_by_parent(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    source = tmp_path / "foreign.inp"
    source.write_text("! HF STO-3G\n")
    artifact = store.import_artifact(source, "native_input")
    with pytest.raises(StoreError, match="authorized files"):
        store.reserve_internal_call(run, parent, "single", "a" * 64,
            code=CodeRef(artifact_id=artifact.id, sha256=artifact.sha256, language="orca"))
    assert run.usage.orca_starts_reserved == 0


def test_internal_cannot_launch_without_owning_parent_lease(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.release_environment(run.id, parent.id, termination_confirmed=True)
    with pytest.raises(EnvironmentBusy, match="does not own"):
        store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)


def test_python_repeat_consumes_cumulative_code_retry_budget(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch, budget_changes={"code_retries": 1})
    first = store.reserve_attempt(run, step)
    store.finish_attempt(run, first.id, state="completed", started=True, termination_confirmed=True)
    second = store.reserve_attempt(run, step)
    assert second.number == 2
    assert run.usage.code_retries == 1
    assert run.usage.orca_starts_reserved == 0
    store.finish_attempt(run, second.id, state="completed", started=True, termination_confirmed=True)
    reopened = Store(store.root, environment_root=store.environment_root)
    loaded = reopened.load_run(run.id)
    with pytest.raises(BudgetExceeded, match="code retry"):
        reopened.reserve_attempt(loaded, step)
    assert loaded.usage.code_retries == 1


def test_reimporting_identical_code_cannot_disguise_failed_input(tmp_path, monkeypatch):
    store, _, step, artifact = make_task(tmp_path, monkeypatch)
    duplicate = store.import_artifact(store.artifact_path(artifact.id), "code")
    other = step.model_copy(deep=True)
    other.id = other.logical_id = "renamed"
    other.parameters.code.artifact_id = duplicate.id
    request = Request(goals=[Goal(id="report", port="artifacts",
        minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    plan = Plan(request_id=request.id, steps=[step, other],
                goal_map={"report": OutputBinding(step_id=other.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(code_execution=True,
        artifact_writes=True, artifact_ids=[artifact.id, duplicate.id], allowed_tools=["python.execute"]),
        BudgetLimits(code_retries=2))
    first = store.reserve_attempt(run, step)
    store.finish_attempt(run, first.id, state="failed", started=True, termination_confirmed=True)
    with pytest.raises(StoreError, match="unchanged failed input"):
        store.reserve_attempt(run, other)
    assert run.usage.code_retries == 0


def test_internal_execution_requires_separate_scientific_permission(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch,
                                   permission_changes={"scientific_execution": False})
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    with pytest.raises(StoreError, match="not authorized"):
        store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    assert not run.internal_calls


def test_internal_record_remains_roundtrippable_with_historical_defaults():
    old = InternalCall(request_key="old", input_fingerprint="a" * 64,
                       inputs=InputManifest(), state="completed")
    assert old.parent_attempt_id is None
    assert InternalCall.model_validate_json(old.model_dump_json()) == old
