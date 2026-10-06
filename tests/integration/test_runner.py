"""Lifecycle tests: synthetic parser evidence and controlled Python children only."""

import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from orca_agent import cli, runner
from orca_agent.backends import local
from orca_agent.config import Config
from orca_agent.models import (
    CalculationParameters,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store, atomic_write, sha256_file
from orca_agent.tools import calculation

HELPER = Path(__file__).resolve().parents[1] / "helpers" / "runner_worker.py"
SPEC = importlib.util.spec_from_file_location("runner_test_fixture", HELPER)
FIXTURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FIXTURE)
WATER = "3\nWater in angstrom\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n"


def make_run(tmp_path, steps=1):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source = tmp_path / "water.xyz"
    source.write_text(WATER)
    artifact = store.import_artifact(source, "initial_geometry")
    request = Request(geometry_artifact_id=artifact.id, goals=[Goal(id="e", port="energy")])
    plan_steps = [Step(
        id=f"sp{number}", logical_id=f"energy{number}", tool="orca.sp",
        geometry=InputRef(artifact_id=artifact.id),
        parameters=CalculationParameters(cores=1, scf_maxiter=99 + number, timeout_seconds=15),
        depends_on=[f"sp{number - 1}"] if number else [],
    ) for number in range(steps)]
    plan = Plan(request_id=request.id, steps=plan_steps,
                goal_map={"e": OutputBinding(step_id=plan_steps[-1].id, port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[artifact.id]))
    executable = Path(sys.executable).resolve()
    config = Config(data_root=store.root, orca_path=executable)
    atomic_write(store.path(f"runs/{run.id}/environment.json"), json.dumps({
        "orca": {"path": str(executable), "sha256": sha256_file(executable)},
        "synthetic_fixture": True,
    }).encode(), immutable=True)
    return store, config, run


@pytest.fixture
def synthetic(monkeypatch):
    launches = []
    monkeypatch.setattr(calculation, "prepare_input", FIXTURE.prepare_fixture)
    monkeypatch.setattr(calculation, "read_outputs", FIXTURE.read_fixture)

    def backend(executable, args, workdir, **kwargs):
        launches.append(str(workdir))
        hook = kwargs.get("fault")
        if hook:
            hook("before_create_process")
        handle = {"pid": 424242, "create_time": 1.0, "job_name": kwargs["job_name"],
                  "launch_id": "synthetic_fixture"}
        if hook:
            hook("after_process_created")
        kwargs["on_started"](handle)
        if hook:
            hook("after_handle_saved")
            hook("after_resumed")
        (workdir / "stdout.out").write_text("Synthetic test evidence only")
        (workdir / "stderr.txt").write_text("")
        cancelled = kwargs["cancel_requested"]()
        if not cancelled:
            (workdir / "fixture-success.txt").write_text("Synthetic fixture only")
        return {"state": "cancelled" if cancelled else "completed", "exit_code": 1 if cancelled else 0,
                "handle": handle, "resource_usage": {"wall_seconds": 0.25,
                "user_cpu_seconds": 0.1, "active_processes": 0}, "reason": "synthetic_fixture"}

    monkeypatch.setattr(local, "run_managed", backend)
    monkeypatch.setattr(local, "reconcile", lambda handle: {
        "state": "terminated" if handle.get("pid") else "unknown",
        "reason": "synthetic_fixture",
    })
    return launches


def test_runner_pause_finishes_current_attempt_then_resume_preserves_budget(tmp_path, synthetic):
    store, config, run = make_run(tmp_path, steps=2)

    def pause(point):
        if point == "after_resumed":
            store.signal(run.id, "pause")

    paused = runner.execute(store, config, run.id, fault=pause)
    assert paused.state == "paused"
    assert len(paused.attempts) == len(synthetic) == 1
    assert paused.attempts[0].state == "completed"
    assert paused.usage.orca_starts_actual == paused.usage.orca_starts_reserved == 1
    initial_result = paused.attempts[0].result_id
    assert store.environment_lease() is None
    resumed = runner.execute(store, config, run.id, resume=True)
    assert resumed.state == "completed"
    assert len(resumed.attempts) == len(synthetic) == 2
    assert resumed.usage.orca_starts_actual == resumed.usage.orca_starts_reserved == 2
    assert resumed.attempts[0].result_id == initial_result
    assert resumed.deadline == run.deadline


def test_runner_cancel_does_not_schedule_following_step(tmp_path, synthetic):
    store, config, run = make_run(tmp_path, steps=2)

    def cancel(point):
        if point == "after_resumed":
            store.signal(run.id, "cancel")

    result = runner.execute(store, config, run.id, fault=cancel)
    assert result.state == "cancelled"
    assert len(result.attempts) == len(synthetic) == 1
    assert result.attempts[0].state == "cancelled"
    assert not store.load_result(run.id, result.result_ids[0]).qualified_outputs
    assert store.environment_lease() is None
    assert runner.execute(store, config, run.id, resume=True).state == "cancelled"
    assert len(synthetic) == 1


def test_runner_crash_after_intent_keeps_unknown_quota_without_restart(tmp_path, synthetic):
    store, config, run = make_run(tmp_path)

    def crash(point):
        if point == "after_intent_saved":
            raise RuntimeError("injected coordinator failure")

    result = runner.execute(store, config, run.id, fault=crash)
    assert result.state == "unknown"
    assert len(synthetic) == 0
    lease = store.environment_lease()
    assert lease is not None
    with pytest.raises(ValueError, match="explicit resume"):
        runner.execute(store, config, run.id)
    for _ in range(2):
        recovered = runner.execute(store, config, run.id, resume=True)
        assert recovered.state == "unknown"
        assert recovered.usage.orca_starts_reserved == 1
        assert store.environment_lease() == lease
    assert len(synthetic) == 0


@pytest.mark.parametrize("point", ["after_execution_saved", "after_result_saved"])
def test_runner_orphan_completion_is_recollected_without_execution(tmp_path, synthetic, point):
    store, config, run = make_run(tmp_path)

    def crash(actual):
        if actual == point:
            raise RuntimeError("injected receipt/reference failure")

    interrupted = runner.execute(store, config, run.id, fault=crash)
    assert interrupted.state == "unknown"
    assert len(synthetic) == 1
    directory = store.path(interrupted.attempts[0].directory)
    before = {p.name: sha256_file(p) for p in directory.iterdir() if p.is_file()}
    orphans = list(store.path(f"runs/{run.id}/results").glob("*.json"))
    if point == "after_result_saved":
        assert len(orphans) == 1
        assert any("orphan result" in issue for issue in store.integrity_issues(run.id))
    recovered = runner.execute(store, config, run.id, resume=True)
    assert recovered.state == "completed"
    assert len(recovered.attempts) == len(synthetic) == 1
    assert recovered.usage.orca_starts_actual == 1
    assert recovered.usage.elapsed_seconds == 0.25
    assert store.environment_lease() is None
    if orphans:
        assert recovered.result_ids == [orphans[0].stem]
    assert {p.name: sha256_file(p) for p in directory.iterdir() if p.is_file()} == before
    assert runner.execute(store, config, run.id, resume=True).usage.elapsed_seconds == 0.25


@pytest.mark.parametrize("mutation", ["permission", "request", "plan"])
def test_runner_rejects_changed_condition_after_preparation(tmp_path, synthetic, mutation):
    store, config, run = make_run(tmp_path)

    def mutate(point):
        if point != "after_input_prepared":
            return
        if mutation == "permission":
            path = store.path(f"runs/{run.id}/permission.json")
            data = json.loads(path.read_text())
            data["scientific_execution"] = False
        elif mutation == "request":
            path = store.path(f"runs/{run.id}/request-revisions/1.json")
            data = json.loads(path.read_text())
            data["version"] = 2
        else:
            path = store.path(f"runs/{run.id}/plan-revisions/1.json")
            data = json.loads(path.read_text())
            data["steps"][0]["parameters"]["scf_maxiter"] = 11
        path.write_text(json.dumps(data))

    try:
        result = runner.execute(store, config, run.id, fault=mutate)
        assert result.state in ("unknown", "failed")
    except ValueError:
        pass  # Request identity corruption may also refuse final goal collection.
    assert not synthetic


@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_runner_control_before_process_creation_is_known_not_started(tmp_path, synthetic, action):
    store, config, run = make_run(tmp_path)

    def signal(point):
        if point == "after_input_prepared":
            store.signal(run.id, action)

    result = runner.execute(store, config, run.id, fault=signal)
    assert not synthetic
    assert result.state == ("paused" if action == "pause" else "cancelled")
    assert result.usage.orca_starts_actual == 0
    assert store.environment_lease() is None
    if action == "pause":
        recovered = runner.execute(store, config, run.id, resume=True)
        assert recovered.state == "completed"
        assert len(synthetic) == 1
        assert recovered.usage.orca_starts_reserved >= 1


def test_runner_resume_does_not_clear_new_control_signal(tmp_path, synthetic, monkeypatch):
    store, config, run = make_run(tmp_path)
    store.signal(run.id, "pause")
    original = runner._recover

    def recover_then_new_cancel(*args):
        result = original(*args)
        store.signal(run.id, "cancel")
        return result

    monkeypatch.setattr(runner, "_recover", recover_then_new_cancel)
    result = runner.execute(store, config, run.id, resume=True)
    assert result.state == "cancelled"
    assert not synthetic
    assert store.read_signal(run.id) == "cancel"


def test_cli_status_is_read_only_for_unfinished_run(tmp_path, synthetic, monkeypatch, capsys):
    store, config, run = make_run(tmp_path)
    runner.execute(store, config, run.id, fault=lambda point: (
        (_ for _ in ()).throw(RuntimeError("crash")) if point == "after_intent_saved" else None))
    before = {p.relative_to(store.root).as_posix(): sha256_file(p)
              for p in store.root.rglob("*.json")}
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    assert cli.main(["status", run.id]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "unknown"
    assert {p.relative_to(store.root).as_posix(): sha256_file(p)
            for p in store.root.rglob("*.json")} == before
    assert not synthetic


def wait_until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    assert predicate(), "bounded fixture wait expired"


def task_records(store):
    return [json.loads(p.read_text()) for p in store.root.rglob("task-*.json")]


def assert_tasks_gone(store):
    def active(record):
        try:
            return abs(psutil.Process(record["pid"]).create_time() - record["create_time"]) < 1e-5
        except psutil.NoSuchProcess:
            return False
    wait_until(lambda: not any(active(record) for record in task_records(store)))


def start_worker(store, run, mode="normal", point="never"):
    return subprocess.Popen([sys.executable, str(HELPER), "run", str(store.root),
                             str(store.environment_root), run.id, mode, point],
                            creationflags=subprocess.CREATE_NO_WINDOW)


@pytest.mark.backend
@pytest.mark.skipif(os.name != "nt", reason="Windows backend")
@pytest.mark.parametrize("point", ["after_intent_saved", "after_process_created",
                                   "after_execution_saved", "after_result_saved"])
def test_runner_hard_crash_recovery_does_not_duplicate_calculation(tmp_path, monkeypatch, point):
    store, config, run = make_run(tmp_path)
    parent = start_worker(store, run, point=point)
    try:
        assert parent.wait(timeout=15) == 62
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
    assert_tasks_gone(store)
    if point == "after_process_created":
        pids = json.loads((store.root / "created-pids.json").read_text())
        assert pids
        wait_until(lambda: not any(psutil.pid_exists(pid) for pid in pids))
        assert not task_records(store)
    monkeypatch.setattr(calculation, "prepare_input", FIXTURE.prepare_fixture)
    monkeypatch.setattr(calculation, "read_outputs", FIXTURE.read_fixture)
    monkeypatch.setattr(local, "run_managed", lambda *a, **kw: pytest.fail("recovery must not launch"))
    recovered = runner.execute(store, config, run.id, resume=True)
    if point in ("after_intent_saved", "after_process_created"):
        assert recovered.state == "unknown"
        assert store.environment_lease() is not None
    else:
        assert recovered.state == "completed"
        assert store.environment_lease() is None
        assert recovered.usage.orca_starts_actual == 1
    assert len(recovered.attempts) == recovered.usage.orca_starts_reserved == 1


@pytest.mark.backend
@pytest.mark.skipif(os.name != "nt", reason="Windows backend")
@pytest.mark.parametrize("action", ["pause", "cancel"])
def test_runner_real_control_signals_stop_new_steps(tmp_path, action):
    store, config, run = make_run(tmp_path, steps=2)
    parent = start_worker(store, run, mode=action)
    try:
        wait_until(lambda: len(task_records(store)) >= 2)
        store.signal(run.id, action)
        assert parent.wait(timeout=10) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
    assert_tasks_gone(store)
    result = store.load_run(run.id)
    assert result.state == ("paused" if action == "pause" else "cancelled")
    assert len(result.attempts) == 1
    assert result.usage.orca_starts_actual == 1
    assert store.environment_lease() is None


@pytest.mark.backend
@pytest.mark.skipif(os.name != "nt", reason="Windows backend")
def test_runner_control_signal_is_serialized_with_actual_resume(tmp_path):
    store, config, run = make_run(tmp_path)
    parent = start_worker(store, run, mode="atomic")
    done = threading.Event()
    errors = []

    def cancel():
        try:
            # A distinct Store is essential: no reentrant coordinator lock shortcuts.
            other = Store(store.root, environment_root=store.environment_root)
            other.signal(run.id, "cancel")
            done.set()
        except Exception as exc:
            errors.append(exc)

    sender = None
    try:
        wait_until(lambda: (store.root / "control-held.txt").exists())
        sender = threading.Thread(target=cancel)
        sender.start()
        assert not done.wait(0.15), "control signal must wait while the process is suspended"
        assert not task_records(store), "application code must not execute before resume"
        (store.root / "release-control.txt").write_text("release")
        assert done.wait(5)
        assert parent.wait(timeout=10) == 0
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        if sender:
            sender.join(timeout=6)
    assert not errors
    assert_tasks_gone(store)
    result = store.load_run(run.id)
    assert result.state == "cancelled"
    assert result.usage.orca_starts_actual == 1
    assert store.environment_lease() is None
