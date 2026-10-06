"""Explicitly gated real ORCA/MPI lifecycle evidence; no scientific simulation.

Run sequentially with --live-orca. This uses the production global environment
lease; retained unknown work therefore blocks it rather than obtaining a new slot.
"""

import ctypes as c
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psutil
import pytest

from orca_agent import runner
from orca_agent.backends import local
from orca_agent.config import Config
from orca_agent.store import Store, atomic_write

pytestmark = [pytest.mark.live, pytest.mark.skipif(os.name != "nt", reason="Windows only")]
ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "tests" / "helpers" / "runner_worker.py"
REQUEST = ROOT / "tests" / "fixtures" / "phase_a" / "water_opt" / "request.json"


def _same_process(identity):
    try:
        process = psutil.Process(identity["pid"])
        return abs(process.create_time() - identity["create_time"]) < 1e-5
    except psutil.NoSuchProcess:
        return False


def _associated_processes(directory):
    """Discover compute processes by attempt cwd/arguments even outside the Job."""
    expected = str(directory.resolve()).casefold()
    observed = []
    for process in psutil.process_iter(["pid", "name", "create_time"]):
        name = (process.info["name"] or "").casefold()
        if not (name.startswith("orca") or name == "mpiexec.exe"):
            continue
        try:
            cwd = process.cwd()
            arguments_match = any(expected in argument.casefold() for argument in process.cmdline())
            if str(Path(cwd).resolve()).casefold() == expected or arguments_match:
                observed.append({"pid": process.pid, "create_time": process.create_time(),
                                 "executable_name": name, "cwd": cwd,
                                 "attempt_path_in_arguments": arguments_match})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return observed


def _confirm_membership(job_name, identity):
    from orca_agent.backends import _windows as win

    job = process = None
    try:
        job = win.OpenJobObject(4, False, job_name)
        if not job and c.get_last_error() == 2:
            return None
        win.check(job, "OpenJobObject(live observation)")
        process = win.OpenProcess(0x1000, False, identity["pid"])
        if not process:
            return None  # A short-lived rank exited between observations.
        _, created = win.creation_time(process)
        if abs(created - identity["create_time"]) > 1e-5:
            return None
        member = win.w.BOOL()
        win.check(win.IsProcessInJob(process, job, c.byref(member)), "IsProcessInJob(live observation)")
        return bool(member.value)
    finally:
        win.close(process)
        win.close(job)


