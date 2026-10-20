"""Log namespace separation only; no process, container or isolation is simulated."""

import ctypes
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orca_agent.backends import local
from orca_agent.context import _result
from orca_agent.models import (
    BudgetLimits,
    ExecutionConfiguration,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    RunConfiguration,
    Step,
)
from orca_agent.store import Store
from orca_agent.tools import managed


@pytest.fixture
def log_boundary(tmp_path, monkeypatch):
    """Stop at CreateProcess's boundary after exercising real exclusive opens."""
    class CompletionPort(ctypes.Structure):
        _fields_ = [("key", ctypes.c_void_p), ("port", ctypes.c_void_p)]

    native = SimpleNamespace(
        HANDLE=ctypes.c_void_p,
        COMPLETION_PORT=CompletionPort,
        check=lambda value, _name: value,
        CreateJobObject=lambda *_: 1,
        CreateIoCompletionPort=lambda *_: 2,
        SetInformationJobObject=lambda *_: True,
        accounting=lambda *_: SimpleNamespace(ActiveProcesses=0),
        close=lambda *_: None,
    )
    monkeypatch.setattr(local, "_native", lambda: native)
    monkeypatch.setattr(local, "_configure_job", lambda *_: 1)
    monkeypatch.setattr(local, "_usage", lambda *_: {})
    monkeypatch.setattr(local.c, "set_last_error", lambda _: None, raising=False)
    monkeypatch.setattr(local.c, "get_last_error", lambda: 0, raising=False)
    calls = []

    def stop_before_process(_win, _executable, _args, workdir, _job, files, *_rest):
        calls.append({"workdir": workdir, "logs": [Path(item.name) for item in files[1:]]})
        files[1].write(b"runtime stdout marker\n")
        files[2].write(b"runtime stderr marker\n")
        raise RuntimeError("unit fixture stops before creating any process")

    monkeypatch.setattr(local, "_create_suspended", stop_before_process)
    executable = tmp_path / "never-executed.exe"
    executable.write_bytes(b"fixture; not executable")
    task = tmp_path / "task"
    task.mkdir()
    logs = tmp_path / "runtime-logs"
    logs.mkdir()
    return executable, task, logs, calls


def test_explicit_runtime_logs_preserve_same_named_task_inputs(log_boundary):
    executable, task, logs, calls = log_boundary
    inputs = {"stdout.out": b"prior native output\n", "stderr.txt": b"prior native stderr\n"}
    hashes = {}
    for name, content in inputs.items():
        (task / name).write_bytes(content)
        hashes[name] = hashlib.sha256(content).hexdigest()
    outcome = local.run_managed(executable, [], task, log_directory=logs)
    assert outcome["state"] == "failed" and outcome["handle"] is None
    assert "unit fixture stops before creating any process" in outcome["reason"]
    assert calls == [{"workdir": task, "logs": [logs / "stdout.out", logs / "stderr.txt"]}]
    for name, expected in hashes.items():
        assert hashlib.sha256((task / name).read_bytes()).hexdigest() == expected
    assert (logs / "stdout.out").read_bytes() == b"runtime stdout marker\n"
    assert (logs / "stderr.txt").read_bytes() == b"runtime stderr marker\n"


def test_default_local_log_directory_preserves_legacy_workdir(log_boundary):
    executable, task, logs, calls = log_boundary
    outcome = local.run_managed(executable, [], task)
    assert outcome["state"] == "failed" and outcome["handle"] is None
    assert calls == [{"workdir": task, "logs": [task / "stdout.out", task / "stderr.txt"]}]
    assert (task / "stdout.out").read_bytes() == b"runtime stdout marker\n"
    assert (task / "stderr.txt").read_bytes() == b"runtime stderr marker\n"
    assert not list(logs.iterdir())


