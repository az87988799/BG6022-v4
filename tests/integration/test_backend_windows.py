"""Real Windows kernel tests with controlled fixtures, not ORCA evidence."""

import ctypes as c
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from orca_agent.backends.local import new_job_name, reconcile, run_managed

pytestmark = [pytest.mark.backend, pytest.mark.skipif(os.name != "nt", reason="Windows only")]
WORKER = Path(__file__).resolve().parents[1] / "helpers" / "backend_worker.py"


def launch(directory, mode, *extra, **kwargs):
    return run_managed(Path(sys.executable), [str(WORKER), mode, str(directory), *extra],
                       directory, **kwargs)


def records(directory):
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in directory.glob("node-*.json")]


def still_same_process(record):
    try:
        return abs(psutil.Process(record["pid"]).create_time() - record["create_time"]) < 1e-5
    except psutil.NoSuchProcess:
        return False


def wait_until(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    assert predicate(), "bounded wait expired"


def assert_tree_gone(directory, minimum=1):
    found = records(directory)
    assert len(found) >= minimum
    wait_until(lambda: not any(still_same_process(record) for record in found))


def test_backend_success_affinity_and_evidence(tmp_path):
    saved = []
    result = launch(tmp_path, "affinity", cores=min(2, len(psutil.Process().cpu_affinity())),
                    on_started=saved.append)
    assert result["state"] == "completed", result
    assert result["exit_code"] == 0
    assert result["handle"] == saved[0]
    assert result["handle"]["atomic_job_assignment"] is True
    output = json.loads((tmp_path / "stdout.out").read_text())
    assert len(output["affinity"]) == result["resource_usage"]["cores"]
    assert sum(1 << cpu for cpu in output["affinity"]) == result["resource_usage"]["cpu_affinity_mask"]
    assert (tmp_path / "stderr.txt").read_text().strip() == "stderr evidence"
    assert result["resource_usage"]["active_processes"] == 0
    assert result["resource_usage"]["job_commit_limit_bytes"] == 1024 * 1024 * 1024
    members = result["resource_usage"]["observed_processes"]
    assert members
    assert all(p["job_membership_confirmed"] for p in members)
    assert all(p["cpu_affinity_mask"].bit_count() <= 2 for p in members)
    assert reconcile(result["handle"])["state"] == "terminated"


def test_backend_unicode_environment_is_explicit_and_not_recorded(tmp_path):
    environment = dict(os.environ)
    environment.update(ORCA_BACKEND_FIXTURE="受控环境", OMP_NUM_THREADS="1")
    result = launch(tmp_path, "environment", environment=environment)
    assert result["state"] == "completed", result
    data = json.loads((tmp_path / "stdout.out").read_text())
    assert data == {"fixture_value": "受控环境", "omp_threads": "1"}
    assert "ORCA_BACKEND_FIXTURE" not in json.dumps(result)
    assert "受控环境" not in json.dumps(result, ensure_ascii=False)


def test_backend_dead_process_object_does_not_make_reconciliation_unknown(tmp_path):
    from orca_agent.backends import _windows as win

    held = []

    def retain(handle):
        held.append(win.check(win.OpenProcess(0x1000, False, handle["pid"]), "OpenProcess(test retain)"))

    try:
        result = launch(tmp_path, "affinity", on_started=retain)
        assert result["state"] == "completed", result
        reconciled = reconcile(result["handle"])
        assert reconciled["state"] == "terminated", reconciled
        assert reconciled["identity_match"] is True
    finally:
        for handle in held:
            win.close(handle)


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_backend_stop_cleans_entire_tree(tmp_path, stop):
    result = launch(tmp_path, "tree", "2", timeout_s=3 if stop == "timeout" else 15,
                    cancel_requested=(lambda: len(records(tmp_path)) >= 3)
                    if stop == "cancel" else None)
    assert result["state"] == ("cancelled" if stop == "cancel" else "timed_out"), result
    assert result["resource_usage"]["active_processes"] == 0
    assert result["resource_usage"]["total_processes"] >= 3
    observed = {record["pid"]: record for record in result["resource_usage"]["observed_processes"]}
    for record in records(tmp_path):
        assert len(record["affinity"]) == 1
        assert record["pid"] in observed
        assert observed[record["pid"]]["job_membership_confirmed"]
    assert_tree_gone(tmp_path, minimum=3)


def test_backend_driver_exit_does_not_release_live_descendants(tmp_path):
    result = launch(tmp_path, "driver_exits", timeout_s=2)
    assert result["state"] == "timed_out", result
    assert result["resource_usage"]["active_processes"] == 0
    assert_tree_gone(tmp_path, minimum=2)


def test_backend_memory_limit_is_enforced_by_kernel(tmp_path):
    result = launch(tmp_path, "allocate", total_memory_mb=96, timeout_s=10)
    assert result["state"] == "failed", result
    assert result["reason"] in ("job_memory_limit_exceeded", "nonzero_exit_code")
    if result["reason"] == "nonzero_exit_code":
        assert result["exit_code"] == 37
        assert "allocation rejected" in (tmp_path / "stdout.out").read_text()
    usage = result["resource_usage"]
    assert usage["job_commit_limit_bytes"] == 96 * 1024 * 1024
    # Preserve the kernel's counter verbatim: its reported peak can also rise
    # during a denied allocation. Do not equate it to successful commit usage.
    assert usage["peak_job_commit_bytes"] > 0
    assert_tree_gone(tmp_path)


def test_backend_kernel_rejects_allocation_larger_than_total_limit(tmp_path, monkeypatch):
    from orca_agent.backends import local

    # Leave the native allocation refusal visible to the child before cleanup.
    # Only notifications are suppressed; the OS job memory limit stays active.
    monkeypatch.setattr(local, "_memory_limit_message", lambda *_args: False)
    result = launch(tmp_path, "oversized_allocation", total_memory_mb=96, timeout_s=10)
    assert result["state"] == "completed", result
    data = json.loads((tmp_path / "stdout.out").read_text())
    assert data["allocated"] is False
    assert data["error"] in (8, 1455)  # NOT_ENOUGH_MEMORY / COMMITMENT_LIMIT
    assert data["requested_bytes"] > result["resource_usage"]["job_commit_limit_bytes"]
    assert data["private_bytes"] < result["resource_usage"]["job_commit_limit_bytes"]


def test_backend_memory_limit_applies_to_combined_process_tree(tmp_path, monkeypatch):
    from orca_agent.backends import local

    monkeypatch.setattr(local, "_memory_limit_message", lambda *_args: False)
    alone = tmp_path / "alone"
    together = tmp_path / "together"
    alone.mkdir()
    together.mkdir()
    control = launch(alone, "oversized_allocation", "64", total_memory_mb=96, timeout_s=10)
    assert control["state"] == "completed", control
    assert json.loads((alone / "stdout.out").read_text())["allocated"] is True
    result = launch(together, "aggregate_allocation", "24", total_memory_mb=96, timeout_s=10)
    assert result["state"] == "completed", result
    data = json.loads((together / "stdout.out").read_text())
    assert data["requested_bytes"] == 64 * 1024 * 1024
    assert data["allocated"] is False, "64 MB fits alone but not alongside the parent's 24 MB"
    assert data["private_bytes"] + data["requested_bytes"] < 96 * 1024 * 1024
    assert result["resource_usage"]["total_processes"] >= 2


def test_backend_breakaway_cannot_escape_our_job(tmp_path):
    from orca_agent.backends import _windows as win

    saved = []
    checked = []

    def cancel_when_child_is_present():
        found = records(tmp_path)
        if len(found) < 2:
            return False
        job = win.check(win.OpenJobObject(4, False, saved[0]["job_name"]), "OpenJobObject(test)")
        try:
            for record in found:
                process = win.check(win.OpenProcess(0x1000, False, record["pid"]), "OpenProcess(test)")
                try:
                    in_job = win.w.BOOL()
                    win.check(win.IsProcessInJob(process, job, c.byref(in_job)), "IsProcessInJob(test)")
                    assert in_job.value, "Breakaway child escaped the managed Job"
                    checked.append(record["pid"])
                finally:
                    win.close(process)
        finally:
            win.close(job)
        return True

    result = launch(tmp_path, "breakaway", timeout_s=8, on_started=saved.append,
                    cancel_requested=cancel_when_child_is_present)
    output = (tmp_path / "stdout.out").read_text()
    if "breakaway rejected" in output:
        assert result["state"] == "completed", result
    else:
        assert result["state"] == "cancelled", result
        assert len(checked) >= 2
    assert_tree_gone(tmp_path)


def coordinator(directory, mode):
    return subprocess.Popen([sys.executable, str(WORKER), "coordinator", str(directory), mode],
                            creationflags=subprocess.CREATE_NO_WINDOW)


def test_backend_coordinator_kill_closes_last_job_handle(tmp_path):
    parent = coordinator(tmp_path, "run")
    try:
        wait_until(lambda: len(records(tmp_path)) >= 3)
        handle = json.loads((tmp_path / "handle.json").read_text())
        assert reconcile(handle)["state"] == "running"
        parent.kill()
        parent.wait(timeout=5)
        assert_tree_gone(tmp_path, minimum=3)
        assert reconcile(handle)["state"] == "terminated"
        assert not (tmp_path / "result.json").exists()
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_backend_crash_after_creation_before_handle_save(tmp_path):
    parent = coordinator(tmp_path, "before_save")
    try:
        assert parent.wait(timeout=10) == 61
        pids = json.loads((tmp_path / "created.json").read_text())
        assert pids, "A real suspended process must have existed"
        wait_until(lambda: not any(psutil.pid_exists(pid) for pid in pids))
        assert not (tmp_path / "handle.json").exists()
        assert not records(tmp_path), "Suspended child never executed application code"
        assert reconcile({"job_name": new_job_name()})["state"] == "unknown"
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)


