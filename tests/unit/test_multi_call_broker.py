"""File-only broker boundaries; these tests launch no ORCA, Docker or model."""

import json
import time

import pytest

from orca_agent.config import Config
from orca_agent.models import fingerprint
from orca_agent.tools import managed
from tests.unit.test_managed_attempt_store import multi_task, native_input


def broker_case(tmp_path, monkeypatch):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    root = store.path(parent.directory)
    requests, responses = root / "requests", root / "responses"
    requests.mkdir()
    responses.mkdir()
    broker = managed._Broker(store, run, step, parent, Config(data_root=store.root),
        None, requests, responses, time.monotonic() + 10, native_boundary={"workdir": root / "native"})
    monkeypatch.setattr(broker, "_active", lambda _: None)
    monkeypatch.setattr(managed, "native_input_binding", lambda *a: None)
    launches = []

    def prepare(folder, parameters, artifact_path, resources, **kwargs):
        folder.mkdir(parents=True)
        source = artifact_path(parameters.input.artifact_id)
        (folder / "job.inp").write_bytes(source.read_bytes())
        return {"input_path": str(folder / "job.inp"), "admission_version": "unit-boundary-1",
                "resources": {"cores": 1, "memory_mb": 1024, "timeout_seconds": 3},
                "execution_impact": {"internal_work_units": 1}, "actual_conditions": {}}

    def launch(store, run, step, config, prepared, task, internal, *, on_started, **kwargs):
        launches.append(str(task))
        on_started({"pid": 100 + len(launches), "create_time": 1})
        (task / "stdout.out").write_text("raw evidence for " + task.name)
        return {"state": "completed", "resource_usage": {}, "handle": None, "exit_code": 0}

    monkeypatch.setattr(managed, "prepare_native_input", prepare)
    monkeypatch.setattr(managed, "launch_orca", launch)
    return store, run, step, parent, broker, launches


def publish(broker, sequence, value):
    (broker.requests / f"request-{sequence:06d}.json").write_text(json.dumps(value))
    broker.poll({"job_name": "never_launched"})
    return json.loads((broker.responses / f"response-{sequence:06d}.json").read_bytes())


def request(key, text="! unit input\n"):
    return {"version": 2, "kind": "orca", "request_key": key, "purpose": "new_candidate",
            "retry_of": None, "input": text, "dependencies": []}


def test_sequential_calls_and_replay_keep_independent_originals_and_charge_only_new_calls(tmp_path, monkeypatch):
    store, run, _, _, broker, launches = broker_case(tmp_path, monkeypatch)
    first_value, second_value = request("first"), request("second", "! different input\n")
    first = publish(broker, 1, first_value)
    second = publish(broker, 2, second_value)
    assert "error" not in first and "error" not in second
    assert len(launches) == 2 and launches[0] != launches[1]
    original = store.artifact_path(run.internal_calls[0].response["files"]["stdout.out"]["artifact_id"])
    original_bytes = original.read_bytes()
    before = run.usage.model_dump()
    monkeypatch.setattr(store, "import_artifact", lambda *a, **k: pytest.fail("replay imported evidence"))
    replay = publish(broker, 3, first_value)
    assert "error" not in replay and replay["input_fingerprint"] == fingerprint(first_value)
    assert replay["files"]["stdout.out"] != first["files"]["stdout.out"]
    assert original.read_bytes() == original_bytes
    assert len(launches) == 2 and run.usage.model_dump() == before
    changed = publish(broker, 4, request("first", "! changed input\n"))
    assert "changed input" in changed["error"]
    assert len(launches) == 2


def test_decision_checkpoint_blocks_later_science_even_if_task_bypasses_sdk_exit(tmp_path, monkeypatch):
    store, run, _, _, broker, launches = broker_case(tmp_path, monkeypatch)
    checkpoint = {"version": 2, "kind": "checkpoint", "request_key": "decide",
                  "state": {"candidate_count": 0}, "requires_decision": True,
                  "reason": "the next action depends on current observations"}
    saved = publish(broker, 1, checkpoint)
    assert saved["requires_decision"] and store.load_run(run.id).task_checkpoints
    rejected = publish(broker, 2, request("forbidden"))
    assert "coordinator decision" in rejected["error"]
    assert not launches and not run.internal_calls
    assert run.usage.orca_starts_reserved == 0


@pytest.mark.parametrize("layout", ["native/task/{key}", "native/{key}/task"])
def test_interrupted_call_collection_preserves_each_scoped_raw_directory(tmp_path, monkeypatch, layout):
    store, run, step, _ = multi_task(tmp_path, monkeypatch)
    parent = store.reserve_attempt(run, step)
    root = store.path(parent.directory)
    (root / "native" / "task").mkdir(parents=True)
    code = native_input(store, run, parent, tmp_path)
    for index, key in enumerate(("first", "second")):
        store.reserve_internal_call(run, parent, key, str(index) * 64, code=code)
        store.finish_internal_call(run, parent, key, state="failed")
        task = root / layout.format(key=key)
        task.mkdir(parents=True, exist_ok=True)
        (task / "stdout.out").write_text("raw output for " + key)
        prepared = root / "internal" / key / "prepared.json"
        prepared.write_text(json.dumps({"input_path": str(task / "job.inp")}))
    outcome = {"state": "failed", "reason": "parent terminated after broker interruption"}
    result = managed.collect_result(store, run, step, parent, outcome)
    for key in ("first", "second"):
        reference = result.source["files"][f"internal/{key}/task/stdout.out"]
        assert store.artifact_path(reference["artifact_id"]).read_text() == "raw output for " + key
    assert not any(item["category"] == "collection_error" for item in result.diagnostics)
