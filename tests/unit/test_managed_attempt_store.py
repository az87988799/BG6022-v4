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
    fingerprint,
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


def container_call(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    gated = {"backend": "linux-container", "container_id": "c" * 64,
             "launch_id": "d" * 32, "launch_phase": "gated_launcher",
             "workload_identity": {"pid": 42, "start_ticks": 12345}}
    return store, run, parent, gated


def test_gated_internal_orca_failure_keeps_reservation_without_actual_start(tmp_path, monkeypatch):
    store, run, parent, gated = container_call(tmp_path, monkeypatch)
    lease = store.environment_lease()
    store.mark_internal_call_started(run, parent, "single", gated)
    persisted = store.load_run(run.id)
    assert persisted.internal_calls[0].execution_handle == gated
    assert persisted.internal_calls[0].started is False
    assert persisted.usage.orca_starts_actual == 0
    assert persisted.usage.orca_starts_reserved == 1
    assert persisted.usage.internal_work_units == 1
    store.finish_internal_call(run, parent, "single", state="failed")
    persisted = store.load_run(run.id)
    assert persisted.internal_calls[0].state == "failed"
    assert persisted.internal_calls[0].started is False
    assert persisted.usage.orca_starts_actual == 0
    assert persisted.usage.orca_starts_reserved == 1
    assert store.environment_lease() == lease
    assert not store.path(f"{parent.directory}/internal/single/actual-start.json").exists()


def test_confirmed_internal_container_start_is_durable_and_charged_once(tmp_path, monkeypatch):
    store, run, parent, gated = container_call(tmp_path, monkeypatch)
    store.mark_internal_call_started(run, parent, "single", gated)
    executed = {**gated, "launch_phase": "executed", "actual_start_evidence": {
        **gated["workload_identity"], "executable": "/opt/orca/orca"}}
    store.mark_internal_call_started(run, parent, "single", executed)
    persisted = store.load_run(run.id)
    assert persisted.internal_calls[0].started is True
    assert persisted.internal_calls[0].execution_handle == executed
    assert persisted.usage.orca_starts_actual == 1
    receipt = store.path(f"{parent.directory}/internal/single/actual-start.json")
    assert json.loads(receipt.read_text()) == executed
    store.mark_internal_call_started(run, parent, "single", executed)
    store.finish_internal_call(run, parent, "single", state="completed",
                               resource_usage={"user_cpu_seconds": 2, "aggregate_charged_to_parent": True})
    assert store.load_run(run.id).usage.orca_starts_actual == 1
    assert run.usage.cpu_seconds == 0


@pytest.mark.parametrize("field,value", [
    ("container_id", "e" * 64),
    ("launch_id", "f" * 32),
    ("workload_identity.pid", 43),
    ("workload_identity.start_ticks", 12346),
    ("actual_start_evidence.executable", "/usr/bin/python3.11"),
    ("actual_start_evidence.pid", 43),
    ("actual_start_evidence.start_ticks", 12346),
])
def test_internal_actual_start_rejects_changed_or_unproven_identity(tmp_path, monkeypatch, field, value):
    store, run, parent, gated = container_call(tmp_path, monkeypatch)
    store.mark_internal_call_started(run, parent, "single", gated)
    executed = {**gated, "workload_identity": dict(gated["workload_identity"]),
                "launch_phase": "executed", "actual_start_evidence": {
                    **gated["workload_identity"], "executable": "/opt/orca/orca"}}
    if "." in field:
        section, name = field.split(".")
        executed[section][name] = value
    else:
        executed[field] = value
    with pytest.raises(StoreError, match="identity cannot change|matching executable evidence"):
        store.mark_internal_call_started(run, parent, "single", executed)
    persisted = store.load_run(run.id)
    assert persisted.internal_calls[0].execution_handle == gated
    assert persisted.internal_calls[0].started is False
    assert persisted.usage.orca_starts_actual == 0
    assert persisted.usage.orca_starts_reserved == 1
    assert not store.path(f"{parent.directory}/internal/single/actual-start.json").exists()


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


def test_same_key_changed_payload_and_unsettled_calls_cannot_be_restarted(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    with pytest.raises(StoreError, match="changed input"):
        store.reserve_internal_call(run, parent, "single", "b" * 64, code=code)
    with pytest.raises(StoreError, match="reconcile"):
        store.reserve_internal_call(run, parent, "single", "a" * 64, code=code)
    with pytest.raises(StoreError, match="reconciliation"):
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


def multi_task(tmp_path, monkeypatch, *, work_units=4):
    return make_task(tmp_path, monkeypatch,
        permission_changes={"allow_additional_science": True},
        budget_changes={"internal_work_units": work_units, "code_retries": 1})


def test_multiple_calls_distinguish_candidates_additions_and_failure_retries(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch, work_units=3)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    goal = {"quantity": "electronic_energy", "condition": "gas_phase"}
    resources = {"cores": 4, "memory_mb": 1024}
    first = store.reserve_internal_call(run, parent, "candidate", "a" * 64,
        code=code, goal=goal, resources=resources)
    store.finish_internal_call(run, parent, first.request_key, state="failed")
    retry = store.reserve_internal_call(run, parent, "corrected", "b" * 64, code=code,
        purpose="failure_retry", retry_of="candidate", goal=goal, resources=resources)
    store.mark_internal_call_started(run, parent, retry.request_key, {"pid": 12, "create_time": 1})
    store.finish_internal_call(run, parent, retry.request_key, state="completed")
    addition = store.reserve_internal_call(run, parent, "extra", "c" * 64,
        code=code, purpose="plan_addition", goal=goal, resources=resources)
    store.finish_internal_call(run, parent, addition.request_key, state="failed")
    loaded = store.load_run(run.id)
    assert [item.purpose for item in loaded.internal_calls] == [
        "new_candidate", "failure_retry", "plan_addition"]
    assert loaded.internal_calls[1].retry_of == "candidate"
    assert loaded.internal_calls[1].goal == goal
    assert loaded.internal_calls[1].resources == resources
    assert loaded.usage.internal_calls_by_purpose == {
        "new_candidate": 1, "failure_retry": 1, "plan_addition": 1}
    assert loaded.usage.orca_starts_reserved == loaded.usage.internal_work_units == 3
    assert loaded.usage.orca_starts_actual == 1
    with pytest.raises(BudgetExceeded, match="work unit"):
        store.reserve_internal_call(run, parent, "exhausted", "d" * 64, code=code)


@pytest.mark.parametrize("state", ["reserved", "running", "unknown", "completed"])
def test_failure_retry_requires_confirmed_failure_and_new_key(tmp_path, monkeypatch, state):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "original", "a" * 64, code=code)
    if state in {"running", "completed"}:
        store.mark_internal_call_started(run, parent, "original", {"pid": 12})
    if state in {"completed", "unknown"}:
        store.finish_internal_call(run, parent, "original", state=state)
    with pytest.raises(StoreError, match="reconciliation|settled failed"):
        store.reserve_internal_call(run, parent, "retry", "b" * 64, code=code,
                                    purpose="failure_retry", retry_of="original")
    with pytest.raises(ValueError, match="new request key"):
        store.reserve_internal_call(run, parent, "original", "a" * 64, code=code,
                                    purpose="failure_retry", retry_of="original")
    assert store.load_run(run.id).usage.orca_starts_reserved == 1


def test_completed_call_replays_across_attempts_without_budget_reset(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch, work_units=1)
    first = store.reserve_attempt(run, step)
    code = native_input(store, run, first, tmp_path)
    call = store.reserve_internal_call(run, first, "stable", "a" * 64, code=code)
    store.mark_internal_call_started(run, first, "stable", {"pid": 12})
    source = tmp_path / "job.out"
    source.write_text("retained raw output")
    output = store.import_artifact(source, "raw_output", run_id=run.id, attempt_id=first.id)
    response = {"request_key": "stable", "input_fingerprint": "a" * 64,
                "files": {"job.out": {"artifact_id": output.id, "sha256": output.sha256}}}
    store.finish_internal_call(run, first, "stable", state="completed",
                               artifact_ids=[output.id], response=response)
    store.finish_attempt(run, first.id, state="completed", started=True, termination_confirmed=True)
    reopened = Store(store.root, environment_root=store.environment_root)
    loaded = reopened.load_run(run.id)
    second = reopened.reserve_attempt(loaded, step)
    second_input = native_input(reopened, loaded, second, tmp_path)
    before = loaded.usage.model_dump()
    replay = reopened.reserve_internal_call(loaded, second, "stable", "a" * 64, code=second_input)
    assert replay == call and replay.response == response
    assert replay.parent_attempt_id == first.id
    assert loaded.usage.model_dump() == before
    assert loaded.usage.code_retries == 1
    with pytest.raises(BudgetExceeded, match="work unit"):
        reopened.reserve_internal_call(loaded, second, "new", "b" * 64, code=second_input)
    changed = reopened.load_run(run.id)
    changed.usage.internal_calls_by_purpose["new_candidate"] = 0
    with pytest.raises(StoreError, match="counters cannot be reset"):
        reopened.save_run(changed)


def test_response_pins_artifact_hash_and_replay_rechecks_retained_evidence(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "stable", "a" * 64, code=code)
    store.mark_internal_call_started(run, parent, "stable", {"pid": 12})
    source = tmp_path / "job.out"
    source.write_text("original raw output")
    artifact = store.import_artifact(source, "raw_output", run_id=run.id, attempt_id=parent.id)
    with pytest.raises(StoreError, match="result Artifact hash"):
        store.finish_internal_call(run, parent, "stable", state="completed",
            artifact_ids=[artifact.id], response={"files": {
                "job.out": {"artifact_id": artifact.id, "sha256": "f" * 64}}})
    assert store.load_run(run.id).internal_calls[0].state == "running"
    response = {"files": {"job.out": {"artifact_id": artifact.id, "sha256": artifact.sha256}}}
    store.finish_internal_call(run, parent, "stable", state="completed",
                               artifact_ids=[artifact.id], response=response)
    store.artifact_path(artifact.id).write_text("tampered evidence")
    with pytest.raises(StoreError, match="hash|changed|size"):
        store.reserve_internal_call(run, parent, "stable", "a" * 64, code=code)


def test_checkpoint_survives_restart_and_preserves_exact_source_state(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "candidate", "a" * 64, code=code)
    store.mark_internal_call_started(run, parent, "candidate", {"pid": 12})
    store.finish_internal_call(run, parent, "candidate", state="completed")
    record = store.save_task_checkpoint(run, parent, "choose_next", {"energies": [-1.0]},
        requires_decision=True, reason="a new result needs a plan decision")
    reopened = Store(store.root, environment_root=store.environment_root)
    loaded = reopened.load_run(run.id)
    assert loaded.task_checkpoints == [record]
    assert record.completed_request_keys == ["candidate"]
    assert record.source_attempt_id == parent.id
    assert record.code_sha256 == step.parameters.code.sha256
    assert reopened.save_task_checkpoint(loaded, loaded.attempts[0], "choose_next",
        {"energies": [-1.0]}, requires_decision=True,
        reason="a new result needs a plan decision") == record
    with pytest.raises(StoreError, match="different immutable"):
        reopened.save_task_checkpoint(loaded, loaded.attempts[0], "choose_next", {"energies": [0.0]})
    changed = reopened.load_run(run.id)
    changed.task_checkpoints[0].source_attempt_id = "another_attempt"
    item = changed.task_checkpoints[0]
    item.sha256 = fingerprint(item.model_dump(mode="json", exclude={"sha256", "created_at"}))
    with pytest.raises(StoreError, match="checkpoint history"):
        reopened.save_run(changed)
    changed = reopened.load_run(run.id)
    changed.task_checkpoints.clear()
    with pytest.raises(StoreError, match="checkpoint history"):
        reopened.save_run(changed)


def test_checkpoint_rejects_unfinished_calls_invalid_json_and_missing_reason(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    with pytest.raises(StoreError, match="only completed"):
        store.save_task_checkpoint(run, parent, "unfinished", {}, completed_request_keys=["missing"])
    with pytest.raises(ValueError, match="reason"):
        store.save_task_checkpoint(run, parent, "decision", {}, requires_decision=True)
    with pytest.raises(ValueError):
        store.save_task_checkpoint(run, parent, "infinite", {"value": float("nan")})
    with pytest.raises(ValueError, match="64 KiB"):
        store.save_task_checkpoint(run, parent, "large", {"value": "x" * 65536})
    assert store.load_run(run.id).task_checkpoints == []


def test_unpublished_checkpoint_is_recovered_without_rewriting_it(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    with monkeypatch.context() as fault:
        fault.setattr(store, "save_run", lambda _: (_ for _ in ()).throw(OSError("crash before Run write")))
        with pytest.raises(OSError, match="crash"):
            store.save_task_checkpoint(run, parent, "saved", {"completed": 1})
    relative = f"{parent.directory}/checkpoints/saved.json"
    original = store.path(relative).read_bytes()
    loaded = store.load_run(run.id)
    restored = store.save_task_checkpoint(loaded, loaded.attempts[0], "saved", {"completed": 1})
    assert store.path(relative).read_bytes() == original
    assert store.load_run(run.id).task_checkpoints == [restored]


def test_two_runs_compete_for_one_environment_while_python_waits_for_orca(tmp_path, monkeypatch):
    store, first, step, _ = multi_task(tmp_path, monkeypatch)
    _, second, second_step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(first, step)
    code = native_input(store, first, parent, tmp_path)
    store.reserve_internal_call(first, parent, "owned", "a" * 64, code=code)
    with pytest.raises(EnvironmentBusy, match="quota is occupied"):
        store.reserve_attempt(second, second_step)
    assert store.load_run(second.id).attempts == []
    assert store.load_run(second.id).usage.orca_starts_reserved == 0
    store.finish_internal_call(first, parent, "owned", state="failed")
    store.finish_attempt(first, parent.id, state="completed", started=True, termination_confirmed=True)
    next_parent = store.reserve_attempt(second, second_step)
    assert store.environment_lease()["attempt_id"] == next_parent.id


def test_unknown_parent_can_only_settle_after_exact_durable_termination_proof(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    store.reserve_internal_call(run, parent, "interrupted", "a" * 64, code=code)
    store.mark_internal_call_started(run, parent, "interrupted", {"pid": 12})
    parent.state = "unknown"
    store.save_run(run)
    before = run.usage.model_dump()
    relative = f"{parent.directory}/reconciled.json"
    for proof in (None,
                  {"attempt_id": "another", "reconciliation": {"state": "terminated"}},
                  {"attempt_id": parent.id, "reconciliation": {"state": "unknown"}}):
        if proof is not None:
            store._write_json(relative, proof)
        with pytest.raises(StoreError, match="active managed Python"):
            store.finish_internal_call(run, parent, "interrupted", state="failed")
    store._write_json(relative, {"attempt_id": parent.id, "reconciliation": {"state": "terminated"}})
    for action in (
        lambda: store.reserve_internal_call(run, parent, "new", "b" * 64, code=code),
        lambda: store.mark_internal_call_started(run, parent, "interrupted", {"pid": 12}),
        lambda: store.save_task_checkpoint(run, parent, "resume", {}),
    ):
        with pytest.raises(StoreError, match="active managed Python"):
            action()
    store.finish_internal_call(run, parent, "interrupted", state="failed",
                               actual_conditions={"termination_basis": "reconciled parent"})
    persisted = store.load_run(run.id)
    assert persisted.internal_calls[0].state == "failed"
    assert persisted.usage.model_dump() == before
    assert persisted.attempts[0].state == "unknown"
    assert store.environment_lease()["attempt_id"] == parent.id
