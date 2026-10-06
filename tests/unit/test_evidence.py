"""Existing evidence may be observed without conversion, execution, or rewrites."""

import json
import subprocess
from pathlib import Path

import pytest

from orca_agent.models import (
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Step,
)
from orca_agent.store import Store, StoreError, sha256_file
from orca_agent.tools import evidence
from orca_agent.tools.registry import dispatch_evidence
from orca_agent.versions import CURRENT_CHECK_VERSION

WATER = "3\nSynthetic fixture\nO 0 0 0\nH 0 0.757 0.587\nH 0 -0.757 0.587\n"


@pytest.fixture
def records(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    geometry = tmp_path / "geometry.xyz"
    geometry.write_text(WATER)
    initial = store.import_artifact(geometry, "initial_geometry")
    request = Request(geometry_artifact_id=initial.id, goals=[Goal(id="e", port="energy", minimum_check_version=CURRENT_CHECK_VERSION)])
    step = Step(id="sp", logical_id="energy", tool="orca.sp",
                geometry=InputRef(artifact_id=initial.id))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"e": OutputBinding(step_id="sp", port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[initial.id]))
    attempt = store.reserve_attempt(run, step, initial.id)
    stdout = tmp_path / "stdout.out"
    stdout.write_text("first\nsecond\nthird\nfourth\n", encoding="utf-8")
    raw = store.import_artifact(stdout, "synthetic_test_evidence", run_id=run.id, attempt_id=attempt.id)
    json_path = tmp_path / "properties.json"
    json_path.write_text(json.dumps({"energy": {"value": -74.0, "unit": "Eh"},
                                    "array": list(range(100)), "plain": "sample"}))
    structured = store.import_artifact(json_path, "synthetic_test_evidence", run_id=run.id,
                                       attempt_id=attempt.id)
    # No scientific process ran. This record only supplies files for inspection tests.
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="failed", artifact_ids=[raw.id, structured.id],
                    diagnostics=[{"category": "synthetic_test_fixture"}])
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="not_started", result_id=result.id,
                         started=False, termination_confirmed=True)
    return store, run, initial, raw, structured


def snapshot(root):
    return {str(path.relative_to(root)): sha256_file(path)
            for path in root.rglob("*") if path.is_file()}