@pytest.mark.parametrize("existing", ["stdout.out", "stderr.txt"])
def test_runtime_log_exclusive_create_refuses_previous_evidence(log_boundary, existing):
    executable, task, logs, calls = log_boundary
    original = b"earlier immutable execution log\n"
    (logs / existing).write_bytes(original)
    outcome = local.run_managed(executable, [], task, log_directory=logs)
    assert outcome["state"] == "failed" and outcome["handle"] is None
    assert "FileExistsError" in outcome["reason"]
    assert not calls
    assert (logs / existing).read_bytes() == original


def test_isolated_python_collection_distinguishes_inputs_from_failure_logs(tmp_path, monkeypatch):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _name: None)
    configuration = RunConfiguration(execution=ExecutionConfiguration(python_runtime_layout="isolated-1"))
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment", configuration=configuration)
    program = tmp_path / "source.py"
    program.write_text("print('fixture; never executed')", encoding="utf-8")
    code = store.import_artifact(program, "code")
    request = Request(goals=[Goal(id="raw", port="artifacts",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])
    step = Step(id="analysis", logical_id="analysis", tool="python.execute", parameters={
        "code": {"artifact_id": code.id, "sha256": code.sha256, "language": "python"}, "sdk_orca": False})
    plan = Plan(request_id=request.id, steps=[step], goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    permission = PermissionSnapshot(code_execution=True, artifact_writes=True, scientific_execution=False,
        artifact_ids=[code.id], allowed_tools=["python.execute"])
    run = store.create_run(request, plan, permission, BudgetLimits(orca_starts=0, internal_work_units=0))
    attempt = store.reserve_attempt(run, step)
    root = store.path(attempt.directory)
    contents = {"task/stdout.out": b"unrelated native input stdout\n",
        "task/stderr.txt": b"unrelated native input stderr\n",
        "runtime-logs/stdout.out": b"current runtime stdout\n",
        "runtime-logs/stderr.txt": b"current runtime failure\n"}
    for name, raw in contents.items():
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
    result = managed.collect_result(store, run, step, attempt, {
        "state": "failed", "handle": None, "resource_usage": {}, "exit_code": 1,
        "reason": "unit collection fixture; no process ran"})
    assert result.operation_status == "failed"
    assert not result.qualified_outputs
    assert result.observations["evidence_collection_complete"] is True
    registered = result.source["files"]
    assert len({registered[name]["artifact_id"] for name in contents}) == 4
    for name, raw in contents.items():
        assert registered[name]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert store.artifact_path(registered[name]["artifact_id"]).read_bytes() == raw
        assert (root / name).read_bytes() == raw
    diagnostic, = [item for item in result.diagnostics if item["category"] == "execution_failed"]
    assert {item["filename"] for item in diagnostic["evidence"]} == {
        "runtime-logs/stdout.out", "runtime-logs/stderr.txt"}
    assert {item["text"] for item in diagnostic["evidence"]} == {
        "current runtime stdout\n", "current runtime failure\n"}
    assert result.source["python_runtime_layout"] == "isolated-1"
    projected = _result(result, 4096)
    assert {item["task_path"] for item in projected["diagnostic_artifacts"]} == {
        "runtime-logs/stdout.out", "runtime-logs/stderr.txt"}
    assert {item["file_key"] for item in projected["file_index"]["files"][:2]} == {
        "runtime-logs/stdout.out", "runtime-logs/stderr.txt"}
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0


def test_execution_log_layout_is_opt_in_immutable_and_legacy_shape_compatible():
    expected = {"backend": "windows-lpac", "image_id": None, "manifest_sha256": None,
        "docker_context": None, "docker_path": None}
    assert ExecutionConfiguration().model_dump(mode="json") == expected
    assert ExecutionConfiguration(python_runtime_layout=None).model_dump(mode="json") == expected
    explicit = ExecutionConfiguration(python_runtime_layout="isolated-1")
    assert explicit.model_dump(mode="json") == {**expected, "python_runtime_layout": "isolated-1"}
    with pytest.raises(ValidationError, match="frozen_instance"):
        explicit.python_runtime_layout = None
    with pytest.raises(ValidationError, match="literal_error"):
        ExecutionConfiguration(python_runtime_layout="arbitrary-task-directory")
