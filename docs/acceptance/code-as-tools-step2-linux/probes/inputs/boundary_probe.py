#!/usr/bin/env python3
"""Development-only Linux container observations; not a production runner.

Run from a trusted development entry point inside the candidate container.
This does not establish a complete security boundary, block Python descendants,
test ORCA/MPI, or prove resource enforcement/coordinator-loss cleanup. It never
reads credential contents, starts a process, or changes container configuration.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
import re
import socket
import stat
import sys
import time
import uuid
from pathlib import Path, PurePosixPath

MAX_READ = 1024 * 1024
CGROUP_FIELDS = (
    "memory.max", "memory.swap.max", "cpu.max", "cpuset.cpus.effective", "pids.max",
)
FORBIDDEN_PATHS = {
    "host": ("/host", "/mnt/c", "/mnt/d", "/mnt/e"),
    "source": ("/workspace", "/src", "/repo", "/opt/product-source", "/opt/orca-agent"),
    "credentials": ("/run/secrets", "/opt/credentials", "/root/.ssh", "/root/.aws",
                    "/root/.codex", "/home/task/.ssh", "/home/task/.aws"),
    "runtime_control": ("/var/run/docker.sock", "/run/docker.sock",
                        "/run/containerd/containerd.sock", "/run/podman/podman.sock"),
    "wsl_interop": ("/mnt/wsl", "/mnt/wslg", "/run/WSL", "/run/WSL_interop",
                    "/usr/lib/wsl", "/proc/sys/fs/binfmt_misc/WSLInterop"),
}


def error_fact(exc):
    return {"error": type(exc).__name__, "errno": getattr(exc, "errno", None)}


def bounded_text(path, limit=MAX_READ):
    with Path(path).open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("probe metadata exceeds read bound")
    return data.decode("utf-8", errors="strict")


def parse_status(text):
    values = dict(line.split(":", 1) for line in text.splitlines() if ":" in line)
    result = {key: values[key].strip() for key in
              ("Uid", "Gid", "CapEff", "CapBnd", "CapPrm", "CapInh", "CapAmb",
               "NoNewPrivs", "Seccomp", "Seccomp_filters") if key in values}
    result["cap_eff_zero"] = int(result["CapEff"], 16) == 0
    result["no_new_privileges"] = result["NoNewPrivs"] == "1"
    result["seccomp_filter_mode"] = result["Seccomp"] == "2"
    return result


def parse_mountinfo(text):
    def unescape(value):
        return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)

    mounts = []
    for line in text.splitlines():
        left, right = line.split(" - ", 1)
        fields, backing = left.split(), right.split()
        mounts.append({"root": unescape(fields[3]), "mount_point": unescape(fields[4]),
                       "read_only": "ro" in fields[5].split(","),
                       "filesystem": backing[0]})
    return mounts


def cpuset_count(value):
    cpus = set()
    for item in value.split(","):
        if not re.fullmatch(r"\d+(?:-\d+)?", item):
            raise ValueError("invalid cpuset metadata")
        first, _, last = item.partition("-")
        first, last = int(first), int(last or first)
        if not 0 <= first <= last < 65536:
            raise ValueError("cpuset exceeds probe bound")
        cpus.update(range(first, last + 1))
    return len(cpus)


def cgroup_observations(mounts):
    memberships = bounded_text("/proc/self/cgroup", 65536).splitlines()
    member = next(line[3:] for line in memberships if line.startswith("0::"))
    if not member.startswith("/") or ".." in member.split("/"):
        raise ValueError("unresolved cgroup namespace membership")
    candidates = [entry for entry in mounts if entry["filesystem"] == "cgroup2"
                  and PurePosixPath(member).is_relative_to(entry["root"])]
    mount = max(candidates, key=lambda entry: len(entry["root"]))
    relative = PurePosixPath(member).relative_to(mount["root"])
    folder = Path(mount["mount_point"]) / relative
    values = {name: bounded_text(folder / name, 4096).strip() for name in CGROUP_FIELDS}
    quota, period = values["cpu.max"].split()
    cpu_limit = None if quota == "max" else int(quota) / int(period)
    return {"version": 2, "membership": member, "read_directory": str(folder),
            "controls": values, "cpu_quota_cores": cpu_limit,
            "cpuset_effective_count": cpuset_count(values["cpuset.cpus.effective"]),
            "process_affinity": sorted(os.sched_getaffinity(0)),
            "memory_limit_is_1024_mib": values["memory.max"] == str(1024 * 1024 * 1024),
            "swap_limit_is_zero": values["memory.swap.max"] == "0",
            "pids_limit_is_finite": values["pids.max"].isdigit()
                                       and int(values["pids.max"]) > 0,
            "enforcement_tested": False}


def path_presence(path):
    """Metadata only; EACCES is not falsely reported as an absent path."""
    try:
        info = Path(path).lstat()
    except FileNotFoundError:
        return {"path": path, "state": "absent"}
    except PermissionError as exc:
        return {"path": path, "state": "metadata_denied", **error_fact(exc)}
    except OSError as exc:
        return {"path": path, "state": "unknown", **error_fact(exc)}
    return {"path": path, "state": "present", "is_symlink": stat.S_ISLNK(info.st_mode),
            "is_socket": stat.S_ISSOCK(info.st_mode)}


def write_observation(directory):
    """Create at most one tiny, exclusive file and remove only that owned file."""
    parent = Path(directory)
    target = parent / (".orca-boundary-probe-" + uuid.uuid4().hex)
    created = False
    result = {"directory": directory, "write_succeeded": False}
    try:
        if parent.resolve(strict=True) != parent or not parent.is_dir():
            raise ValueError("probe write directory is missing or redirected")
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"bounded development probe\n")
        result["write_succeeded"] = True
    except (OSError, ValueError) as exc:
        result.update(error_fact(exc))
        result["denied"] = getattr(exc, "errno", None) in {errno.EACCES, errno.EPERM, errno.EROFS}
    finally:
        if created:
            try:
                target.unlink()
                result["owned_file_removed"] = True
            except OSError as exc:
                result["owned_file_removed"] = False
                result["cleanup_error"] = error_fact(exc)
    return result


def connection_observation(ip, timeout):
    """One TCP handshake attempt to a literal public IP; no DNS or payload."""
    address = ipaddress.ip_address(ip)
    if not address.is_global:
        raise ValueError("external probe requires a literal global IP address")
    result = {"ip": str(address), "port": 443, "timeout_seconds": timeout,
              "payload_sent": False, "isolation_denial_observed": False}
    start = time.monotonic()
    try:
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect((str(address), 443))
        result["state"] = "connected"
    except OSError as exc:
        denied = exc.errno in {errno.EACCES, errno.EPERM, errno.ENETUNREACH, errno.EHOSTUNREACH}
        result.update(state="denied" if denied else "inconclusive",
                      isolation_denial_observed=denied, **error_fact(exc))
    result["elapsed_seconds"] = time.monotonic() - start
    return result


def capture(operation):
    try:
        return operation()
    except (OSError, ValueError, KeyError, StopIteration, ZeroDivisionError) as exc:
        return {"state": "unknown", **error_fact(exc)}


def collect(args):
    mounts = capture(lambda: parse_mountinfo(bounded_text("/proc/self/mountinfo")))
    forbidden = {category: [path_presence(path) for path in paths]
                 for category, paths in FORBIDDEN_PATHS.items()}
    forbidden["explicit_canaries"] = [path_presence(path) for path in args.forbidden_path]
    return {
        "probe_version": 1, "scope": "development_observations_only",
        "complete_security_boundary_proven": False, "python_child_creation_tested": False,
        "limitations": ["No ORCA/MPI execution or scientific validation",
                        "No resource stress, coordinator-loss, or SDK bypass test",
                        "Path absence covers listed paths only; mount origins need trusted host inspection",
                        "Network namespace identity needs host comparison; timeout/refused is inconclusive"],
        "identity": {"uid": os.getuid(), "euid": os.geteuid(), "gid": os.getgid(),
                     "groups": os.getgroups(), "non_root": os.geteuid() != 0,
                     "proc_status": capture(lambda: parse_status(bounded_text("/proc/self/status", 65536)))},
        "mounts": mounts,
        "root_mount_read_only": next((entry["read_only"] for entry in mounts
                                       if entry["mount_point"] == "/"), None)
                                       if isinstance(mounts, list) else None,
        "cgroup": capture(lambda: cgroup_observations(mounts)) if isinstance(mounts, list) else
                  {"state": "unknown", "reason": "mount metadata unavailable"},
        "network": {"namespace": capture(lambda: os.readlink("/proc/self/ns/net")),
                    "interfaces": capture(lambda: [name for _, name in socket.if_nameindex()]),
                    "external_connection": capture(lambda: connection_observation(args.external_ip, args.timeout))},
        "forbidden_entries": forbidden,
        "host_environment_entry_names_present": [name for name in
            ("WSL_INTEROP", "DOCKER_HOST", "CONTAINER_HOST", "SSH_AUTH_SOCK") if name in os.environ],
        "writes": {"task": write_observation("/tmp/task"), "usr": write_observation("/usr")},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forbidden-path", action="append", default=[],
                        help="Additional known source/credential canary path; lstat only, never read")
    parser.add_argument("--external-ip", default="1.1.1.1", help="Literal public IP for one TCP connect")
    parser.add_argument("--timeout", type=float, default=0.5, help="TCP connect deadline, 0 < seconds <= 2")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 2 or len(args.forbidden_path) > 32:
        parser.error("timeout must be within (0, 2] seconds and at most 32 canary paths are allowed")
    if any(not path.startswith("/") or ".." in path.split("/") for path in args.forbidden_path):
        parser.error("canary paths must be absolute without parent traversal")
    if not sys.platform.startswith("linux"):
        print(json.dumps({"scope": "development_observations_only", "state": "unsupported_platform",
                          "platform": sys.platform, "writes_attempted": False, "network_attempted": False}))
        return 2
    result = collect(args)
    result["network"]["only_loopback_interface"] = result["network"]["interfaces"] == ["lo"]
    print(json.dumps(result, sort_keys=True, indent=2, allow_nan=False))
    return 0  # A JSON observation report, never an isolation acceptance verdict.


if __name__ == "__main__":
    raise SystemExit(main())
