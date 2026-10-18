"""One real ORCA start; coordinator loss never becomes a scientific success or replay."""

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from test_container_execution import configured
from test_container_lifecycle import (
    ALLOWED_ENVIRONMENT,
    _assert_identity,
    _attempt_containers,
    _cleanup_owned,
    _inspect,
    _wait,
    _write,
)

from orca_agent.backends import container
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.runner import _recover
from orca_agent.store import Store, sha256_file

pytestmark = [pytest.mark.container, pytest.mark.live]
WORKER = Path(__file__).resolve().parents[1] / "helpers" / "container_orca_crash_worker.py"
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "phase_a" / "water_sp"
CRASH_EXIT = 73


def _native_task(tmp_path):
    config = configured(tmp_path)
    assert config.model_profile == "disabled"
    config.resources = config.resources.model_copy(update={
        "max_cores": 4, "total_memory_mb": 1024, "maxcore_mb": 192,
        "environment_concurrency": 1, "job_timeout_seconds": 120,
    })
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    source = store.import_artifact(FIXTURE / "reference.inp", "native_input")
    geometry = store.import_artifact(FIXTURE / "geometry.xyz", "geometry")
    request = Request(goals=[Goal(id="raw", port="artifacts", quantity="interrupted ORCA evidence",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])
    step = Step(id="water", logical_id="water", tool="orca.run", parameters={
        "input": {"artifact_id": source.id, "sha256": source.sha256, "language": "orca"},
        "inputs": {"entries": [{"artifact_id": geometry.id, "sha256": geometry.sha256,
                                 "target_path": "geometry.xyz", "purpose": "initial geometry"}]},
    })
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(
        allowed_tools=["orca.run"], scientific_execution=True, code_execution=True,
        artifact_writes=True, artifact_ids=[source.id, geometry.id]),
        BudgetLimits(logical_steps=1, attempts_per_step=1, orca_starts=1, extra_orca_starts=0,
                     internal_work_units=0, code_retries=0, model_calls=0, run_seconds=300),
        configuration=config.run_configuration())
    return config, store, run


