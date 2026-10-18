#!/usr/bin/env python3
"""Development-only aggregate-memory probe, never a production entry point.

Require --inside-bounded-container, Linux, a private Docker cgroup namespace,
1 GiB memory.max, zero swap, and finite pids before allocating. A lightweight
parent supervises two children that each request/touch 600 MiB. Only /proc and
cgroup metadata are read;
no cgroup limits, OOM scores, network, or host files are changed. A surviving
parent emits final JSON. If it is OOM-killed, children receive PDEATHSIG and the
trusted host must collect container exit/OOM evidence; missing JSON is not a pass.
"""

import ctypes
import json
import mmap
import os
import signal
import sys
import time
from pathlib import Path

LIMIT = 1024 * 1024 * 1024
ALLOCATION = 600 * 1024 * 1024
CGROUP = Path("/sys/fs/cgroup")


def emit(event, **facts):
    print(json.dumps({"event": event, "pid": os.getpid(), **facts}), flush=True)


def read(path):
    with Path(path).open("r", encoding="ascii") as stream:
        value = stream.read(8193)
    if len(value) > 8192:
        raise ValueError("metadata exceeds read bound")
    return value.strip()


def snapshot():
    return {
        "events": {key: int(value) for key, value in
                   (line.split() for line in read(CGROUP / "memory.events").splitlines())},
        "peak": int(read(CGROUP / "memory.peak")),
        "current": int(read(CGROUP / "memory.current")),
    }


def gate():
    if sys.platform != "linux" or sys.argv[1:] != ["--inside-bounded-container"]:
        raise ValueError("Linux and explicit --inside-bounded-container are required")
    if not Path("/.dockerenv").is_file() or os.geteuid() == 0:
        raise ValueError("a non-root Docker container is required")
    membership = read("/proc/self/cgroup")
    if membership != "0::/" or not (CGROUP / "cgroup.controllers").is_file():
        raise ValueError("private cgroup v2 namespace is required")
    limits = {name: read(CGROUP / name) for name in
              ("memory.max", "memory.swap.max", "pids.max", "memory.oom.group")}
    if limits["memory.max"] != str(LIMIT) or limits["memory.swap.max"] != "0":
        raise ValueError("refusing allocation without exactly 1 GiB and zero swap")
    if not limits["pids.max"].isdigit() or not 4 <= int(limits["pids.max"]) <= 4096:
        raise ValueError("finite pids.max in [4, 4096] is required")
    identity = CGROUP.stat()
    return {"membership": membership, "device": identity.st_dev,
            "inode": identity.st_ino, "limits": limits}


def touch():
    memory = mmap.mmap(-1, ALLOCATION, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS)
    for offset in range(0, ALLOCATION, mmap.PAGESIZE):
        memory[offset] = 1
    return memory


def child(parent, expected, start_fd, ready_fd):
    # These children never fork. PDEATHSIG also covers an uncatchable parent OOM.
    try:
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(10)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0 or os.getppid() != parent:
            raise RuntimeError("parent-death cleanup could not be armed")
        actual = gate()
        if actual != expected:
            raise RuntimeError("child cgroup or limits differ from parent")
        emit("child_ready", cgroup=actual, requested_bytes=ALLOCATION)
        os.write(ready_fd, b"1")
        os.close(ready_fd)
        if os.read(start_fd, 1) != b"1":
            raise RuntimeError("parent did not release allocation")
        os.close(start_fd)
        memory = touch()
        emit("child_touched", touched_bytes=ALLOCATION)
        while memory:
            signal.pause()
    except BaseException as exc:
        emit("child_error", error=type(exc).__name__, detail=str(exc))
    os._exit(2)


def interrupted(signum, frame):
    raise TimeoutError("probe deadline or cancellation signal: " + str(signum))


def main():
    try:
        cgroup, before = gate(), snapshot()
    except (OSError, ValueError) as exc:
        emit("refused", error=type(exc).__name__, detail=str(exc), allocated_bytes=0)
        return 2
    emit("preflight", cgroup=cgroup, before=before, requested_bytes_per_child=ALLOCATION,
         parent_requested_bytes=0, allocator_count=2, total_requested_bytes=2 * ALLOCATION, monitor_seconds=10,
         cleanup_seconds=2, parent_oom_requires_host_evidence=True)
    started = time.monotonic()
    children, returncodes, cleanup_signals = [], {}, {}
    error = None
    start_read, start_write = os.pipe()
    ready_read, ready_write = os.pipe()
    parent = os.getpid()
    for signum in (signal.SIGALRM, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        for unused in range(2):
            pid = os.fork()  # The parent remains a lightweight supervisor.
            if pid == 0:
                os.close(start_write)
                os.close(ready_read)
                child(parent, cgroup, start_read, ready_write)
            children.append(pid)
        os.close(start_read)
        os.close(ready_write)
        ready = b""
        while len(ready) < 2:
            item = os.read(ready_read, 2 - len(ready))
            if not item:
                raise RuntimeError("children did not both pass preflight")
            ready += item
        os.write(start_write, b"11")
        emit("children_released", children=children, requested_bytes_per_child=ALLOCATION)
        while time.monotonic() - started < 10:
            current = snapshot()
            emit("monitor", **current)
            for pid in children:
                if pid not in returncodes:
                    done, status = os.waitpid(pid, os.WNOHANG)
                    if done:
                        returncodes[pid] = os.waitstatus_to_exitcode(status)
            if current["events"].get("oom_kill", 0) > before["events"].get("oom_kill", 0):
                break
            if len(returncodes) == len(children):
                break
            time.sleep(0.25)
    except BaseException as exc:
        error = {"type": type(exc).__name__, "detail": str(exc)}
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        for sig in (signal.SIGTERM, signal.SIGKILL):
            until = time.monotonic() + 1
            for pid in children:
                if pid not in returncodes:
                    try:
                        os.kill(pid, sig)
                        cleanup_signals.setdefault(pid, []).append(int(sig))
                    except ProcessLookupError:
                        pass
            while time.monotonic() < until and len(returncodes) < len(children):
                for pid in children:
                    if pid not in returncodes:
                        done, status = os.waitpid(pid, os.WNOHANG)
                        if done:
                            returncodes[pid] = os.waitstatus_to_exitcode(status)
                time.sleep(0.01)
        for fd in (start_read, start_write, ready_read, ready_write):
            try:
                os.close(fd)
            except OSError:
                pass
    after = snapshot()
    delta = {key: value - before["events"].get(key, 0) for key, value in after["events"].items()}
    local_oom = all(delta.get(key, 0) > 0 for key in ("max", "oom", "oom_kill"))
    emit("final", cgroup=cgroup, requested_bytes_per_child=ALLOCATION, parent_requested_bytes=0,
         allocator_count=2, total_requested_bytes=2 * ALLOCATION, before=before, after=after, events_delta=delta,
         returncodes=returncodes, cleanup_signals=cleanup_signals,
         unreaped_children=[pid for pid in children if pid not in returncodes],
         elapsed_seconds=round(time.monotonic() - started, 3), error=error,
         interpretation="cgroup_limit_oom_observed" if local_oom else "inconclusive",
         scientific_result=False)
    return 0 if local_oom and len(returncodes) == 2 else 1


if __name__ == "__main__":
    raise SystemExit(main())
