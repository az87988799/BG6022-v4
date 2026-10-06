"""Windows execution policy for the fixed, registered tool implementations.

This is an internal API, not a user/model-facing executable or script tool.
The caller owns authorization, immutable inputs, and environment-wide admission.
"""

from __future__ import annotations

import ctypes as c
import math
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

CLEANUP_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.05


def _now() -> str:
    return datetime.now(UTC).isoformat()


def new_job_name() -> str:
    """Generate before persisting launch intent; reuse only for that attempt."""
    return f"Local\\ORCAAgent-{uuid.uuid4().hex}"


def _native():
    if os.name != "nt":
        raise RuntimeError("Only the Windows Job Object backend is implemented")
    from . import _windows
    return _windows


def _configure_job(win, job, cores, total_memory_mb):
    if win.GetActiveProcessorGroupCount() != 1:
        raise RuntimeError("Multiple processor groups have not been validated; launch refused")
    process_mask, system_mask = win.SIZE_T(), win.SIZE_T()
    win.check(win.GetProcessAffinityMask(win.GetCurrentProcess(), c.byref(process_mask),
                                       c.byref(system_mask)), "GetProcessAffinityMask")
    available = [bit for bit in range(c.sizeof(win.SIZE_T) * 8)
                 if process_mask.value & (1 << bit)]
    if len(available) < cores:
        raise ValueError("Requested cores exceed the coordinator's available affinity")
    affinity = sum(1 << bit for bit in available[:cores])
    limit = win.EXTENDED_LIMIT()
    # AFFINITY | JOB_MEMORY | KILL_ON_JOB_CLOSE. No breakaway permissions.
    limit.BasicLimitInformation.LimitFlags = 0x10 | 0x200 | 0x2000
    limit.BasicLimitInformation.Affinity = affinity
    limit.JobMemoryLimit = total_memory_mb * 1024 * 1024
    win.check(win.SetInformationJobObject(job, 9, c.byref(limit), c.sizeof(limit)),
              "SetInformationJobObject(limits)")
    win.check(win.SetHandleInformation(job, 1, 0), "Make job handle non-inheritable")
    inherited = win.w.DWORD()
    win.check(win.GetHandleInformation(job, c.byref(inherited)), "GetHandleInformation(job)")
    if inherited.value & 1:
        raise RuntimeError("Job handle unexpectedly inheritable")
    actual = win.limits(job)
    if (actual.BasicLimitInformation.LimitFlags != limit.BasicLimitInformation.LimitFlags
            or actual.JobMemoryLimit != limit.JobMemoryLimit
            or actual.BasicLimitInformation.Affinity != affinity):
        raise RuntimeError("Kernel did not retain required job limits")
    return affinity


def _create_suspended(win, executable, args, workdir, job, output_files, environment=None):
    import msvcrt

    size = win.SIZE_T()
    win.InitializeAttributes(None, 2, 0, c.byref(size))
    buffer = c.create_string_buffer(size.value)
    win.check(win.InitializeAttributes(buffer, 2, 0, c.byref(size)),
              "InitializeProcThreadAttributeList")
    standard_handles = [msvcrt.get_osfhandle(file.fileno()) for file in output_files]
    try:
        for handle in standard_handles:
            os.set_handle_inheritable(handle, True)
        jobs = (win.HANDLE * 1)(job)
        handles = (win.HANDLE * 3)(*standard_handles)
        # JOB_LIST = ProcThreadAttributeValue(13, FALSE, TRUE, FALSE).
        # Failure is fatal: never create then assign, even while suspended.
        win.check(win.UpdateAttribute(buffer, 0, 0x2000D, jobs, c.sizeof(jobs), None, None),
                  "UpdateProcThreadAttribute(JOB_LIST); atomic assignment required")
        win.check(win.UpdateAttribute(buffer, 0, 0x20002, handles, c.sizeof(handles), None, None),
                  "UpdateProcThreadAttribute(HANDLE_LIST)")
        startup = win.STARTUPINFOEX()
        startup.StartupInfo.cb = c.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        (startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput,
         startup.StartupInfo.hStdError) = standard_handles
        startup.lpAttributeList = c.cast(buffer, win.LPVOID)
        process = win.PROCESS_INFORMATION()
        command_line = c.create_unicode_buffer(subprocess.list2cmdline([str(executable), *args]))
        environment_block = (c.create_unicode_buffer("\0".join(
            f"{key}={value}" for key, value in sorted(environment.items(), key=lambda item: item[0].upper())
        ) + "\0\0") if environment is not None else None)
        environment_pointer = c.cast(environment_block, win.LPVOID) if environment_block else None
        # EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW.
        win.check(win.CreateProcess(str(executable), command_line, None, None, True,
                                    0x80000 | 0x4 | 0x8000000 | (0x400 if environment is not None else 0),
                                    environment_pointer, str(workdir),
                                    c.byref(startup), c.byref(process)),
                  "CreateProcessW(atomically job-assigned, suspended)")
        return process
    finally:
        for handle in standard_handles:
            os.set_handle_inheritable(handle, False)
        win.DeleteAttributes(buffer)