def test_backend_handle_save_failure_never_resumes_child(tmp_path):
    def fail(_handle):
        raise OSError("test durable write failure")

    result = launch(tmp_path, "affinity", on_started=fail)
    assert result["state"] == "failed"
    assert "durable write failure" in result["reason"]
    assert result["resource_usage"]["active_processes"] == 0
    assert not records(tmp_path)


def test_backend_pid_reuse_is_not_mistaken_for_our_process():
    current = psutil.Process()
    result = reconcile({"pid": current.pid, "create_time": current.create_time() - 60,
                        "job_name": new_job_name()})
    assert result["state"] == "unknown"
    assert result["identity_match"] is False
    assert result["reason"] == "pid_reused_or_identity_mismatch"
    assert current.is_running()


def test_backend_atomic_job_attribute_failure_fails_closed(tmp_path, monkeypatch):
    from orca_agent.backends import _windows as win

    called = []

    def fail_attribute(*_args):
        c.set_last_error(50)
        return False

    monkeypatch.setattr(win, "UpdateAttribute", fail_attribute)
    monkeypatch.setattr(win, "CreateProcess", lambda *_args: called.append(True))
    result = launch(tmp_path, "affinity")
    assert result["state"] == "failed"
    assert "atomic assignment required" in result["reason"]
    assert not called
    assert not records(tmp_path)


