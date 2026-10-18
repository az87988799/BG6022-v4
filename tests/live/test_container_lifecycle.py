"""Real Docker lifecycle evidence, explicitly gated; no model or ORCA starts."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_container_execution import python_task

from orca_agent.agent import _science
from orca_agent.backends import container
from orca_agent.runner import _recover
from orca_agent.store import EnvironmentBusy, Store

pytestmark = pytest.mark.container
WORKER = Path(__file__).resolve().parents[1] / "helpers" / "container_worker.py"
CRASH_EXIT = 73
ALLOWED_ENVIRONMENT = {
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "LC_CTYPE",
}
WAITING_TASK = """import json, os, time
from pathlib import Path
Path("before-stop.json").write_text(json.dumps({"pid": os.getpid(), "uid": os.getuid(),
    "cgroup": Path("/proc/self/cgroup").read_text(), "started": time.monotonic()}))
print("real lifecycle task started", flush=True)
while True:
    time.sleep(.05)
"""


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")


def _wait(predicate, seconds, detail):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("bounded wait expired: " + detail)


@contextmanager
def _worker(tmp_path, config, store, run, *, crash_at=None):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    evidence = tmp_path / "worker"
    evidence.mkdir()
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in ALLOWED_ENVIRONMENT
    }
    arguments = [
        sys.executable,
        "-I",
        str(WORKER),
        "--config",
        str(config_path),
        "--environment",
        str(store.environment_root),
        "--run-id",
        run.id,
        "--evidence",
        str(evidence),
    ]
    if crash_at:
        arguments += ["--crash-at", crash_at]
    with (evidence / "stdout.txt").open("wb") as out, (evidence / "stderr.txt").open("wb") as err:
        process = subprocess.Popen(
            arguments,
            stdout=out,
            stderr=err,
            stdin=subprocess.DEVNULL,
            env=environment,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            yield process, evidence
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def _inspect(config, handle):
    return json.loads(
        container._command(
            config.execution,
            ["container", "inspect", handle.get("container_id") or handle["container_name"]],
        )[1]
    )[0]


def _attempt_containers(config):
    return sorted(
        container._command(
            config.execution,
            ["ps", "--all", "--no-trunc", "--filter", "label=orca.attempt", "--format", "{{.ID}}"],
        )[1]
        .decode()
        .splitlines()
    )


def _assert_identity(actual, handle):
    assert actual["Image"] == handle["image_id"]
    assert actual["Name"].lstrip("/") == handle["container_name"]
    assert actual["Config"]["Labels"]["orca.nonce"] == handle["launch_id"]
    if handle.get("container_id"):
        assert actual["Id"] == handle["container_id"]
    assert [item["Name"] for item in actual["Mounts"] if item["Destination"] == "/work"] == [
        handle["volume_name"]
    ]


def _cleanup_owned(config, store, run_id, tmp_path):
    """Stop only this test's exact container on failure; preserve all evidence volumes."""
    run = store.load_run(run_id)
    if not run.attempts:
        return
    handle = run.attempts[0].execution_handle or {}
    if not handle.get("container_name"):
        return
    try:
        actual = _inspect(config, handle)
        _assert_identity(actual, handle)
        if actual["State"].get("Running"):
            container._command(config.execution, ["kill", actual["Id"]])
        _write(
            tmp_path / "cleanup.json",
            {"container": _inspect(config, handle), "volume_retained": handle["volume_name"]},
        )
    except Exception as exc:
        _write(tmp_path / "cleanup.json", {"unconfirmed_cleanup": str(exc), "handle": handle})


def _recover_once_and_again(config, store, run_id):
    run = store.load_run(run_id)
    count, usage = len(run.attempts), run.usage.model_dump(mode="json")
    ids = _attempt_containers(config)
    assert _recover(store, config, run, store.load_plan(run)) is True
    first = store.load_run(run_id)
    first_ids = list(first.result_ids)
    assert len(first.attempts) == count == 1
    assert first.usage.logical_attempts == usage["logical_attempts"]
    assert first.usage.code_retries == usage["code_retries"] == 0
    assert first.usage.orca_starts_actual == first.usage.orca_starts_reserved == 0
    assert store.environment_lease() is None
    assert _attempt_containers(config) == ids
    assert _recover(store, config, first, store.load_plan(first)) is True
    second = store.load_run(run_id)
    assert second.result_ids == first_ids
    assert len(second.attempts) == 1
    assert _attempt_containers(config) == ids
    result = store.load_result(second.id, second.attempts[0].result_id)
    assert result.operation_status == "failed"
    assert result.observations["evidence_collection_complete"], result.diagnostics
    assert not result.qualified_outputs
    assert not second.usage.resource_usage_complete
    return second, result