def _wait_gone(identities, directory, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_same_process(identity) for identity in identities) and not _associated_processes(directory):
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("action", ["cancel", "kill_coordinator"])
def test_real_mpi_ranks_are_managed_and_cleaned(action):
    orca = Path(os.environ.get("ORCA_AGENT_ORCA", "E:/orca/orca.exe")).resolve()
    mpi = Path(os.environ.get("ORCA_AGENT_MPI", "C:/Program Files/Microsoft MPI/Bin/mpiexec.exe")).resolve()
    if not orca.is_file() or not mpi.is_file():
        pytest.skip("ORCA or MS-MPI missing: real parallel lifecycle unverified")
    base = Path(os.environ.get("ORCA_AGENT_LIVE_ROOT", str(ROOT / "data" / "live-lifecycle")))
    directory = (base / f"{action}-{uuid.uuid4().hex}").resolve()
    store = Store(directory)
    config = Config(data_root=directory, orca_path=orca, mpi_path=mpi)
    run = runner.initialize(store, config, REQUEST)
    assert all(step.parameters.cores == 4 for step in store.load_plan(run).steps)
    evidence = {"kind": "real_orca_mpi_lifecycle", "action": action, "run_id": run.id,
                "created_at": datetime.now(UTC).isoformat(), "observed_mpi_ranks": [],
                "associated_processes": [], "status": "unverified"}
    parent = None
    handle = None
    attempt_directory = None
    tracked = {}
    with (directory / "coordinator.stdout.txt").open("wb") as output, (
        directory / "coordinator.stderr.txt"
    ).open("wb") as errors:
        try:
            parent = subprocess.Popen([sys.executable, str(HELPER), "live", str(directory),
                                       run.id, str(orca), str(mpi)], stdout=output, stderr=errors,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + 30
            next_associated_sample = 0.0
            while time.monotonic() < deadline and parent.poll() is None:
                current = store.load_run(run.id)
                if current.attempts and current.attempts[0].execution_handle:
                    candidate = current.attempts[0].execution_handle
                    if candidate.get("pid"):
                        handle = candidate
                        attempt_directory = store.path(current.attempts[0].directory)
                        try:
                            sample = local.sample_job_processes(handle["job_name"])
                        except OSError:
                            time.sleep(0.01)
                            continue
                        for identity in sample["processes"]:
                            tracked[(identity["pid"], identity["create_time"])] = identity
                        if time.monotonic() >= next_associated_sample:
                            for identity in _associated_processes(attempt_directory):
                                member = _confirm_membership(handle["job_name"], identity)
                                identity["job_membership_confirmed"] = member
                                tracked[(identity["pid"], identity["create_time"])] = identity
                                assert member is not False, "Compute process escaped the Job"
                            next_associated_sample = time.monotonic() + 0.2
                        ranks = [item for item in sample["processes"]
                                 if Path(item["executable"]).name.lower().startswith("orca_")
                                 and "_mpi" in Path(item["executable"]).name.lower()]
                        if len(ranks) >= 4:
                            evidence["observed_mpi_ranks"] = ranks
                            evidence["sample_errors"] = sample["errors"]
                            break
                time.sleep(0.01)
            if len(evidence["observed_mpi_ranks"]) < 4:
                evidence["reason"] = "Four simultaneous real MPI ranks were not observed within 30 seconds"
                pytest.skip(evidence["reason"] + "; parallel lifecycle remains unverified")
            assert handle and attempt_directory
            assert all(item["job_membership_confirmed"] for item in evidence["observed_mpi_ranks"])
            assert all(0 < item["cpu_affinity_mask"].bit_count() <= 4
                       for item in evidence["observed_mpi_ranks"])
            associated = _associated_processes(attempt_directory)
            for identity in associated:
                identity["job_membership_confirmed"] = _confirm_membership(handle["job_name"], identity)
                tracked[(identity["pid"], identity["create_time"])] = identity
                assert identity["job_membership_confirmed"] is not False, "Compute process escaped the Job"
            evidence["associated_processes"] = associated
            if action == "cancel":
                store.signal(run.id, "cancel")
            else:
                coordinator = {"pid": handle["coordinator_pid"],
                               "create_time": handle["coordinator_create_time"]}
                assert coordinator["pid"] != os.getpid()
                assert _same_process(coordinator), "Coordinator PID identity changed; refusing to kill"
                psutil.Process(coordinator["pid"]).kill()
            parent.wait(timeout=15)
            assert _wait_gone(list(tracked.values()), attempt_directory), "Managed ORCA/MPI processes remain"
            reconciliation = local.reconcile(handle)
            evidence["reconciliation"] = reconciliation
            assert reconciliation["state"] == "terminated"
            if action == "kill_coordinator":
                result = runner.execute(store, config, run.id, resume=True)
                assert result.state == "failed"
            else:
                result = store.load_run(run.id)
                assert result.state == "cancelled"
            assert len(result.attempts) == result.usage.orca_starts_actual == 1
            assert store.environment_lease() is None
            evidence.update(status="passed", final_state=result.state,
                            remaining_associated_processes=_associated_processes(attempt_directory),
                            attempt_count=len(result.attempts), usage=result.usage.model_dump())
        finally:
            # Even unverified sampling cannot abandon a real calculation.
            try:
                if parent and parent.poll() is None:
                    store.signal(run.id, "cancel")
                    try:
                        parent.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        if handle and _same_process({"pid": handle["coordinator_pid"],
                                                     "create_time": handle["coordinator_create_time"]}):
                            psutil.Process(handle["coordinator_pid"]).kill()
                        parent.wait(timeout=10)
                if attempt_directory:
                    # This is test-harness emergency cleanup, never a fallback
                    # execution backend or evidence that containment passed.
                    remaining = _associated_processes(attempt_directory)
                    if remaining:
                        evidence["status"] = "failed"
                        evidence["emergency_cleanup"] = remaining
                        for identity in remaining:
                            if _same_process(identity):
                                psutil.Process(identity["pid"]).kill()
                        assert _wait_gone(remaining, attempt_directory), "Emergency cleanup unconfirmed"
                current = store.load_run(run.id)
                if any(item.state in ("intent", "running", "unknown") for item in current.attempts):
                    runner.execute(store, config, run.id, resume=True)
                if evidence.get("emergency_cleanup"):
                    pytest.fail("Real compute processes needed emergency cleanup; containment failed")
            finally:
                atomic_write(directory / "lifecycle-evidence.json",
                             (json.dumps(evidence, indent=2, ensure_ascii=False) + "\n").encode(),
                             immutable=True)