@contextmanager
def _worker(tmp_path, config, store, run):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    evidence = tmp_path / "worker"
    evidence.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in ALLOWED_ENVIRONMENT}
    arguments = [sys.executable, "-I", str(WORKER), "--config", str(config_path),
                 "--environment", str(store.environment_root), "--run-id", run.id,
                 "--evidence", str(evidence)]
    with (evidence / "stdout.txt").open("wb") as out, (evidence / "stderr.txt").open("wb") as err:
        process = subprocess.Popen(arguments, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
            env=environment, shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            yield process, evidence
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_actual_orca_start_survives_coordinator_crash_without_replay(tmp_path):
    fixture_hashes = {name: sha256_file(FIXTURE / name)
                      for name in ("reference.inp", "geometry.xyz")}
    config, store, run = _native_task(tmp_path)
    try:
        with _worker(tmp_path, config, store, run) as (process, evidence):
            assert process.wait(timeout=90) == CRASH_EXIT, (evidence / "stderr.txt").read_text()
            crash = json.loads((evidence / "after_actual_start_saved.json").read_bytes())
            assert not (evidence / "unexpected-worker-result.json").exists()
            durable = store.load_run(run.id)
            assert len(durable.attempts) == 1 and not durable.internal_calls
            attempt = durable.attempts[0]
            root = store.path(attempt.directory)
            gated_bytes = (root / "started.json").read_bytes()
            actual_bytes = (root / "actual-start.json").read_bytes()
            handle = json.loads(actual_bytes)
            assert handle == crash["actual"] == attempt.execution_handle
            assert handle["launch_phase"] == "executed"
            assert handle["actual_start_evidence"]["executable"] == "/opt/orca/orca"
            assert durable.usage.orca_starts_reserved == 1
            assert durable.usage.orca_starts_actual in (0, 1)
            assert store.environment_lease()["execution_handle"] == handle
            assert not attempt.finished_at and not attempt.result_id
            logical_attempts = dict(durable.usage.logical_attempts)
            snapshots = []

            def stopped():
                actual = _inspect(config, handle)
                _assert_identity(actual, handle)
                snapshots.append({"elapsed_since_crash": time.time() - crash["time"],
                                  "state": actual["State"]})
                return actual if not actual["State"]["Running"] else None

            actual = _wait(stopped, 35, "keeper cleanup after ORCA coordinator hard exit")
            _write(tmp_path / "keeper-observations.json", snapshots)
            assert actual["State"]["ExitCode"] == 124
            assert not actual["State"]["OOMKilled"] and actual["State"]["Pid"] == 0
            assert actual["HostConfig"]["Memory"] == actual["HostConfig"]["MemorySwap"] == 1024**3
            assert actual["HostConfig"]["NanoCpus"] == 4_000_000_000
            assert actual["HostConfig"]["CpusetCpus"] == "0-3"
            container._command(config.execution, ["cp", f"{actual['Id']}:/work/control/stopped.json",
                                                   str(tmp_path / "keeper-stopped.json")])
            assert json.loads((tmp_path / "keeper-stopped.json").read_bytes()) == {
                "reason": "coordinator_heartbeat_lost"}
            proof = container.reconcile(handle)
            assert proof["state"] == "terminated" and proof["active_processes"] == 0
            assert proof["identity_match"] is True
            # The coordinator's death cannot release the lease before reconciliation.
            assert store.environment_lease()["execution_handle"] == handle
            container_ids = _attempt_containers(config)
            assert _recover(store, config, durable, store.load_plan(durable)) is True
            recovered = store.load_run(run.id)
            assert len(recovered.attempts) == 1 and not recovered.internal_calls
            assert recovered.usage.orca_starts_actual == recovered.usage.orca_starts_reserved == 1
            assert recovered.usage.model_calls == recovered.usage.code_retries == 0
            assert recovered.usage.logical_attempts == logical_attempts
            assert not recovered.usage.resource_usage_complete
            assert recovered.attempts[0].started and recovered.attempts[0].state == "failed"
            assert store.environment_lease() is None
            result = store.load_result(run.id, recovered.attempts[0].result_id)
            assert result.operation_status == "failed" and not result.qualified_outputs
            assert result.observations["evidence_collection_complete"], result.diagnostics
            assert result.observations["scientific_validation"] == "unverified"
            assert not result.observations["execution_completed"]
            assert "coordinator_lost" in result.source["execution"]["reason"]
            files = result.source["files"]
            assert {"task/job.inp", "task/stdout.out", "task/stderr.txt",
                    "actual-start.json", "reconciled.json"} <= set(files)
            raw_hashes = {name: sha256_file(store.artifact_path(entry["artifact_id"]))
                          for name, entry in files.items()}
            result_ids = list(recovered.result_ids)
            usage = recovered.usage.model_dump(mode="json")
            assert _recover(store, config, recovered, store.load_plan(recovered)) is True
            repeated = store.load_run(run.id)
            assert repeated.usage.model_dump(mode="json") == usage
            assert repeated.result_ids == result_ids and len(repeated.attempts) == 1
            assert store.environment_lease() is None
            assert _attempt_containers(config) == container_ids
            after = _inspect(config, handle)
            assert after["State"]["StartedAt"] == actual["State"]["StartedAt"]
            assert after["RestartCount"] == 0 and not after["State"]["Running"]
            assert (root / "started.json").read_bytes() == gated_bytes
            assert (root / "actual-start.json").read_bytes() == actual_bytes
            assert {name: sha256_file(store.artifact_path(entry["artifact_id"]))
                    for name, entry in files.items()} == raw_hashes
            assert {name: sha256_file(FIXTURE / name) for name in fixture_hashes} == fixture_hashes
            _write(tmp_path / "evidence.json", {
                "fault_point": "after_actual_start_saved", "crash": crash,
                "reconciliation": proof, "container": after,
                "retained_volume": handle["volume_name"], "raw_artifact_hashes": raw_hashes,
                "run": repeated.model_dump(mode="json"), "result": result.model_dump(mode="json"),
                "scientific_starts": 1, "scientific_validation": "unverified",
            })
    finally:
        _cleanup_owned(config, store, run.id, tmp_path)