def _usage(win, job, affinity, total_memory_mb, elapsed):
    usage, limit = win.accounting(job), win.limits(job)
    return {
        "wall_seconds": elapsed,
        "user_cpu_seconds": usage.TotalUserTime / 10_000_000,
        "kernel_cpu_seconds": usage.TotalKernelTime / 10_000_000,
        "total_processes": usage.TotalProcesses,
        "active_processes": usage.ActiveProcesses,
        "peak_job_commit_bytes": limit.PeakJobMemoryUsed,
        "job_commit_limit_bytes": total_memory_mb * 1024 * 1024,
        "memory_metric": "Windows Job Object committed virtual memory, not working set",
        "cpu_affinity_mask": affinity,
        "cores": affinity.bit_count(),
    }


def _sample_job(win, job):
    identities, errors = [], []
    for pid in win.process_ids(job):
        process = win.OpenProcess(0x1400, False, pid)
        if not process:
            errors.append({"pid": pid, "reason": "exited_or_query_unavailable",
                           "winerror": c.get_last_error()})
            continue
        try:
            ticks, created = win.creation_time(process)
            member = win.w.BOOL()
            win.check(win.IsProcessInJob(process, job, c.byref(member)), "IsProcessInJob(sample)")
            image_size = win.w.DWORD(32768)
            image = c.create_unicode_buffer(image_size.value)
            win.check(win.QueryFullProcessImageName(process, 0, image, c.byref(image_size)),
                      "QueryFullProcessImageName(sample)")
            affinity, system_affinity = win.SIZE_T(), win.SIZE_T()
            win.check(win.GetProcessAffinityMask(process, c.byref(affinity), c.byref(system_affinity)),
                      "GetProcessAffinityMask(sample)")
            identities.append({"pid": pid, "create_time": created,
                "create_time_100ns": str(ticks), "executable": image.value,
                "job_membership_confirmed": bool(member.value),
                "cpu_affinity_mask": affinity.value})
        except OSError as error:
            errors.append({"pid": pid, "reason": str(error)})
        finally:
            win.close(process)
    return {"processes": identities, "errors": errors}


def sample_job_processes(job_name: str) -> dict:
    """Bounded read-only observation of exact Job membership, not PPID guesses.

    Short-lived members can exit between enumeration and inspection; errors are
    reported explicitly. An empty sample is not evidence that MPI was contained.
    """
    win = _native()
    job = win.check(win.OpenJobObject(4, False, job_name), "OpenJobObject(sample)")
    try:
        return {**_sample_job(win, job), "active_processes": win.accounting(job).ActiveProcesses}
    finally:
        win.close(job)


def _terminate_and_confirm(win, job):
    win.check(win.TerminateJobObject(job, 1), "TerminateJobObject")
    deadline = time.monotonic() + CLEANUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if win.accounting(job).ActiveProcesses == 0:
            return True
        time.sleep(POLL_INTERVAL_S)
    return False


def _memory_limit_message(win, port):
    # Bound queue draining even when a program forks or exits rapidly.
    for _ in range(256):
        message, key, pointer = win.w.DWORD(), win.SIZE_T(), win.LPVOID()
        if not win.GetQueuedCompletionStatus(port, c.byref(message), c.byref(key),
                                             c.byref(pointer), 0):
            if c.get_last_error() == 258:  # WAIT_TIMEOUT
                return False
            win.check(False, "GetQueuedCompletionStatus")
        if message.value in (9, 10):  # PROCESS_MEMORY_LIMIT / JOB_MEMORY_LIMIT
            return True
    return False


