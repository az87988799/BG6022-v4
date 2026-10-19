"""One local Linux container per Attempt; Docker is controlled only by the host.

No task path, command, Docker option, image or lifecycle decision comes from IPC.
The synchronous caller remains the sole coordinator and owns all Store updates.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath, PureWindowsPath

HELPER = "/opt/orca-agent-runtime/helper.py"
PYTHON = "/usr/bin/python3.11"
HEARTBEAT_SECONDS = 15
COMMAND_SECONDS = 8
CHUNK = 65536
_ACTIVE = {}
_UID = {"python": 10001, "orca": 10002, "sealed": 0, "responses": 0}


def _value(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _validate_execution(execution):
    value = _value(execution)
    if value.get("backend") != "linux-container":
        raise ValueError("explicit linux-container backend is required")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value.get("image_id", "")):
        raise ValueError("an immutable local image ID is required")
    if not re.fullmatch(r"[0-9a-f]{64}", value.get("manifest_sha256", "")):
        raise ValueError("runtime manifest digest is required")
    executable = Path(value.get("docker_path", ""))
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("docker_path must name an existing absolute executable")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value.get("docker_context", "")):
        raise ValueError("an explicit local Docker context is required")
    if value.get("_docker_endpoint") and not value["_docker_endpoint"].startswith(("npipe://", "unix://")):
        raise ValueError("recorded Docker endpoint must remain local")
    return value


def _argv(execution, args):
    value = _validate_execution(execution)
    connection = (["--host", value["_docker_endpoint"]] if value.get("_docker_endpoint")
                  else ["--context", value["docker_context"]])
    return [str(value["docker_path"]), *connection, *args]


def _environment():
    # Host CLI only; this environment is never copied into a workload.
    return {key: value for key, value in os.environ.items()
            if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "HOME",
                               "HOMEDRIVE", "HOMEPATH", "TEMP", "TMP", "APPDATA",
                               "LOCALAPPDATA"}}


def _command(execution, args, *, timeout=COMMAND_SECONDS, input_stream=None,
             output_stream=None, max_output=8 * 1024 * 1024, checkpoint=None, check=True):
    """Pump bounded bytes without blocking the coordinator on a full pipe."""
    if not math.isfinite(timeout) or timeout <= 0:
        raise TimeoutError("Docker operation deadline exhausted")
    output = output_stream or io.BytesIO()
    errors = io.BytesIO()
    failure = []
    process = subprocess.Popen(_argv(execution, args), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_environment(), shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def copy_output(reader, writer, limit):
        total = 0
        try:
            while block := reader.read(CHUNK):
                total += len(block)
                if total > limit:
                    raise ValueError("Docker transport output exceeded its bound")
                writer.write(block)
        except BaseException as exc:
            failure.append(exc)
        finally:
            reader.close()

    def copy_input():
        try:
            if input_stream:
                while block := input_stream.read(CHUNK):
                    process.stdin.write(block)
        except (BrokenPipeError, OSError) as exc:
            if process.poll() is None:
                failure.append(exc)
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    workers = [threading.Thread(target=copy_output, args=(process.stdout, output, max_output), daemon=True),
               threading.Thread(target=copy_output, args=(process.stderr, errors, 65536), daemon=True),
               threading.Thread(target=copy_input, daemon=True)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    try:
        while process.poll() is None:
            if failure:
                raise failure[0]
            if time.monotonic() >= deadline:
                raise TimeoutError("Docker transport deadline exhausted; execution may be unknown")
            if checkpoint:
                checkpoint()
            time.sleep(0.025)
        for worker in workers:
            worker.join(timeout=max(0.01, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            raise TimeoutError("Docker transport streams did not close")
        if failure:
            raise failure[0]
        error = errors.getvalue().decode("utf-8", "replace")
        if check and process.returncode:
            raise RuntimeError(f"Docker command failed ({process.returncode}): {error[:4096]}")
        return process.returncode, output.getvalue() if output_stream is None else b"", error
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=1)


def _json(execution, args, **kwargs):
    return json.loads(_command(execution, args, **kwargs)[1])


def inspect_environment(execution):
    value = _validate_execution(execution)
    result = {"backend": "linux-container", "image_id": value["image_id"],
              "manifest_sha256": value["manifest_sha256"], "verified": False, "issues": []}
    try:
        if value.get("_docker_endpoint"):
            endpoint = value["_docker_endpoint"]
        else:
            context = _json(value, ["context", "inspect", value["docker_context"]])[0]
            endpoint = context.get("Endpoints", {}).get("docker", {}).get("Host", "")
        if not endpoint.startswith(("npipe://", "unix://")):
            raise ValueError("only an explicit local named-pipe/Unix Docker endpoint is supported")
        value["_docker_endpoint"] = endpoint
        info = _json(value, ["info", "--format", "{{json .}}"])
        if info.get("OSType") != "linux" or not info.get("ID"):
            raise ValueError("Docker daemon is not an identified Linux engine")
        image = _json(value, ["image", "inspect", value["image_id"]])[0]
        if image.get("Id") != value["image_id"] or image.get("Os") != "linux":
            raise ValueError("image identity/platform differs")
        labels = image.get("Config", {}).get("Labels") or {}
        raw = labels.get("org.orca-agent.runtime-manifest", "").encode()
        if (hashlib.sha256(raw).hexdigest() != value["manifest_sha256"]
                or labels.get("org.orca-agent.manifest-sha256") != value["manifest_sha256"]):
            raise ValueError("frozen runtime manifest hash differs from image labels")
        manifest = json.loads(raw)
        if any(not isinstance(manifest.get(key), dict) for key in ("orca", "mpi", "python")):
            raise ValueError("runtime manifest lacks fixed runtime identities")
        source = Path(__file__).with_name("container_runtime")
        if (manifest.get("isolation_policy") != "linux-container-v1"
                or manifest.get("sandbox_source_sha256") != hashlib.sha256(
                    (source / "sandbox.c").read_bytes()).hexdigest()):
            raise ValueError("runtime isolation policy differs from this backend")
        for name in ("helper.py", "python_task.py"):
            expected = hashlib.sha256((source / name).read_bytes()).hexdigest()
            entry = manifest.get("files", {}).get("/opt/orca-agent-runtime/" + name)
            actual = entry.get("sha256") if isinstance(entry, dict) else entry
            if actual != expected:
                raise ValueError("runtime helper differs from this backend: " + name)
        sdk_hash = hashlib.sha256((Path(__file__).parents[1] / "task_sdk.py").read_bytes()).hexdigest()
        if manifest.get("sdk", {}).get("sha256") != sdk_hash:
            raise ValueError("runtime SDK differs from this backend")
        result.update(engine_id=info["ID"], daemon_id=info["ID"], manifest=manifest,
                      docker_endpoint=endpoint, verified=True)
    except Exception as exc:
        result["issues"].append(str(exc))
    return result


def new_attempt_handle(execution, run_id, attempt_id):
    value = _validate_execution(execution)
    environment = inspect_environment(value)
    if not environment["verified"]:
        raise ValueError("container environment rejected: " + "; ".join(environment["issues"]))
    value["_docker_endpoint"] = environment["docker_endpoint"]
    nonce = uuid.uuid4().hex
    return {"backend": "linux-container", "version": 1, "launch_id": nonce,
            "container_name": "orca-attempt-" + nonce, "volume_name": "orca-evidence-" + nonce,
            "run_id": run_id, "attempt_id": attempt_id, "image_id": value["image_id"],
            "manifest_sha256": value["manifest_sha256"], "execution": value,
            "engine_id": environment["engine_id"], "daemon_id": environment["engine_id"]}


def _inspect(boundary):
    return _json(boundary["execution"], ["container", "inspect", boundary["handle"]["container_id"]])[0]


def _helper(boundary, uid, args, **kwargs):
    command = ["exec", "--user", f"{uid}:{uid}"]
    if kwargs.get("input_stream") is not None:
        command.append("-i")
    command += [boundary["handle"]["container_id"], PYTHON, "-I", HELPER, *args]
    return _command(boundary["execution"], command, **kwargs)


def prepare_boundary(execution, intent_handle, *, local_root, resources, timeout_s,
                     on_prepared, fault=None):
    value = _validate_execution(execution)
    if intent_handle.get("execution", {}).get("_docker_endpoint"):
        value["_docker_endpoint"] = intent_handle["execution"]["_docker_endpoint"]
    if not math.isfinite(timeout_s) or not 0 < timeout_s <= 86400:
        raise ValueError("boundary timeout must be within (0, 86400] seconds")
    environment = inspect_environment(value)
    if not environment["verified"]:
        raise ValueError("container environment rejected: " + "; ".join(environment["issues"]))
    handle = dict(intent_handle)
    if handle.get("execution") != value or handle.get("image_id") != value["image_id"]:
        raise ValueError("container launch differs from durable intent")
    if handle.get("engine_id") != environment["engine_id"]:
        raise ValueError("Docker engine differs from durable launch intent")
    envelope = _value(resources)
    boundary = {"execution": value, "handle": handle, "local_root": Path(local_root),
                "resources": envelope, "deadline": time.monotonic() + timeout_s,
                "last_heartbeat": 0.0, "last_snapshot": None, "control_outcome": None,
                "observed_processes": {}, "running": False}
    handle.update(engine_id=environment["engine_id"], daemon_id=environment["engine_id"],
                  heartbeat_ttl_seconds=HEARTBEAT_SECONDS, timeout_seconds=timeout_s)
    code, _, error = _command(value, ["volume", "inspect", handle["volume_name"]], check=False)
    if code == 0:
        raise ValueError("attempt volume already exists; no implicit reuse")
    if "no such volume" not in error.lower():
        raise RuntimeError("volume absence could not be established")
    _command(value, ["volume", "create", "--label", f"orca.nonce={handle['launch_id']}",
                     handle["volume_name"]])
    create = ["create", "--pull=never", "--name", handle["container_name"], "--network", "none",
              "--log-driver", "json-file", "--log-opt", "compress=false",
              "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
              "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
              "--user", "10003:10003", "--memory", str(envelope["total_memory_mb"] * 1024**2),
              "--memory-swap", str(envelope["total_memory_mb"] * 1024**2),
              "--cpus", str(envelope["max_cores"]), "--cpuset-cpus",
              _cpu_set(envelope["max_cores"]), "--pids-limit", "256",
              "--restart", "no", "--label", f"orca.nonce={handle['launch_id']}",
              "--label", f"orca.attempt={handle['attempt_id']}",
              "--mount", f"type=volume,src={handle['volume_name']},dst=/work",
              "--env", f"ORCA_BOUNDARY_SECONDS={timeout_s}",
              "--env", f"ORCA_HEARTBEAT_SECONDS={HEARTBEAT_SECONDS}",
              "--entrypoint", PYTHON, value["image_id"], "-I", HELPER, "keeper"]
    if fault:
        fault("before_create_process")
    handle["container_id"] = _command(value, create)[1].decode().strip()
    if not re.fullmatch(r"[0-9a-f]{64}", handle["container_id"]):
        raise RuntimeError("Docker returned an invalid container identity")
    if fault:
        fault("after_container_created")
    on_prepared(dict(handle))
    if fault:
        fault("after_boundary_saved")
    _command(value, ["start", handle["container_id"]])
    boundary["running"] = True
    _heartbeat(boundary, force=True)
    raw = _helper(boundary, 10003, ["manifest"])[1]
    if hashlib.sha256(raw).hexdigest() != value["manifest_sha256"]:
        _terminate(boundary)
        raise ValueError("container runtime file differs from frozen manifest")
    try:
        _verify_boundary(boundary, _sample(boundary), _inspect(boundary))
    except Exception:
        _terminate(boundary)
        raise
    return boundary


def _cpu_set(cores):
    return "0" if cores == 1 else f"0-{cores - 1}"


def _verify_boundary(boundary, snapshot, inspected):
    """Reject unavailable controls before the first workload gate can open."""
    resources = boundary["resources"]
    engine = _json(boundary["execution"], ["info", "--format", "{{json .}}"])
    if engine.get("ID") != boundary["handle"]["engine_id"]:
        raise ValueError("Docker engine changed before workload gate validation")
    memory = resources["total_memory_mb"] * 1024**2
    cores = resources["max_cores"]
    actual = snapshot["cgroup"]
    quota, period = actual["cpu.max"].split()
    if (int(actual["memory.max"]) != memory or int(actual["memory.swap.max"]) != 0
            or int(actual["pids.max"]) != 256 or int(quota) != cores * int(period)
            or actual["cpuset.cpus.effective"] != _cpu_set(cores)):
        raise ValueError("observed cgroup CPU/memory/swap/PID controls differ from frozen limits")
    keeper = next((p for p in snapshot["processes"] if p["pid"] == 1), None)
    if (keeper is None or keeper["uid"] != 10003
            or any(int(keeper[k], 16) for k in ("cap_eff", "cap_prm", "cap_inh", "cap_amb"))
            or keeper["no_new_privs"] != "1" or keeper["seccomp"] != "2"):
        raise ValueError("keeper identity/capability/no-new-privileges/seccomp boundary missing")
    host = inspected["HostConfig"]
    if (not host.get("ReadonlyRootfs") or host.get("NetworkMode") != "none"
            or host.get("CapAdd") or {v.upper() for v in host.get("CapDrop", [])} != {"ALL"}
            or host.get("Privileged") or host.get("Memory") != memory
            or host.get("MemorySwap") != memory or host.get("PidsLimit") != 256
            or host.get("CpusetCpus") != _cpu_set(cores)
            or "no-new-privileges:true" not in host.get("SecurityOpt", [])
            or host.get("RestartPolicy", {}).get("Name") != "no"
            or inspected["Config"].get("User") != "10003:10003"):
        raise ValueError("observed Docker isolation settings differ from the requested boundary")
    logging = host.get("LogConfig", {})
    if (logging.get("Type") != "json-file"
            or any(logging.get("Config", {}).get(key) != value for key, value in
                   {"compress": "false", "max-size": "1m", "max-file": "1"}.items())):
        raise ValueError("Docker logging does not have the explicit bounded configuration")
    mounts = inspected["Mounts"]
    if (len(mounts) != 1 or mounts[0].get("Type") != "volume"
            or mounts[0].get("Name") != boundary["handle"]["volume_name"]
            or mounts[0].get("Destination") != "/work" or not mounts[0].get("RW")):
        raise ValueError("unexpected host mount or missing private evidence volume")
    boundary["handle"]["isolation_verified"] = True
    boundary["handle"]["isolation_policy"] = "linux-container-v1"


def _heartbeat(boundary, *, force=False):
    if force or time.monotonic() - boundary["last_heartbeat"] >= 2:
        _helper(boundary, 10003, ["control", "heartbeat"], timeout=4)
        boundary["last_heartbeat"] = time.monotonic()


def _sample(boundary):
    snapshot = json.loads(_helper(boundary, 10003, ["snapshot"], timeout=4)[1])
    boundary["last_snapshot"] = snapshot
    for process in snapshot["processes"]:
        key = (process["pid"], process["start_ticks"])
        if len(boundary["observed_processes"]) < 512 or key in boundary["observed_processes"]:
            boundary["observed_processes"][key] = process
    return snapshot


def _usage(boundary):
    data = (boundary.get("last_snapshot") or {}).get("cgroup", {})
    cpu = dict(line.split() for line in data.get("cpu.stat", "").splitlines())
    return {"wall_seconds": max(0, time.monotonic() - boundary.get("start", time.monotonic())),
            "user_cpu_seconds": int(cpu.get("user_usec", 0)) / 1e6,
            "kernel_cpu_seconds": int(cpu.get("system_usec", 0)) / 1e6,
            "peak_memory_bytes": int(data.get("memory.peak", 0)), "cgroup": data,
            "observed_processes": list(boundary["observed_processes"].values()),
            "active_processes": 0 if not boundary["running"] else None,
            "accounting": "aggregate container cgroup v2 including keeper and helpers",
            "usage_scope": "last successful cgroup observation before container termination",
            "complete": False}


def _terminate(boundary):
    try:
        state = _inspect(boundary)["State"]
        if state.get("Running"):
            _command(boundary["execution"], ["kill", boundary["handle"]["container_id"]],
                     check=False)
        state = _inspect(boundary)["State"]
        boundary["running"] = bool(state.get("Running"))
        boundary["final_state"] = state
        return not state.get("Running") and not state.get("Paused")
    except Exception:
        return False


def check_active(boundary):
    callback = _ACTIVE.get(boundary["handle"]["launch_id"])
    if callback:
        callback()
    else:
        if time.monotonic() >= boundary["deadline"]:
            raise TimeoutError("container boundary deadline exhausted")
        if not boundary["running"]:
            raise RuntimeError("container boundary is not active")
        _heartbeat(boundary)


def _relative(name):
    if (not isinstance(name, str) or not name or "\\" in name or "\0" in name
            or PurePosixPath(name).is_absolute()
            or any(p in {"", ".", ".."} for p in name.split("/"))
            or PureWindowsPath(name).drive or PureWindowsPath(name).root
            or len(name.encode()) > 1024 or len(name.split("/")) > 16):
        raise ValueError("invalid relative transfer path")
    for component in name.split("/"):
        stem = component.split(".", 1)[0].upper()
        if (component != component.rstrip(" .")
                or any(ord(ch) < 32 or ch in '<>:"|?*' for ch in component)
                or stem in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem)
                or len(component.encode("utf-16-le")) > 510):
            raise ValueError("output name is not a safe portable filesystem component")
    return name


def _scoped_area(boundary, area):
    scope = boundary.get("orca_scope") if area == "orca" else None
    if scope is not None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", scope):
            raise ValueError("invalid internal ORCA scope")
        return "orca:" + scope
    return area


def stage(boundary, area, files, *, checkpoint=None):
    if area not in {"sealed", "python", "orca", "responses"}:
        raise ValueError("invalid staging area")
    if area in {"sealed", "python"} and boundary.get("python_started"):
        raise ValueError("Python staging is closed once task execution begins")
    for name, source in files.items():
        _relative(name)
        source = Path(source)
        info = source.stat()
        if source.is_symlink() or not source.is_file() or info.st_nlink != 1:
            raise ValueError("staged source must be a private regular file")
        digest = hashlib.sha256()
        with source.open("rb") as reader:
            while block := reader.read(CHUNK):
                digest.update(block)
                if checkpoint:
                    checkpoint()
            reader.seek(0)
            _helper(boundary, _UID[area], ["put", _scoped_area(boundary, area), name,
                                        str(info.st_size), digest.hexdigest()],
                    input_stream=reader, timeout=min(30, boundary["deadline"] - time.monotonic()),
                    checkpoint=lambda: (check_active(boundary), checkpoint() if checkpoint else None))


def read_request(boundary, *, max_bytes, name="request.json"):
    code, data, error = _helper(boundary, 0, ["request", str(max_bytes), name],
                                max_output=max_bytes, check=False)
    if code == 3:
        return None
    if code:
        raise ValueError("mailbox read rejected: " + error[:2048])
    return data


def publish_response(boundary, response, files, *, sequence=None):
    prefix = "files/" if sequence is None else f"files/{sequence:06d}/"
    paths = {name: "/work/responses/" + prefix + _relative(name) for name in files}
    stage(boundary, "responses", {prefix + name: path for name, path in files.items()})
    value = {**response, "files": paths}
    payload = json.dumps(value, allow_nan=False).encode()
    if len(payload) > 1024**2:
        raise ValueError("SDK response exceeds transport budget")
    name = "response.json" if sequence is None else f"response-{sequence:06d}.json"
    _helper(boundary, 0, ["put", "responses", name, str(len(payload)),
                         hashlib.sha256(payload).hexdigest()], input_stream=io.BytesIO(payload))
    return paths


def run_managed(boundary, *, kind, local_workdir, timeout_s, on_started, cancel_requested,
                on_poll=None, parent_handle=None, fault=None, on_actual_started=None):
    del local_workdir  # Host path is used only by explicit staging/collection.
    if kind not in {"python", "orca"}:
        raise ValueError("invalid fixed workload kind")
    nested = parent_handle is not None
    if nested and parent_handle.get("container_id") != boundary["handle"]["container_id"]:
        raise ValueError("internal ORCA must share its parent's exact container")
    start = time.monotonic()
    boundary.setdefault("start", start)
    deadline = min(boundary["deadline"], start + timeout_s)
    result = {"state": "failed", "exit_code": None, "handle": None, "not_started": True,
              "reason": None, "resource_usage": {},
              "actual_start_status": "not_started" if kind == "orca" else "not_applicable"}
    area = kind
    scoped_area = _scoped_area(boundary, area)
    process = None
    transport = tempfile.TemporaryFile()
    previous = _ACTIVE.get(boundary["handle"]["launch_id"])

    def controls():
        if previous:
            previous()
        outcome = boundary.get("control_outcome")
        if not outcome:
            if cancel_requested and cancel_requested():
                outcome = ("cancelled", "cancel_requested")
            elif time.monotonic() >= deadline:
                outcome = ("timed_out", "deadline_exceeded")
            else:
                _heartbeat(boundary)
                if time.monotonic() - boundary.get("sample_time", 0) >= 0.5:
                    values = _sample(boundary)["cgroup"]
                    boundary["sample_time"] = time.monotonic()
                    events = dict(line.split() for line in values["memory.events"].splitlines())
                    if int(events.get("oom", 0)) or int(events.get("oom_kill", 0)):
                        outcome = ("failed", "container_memory_limit_exceeded")
        if outcome:
            boundary["control_outcome"] = outcome
            if not _terminate(boundary):
                boundary["control_outcome"] = ("unknown", "container_termination_unconfirmed")
            raise RuntimeError("managed_parent_control: " + ": ".join(boundary["control_outcome"]))

    try:
        controls()
        args = ["exec", "--user", f"{_UID[area]}:{_UID[area]}", boundary["handle"]["container_id"],
                PYTHON, "-I", HELPER, "launch", kind, str(timeout_s), scoped_area]
        process = subprocess.Popen(_argv(boundary["execution"], args), stdin=subprocess.DEVNULL,
            stdout=transport, stderr=transport, env=_environment(), shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        identity = None
        gate_deadline = min(deadline, time.monotonic() + 12)
        while time.monotonic() < gate_deadline:
            controls()
            code, data, _ = _helper(boundary, _UID[area], ["identity", scoped_area], check=False)
            if code == 0:
                identity = json.loads(data)
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if identity is None:
            raise RuntimeError("gated workload identity unavailable")
        handle = {**boundary["handle"], "kind": kind, "workload_identity": identity,
                  "orca_scope": boundary.get("orca_scope") if kind == "orca" else None,
                  "launch_phase": "gated_launcher",
                  "parent_container_id": parent_handle.get("container_id") if nested else None}
        result.update(handle=handle, not_started=False)
        if fault:
            fault("after_process_created")
        on_started(handle)
        if fault:
            fault("after_handle_saved")
        controls()
        if kind == "orca":
            # A lost gate acknowledgement cannot prove that exec did not happen.
            result["actual_start_status"] = "unconfirmed"
        _helper(boundary, 0, ["gate", scoped_area])
        if kind == "python":
            boundary["python_started"] = True
        _ACTIVE[boundary["handle"]["launch_id"]] = controls
        if fault:
            fault("after_resumed")
        while process.poll() is None:
            controls()
            if kind == "orca" and result["actual_start_status"] != "confirmed":
                code, data, _ = _helper(boundary, _UID[area], ["exec-identity", scoped_area], check=False)
                if code == 0:
                    observed = json.loads(data)
                    proof = observed.get("actual_start_evidence", {})
                    if (observed.get("pid") != identity["pid"]
                            or observed.get("start_ticks") != identity["start_ticks"]
                            or proof.get("executable") != "/opt/orca/orca"
                            or proof.get("pid") != identity["pid"]
                            or proof.get("start_ticks") != identity["start_ticks"]):
                        raise ValueError("actual executable proof differs from gated identity")
                    handle = {**handle, "launch_phase": "executed", "actual_start_evidence": proof}
                    result.update(handle=handle, actual_start_status="confirmed")
                    if on_actual_started:
                        on_actual_started(handle)
                    if fault:
                        fault("after_actual_start_saved")
            if transport.tell() > 65536:
                raise ValueError("Docker workload transport error log exceeded bound")
            if on_poll:
                on_poll(handle)
            time.sleep(0.1)
        controls()
        _helper(boundary, _UID[area], ["quiesce", scoped_area], timeout=5)
        result.update(exit_code=process.returncode,
                      state="completed" if process.returncode == 0 else "failed",
                      reason="workload_tree_exited" if process.returncode == 0 else "nonzero_exit_code")
        if kind == "orca" and result["actual_start_status"] != "confirmed":
            result.update(state="failed", reason="orca_actual_start_unconfirmed")
    except Exception as exc:
        control = boundary.get("control_outcome")
        result.update(state=control[0] if control else "failed",
                      reason=control[1] if control else f"container_backend_error: {exc}")
        if not _terminate(boundary):
            result.update(state="unknown", reason="container_termination_unconfirmed")
    finally:
        if previous is not None:
            _ACTIVE[boundary["handle"]["launch_id"]] = previous
        else:
            _ACTIVE.pop(boundary["handle"]["launch_id"], None)
        if not nested:
            if boundary["running"]:
                try:
                    _sample(boundary)
                except Exception:
                    pass
            if not _terminate(boundary):
                result.update(state="unknown", reason="container_termination_unconfirmed")
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=2)
        final = boundary.get("final_state", {})
        if final.get("OOMKilled") and result["state"] != "unknown":
            result.update(state="failed", reason="container_memory_limit_exceeded")
        result["resource_usage"] = _usage(boundary)
        result["resource_usage"]["oom_killed"] = bool(final.get("OOMKilled"))
        if nested:
            result["resource_usage"]["aggregate_charged_to_parent"] = True
        transport.seek(0)
        result["transport_stderr"] = transport.read(65536).decode("utf-8", "replace")
        transport.close()
    return result


def _decode_export(stream, destination, limits, checkpoint=None, *, deadline=None):
    destination = Path(destination)
    destination_absolute = destination.absolute()
    if any(_is_reparse(parent) for parent in [destination_absolute, *destination_absolute.parents]):
        raise ValueError("linked host output directory")
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    count = total = 0
    result = {}
    names = {}

    def checked():
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError("artifact collection deadline exhausted during decoding")
        if checkpoint:
            checkpoint()

    while True:
        checked()
        raw = stream.read(4)
        if len(raw) != 4:
            raise ValueError("truncated export frame")
        length = struct.unpack("!I", raw)[0]
        if not 0 < length <= 8192:
            raise ValueError("invalid export header length")
        value = json.loads(stream.read(length))
        if value.get("end") is True:
            if value != {"end": True, "count": count, "bytes": total} or stream.read(1):
                raise ValueError("invalid export completion receipt")
            return result
        name = _relative(value["path"])
        for index in range(1, len(name.split("/")) + 1):
            prefix = "/".join(name.split("/")[:index])
            folded = prefix.casefold()
            if folded in names and (names[folded] != prefix or index == len(name.split("/"))):
                raise ValueError("duplicate or case-aliased export path")
            names[folded] = prefix
        size = value["size"]
        if type(size) is not int or size < 0:
            raise ValueError("invalid exported size")
        count += 1
        total += size
        if count > limits["count"] or size > limits["file_bytes"] or total > limits["total_bytes"]:
            raise ValueError("export exceeds frozen artifact budget")
        target = destination.joinpath(*PurePosixPath(name).parts)
        if not target.resolve().is_relative_to(destination):
            raise ValueError("export target escapes its host directory")
        if any(_is_reparse(parent) for parent in [target, *target.parents]):
            raise ValueError("linked host output ancestor")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        remaining = size
        existing = target.exists()
        if existing and (target.is_symlink() or not target.is_file() or target.stat().st_nlink != 1):
            raise ValueError("existing export target is not a private regular file")
        with (tempfile.TemporaryFile() if existing else target.open("xb")) as output:
            while remaining:
                checked()
                block = stream.read(min(CHUNK, remaining))
                if not block:
                    raise ValueError("truncated exported bytes")
                output.write(block)
                digest.update(block)
                remaining -= len(block)
        if stream.read(32) != digest.digest():
            raise ValueError("exported checksum mismatch")
        if existing:
            prior = hashlib.sha256()
            with target.open("rb") as original:
                while block := original.read(CHUNK):
                    prior.update(block)
                    checked()
            if prior.digest() != digest.digest():
                raise ValueError("export conflicts with preserved host input; volume retained")
        result[name] = {"path": str(target), "sha256": digest.hexdigest(), "size": size}


def collect(boundary, area, destination, *, limits, checkpoint=None):
    if area not in {"python", "orca"}:
        raise ValueError("invalid collection area")
    limits = dict(limits)
    deadline = time.monotonic() + limits["seconds"]
    maximum = limits["total_bytes"] + limits["count"] * 8196 + 8196
    args = ["export", _scoped_area(boundary, area), json.dumps(limits, separators=(",", ":"))]
    spool_path = boundary["local_root"] / ("container-export-" + uuid.uuid4().hex + ".bin")
    failure = None
    result = None
    with spool_path.open("x+b") as spool:
        try:
            if boundary["running"]:
                _helper(boundary, _UID[area], args, output_stream=spool, max_output=maximum,
                        timeout=limits["seconds"], checkpoint=checkpoint)
            else:
                # A new fixed collector only; the original computation is never restarted.
                name = "orca-collector-" + uuid.uuid4().hex
                intent = {"name": name, "parent_launch_id": boundary["handle"]["launch_id"],
                          "image_id": boundary["handle"]["image_id"], "area": area,
                          "volume_name": boundary["handle"]["volume_name"],
                          "timeout_seconds": limits["seconds"]}
                intent_path = boundary["local_root"] / (name + ".intent.json")
                with intent_path.open("x", encoding="utf-8") as record:
                    json.dump(intent, record)
                    record.flush()
                    os.fsync(record.fileno())
                command = ["create", "--pull=never", "--name", name, "--network", "none", "--read-only",
                           "--log-driver", "json-file", "--log-opt", "compress=false",
                           "--log-opt", "max-size=1m", "--log-opt", "max-file=1",
                           "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                           "--user", f"{_UID[area]}:{_UID[area]}", "--memory", "128m",
                           "--memory-swap", "128m", "--cpus", "1", "--cpuset-cpus", "0",
                           "--pids-limit", "16", "--restart", "no",
                           "--label", f"orca.owner={boundary['handle']['launch_id']}",
                           "--label", "orca.role=collector",
                           "--mount", f"type=volume,src={boundary['handle']['volume_name']},dst=/work,readonly",
                           "--entrypoint", PYTHON, boundary["handle"]["image_id"], "-I", HELPER, *args]
                collector_id = _command(boundary["execution"], command)[1].decode().strip()
                if not re.fullmatch(r"[0-9a-f]{64}", collector_id):
                    raise ValueError("collector create receipt has no exact identity")
                with intent_path.with_name(name + ".started.json").open("x", encoding="utf-8") as record:
                    json.dump({**intent, "container_id": collector_id}, record)
                    record.flush()
                    os.fsync(record.fileno())
                try:
                    _command(boundary["execution"], ["start", "--attach", collector_id],
                             output_stream=spool, max_output=maximum,
                             timeout=deadline - time.monotonic(), checkpoint=checkpoint)
                finally:
                    _command(boundary["execution"], ["kill", collector_id], check=False)
                    stopped = _json(boundary["execution"], ["container", "inspect", collector_id])[0]
                    if stopped["State"].get("Running") or stopped["State"].get("Paused"):
                        raise RuntimeError("collector termination unconfirmed; environment lease required")
                    _command(boundary["execution"], ["rm", collector_id])
        except Exception as exc:
            failure = exc
        spool.flush()
        spool.seek(0)
        try:
            result = _decode_export(spool, destination, limits, checkpoint, deadline=deadline)
        except Exception as exc:
            failure = failure or exc
    if failure:
        receipt = {"operation": "incomplete_container_export", "area": area,
                   "raw_transport": spool_path.name, "bytes": spool_path.stat().st_size,
                   "container_id": boundary["handle"].get("container_id"),
                   "container_name": boundary["handle"].get("container_name"),
                   "volume_name": boundary["handle"]["volume_name"], "reason": str(failure),
                   "complete": False, "raw_transport_verified": False}
        spool_path.with_suffix(".json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        raise RuntimeError(f"container export incomplete; retained {spool_path.name}: {failure}") from failure
    spool_path.unlink()
    return result


def _is_reparse(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def boundary_from_handle(handle, *, local_root):
    execution = _validate_execution(handle["execution"])
    return {"execution": execution, "handle": dict(handle), "local_root": Path(local_root),
            "orca_scope": handle.get("orca_scope"),
            "resources": {}, "deadline": time.monotonic(), "running": False,
            "observed_processes": {}, "last_snapshot": None}


def reconcile(handle):
    result = {"state": "unknown", "identity_match": None, "active_processes": None,
              "reason": "incomplete_container_identity"}
    try:
        execution = _validate_execution(handle["execution"])
        # Recovery uses the recorded image, not the current helper source version.
        # It only inspects identities and must not upgrade or execute historical code.
        info = _json(execution, ["info", "--format", "{{json .}}"])
        if info.get("OSType") != "linux" or info.get("ID") != handle.get("engine_id"):
            return {**result, "reason": "daemon_or_image_identity_unavailable_or_changed"}
        target = handle.get("container_id") or handle.get("container_name")
        if not target or not handle.get("launch_id"):
            return result
        code, raw, _ = _command(execution, ["container", "inspect", target], check=False)
        if code:
            return {**result, "reason": "recorded_container_unavailable; evidence retained"}
        actual = json.loads(raw)[0]
        match = (actual["Image"] == handle["image_id"]
                 and actual["Config"]["Labels"].get("orca.nonce") == handle["launch_id"]
                 and (not handle.get("container_id") or actual["Id"] == handle["container_id"]))
        if not match:
            return {**result, "identity_match": False, "reason": "container_identity_mismatch"}
        mounts = actual.get("Mounts", [])
        if (len(mounts) != 1 or mounts[0].get("Type") != "volume"
                or mounts[0].get("Name") != handle["volume_name"]
                or mounts[0].get("Destination") != "/work" or not mounts[0].get("RW")):
            return {**result, "identity_match": False, "reason": "container_volume_identity_mismatch"}
        state = actual["State"]
        if state.get("Running") or state.get("Paused") or state.get("Restarting"):
            return {**result, "identity_match": True, "state": "running",
                    "reason": "exact_container_still_active"}
        if state.get("Status") not in {"exited", "dead"} or state.get("Pid") != 0:
            return {**result, "identity_match": True, "reason": "container_start_or_stop_not_final"}
        raw_ids = _command(execution, ["container", "ls", "--all", "--no-trunc",
             "--filter", f"label=orca.owner={handle['launch_id']}",
             "--filter", "label=orca.role=collector", "--format", "{{json .ID}}"],
             max_output=65536)[1]
        ids = [json.loads(line) for line in raw_ids.splitlines() if line.strip()]
        if len(ids) > 32:
            return {**result, "reason": "collector_identity_enumeration_exceeded_bound"}
        collectors = []
        for identity in ids:
            collector = _json(execution, ["container", "inspect", identity])[0]
            labels = collector.get("Config", {}).get("Labels") or {}
            volumes = collector.get("Mounts", [])
            if (collector.get("Id") != identity or collector.get("Image") != handle["image_id"]
                    or labels.get("orca.owner") != handle["launch_id"]
                    or labels.get("orca.role") != "collector" or len(volumes) != 1
                    or volumes[0].get("Type") != "volume"
                    or volumes[0].get("Name") != handle["volume_name"]
                    or volumes[0].get("Destination") != "/work" or volumes[0].get("RW")):
                return {**result, "reason": "collector_identity_or_volume_mismatch"}
            observed = collector["State"]
            if (observed.get("Running") or observed.get("Paused") or observed.get("Restarting")
                    or observed.get("Status") not in {"exited", "dead"} or observed.get("Pid") != 0):
                return {**result, "state": "running", "identity_match": True,
                        "reason": "derived_collector_not_confirmed_terminated", "collector_id": identity}
            collectors.append(identity)
        return {**result, "identity_match": True, "state": "terminated", "active_processes": 0,
                "container_id": actual["Id"], "volume_name": handle["volume_name"],
                "reason": "exact_container_and_collectors_stopped; no_workload_replay",
                "collector_ids": collectors, "docker_state": state}
    except Exception as exc:
        return {**result, "reason": f"container_reconciliation_unavailable: {exc}"}
