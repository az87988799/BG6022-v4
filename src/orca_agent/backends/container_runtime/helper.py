"""Fixed, image-owned transport and lifecycle helper; no product imports."""

import hashlib
import json
import os
import re
import signal
import stat
import struct
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path("/work")
RUNTIME = Path("/opt/orca-agent-runtime")
AREAS = {"sealed": "sealed", "python": "task", "orca": "native",
         "requests": "requests", "responses": "responses", "control": "control"}
UIDS = {"sealed": 0, "python": 10001, "orca": 10002, "requests": 10001,
        "responses": 0, "control": 10003}
CHUNK = 65536


def base_area(area):
    if area.startswith("orca:") and re.fullmatch(r"[A-Za-z0-9_-]{1,96}", area[5:]):
        return "orca"
    if area not in AREAS:
        raise ValueError("invalid fixed transport area")
    return area


def area_root(area, *, create=False):
    base = base_area(area)
    path = ROOT / AREAS[base]
    if area != base:
        path = path / area[5:]
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        if path.is_symlink():
            raise ValueError("linked native call directory")
    return path


def require_identity(area):
    base = base_area(area)
    if os.getuid() != UIDS[base] or os.getgid() != UIDS[base]:
        raise ValueError("helper identity/area mismatch")


def relative(value):
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\0" in value or path.is_absolute()
            or any(p in ("", ".", "..") for p in value.split("/"))
            or len(value.encode()) > 1024 or len(path.parts) > 16):
        raise ValueError("invalid relative transport path")
    return path.parts