def run_managed(
    executable: Path,
    args: list[str],
    workdir: Path,
    cores: int = 1,
    total_memory_mb: int = 1024,
    timeout_s: float = 300,
    cancel_requested: Callable[[], bool] | None = None,
    on_started: Callable[[dict], None] | None = None,
    job_name: str | None = None,
    fault: Callable[[str], None] | None = None,
    environment: dict[str, str] | None = None,
) -> dict:
    """Run one immutable attempt. on_started must durably save before returning.

    Normal failures are structured facts. Test fault callbacks may hard-exit the
    coordinator; the kernel then owns cleanup. A raised callback fails the job.
    """
    started_at, start = _now(), time.monotonic()
    result = {"state": "failed", "exit_code": None, "handle": None,
              "resource_usage": {}, "started_at": started_at, "ended_at": None,
              "reason": None}
    job = port = None
    process = None
    win = None
    affinity = 0
    files = []
    observed = {}
    sample_errors = []
    sample_truncated = False

    def sample():
        nonlocal sample_truncated
        try:
            snapshot = _sample_job(win, job)
            for identity in snapshot["processes"]:
                key = (identity["pid"], identity["create_time_100ns"])
                if key in observed or len(observed) < 512:
                    observed[key] = identity
                else:
                    sample_truncated = True
            sample_errors.extend(snapshot["errors"][:max(0, 32 - len(sample_errors))])
        except (OSError, RuntimeError) as error:
            if len(sample_errors) < 32:
                sample_errors.append({"reason": str(error)})
    try:
        if type(cores) is not int or not 1 <= cores <= 4:
            raise ValueError("cores must be an integer in [1, 4]")
        if type(total_memory_mb) is not int or not 32 <= total_memory_mb <= 1024:
            raise ValueError("total_memory_mb must be an integer in [32, 1024]")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError("timeout_s must be finite and positive")
        executable, workdir = Path(executable), Path(workdir)
        if not executable.is_absolute() or not executable.is_file():
            raise ValueError("executable must name an existing absolute file")
        if executable.suffix.lower() != ".exe":
            raise ValueError("Only native executables are allowed, never shell/batch files")
        if not workdir.is_absolute() or not workdir.is_dir():
            raise ValueError("workdir must be an existing absolute directory")
        if not isinstance(args, list) or any(not isinstance(arg, str) or "\0" in arg for arg in args):
            raise ValueError("args must be a list of strings without NUL")
        if environment is not None:
            if not isinstance(environment, dict) or any(
                not isinstance(key, str) or not key or "=" in key or "\0" in key
                or not isinstance(value, str) or "\0" in value
                for key, value in environment.items()
            ):
                raise ValueError("environment must contain valid Windows string keys and values")
            if len({key.upper() for key in environment}) != len(environment):
                raise ValueError("environment keys must be unique ignoring case")
        executable, workdir = executable.resolve(), workdir.resolve()
        job_name = job_name or new_job_name()
        if not re.fullmatch(r"Local\\[A-Za-z0-9_-]{1,160}", job_name):
            raise ValueError("job_name must be a unique Local\\ name using letters, digits, _ or -")
        win = _native()
        c.set_last_error(0)
        job = win.check(win.CreateJobObject(None, job_name), "CreateJobObject")
        if c.get_last_error() == 183:
            # Do not terminate or alter an existing job bearing the same name.
            win.close(job)
            job = None
            raise ValueError("Job name already exists; attempt identity cannot be reused")
        affinity = _configure_job(win, job, cores, total_memory_mb)
        port = win.check(win.CreateIoCompletionPort(win.HANDLE(-1), None, 0, 1),
                         "CreateIoCompletionPort")
        association = win.COMPLETION_PORT(1, port)
        win.check(win.SetInformationJobObject(job, 7, c.byref(association), c.sizeof(association)),
                  "SetInformationJobObject(completion port)")
        # xb preserves earlier evidence even if the caller accidentally repeats.
        files.append(open(os.devnull, "rb"))
        files.append((workdir / "stdout.out").open("xb", buffering=0))
        files.append((workdir / "stderr.txt").open("xb", buffering=0))
        if cancel_requested and cancel_requested():
            result.update(state="cancelled", reason="cancelled_before_process_creation")
            return result
        if fault:
            fault("before_create_process")
        process = _create_suspended(win, executable, args, workdir, job, files, environment)
        ticks, created = win.creation_time(process.hProcess)
        coordinator_ticks, coordinator_created = win.creation_time(win.GetCurrentProcess())
        result["handle"] = {
            "pid": process.dwProcessId, "create_time": created,
            "create_time_100ns": str(ticks), "job_name": job_name,
            "launch_id": uuid.uuid4().hex, "executable": str(executable),
            "args": args.copy(), "workdir": str(workdir), "started_at": started_at,
            "coordinator_pid": os.getpid(), "coordinator_create_time": coordinator_created,
            "coordinator_create_time_100ns": str(coordinator_ticks),
            "atomic_job_assignment": True,
        }
        sample()
        if fault:
            fault("after_process_created")
        if on_started:
            on_started(result["handle"].copy())
        if fault:
            fault("after_handle_saved")
        if win.ResumeThread(process.hThread) == 0xFFFFFFFF:
            win.check(False, "ResumeThread")
        if fault:
            fault("after_resumed")
        win.close(process.hThread)
        process.hThread = None
        while True:
            sample()
            if _memory_limit_message(win, port):
                result.update(state="failed", reason="job_memory_limit_exceeded")
                break
            if win.accounting(job).ActiveProcesses == 0:
                exit_code = win.w.DWORD()
                win.check(win.GetExitCodeProcess(process.hProcess, c.byref(exit_code)),
                          "GetExitCodeProcess")
                result.update(exit_code=exit_code.value,
                              state="completed" if exit_code.value == 0 else "failed",
                              reason="process_tree_exited" if exit_code.value == 0
                              else "nonzero_exit_code")
                break
            if cancel_requested and cancel_requested():
                result.update(state="cancelled", reason="cancel_requested")
                break
            if time.monotonic() - start >= timeout_s:
                result.update(state="timed_out", reason="deadline_exceeded")
                break
            time.sleep(POLL_INTERVAL_S)
    except Exception as error:
        result.update(state="failed", reason=f"backend_error: {type(error).__name__}: {error}")
    finally:
        if job and win:
            try:
                if win.accounting(job).ActiveProcesses:
                    if not _terminate_and_confirm(win, job):
                        result.update(state="unknown", reason="process_tree_termination_unconfirmed")
                result["resource_usage"] = _usage(
                    win, job, affinity, total_memory_mb, time.monotonic() - start,
                )
                result["resource_usage"].update(
                    observed_processes=list(observed.values()), process_sampling_errors=sample_errors,
                    process_sampling_truncated=sample_truncated,
                    process_sampling="JobObjectBasicProcessIdList + identity/membership/affinity query",
                )
                if process and result["exit_code"] is None:
                    exit_code = win.w.DWORD()
                    win.check(win.GetExitCodeProcess(process.hProcess, c.byref(exit_code)),
                              "GetExitCodeProcess(cleanup)")
                    if exit_code.value != 259:
                        result["exit_code"] = exit_code.value
            except Exception as error:
                result.update(state="unknown", reason=f"cleanup_unconfirmed: {error}")
            win.close(job)
        if process and win:
            win.close(process.hThread)
            win.close(process.hProcess)
        if port and win:
            win.close(port)
        for file in files:
            file.close()
        result["ended_at"] = _now()
    return result


