"""Admission, raw evidence and recovery facts; no simulated isolation success."""

import json
import time

import pytest

from orca_agent import runner
from orca_agent.backends import local, runtime_files
from orca_agent.config import Config
from orca_agent.tools import managed
from orca_agent.tools.registry import get_tool
from tests.unit.test_managed_attempt_store import make_task, native_input


def outcome(state="completed"):
    return {"state": state, "handle": None, "resource_usage": {}, "exit_code": 0,
            "reason": "unit test execution fact only; no process was launched"}


@pytest.mark.parametrize("snapshot,field,value", [
    ("permission", "code_execution", False),
    ("budget", "orca_starts", 1000),
])
def test_managed_control_revalidates_immutable_admission_snapshots(
    tmp_path, monkeypatch, snapshot, field, value,
):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    path = store.path(f"runs/{run.id}/{snapshot}.json")
    changed = json.loads(path.read_bytes())
    changed[field] = value
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ValueError, match=f"immutable {snapshot} changed"):
        managed._control(store, run, step)
    assert not run.attempts
    assert run.usage.orca_starts_reserved == 0


def test_managed_control_blocks_pending_user_message(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    store.enqueue_message(run.id, "先不要执行，条件需要调整")
    with pytest.raises(ValueError):
        managed._control(store, run, step)
    assert not run.attempts
    assert store.environment_lease() is None


def test_shared_orca_launch_rejects_parallel_input_before_staging_without_serial_fallback(
    tmp_path, monkeypatch,
):
    store, run, step, _ = make_task(tmp_path, monkeypatch, tool="orca.run")
    attempt = store.reserve_attempt(run, step)
    root = store.path(attempt.directory)
    resources = store.run_configuration(run).resources
    assert resources.max_cores == 4
    reason = "MPI_Init fails inside network-disabled Windows LPAC; explicit serial input is required"
    for name in ("orca.sp", "orca.opt", "orca.run"):
        contract = get_tool(name).check_contract
        assert contract["parallel_execution"] == ["linux-container"]
        assert contract["parallel_execution_reason"] == reason
    prepared = {"input_path": str(root / "task" / "job.inp"),
                "resources": {"cores": resources.max_cores, "memory_mb": resources.total_memory_mb,
                              "timeout_seconds": resources.job_timeout_seconds}}
    monkeypatch.setattr(managed, "_prepare_native_boundary",
                        lambda *a, **k: pytest.fail("rejected parallel input staged a runtime"))
    monkeypatch.setattr(local, "run_managed",
                        lambda *a, **k: pytest.fail("rejected parallel input started a process"))
    execution = managed.launch_orca(
        store, run, step, Config(data_root=store.root), prepared, root / "task", root,
        on_started=lambda handle: pytest.fail("rejected parallel input created a process"),
    )
    assert execution == {
        "state": "failed",
        "reason": "prelaunch_rejected: parallel_execution_unavailable: " + reason,
        "not_started": True, "handle": None, "exit_code": None, "resource_usage": {},
    }
    assert prepared["resources"]["cores"] == resources.max_cores == 4
    persisted = store.load_run(run.id)
    assert persisted.usage.orca_starts_reserved == 1
    assert persisted.usage.orca_starts_actual == 0
    assert not persisted.attempts[0].started


def test_python_runtime_preparation_failure_is_recorded_as_known_not_started(tmp_path, monkeypatch):
    store, run, step, artifact = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    original = store.artifact_path(artifact.id).read_bytes()

    def unavailable(*args, **kwargs):
        raise ValueError("required private Python runtime is unavailable")

    monkeypatch.setattr(runtime_files, "stage_python_runtime", unavailable)
    monkeypatch.setattr(local, "run_managed", lambda *args, **kwargs: pytest.fail("process boundary reached"))
    result, execution = managed.execute(store, run, step, attempt, Config(data_root=store.root))
    assert execution["state"] == result.operation_status == "failed"
    assert execution["not_started"] and execution["handle"] is None
    assert "runtime is unavailable" in execution["reason"]
    assert store.path(f"{attempt.directory}/execution.json").is_file()
    assert store.path(f"{attempt.directory}/sealed/task.py").read_bytes() == original
    assert store.artifact_path(artifact.id).read_bytes() == original
    assert result.qualified_outputs == {}
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == 0
    store.save_result(result)
    runner._settle(store, run, attempt, result, execution)
    assert store.environment_lease() is None
    assert not run.attempts[0].started


def test_generic_collection_archives_existing_raw_files_without_json_or_qualification(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    task = store.path(attempt.directory) / "task"
    (task / "nested").mkdir(parents=True)
    (task / "stdout.out").write_text("raw execution output\n", encoding="utf-8")
    (task / "nested" / "stdout.out").write_text("different nested evidence\n", encoding="utf-8")
    result = managed.collect_result(store, run, step, attempt, outcome())
    assert result.operation_status == "completed"
    assert not result.qualified_outputs and not result.checks
    assert result.observations == {"execution_completed": True, "evidence_collection_complete": True,
                                   "scientific_validation": "unverified"}
    files = result.source["files"]
    assert files["task/stdout.out"]["artifact_id"] != files["task/nested/stdout.out"]["artifact_id"]
    assert store.artifact_path(files["task/stdout.out"]["artifact_id"]).read_text() == "raw execution output\n"
    assert store.artifact_path(files["task/nested/stdout.out"]["artifact_id"]).read_text() == "different nested evidence\n"
    assert not (task / "job.property.json").exists()
    store.save_result(result)


def test_collection_budget_failure_keeps_archived_prefix_and_marks_evidence_incomplete(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch, budget_changes={"artifact_count": 1})
    attempt = store.reserve_attempt(run, step)
    task = store.path(attempt.directory) / "task"
    task.mkdir()
    (task / "raw.out").write_text("retain this evidence", encoding="utf-8")
    result = managed.collect_result(store, run, step, attempt, outcome())
    assert result.operation_status == "completed"  # Execution and collection remain distinct.
    assert result.observations["evidence_collection_complete"] is False
    assert any(item["category"] == "collection_error" for item in result.diagnostics)
    retained = result.source["files"]["task/raw.out"]["artifact_id"]
    assert retained in result.artifact_ids
    assert store.artifact_path(retained).read_text() == "retain this evidence"
    assert not result.qualified_outputs


def test_unknown_execution_does_not_read_or_archive_mutable_task_files(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    task = store.path(attempt.directory) / "task"
    task.mkdir()
    raw = task / "stdout.out"
    raw.write_text("possibly still being written", encoding="utf-8")
    before = raw.read_bytes()
    monkeypatch.setattr(store, "import_artifact", lambda *args, **kwargs: pytest.fail("unknown tree archived"))
    result = managed.collect_result(store, run, step, attempt, outcome("unknown"))
    assert result.operation_status == "unknown"
    assert result.artifact_ids == [] and result.source["files"] == {}
    assert result.observations["evidence_collection_complete"] is False
    assert raw.read_bytes() == before


@pytest.mark.parametrize("payload", [
    {"version": True, "request_key": "call", "input": "! HF", "dependencies": []},
    {"version": 1, "request_key": "call", "input": "! HF", "dependencies": [], "command": "shell"},
    {"version": 1, "request_key": "call", "input": "! HF", "dependencies": [{}] * 33},
    {"version": 1, "request_key": "../outside", "input": "! HF", "dependencies": []},
])
def test_broker_rejects_protocol_or_identity_escalation_before_input_import(tmp_path, monkeypatch, payload):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    root = store.path(attempt.directory)
    broker = managed._Broker(store, run, step, attempt, Config(data_root=store.root), None,
                              root / "requests", root / "responses", time.monotonic() + 1,
                              native_boundary={})
    monkeypatch.setattr(store, "import_artifact", lambda *args, **kwargs: pytest.fail("invalid request imported"))
    with pytest.raises(ValueError):
        broker.execute(payload, {"job_name": "never_launched"})
    assert run.usage.orca_starts_reserved == 0
    assert not run.internal_calls


def test_unfinished_internal_recovery_preserves_budget_raw_files_and_lease(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    code = native_input(store, run, parent, tmp_path)
    receipt = store.reserve_internal_call(run, parent, "interrupted", "a" * 64, code=code)
    internal = store.path(f"{parent.directory}/internal/interrupted/task")
    internal.mkdir()
    raw = internal / "stdout.out"
    raw.write_text("incomplete original evidence", encoding="utf-8")
    run.state = "unknown"
    store.save_run(run)
    lease = store.environment_lease()
    budget = run.budget.model_copy(deep=True)
    monkeypatch.setattr(local, "run_managed", lambda *args, **kwargs: pytest.fail("recovery started a process"))
    monkeypatch.setattr(managed, "execute", lambda *args, **kwargs: pytest.fail("recovery replayed task code"))
    reconciled = []

    def parent_termination_fact(handle):
        # Synthetic read-only parent fact; it does not establish internal receipt completion.
        reconciled.append(handle)
        return {"state": "terminated", "reason": "unit test parent absence fact"}

    monkeypatch.setattr(local, "reconcile", parent_termination_fact)
    for _ in range(2):
        recovered = runner.execute(store, Config(data_root=store.root), run.id, resume=True)
        assert recovered.state == "unknown"
        assert any("internal_reconciliation_not_implemented" in str(item) for item in recovered.diagnostics)
        assert len(recovered.attempts) == 1 and recovered.attempts[0].id == parent.id
        assert recovered.internal_calls == [receipt]
        assert recovered.usage.orca_starts_reserved == recovered.usage.internal_work_units == 1
        assert recovered.usage.orca_starts_actual == 0
        assert recovered.budget == budget
        assert store.environment_lease() == lease
        assert raw.read_text() == "incomplete original evidence"
    assert reconciled