def test_coordinator_hard_exit_keeper_stops_task_and_recovery_never_replays(tmp_path):
    config, store, run, _ = python_task(
        tmp_path, WAITING_TASK, resources={"script_timeout_seconds": 120}
    )
    try:
        with _worker(tmp_path, config, store, run, crash_at="after_resumed") as (process, evidence):
            assert process.wait(timeout=60) == CRASH_EXIT
            crash = json.loads((evidence / "after_resumed.json").read_bytes())
            assert not (evidence / "worker-result.json").exists()
            durable = store.load_run(run.id)
            handle = durable.attempts[0].execution_handle
            assert handle["heartbeat_ttl_seconds"] == container.HEARTBEAT_SECONDS == 15
            assert handle["timeout_seconds"] > 60
            assert store.environment_lease()["run_id"] == run.id
            snapshots = []

            def stopped():
                actual = _inspect(config, handle)
                _assert_identity(actual, handle)
                snapshots.append(
                    {"elapsed_since_crash": time.time() - crash["time"], "state": actual["State"]}
                )
                return actual if not actual["State"]["Running"] else None

            actual = _wait(stopped, 35, "keeper's 15-second heartbeat TTL")
            _write(tmp_path / "keeper-observations.json", snapshots)
            assert actual["State"]["ExitCode"] == 124
            assert not actual["State"]["OOMKilled"]
            container._command(
                config.execution,
                [
                    "cp",
                    f"{actual['Id']}:/work/control/stopped.json",
                    str(tmp_path / "keeper-stopped.json"),
                ],
            )
            assert json.loads((tmp_path / "keeper-stopped.json").read_bytes()) == {
                "reason": "coordinator_heartbeat_lost"
            }
            proof = container.reconcile(handle)
            assert proof["state"] == "terminated" and proof["active_processes"] == 0
            assert proof["identity_match"] is True
            before = actual["State"]["StartedAt"]
            recovered, result = _recover_once_and_again(config, store, run.id)
            after = _inspect(config, handle)
            assert after["State"]["StartedAt"] == before and after["RestartCount"] == 0
            files = result.source["files"]
            facts = json.loads(
                store.artifact_path(files["task/before-stop.json"]["artifact_id"]).read_bytes()
            )
            assert facts["uid"] == 10001 and facts["pid"] > 1
            assert "real lifecycle task started" in store.artifact_path(
                files["task/stdout.out"]["artifact_id"]
            ).read_text("utf-8")
            volume = json.loads(
                container._command(config.execution, ["volume", "inspect", handle["volume_name"]])[
                    1
                ]
            )[0]
            assert volume["Labels"]["orca.nonce"] == handle["launch_id"]
            _write(
                tmp_path / "evidence.json",
                {
                    "reconciliation": proof,
                    "container": after,
                    "volume": volume,
                    "run": recovered.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                    "scientific_starts": 0,
                },
            )
    finally:
        _cleanup_owned(config, store, run.id, tmp_path)