def test_reading_lists_lines_and_known_fields_has_no_side_effects(records, monkeypatch):
    store, run, initial, raw, structured = records
    calls = []

    def deny(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("evidence inspection attempted process execution")

    from orca_agent.backends import local
    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(local, "run_managed", deny)
    before = snapshot(store.root)
    listing = evidence.list_artifacts(store, run.id, offset=0, limit=2)
    assert listing["total"] == 3
    assert [artifact["id"] for artifact in listing["artifacts"]] == [initial.id, raw.id]
    last = evidence.list_artifacts(store, run.id, offset=2, limit=1)
    assert last["artifacts"][0]["id"] == structured.id
    assert evidence.list_artifacts(store, run.id, offset=3, limit=1)["artifacts"] == []
    window = evidence.inspect_artifact(store, raw.id, start_line=2, lines=2)
    assert window["lines"] == [{"line": 2, "text": "second"}, {"line": 3, "text": "third"}]
    assert window["sha256"] == raw.sha256
    assert "not a scientific qualification" in window["validation"]
    field = evidence.read_field(store, structured.id, "energy.value")
    assert field["value"] == -74.0
    assert field["sha256"] == structured.sha256
    assert "unverified observation" in field["validation"]
    assert evidence.read_field(store, structured.id, "energy.missing")["status"] == "missing"
    assert evidence.read_field(store, structured.id, "energy.value.child")["status"] == "missing"
    assert evidence.read_field(store, structured.id, "array")["value"] == list(range(32))
    assert snapshot(store.root) == before
    assert calls == []


@pytest.mark.parametrize("offset,limit", [(-1, 1), (10001, 1), (0, 0), (0, 201),
                                          (True, 1), (0, True), (1.5, 1), (0, 2.5)])
def test_list_page_bounds_are_strict(records, offset, limit):
    store, run, *_ = records
    with pytest.raises(ValueError):
        evidence.list_artifacts(store, run.id, offset=offset, limit=limit)


@pytest.mark.parametrize("start,lines", [(0, 1), (10001, 1), (1, 0), (1, 201),
                                          (True, 1), (1, False), (1.5, 1), (1, 2.5)])
def test_text_window_bounds_are_strict(records, start, lines):
    store, _, _, raw, _ = records
    with pytest.raises(ValueError):
        evidence.inspect_artifact(store, raw.id, start_line=start, lines=lines)


@pytest.mark.parametrize("field", ["", ".value", "energy.", "energy..value", "x" * 101,
                                   ".".join(["a"] * 13)])
def test_field_path_is_bounded_data_not_a_program(records, field):
    store, _, _, _, structured = records
    with pytest.raises(ValueError):
        evidence.read_field(store, structured.id, field)
    # A code-like token is merely an absent dictionary key, never evaluated.
    assert evidence.read_field(store, structured.id, "__import__('os')")["status"] == "missing"


def test_text_and_field_reads_reject_changed_hash(records):
    store, _, _, raw, structured = records
    store.artifact_path(raw.id).write_text("corruption")
    store.artifact_path(structured.id).write_text("{}")
    for operation, artifact, args in ((evidence.inspect_artifact, raw, ()),
                                      (evidence.read_field, structured, ("energy",))):
        with pytest.raises(StoreError, match="hash changed"):
            operation(store, artifact.id, *args)


def test_missing_source_is_fact_and_does_not_create_replacement(records):
    store, _, _, _, structured = records
    path = store.artifact_path(structured.id)
    path.unlink()
    before = snapshot(store.root)
    with pytest.raises(FileNotFoundError):
        evidence.read_field(store, structured.id, "energy.value")
    assert snapshot(store.root) == before
    assert not path.exists()


def test_changed_source_during_missing_field_read_is_detected(records, monkeypatch):
    store, _, _, _, structured = records
    target = store.artifact_path(structured.id)
    original_read = Path.read_text

    def read_and_modify(path, *args, **kwargs):
        text = original_read(path, *args, **kwargs)
        if path == target:
            path.write_text('{"new_value": 1}', encoding="utf-8")
        return text

    monkeypatch.setattr(Path, "read_text", read_and_modify)
    with pytest.raises(StoreError, match="hash changed"):
        evidence.read_field(store, structured.id, "missing")


def test_long_lines_large_fields_and_large_files_are_bounded(records, tmp_path):
    store, *_ = records
    path = tmp_path / "large.txt"
    path.write_text("a" * 32769 + "\n")
    long_line = store.import_artifact(path, "synthetic_test_evidence")
    with pytest.raises(ValueError, match="line exceeds"):
        evidence.inspect_artifact(store, long_line.id)
    path.write_bytes(b"a" * 20000 + b"\n" + b"b" * 20000 + b"\n")
    limited = store.import_artifact(path, "synthetic_test_evidence")
    response = evidence.inspect_artifact(store, limited.id, lines=2)
    assert len(response["lines"]) == 1
    assert response["returned_bytes"] == 20001
    path.write_text(json.dumps({"field": "x" * 32769}))
    large_field = store.import_artifact(path, "synthetic_test_evidence")
    with pytest.raises(ValueError, match="32 KiB"):
        evidence.read_field(store, large_field.id, "field")
    path.write_bytes(b" " * (1024 * 1024 + 1))
    large_json = store.import_artifact(path, "synthetic_test_evidence")
    with pytest.raises(ValueError, match="1 MiB"):
        evidence.read_field(store, large_json.id, "field")


def test_text_reading_has_a_time_bound(records, monkeypatch):
    store, _, _, raw, _ = records
    times = iter([1.0, 7.0])
    monkeypatch.setattr(evidence.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError, match="5 seconds"):
        evidence.inspect_artifact(store, raw.id)


def test_registered_dispatch_uses_existing_reader_without_a_new_plan_or_run(records):
    store, run, _, raw, structured = records
    before = snapshot(store.root)
    assert dispatch_evidence(store, "evidence.list", {"run_id": run.id, "limit": 1})["total"] == 3
    assert dispatch_evidence(store, "evidence.text", {
        "artifact_id": raw.id, "start_line": 2, "lines": 1,
    })["lines"] == [{"line": 2, "text": "second"}]
    assert dispatch_evidence(store, "evidence.field", {
        "artifact_id": structured.id, "field": "energy.value",
    })["value"] == -74
    assert snapshot(store.root) == before


@pytest.mark.parametrize("name,parameters", [
    ("evidence.list", {"run_id": "run1", "limit": True}),
    ("evidence.text", {"artifact_id": "a1", "lines": 100000}),
    ("evidence.text", {"artifact_id": "../outside"}),
    ("evidence.field", {"artifact_id": "a1", "field": "a..b"}),
    ("evidence.field", {"artifact_id": "a1", "field": "a", "code": "arbitrary"}),
])
def test_registered_dispatch_rejects_bad_parameters_before_file_access(name, parameters):
    with pytest.raises(ValueError):
        dispatch_evidence(None, name, parameters)


def test_cli_inspect_dispatches_through_registered_readonly_tool(records, monkeypatch, capsys):
    from orca_agent import cli
    from orca_agent.config import Config
    from orca_agent.tools import registry
    store, run, _, raw, structured = records
    monkeypatch.setattr(cli, "load_config", lambda _: Config(data_root=store.root))
    called = []
    original = registry.dispatch_evidence

    def record(*args, **kwargs):
        called.append(args[1])
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "dispatch_evidence", record)
    before = snapshot(store.root)
    assert cli.main(["inspect", "--run-id", run.id, "--limit", "1"]) == 0
    assert cli.main(["inspect", raw.id, "--lines", "1"]) == 0
    assert cli.main(["inspect", structured.id, "--field", "energy.unit"]) == 0
    capsys.readouterr()
    assert called == ["evidence.list", "evidence.text", "evidence.field"]
    assert snapshot(store.root) == before