def test_backend_unconfirmed_cleanup_preserves_unknown(tmp_path, monkeypatch):
    from orca_agent.backends import local

    monkeypatch.setattr(local, "_terminate_and_confirm", lambda *_args: False)
    result = launch(tmp_path, "tree", "0", timeout_s=1)
    assert result["state"] == "unknown"
    assert result["reason"] == "process_tree_termination_unconfirmed"
    # Closing the job is still a final safety net, but is not proof for quota release.
    if records(tmp_path):
        assert_tree_gone(tmp_path)
    else:
        # The one-process fixture may hit the deadline while importing, before
        # publishing a node record. The actual created PID/time still proves
        # its exit; an empty record collection alone is never sufficient.
        identity = result["handle"]
        assert identity["pid"] > 0 and identity["create_time"] > 0
        wait_until(lambda: not still_same_process(identity))


def test_backend_rejects_resources_and_existing_evidence_before_creation(tmp_path):
    for kwargs in ({"cores": 5}, {"cores": 0}, {"total_memory_mb": 1025}, {"timeout_s": float("inf")}):
        result = launch(tmp_path, "affinity", **kwargs)
        assert result["state"] == "failed"
        assert result["handle"] is None
    (tmp_path / "stdout.out").write_text("immutable evidence")
    result = launch(tmp_path, "affinity")
    assert result["state"] == "failed"
    assert result["handle"] is None
    assert (tmp_path / "stdout.out").read_text() == "immutable evidence"