def test_two_stores_share_environment_quota_without_second_container(tmp_path):
    first_path, second_path = tmp_path / "first", tmp_path / "second"
    config, original, run, _ = python_task(
        first_path, WAITING_TASK, resources={"script_timeout_seconds": 120}
    )
    second_config, other, second_run, second_step = python_task(
        second_path, "raise AssertionError('busy task must never execute')\n"
    )
    shared = tmp_path / "shared-environment"
    store = Store(original.root, environment_root=shared)
    second_store = Store(other.root, environment_root=shared)
    try:
        with _worker(first_path, config, store, run) as (process, evidence):

            def resumed():
                if process.poll() is not None:
                    raise AssertionError("coordinator exited before the live concurrency test")
                path = evidence / "after_resumed.json"
                return path if path.exists() else None

            _wait(resumed, 60, "first real task launch")
            handle = store.load_run(run.id).attempts[0].execution_handle
            before = _inspect(config, handle)
            _assert_identity(before, handle)
            assert before["State"]["Running"]
            ids = _attempt_containers(config)
            lease = store.environment_lease()
            with pytest.raises(EnvironmentBusy, match="environment quota"):
                _science(second_store, second_config, second_run, second_step, {}, None, None)
            rejected = second_store.load_run(second_run.id)
            assert rejected.attempts == [] and rejected.usage.logical_attempts == {}
            assert _attempt_containers(config) == ids
            assert second_store.environment_lease() == lease
            assert _inspect(config, handle)["State"]["Running"]
            store.signal(run.id, "cancel")
            assert process.wait(timeout=45) == 0
            worker_result = json.loads((evidence / "worker-result.json").read_bytes())
            assert worker_result["outcome"]["state"] == "cancelled"
            assert store.environment_lease() is None
            assert container.reconcile(handle)["state"] == "terminated"
            _write(
                tmp_path / "evidence.json",
                {
                    "first_handle": handle,
                    "lease_while_running": lease,
                    "container_ids_during_refusal": ids,
                    "second_run": rejected.model_dump(mode="json"),
                    "first_result": worker_result,
                    "scientific_starts": 0,
                },
            )
    finally:
        _cleanup_owned(config, store, run.id, tmp_path)


@pytest.mark.parametrize("crash_at", ["after_container_created", "after_boundary_saved"])
def test_lost_create_ack_reconciles_same_intent_without_start(tmp_path, crash_at):
    config, store, run, _ = python_task(
        tmp_path, WAITING_TASK, resources={"script_timeout_seconds": 120}
    )
    try:
        with _worker(tmp_path, config, store, run, crash_at=crash_at) as (process, evidence):
            assert process.wait(timeout=60) == CRASH_EXIT
            assert (evidence / (crash_at + ".json")).exists()
            durable = store.load_run(run.id)
            assert len(durable.attempts) == 1
            handle = durable.attempts[0].execution_handle
            assert ("container_id" in handle) == (crash_at == "after_boundary_saved")
            original = _inspect(config, handle)
            _assert_identity(original, handle)
            assert original["State"]["Status"] == "created"
            assert not original["State"]["Running"]
            assert original["State"]["StartedAt"].startswith("0001-01-01")
            assert store.environment_lease()["execution_handle"] == handle
            lease = store.environment_lease()
            usage = durable.usage.model_dump(mode="json")
            ids = _attempt_containers(config)
            proof = container.reconcile(handle)
            assert proof["state"] == "unknown" and proof["identity_match"] is True
            assert proof["reason"] == "container_start_or_stop_not_final"
            for _ in range(2):
                recovered = store.load_run(run.id)
                assert _recover(store, config, recovered, store.load_plan(recovered)) is False
                recovered = store.load_run(run.id)
                assert recovered.state == "unknown"
                assert len(recovered.attempts) == 1 and recovered.result_ids == []
                assert recovered.attempts[0].execution_handle == handle
                assert recovered.usage.logical_attempts == usage["logical_attempts"]
                assert recovered.usage.code_retries == usage["code_retries"] == 0
                assert (
                    recovered.usage.orca_starts_reserved == recovered.usage.orca_starts_actual == 0
                )
                assert store.environment_lease() == lease
                assert _attempt_containers(config) == ids
            current = _inspect(config, handle)
            assert current["Id"] == original["Id"]
            assert current["State"]["Status"] == "created"
            assert current["State"]["StartedAt"] == original["State"]["StartedAt"]
            assert current["RestartCount"] == 0
            _write(
                tmp_path / "evidence.json",
                {
                    "fault_point": crash_at,
                    "durable_intent": handle,
                    "original_container": original,
                    "reconciliation": proof,
                    "final_container": current,
                    "run": recovered.model_dump(mode="json"),
                    "retained_lease": lease,
                    "result_created": False,
                    "workload_started": False,
                    "scientific_starts": 0,
                },
            )
    finally:
        _cleanup_owned(config, store, run.id, tmp_path)