def reconcile(handle: dict) -> dict:
    """Read-only identity check; never signal a process found by PID alone.

    An absent tree establishes termination, never success. A partial launch
    intent without a durable process identity remains unknown and retains quota.
    """
    result = {"state": "unknown", "identity_match": None, "active_processes": None,
              "reason": "incomplete_launch_identity"}
    if not handle.get("pid") or not handle.get("job_name") or not handle.get("create_time"):
        return result
    process = job = None
    win = None
    try:
        win = _native()
        process = win.OpenProcess(0x1000, False, int(handle["pid"]))  # QUERY_LIMITED_INFORMATION
        process_error = c.get_last_error() if not process else None
        if not process and process_error != 87:  # ERROR_INVALID_PARAMETER = PID absent
            return {**result, "reason": f"process_query_denied_or_unknown: {process_error}"}
        if process:
            ticks, created = win.creation_time(process)
            expected = handle.get("create_time_100ns")
            match = str(ticks) == str(expected) if expected else abs(created - handle["create_time"]) < 1e-6
            result["identity_match"] = match
            if not match:
                return {**result, "reason": "pid_reused_or_identity_mismatch"}
        job = win.OpenJobObject(4, False, handle["job_name"])  # JOB_OBJECT_QUERY only
        job_error = c.get_last_error() if not job else None
        if not job:
            if job_error == 2:
                if not process:
                    return {**result, "state": "terminated", "active_processes": 0,
                            "reason": "recorded_process_and_job_absent"}
                exit_code = win.w.DWORD()
                win.check(win.GetExitCodeProcess(process, c.byref(exit_code)),
                          "GetExitCodeProcess(reconcile)")
                if exit_code.value != 259:
                    # A debugger/observer can retain a dead process object after
                    # its Job has gone. Its exact creation identity still matched.
                    return {**result, "state": "terminated", "active_processes": 0,
                            "reason": "recorded_process_exited_and_job_absent"}
            return {**result, "reason": f"job_absent_or_query_unavailable: {job_error}"}
        active = win.accounting(job).ActiveProcesses
        result["active_processes"] = active
        if process:
            in_job = win.w.BOOL()
            win.check(win.IsProcessInJob(process, job, c.byref(in_job)), "IsProcessInJob")
            if not in_job.value:
                return {**result, "reason": "process_not_in_recorded_job"}
        if active == 0:
            return {**result, "state": "terminated", "reason": "job_has_no_active_processes"}
        if not process:
            # The driver may exit before descendants; retaining quota is safe.
            return {**result, "reason": "recorded_driver_absent_but_job_still_active"}
        return {**result, "state": "running", "reason": "process_identity_and_job_confirmed"}
    except Exception as error:
        return {**result, "reason": f"reconcile_unavailable: {error}"}
    finally:
        if win:
            win.close(job)
            win.close(process)