def parent_fd(area, name, *, create=False):
    parts = relative(name)
    descriptor = os.open(area_root(area, create=create),
                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o755, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor, parts[-1]
    except BaseException:
        os.close(descriptor)
        raise


def open_file(area, name, flags, mode=0o600):
    parent, basename = parent_fd(area, name, create=bool(flags & os.O_CREAT))
    try:
        fd = os.open(basename, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError("transport accepts only a regular single-link file")
    return fd


def put(area, name, size, digest):
    require_identity(area)
    if not 0 <= size <= 1024 * 1024 * 1024 or len(digest) != 64:
        raise ValueError("invalid bounded upload")
    temporary = name + ".upload-" + uuid.uuid4().hex
    fd = open_file(area, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                   0o444 if area in {"sealed", "responses"} else 0o600)
    actual = hashlib.sha256()
    remaining = size
    with os.fdopen(fd, "wb") as target:
        while remaining:
            block = sys.stdin.buffer.read(min(CHUNK, remaining))
            if not block:
                raise ValueError("truncated upload")
            target.write(block)
            actual.update(block)
            remaining -= len(block)
        if sys.stdin.buffer.read(1):
            raise ValueError("upload longer than declared")
        target.flush()
        os.fsync(target.fileno())
    if actual.hexdigest() != digest:
        raise ValueError("upload checksum mismatch")
    parent, basename = parent_fd(area, name)
    try:
        try:
            os.stat(basename, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError("published upload already exists")
        os.rename(PurePosixPath(temporary).name, basename, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        os.close(parent)
    print(json.dumps({"bytes": size, "sha256": actual.hexdigest()}))


def read_request(limit, name="request.json"):
    if os.getuid() != 0 or os.getgid() != 0:
        raise ValueError("request reader must use a different UID from task code")
    if name != "request.json" and not re.fullmatch(r"request-[0-9]{6}\.json", name):
        raise ValueError("invalid mailbox sequence")
    try:
        fd = open_file("requests", name, os.O_RDONLY)
    except FileNotFoundError:
        return 3
    with os.fdopen(fd, "rb") as source:
        size = os.fstat(source.fileno()).st_size
        if size > limit:
            raise ValueError("request exceeds limit")
        content = source.read(limit + 1)
        if len(content) != size or len(content) > limit:
            raise ValueError("request changed during read")
        sys.stdout.buffer.write(content)
    return 0


def header(value):
    data = json.dumps(value, separators=(",", ":")).encode()
    sys.stdout.buffer.write(struct.pack("!I", len(data)) + data)


def output_files(directory, limits, start):
    pending, visited = [directory], 0
    while pending:
        folder = pending.pop()
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with os.scandir(descriptor) as entries:
                for entry in entries:
                    visited += 1
                    if (visited > limits["count"] * 2 + 64
                            or time.monotonic() - start > limits["seconds"]):
                        raise ValueError("output directory traversal budget exhausted")
                    if entry.is_symlink():
                        raise ValueError("linked output entry")
                    path = folder / entry.name
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        yield path
                    else:
                        raise ValueError("special output entry")
        finally:
            os.close(descriptor)


def export(area, limits):
    require_identity(area)
    start, count, total = time.monotonic(), 0, 0
    directory = area_root(area)
    def expired(_number, _frame):
        raise TimeoutError("fixed collector lifetime exhausted")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, limits["seconds"])
    for item in output_files(directory, limits, start):
            if item == directory / ".launch.json":
                continue
            if time.monotonic() - start > limits["seconds"]:
                raise TimeoutError("export deadline exhausted")
            path = item.relative_to(directory).as_posix()
            fd = open_file(area, path, os.O_RDONLY)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                count += 1
                total += info.st_size
                if (count > limits["count"] or info.st_size > limits["file_bytes"]
                        or total > limits["total_bytes"]):
                    raise ValueError("output budget exhausted")
                header({"path": path, "size": info.st_size})
                digest = hashlib.sha256()
                remaining = info.st_size
                while remaining:
                    if time.monotonic() - start > limits["seconds"]:
                        raise TimeoutError("export deadline exhausted")
                    block = source.read(min(CHUNK, remaining))
                    if not block:
                        raise ValueError("output truncated")
                    digest.update(block)
                    sys.stdout.buffer.write(block)
                    remaining -= len(block)
                if source.read(1) or os.fstat(source.fileno()).st_size != info.st_size:
                    raise ValueError("output changed during export")
                sys.stdout.buffer.write(digest.digest())
    header({"end": True, "count": count, "bytes": total})


def status_fields(pid):
    values = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    return values


def processes(uid=None):
    result = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            values = status_fields(entry.name)
            owner = int(values["Uid"].split()[0])
            if uid is not None and owner != uid:
                continue
            data = (entry / "stat").read_text()
            fields = data[data.rindex(")") + 2:].split()
            result.append({"pid": int(entry.name), "uid": owner, "state": values["State"],
                           "start_ticks": fields[19], "name": values["Name"],
                           "cap_eff": values["CapEff"], "cap_prm": values["CapPrm"],
                           "cap_inh": values["CapInh"], "cap_amb": values["CapAmb"],
                           "no_new_privs": values["NoNewPrivs"], "seccomp": values["Seccomp"]})
        except (FileNotFoundError, ProcessLookupError):
            pass
    return result


def snapshot():
    root = Path("/sys/fs/cgroup")
    values = {}
    for name in ("memory.current", "memory.max", "memory.peak", "memory.events",
                 "memory.swap.max", "cpu.max", "cpu.stat", "pids.current", "pids.max",
                 "cpuset.cpus.effective"):
        values[name] = (root / name).read_text().strip()
    print(json.dumps({"cgroup": values, "processes": processes()[:512]}))


def control(action):
    require_identity("control")
    if action == "heartbeat":
        target = ROOT / "control" / "heartbeat"
        temporary = ROOT / "control" / "heartbeat.pending"
        with temporary.open("w") as stream:
            stream.write(str(time.monotonic()))
        os.replace(temporary, target)
    elif action == "cancel":
        (ROOT / "control" / "cancel").touch(exist_ok=False)
    else:
        raise ValueError("invalid control action")


def keeper():
    require_identity("control")
    if os.getpid() != 1:
        raise ValueError("keeper must be PID 1")
    start = time.monotonic()
    duration = float(os.environ["ORCA_BOUNDARY_SECONDS"])
    ttl = float(os.environ["ORCA_HEARTBEAT_SECONDS"])
    if not 0 < duration <= 86400 or not 0 < ttl <= 60:
        raise ValueError("invalid keeper deadlines")
    while True:
        now = time.monotonic()
        heartbeat = ROOT / "control" / "heartbeat"
        last = float(heartbeat.read_text()) if heartbeat.exists() else start
        reason = None
        if (ROOT / "control" / "cancel").exists():
            reason = "cancel_requested"
        elif now - start >= duration:
            reason = "deadline_exceeded"
        elif now - last >= ttl:
            reason = "coordinator_heartbeat_lost"
        if reason:
            (ROOT / "control" / "stopped.json").write_text(json.dumps({"reason": reason}))
            return 124
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        time.sleep(0.1)


def launch(kind, timeout, area=None):
    area = area or kind
    require_identity(area)
    if kind not in {"python", "orca"} or base_area(area) != kind:
        raise ValueError("invalid workload kind")
    folder = area_root(area)
    os.chdir(folder)
    identity = next(item for item in processes(os.getuid()) if item["pid"] == os.getpid())
    with (folder / ".launch.json").open("x") as target:
        json.dump(identity, target)
    deadline = time.monotonic() + min(timeout, 20)
    while not (ROOT / "gates" / f"{area}.gate").exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("workload gate was never released")
        time.sleep(0.025)
    with (folder / "stdout.out").open("xb", buffering=0) as out:
        os.dup2(out.fileno(), 1)
    with (folder / "stderr.txt").open("xb", buffering=0) as err:
        os.dup2(err.fileno(), 2)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(folder),
           "TMPDIR": str(folder), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
           "ORCA_TASK_REQUESTS": "/work/requests", "ORCA_TASK_RESPONSES": "/work/responses",
           "ORCA_TASK_TIMEOUT_SECONDS": str(timeout)}
    if kind == "python":
        executable = str(RUNTIME / "venv/bin/python")
        argv = [executable, "-I", str(RUNTIME / "python_task.py")]
    else:
        env.update(PATH="/opt/orca:/opt/openmpi/bin:/usr/bin:/bin",
                   LD_LIBRARY_PATH="/opt/orca/lib:/opt/openmpi/lib", OMPI_MCA_plm="isolated",
                   OMPI_MCA_pml="ob1", OMPI_MCA_btl="self,vader",
                   OMPI_MCA_btl_vader_single_copy_mechanism="none")
        executable = "/opt/orca/orca"
        argv = [executable, "job.inp"]
    os.execve(executable, argv, env)


def launch_identity(area, *, executable=False):
    require_identity(area)
    with os.fdopen(open_file(area, ".launch.json", os.O_RDONLY), "r") as stream:
        expected = json.load(stream)
    actual = next((p for p in processes(os.getuid()) if p["pid"] == expected["pid"]), None)
    if not actual or actual["start_ticks"] != expected["start_ticks"]:
        raise ValueError("launch process identity unavailable")
    if any(int(actual[k], 16) for k in ("cap_eff", "cap_prm", "cap_inh", "cap_amb")):
        raise ValueError("workload retained capabilities")
    if actual["no_new_privs"] != "1" or actual["seccomp"] != "2":
        raise ValueError("workload base isolation unavailable")
    if executable:
        if base_area(area) != "orca":
            raise ValueError("actual executable proof is only defined for ORCA")
        target = Path("/opt/orca/orca")
        loaded = Path(f"/proc/{actual['pid']}/exe")
        fixed = target.stat()
        observed = loaded.stat()
        if ((observed.st_dev, observed.st_ino) != (fixed.st_dev, fixed.st_ino)
                or os.readlink(loaded) != str(target)):
            raise ValueError("ORCA executable has not been observed")
        again = next((p for p in processes(os.getuid()) if p["pid"] == actual["pid"]), None)
        if not again or again["start_ticks"] != actual["start_ticks"]:
            raise ValueError("ORCA process identity changed during observation")
        actual["actual_start_evidence"] = {
            "method": "proc_exe_inode_and_pid_start_ticks", "executable": str(target),
            "device": observed.st_dev, "inode": observed.st_ino,
            "pid": actual["pid"], "start_ticks": actual["start_ticks"],
        }
    print(json.dumps(actual))


def gate(area):
    if base_area(area) not in {"python", "orca"} or os.getuid() != 0 or os.getgid() != 0:
        raise ValueError("gate requires the fixed trusted identity")
    with (ROOT / "gates" / f"{area}.gate").open("x"):
        pass


def quiesce(area):
    require_identity(area)
    until = time.monotonic() + 3
    while True:
        active = [p for p in processes(os.getuid()) if p["pid"] != os.getpid()
                  and not p["state"].startswith("Z")]
        if not active:
            print(json.dumps({"active_processes": 0}))
            return
        if time.monotonic() >= until:
            raise TimeoutError("same-UID descendants remain")
        for item in active:
            try:
                fd = os.pidfd_open(item["pid"])
                try:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                finally:
                    os.close(fd)
            except ProcessLookupError:
                pass
        time.sleep(0.05)


def main():
    args = sys.argv[1:]
    command = args.pop(0)
    if command == "keeper":
        return keeper()
    if command == "manifest":
        sys.stdout.buffer.write((RUNTIME / "runtime-manifest.json").read_bytes())
    elif command == "put":
        put(args[0], args[1], int(args[2]), args[3])
    elif command == "request":
        return read_request(int(args[0]), args[1] if len(args) > 1 else "request.json")
    elif command == "export":
        export(args[0], json.loads(args[1]))
    elif command == "snapshot":
        snapshot()
    elif command == "control":
        control(args[0])
    elif command == "launch":
        launch(args[0], float(args[1]), args[2] if len(args) > 2 else None)
    elif command == "identity":
        launch_identity(args[0])
    elif command == "exec-identity":
        launch_identity(args[0], executable=True)
    elif command == "gate":
        gate(args[0])
    elif command == "quiesce":
        quiesce(args[0])
    else:
        raise ValueError("unknown fixed helper command")
    return 0


if __name__ == "__main__":
    sys.exit(main())