@pytest.mark.parametrize("failure", ["schema", "value", "missing"])
def test_cli_pre_run_rejection_is_durable_and_contains_no_input_values(
    tmp_path, monkeypatch, capsys, failure
):
    from pydantic import ValidationError

    from orca_agent import cli, runner
    from orca_agent.config import Config
    from orca_agent.structured import TaskInput
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    monkeypatch.setattr(cli, "load_config", lambda _: Config(data_root=store.root))
    secret = "private-value-that-must-not-enter-diagnostics"

    def reject(*args):
        if failure == "schema":
            try:
                TaskInput.model_validate({"description": secret, "geometry": secret,
                    "steps": [{"name": "s", "tool": "orca.sp", "parameters": {"cores": secret}}],
                    "goals": [{"name": "e", "step": "s", "port": "energy"}], secret: secret})
            except ValidationError as error:
                raise error
        if failure == "value":
            raise ValueError(secret)
        raise FileNotFoundError(secret)

    monkeypatch.setattr(runner, "initialize", reject)
    assert cli.main(["run", str(tmp_path / "task.json")]) == 2
    output = capsys.readouterr().out
    response = json.loads(output)
    assert secret not in output
    assert response["rejection_recorded"] is True
    records = list(store.path("rejections").glob("*.json"))
    assert len(records) == 1
    raw = records[0].read_text(encoding="utf-8")
    assert secret not in raw
    diagnostic = json.loads(raw)
    assert set(diagnostic) == {"category", "errors", "time"}
    assert diagnostic["errors"] == response["errors"]
    assert all(set(error) == {"loc", "msg"} for error in diagnostic["errors"])
    assert not (store.root / "runs").exists()


def test_cli_preserves_primary_failure_when_rejection_audit_cannot_be_saved(tmp_path, monkeypatch, capsys):
    from orca_agent import cli, runner
    from orca_agent import store as store_module
    from orca_agent.config import Config
    monkeypatch.setattr(cli, "load_config", lambda _: Config(data_root=tmp_path / "data"))
    monkeypatch.setattr(runner, "initialize", lambda *args: (_ for _ in ()).throw(ValueError("private")))
    monkeypatch.setattr(store_module, "atomic_write", lambda *args, **kwargs: (
        (_ for _ in ()).throw(PermissionError("private audit path"))))
    assert cli.main(["run", str(tmp_path / "task.json")]) == 2
    output = capsys.readouterr().out
    response = json.loads(output)
    assert response["error"] == "ValueError"
    assert response["rejection_recorded"] is False
    assert response["audit_error"] == "PermissionError"
    assert "private" not in output
