"""Bounded real multi-call evidence: six ORCA starts, no model requests.

These developer-authored programs prove the execution contract, not autonomous
scientific planning. Run only with --live-container --live-orca and a frozen
ORCA_AGENT_CONTAINER_CONFIG. Every invocation needs a fresh --basetemp.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from orca_agent._atomic import atomic_write
from orca_agent.agent import _science
from orca_agent.config import Config
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
CRASH_EXIT = 73
ALLOWED_ENVIRONMENT = {
    "SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "LC_CTYPE",
}

NATIVE_INPUT = """! RHF STO-3G TightSCF NoRI NoAutoStart
%maxcore 192
%pal nprocs 4 end
%scf ConvForced 1 MaxIter 100 end
* xyz 0 1
O 0 0 0
H {distance} 0 .586260
H -{distance} 0 .586260
*
"""


def _program(*, yield_decision):
    """Construct fresh task code rather than importing a production solver."""
    return '''from pathlib import Path
import json, re, time
from orca_task_sdk import run_orca, checkpoint

template = ''' + repr(NATIVE_INPUT) + '''
receipts = []
energies = []
for key, distance in (("candidate_a", ".757160"), ("candidate_b", ".800000")):
    source = Path(key + ".inp")
    source.write_text(template.format(distance=distance))
    receipt = run_orca(source, request_key=key, purpose="new_candidate")
    assert receipt["operation_status"] == "completed", receipt
    text = Path(receipt["files"]["stdout.out"]).read_text()
    assert "ORCA TERMINATED NORMALLY" in text
    assert "4 parallel MPI-processes" in text
    energies.append(float(re.findall(r"FINAL SINGLE POINT ENERGY\\s+([-+0-9.]+)", text)[-1]))
    receipts.append(receipt)
    if key == "candidate_a":
        reused = run_orca(source, request_key=key, purpose="new_candidate")
        assert reused["input_fingerprint"] == receipt["input_fingerprint"]
        assert Path(reused["files"]["stdout.out"]).read_text() == text
Path("receipts.json").write_text(json.dumps(receipts, indent=2))
state = {"completed": [r["request_key"] for r in receipts], "energies_hartree": energies,
         "scientific_validation": "unverified"}
Path("computed.json").write_text(json.dumps(state, indent=2))
''' + ('''checkpoint(state, request_key="review", requires_decision=True,
           reason="Review the two observations before deciding whether to add calculations")
Path("after-checkpoint.txt").write_text("must not run")
''' if yield_decision else '''time.sleep(120)
''')


def _task(tmp_path, *, yield_decision=True):
    from test_container_execution import configured

    config = configured(tmp_path)
    assert config.model_profile == "disabled", "this evidence may not call a paid model"
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    source = tmp_path / "task.py"
    source.write_text(_program(yield_decision=yield_decision), encoding="utf-8")
    artifact = store.import_artifact(source, "task_code")
    request = Request(goals=[Goal(
        id="raw", port="artifacts", quantity="multi-calculation execution evidence",
        minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations",
    )])
    step = Step(id="dynamic", logical_id="dynamic", tool="python.execute", parameters={
        "code": {"artifact_id": artifact.id, "sha256": artifact.sha256, "language": "python"},
    })
    plan = Plan(request_id=request.id, steps=[step], goal_map={
        "raw": OutputBinding(step_id=step.id, port="artifacts"),
    })
    permission = PermissionSnapshot(
        allowed_tools=["python.execute", "orca.run"], code_execution=True,
        scientific_execution=True, allow_additional_science=True,
        artifact_writes=True, artifact_ids=[artifact.id],
    )
    run = store.create_run(
        request, plan, permission,
        BudgetLimits(orca_starts=2, extra_orca_starts=1, internal_work_units=2,
                     run_seconds=600),
        configuration=config.run_configuration(),
    )
    return config, store, run, step


def _raw_evidence(store, call):
    """Independently find the original ORCA stdout, with its archived hash."""
    candidates = []
    for artifact_id in call.artifact_ids:
        artifact = store.load_artifact(artifact_id)
        path = store.artifact_path(artifact_id)
        if artifact.source.get("task_path") == "stdout.out":
            text = path.read_text(encoding="utf-8")
            if "ORCA TERMINATED NORMALLY" in text:
                energy = re.findall(r"FINAL SINGLE POINT ENERGY\s+([-+0-9.]+)", text)
                assert energy
                assert sha256_file(path) == artifact.sha256
                candidates.append((artifact_id, artifact.sha256, float(energy[-1])))
    assert len(candidates) == 1, call
    return candidates[0]


def test_dynamic_two_real_calculations_reuse_and_decision_checkpoint(tmp_path):
    from test_container_execution import save_evidence

    config, store, run, step = _task(tmp_path)
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert result.observations["evidence_collection_complete"], evidence
    assert not result.qualified_outputs
    assert not result.diagnostics, evidence
    assert [call.request_key for call in run.internal_calls] == ["candidate_a", "candidate_b"]
    assert all(call.state == "completed" for call in run.internal_calls)
    assert len({call.input_fingerprint for call in run.internal_calls}) == 2
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 2
    assert run.usage.internal_work_units == 2
    assert run.usage.model_calls == run.usage.code_retries == 0
    assert len(run.task_checkpoints) == 1
    checkpoint = run.task_checkpoints[0]
    assert checkpoint.name == "review" and checkpoint.requires_decision
    assert checkpoint.source_attempt_id == run.attempts[0].id
    assert checkpoint.completed_request_keys == ["candidate_a", "candidate_b"]
    assert checkpoint.state["completed"] == checkpoint.completed_request_keys
    assert "task/after-checkpoint.txt" not in result.source["files"]
    assert "task/computed.json" in result.source["files"]
    original = [_raw_evidence(store, call) for call in run.internal_calls]
    assert abs(original[0][2] - original[1][2]) > 1e-5
    assert checkpoint.state["energies_hartree"] == [item[2] for item in original]
    assert {call.execution_handle["container_id"] for call in run.internal_calls} == {
        outcome["handle"]["container_id"],
    }
    assert store.environment_lease() is None


@contextmanager
def _worker(tmp_path, config, store, run, action):
    config_path = tmp_path / "worker-config.json"
    config_path.write_text(config.model_dump_json(indent=2), encoding="utf-8")
    evidence = tmp_path / "worker"
    evidence.mkdir()
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in ALLOWED_ENVIRONMENT}
    arguments = [sys.executable, "-I", str(Path(__file__).resolve()),
                 "--config", str(config_path), "--environment", str(store.environment_root),
                 "--run-id", run.id, "--evidence", str(evidence), "--action", action]
    with (evidence / "stdout.txt").open("wb") as out, (evidence / "stderr.txt").open("wb") as err:
        process = subprocess.Popen(
            arguments, stdout=out, stderr=err, stdin=subprocess.DEVNULL, env=environment,
            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            yield process, evidence
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.mark.parametrize("action", ["crash", "cancel"])
def test_second_real_orca_interruption_preserves_first_and_never_replays(tmp_path, action):
    from test_container_lifecycle import (
        _assert_identity,
        _attempt_containers,
        _cleanup_owned,
        _inspect,
        _wait,
    )

    config, store, run, _ = _task(tmp_path, yield_decision=False)
    try:
        with _worker(tmp_path, config, store, run, action) as (process, worker):
            expected_exit = CRASH_EXIT if action == "crash" else 0
            assert process.wait(timeout=180) == expected_exit, (worker / "stderr.txt").read_text()
            event = json.loads((worker / "interrupted.json").read_bytes())
            durable = store.load_run(run.id)
            assert len(durable.internal_calls) == 2, durable.diagnostics
            first, second = durable.internal_calls
            assert first.state == "completed" and second.started
            assert second.request_key == "candidate_b"
            original = _raw_evidence(store, first)
            assert original[:2] == (event["first_stdout_id"], event["first_stdout_sha256"])
            handle = durable.attempts[0].execution_handle
            actual = _wait(
                lambda: (value if not value["State"]["Running"] else None)
                if (value := _inspect(config, handle)) else None,
                35, "the shared container must stop after cancellation/coordinator heartbeat loss",
            )
            _assert_identity(actual, handle)
            assert actual["State"]["Pid"] == 0 and actual["RestartCount"] == 0
            containers = _attempt_containers(config)
            if action == "cancel":
                worker_result = json.loads((worker / "worker-result.json").read_bytes())
                assert worker_result["outcome"]["state"] == "cancelled", worker_result
                assert worker_result["outcome"]["resource_usage"]["active_processes"] == 0
            assert _recover(store, config, durable, store.load_plan(durable)) is True
            recovered = store.load_run(run.id)
            assert len(recovered.attempts) == 1
            assert recovered.usage.orca_starts_actual == recovered.usage.orca_starts_reserved == 2
            assert recovered.usage.internal_work_units == 2
            assert recovered.usage.code_retries == recovered.usage.model_calls == 0
            assert recovered.internal_calls[0] == first
            assert recovered.internal_calls[1].state in {"cancelled", "failed"}
            assert _raw_evidence(store, recovered.internal_calls[0]) == original
            assert store.environment_lease() is None
            usage = recovered.usage.model_dump(mode="json")
            result_ids = list(recovered.result_ids)
            assert _recover(store, config, recovered, store.load_plan(recovered)) is True
            repeated = store.load_run(run.id)
            assert repeated.usage.model_dump(mode="json") == usage
            assert repeated.result_ids == result_ids
            assert len(repeated.attempts) == 1
            assert _attempt_containers(config) == containers
            after = _inspect(config, handle)
            assert after["State"]["StartedAt"] == actual["State"]["StartedAt"]
            assert after["RestartCount"] == 0 and not after["State"]["Running"]
            assert _raw_evidence(store, repeated.internal_calls[0]) == original
            (tmp_path / "evidence.json").write_text(json.dumps({
                "action": action, "event": event, "container": after,
                "run": repeated.model_dump(mode="json"),
                "first_original_stdout": original,
                "scientific_validation": "unverified", "model_calls": 0,
            }, indent=2), encoding="utf-8")
    finally:
        _cleanup_owned(config, store, run.id, tmp_path)


def _worker_main():
    """A separate actual coordinator process, with no inherited API-key variables."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--action", choices=("crash", "cancel"), required=True)
    args = parser.parse_args()
    assert not (set(key.upper() for key in os.environ) - ALLOWED_ENVIRONMENT)
    config = Config.model_validate_json(args.config.read_bytes())
    assert config.model_profile == "disabled" and config.execution.backend == "linux-container"
    store = Store(config.data_root, environment_root=args.environment)
    run = store.load_run(args.run_id)
    plan = store.load_plan(run)
    assert len(plan.steps) == 1 and plan.steps[0].tool == "python.execute"
    assert run.budget.orca_starts == 2
    stop = threading.Event()

    def watch_second_start():
        while not stop.wait(.05):
            durable = store.load_run(run.id)
            if len(durable.internal_calls) < 2 or not durable.internal_calls[1].started:
                continue
            first = _raw_evidence(store, durable.internal_calls[0])
            event = {"action": args.action, "time": time.time(),
                     "first_stdout_id": first[0], "first_stdout_sha256": first[1],
                     "calls": [call.model_dump(mode="json") for call in durable.internal_calls]}
            atomic_write(args.evidence / "interrupted.json", json.dumps(event, indent=2).encode())
            if args.action == "crash":
                os._exit(CRASH_EXIT)
            store.signal(run.id, "cancel")
            return

    watcher = threading.Thread(target=watch_second_start, daemon=True)
    watcher.start()
    try:
        result, outcome = _science(store, config, run, plan.steps[0], {}, None, None)
        atomic_write(args.evidence / "worker-result.json", json.dumps({
            "run": run.model_dump(mode="json"), "result": result.model_dump(mode="json"),
            "outcome": outcome,
        }, indent=2).encode())
    finally:
        stop.set()
        watcher.join(timeout=5)


if __name__ == "__main__":
    _worker_main()
