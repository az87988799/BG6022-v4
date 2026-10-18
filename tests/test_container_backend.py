"""Offline protocol tests only; none claim a real container boundary passed."""

import hashlib
import io
import json
import struct
import time

import pytest

from orca_agent.backends import container

LIMITS = {"count": 8, "file_bytes": 100, "total_bytes": 200, "seconds": 5}


def frame(value):
    data = json.dumps(value).encode()
    return struct.pack("!I", len(data)) + data


def export(*files):
    data = b""
    for name, content in files:
        data += frame({"path": name, "size": len(content)}) + content + hashlib.sha256(content).digest()
    return io.BytesIO(data + frame({"end": True, "count": len(files),
                                   "bytes": sum(len(content) for _, content in files)}))


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/escape", "C:relative",
                                  "name:stream", "AUX.txt", "con", "COM1", "LPT².txt",
                                  "trailing.", "trailing ", "a//b", "a\\b", "a/./b"])
def test_export_rejects_cross_platform_path_aliases(tmp_path, path):
    with pytest.raises(ValueError):
        container._decode_export(export((path, b"x")), tmp_path / "output", LIMITS)
    assert not any(path.is_file() for path in tmp_path.rglob("*"))


def test_export_checks_digest_and_keeps_valid_prefix(tmp_path):
    payload = export(("first", b"complete"), ("second", b"partial")).getvalue()
    payload = payload[:-55]
    with pytest.raises((ValueError, json.JSONDecodeError)):
        container._decode_export(io.BytesIO(payload), tmp_path, LIMITS)
    assert (tmp_path / "first").read_bytes() == b"complete"


def test_existing_input_is_verified_without_overwrite(tmp_path):
    original = tmp_path / "job.inp"
    original.write_bytes(b"immutable")
    result = container._decode_export(export(("job.inp", b"immutable")), tmp_path, LIMITS)
    assert result["job.inp"]["sha256"] == hashlib.sha256(b"immutable").hexdigest()
    with pytest.raises(ValueError, match="conflicts"):
        container._decode_export(export(("job.inp", b"changed")), tmp_path, LIMITS)
    assert original.read_bytes() == b"immutable"


@pytest.mark.parametrize("names", [("a", "a"), ("A", "a"), ("Dir/a", "dir/b")])
def test_duplicate_and_casefold_conflicts_rejected(tmp_path, names):
    with pytest.raises(ValueError, match="case-aliased"):
        container._decode_export(export(*((name, b"x") for name in names)), tmp_path, LIMITS)


def test_export_budget_and_deadline_apply_to_decode(tmp_path):
    with pytest.raises(TimeoutError, match="deadline"):
        container._decode_export(export(("a", b"x")), tmp_path / "expired", LIMITS,
                                 deadline=time.monotonic() - 1)
    with pytest.raises(ValueError, match="budget"):
        container._decode_export(export(("a", b"x" * 101)), tmp_path / "oversize", LIMITS)


def test_invalid_completion_is_not_success(tmp_path):
    payload = export(("a", b"x")).getvalue() + b"unexpected"
    with pytest.raises(ValueError, match="completion"):
        container._decode_export(io.BytesIO(payload), tmp_path, LIMITS)


def test_aggregate_usage_is_an_explicit_lower_bound():
    result = container._usage({"running": False, "observed_processes": {}, "last_snapshot": {
        "cgroup": {"cpu.stat": "user_usec 1000000\nsystem_usec 500000", "memory.peak": "123"}}})
    assert result["complete"] is False
    assert result["user_cpu_seconds"] == 1
    assert result["kernel_cpu_seconds"] == 0.5
    assert result["active_processes"] == 0


def test_reconcile_never_releases_for_active_derived_collector(monkeypatch, tmp_path):
    executable = tmp_path / "docker.exe"
    executable.write_bytes(b"test stub, not executed")
    image = "sha256:" + "a" * 64
    execution = {"backend": "linux-container", "image_id": image,
                 "manifest_sha256": "b" * 64, "docker_context": "desktop-linux",
                 "docker_path": str(executable)}
    handle = {"execution": execution, "engine_id": "engine", "launch_id": "nonce",
              "container_id": "primary", "image_id": image, "volume_name": "volume"}
    mount = {"Type": "volume", "Name": "volume", "Destination": "/work", "RW": True}
    primary = {"Id": "primary", "Image": image, "Config": {"Labels": {"orca.nonce": "nonce"}},
               "Mounts": [mount], "State": {"Status": "exited", "Running": False, "Pid": 0}}
    collector = {"Id": "collector", "Image": image,
                 "Config": {"Labels": {"orca.owner": "nonce", "orca.role": "collector"}},
                 "Mounts": [{**mount, "RW": False}],
                 "State": {"Status": "running", "Running": True, "Pid": 42}}

    def response(_execution, args, **kwargs):
        if args[0] == "info":
            return 0, json.dumps({"ID": "engine", "OSType": "linux"}).encode(), ""
        if args[:2] == ["container", "ls"]:
            return 0, b'"collector"\n', ""
        return 0, json.dumps([primary if args[-1] == "primary" else collector]).encode(), ""

    monkeypatch.setattr(container, "_command", response)
    result = container.reconcile(handle)
    assert result["state"] == "running"
    assert result["reason"] == "derived_collector_not_confirmed_terminated"
    collector["State"] = {"Status": "exited", "Running": False, "Pid": 0}
    assert container.reconcile(handle)["state"] == "terminated"
    collector["Mounts"][0]["Name"] = "unrelated"
    assert container.reconcile(handle)["state"] == "unknown"
    primary["State"]["Status"] = "created"
    handle["container_name"] = handle.pop("container_id")
    created = container.reconcile(handle)
    assert created["state"] == "unknown"
    assert created["identity_match"] is True
    assert created["reason"] == "container_start_or_stop_not_final"


def test_failed_export_without_create_ack_preserves_original_diagnostic(monkeypatch, tmp_path):
    boundary = {"handle": {"launch_id": "intent-only", "image_id": "immutable-image",
                            "volume_name": "evidence", "container_name": "original-name"},
                "execution": {}, "local_root": tmp_path, "running": False}

    def unavailable(*args, **kwargs):
        raise RuntimeError("original transport failure")

    monkeypatch.setattr(container, "_command", unavailable)
    with pytest.raises(RuntimeError, match="original transport failure"):
        container.collect(boundary, "orca", tmp_path / "collected", limits=LIMITS)
    receipt = json.loads(next(tmp_path.glob("container-export-*.json")).read_text())
    assert receipt["container_id"] is None
    assert receipt["container_name"] == "original-name"
    assert receipt["reason"] == "original transport failure"
    assert (tmp_path / receipt["raw_transport"]).is_file()


@pytest.mark.parametrize("observed", [False, True])
def test_actual_start_requires_loaded_executable_proof_before_poll(monkeypatch, tmp_path, observed):
    """Exercise callback accounting only; fake transport is not isolation evidence."""
    events = []
    identity = {"pid": 42, "start_ticks": "123"}
    proof = {**identity, "executable": "/opt/orca/orca", "method": "test_fixture"}
    boundary = {"handle": {"launch_id": "accounting", "container_id": "container"},
                "execution": {}, "deadline": time.monotonic() + 60, "running": True,
                "observed_processes": {}}

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = 1

        def wait(self, timeout):
            return self.returncode

    process = Process()

    def helper(_boundary, _uid, args, **kwargs):
        if args[0] == "identity":
            return 0, json.dumps(identity).encode(), ""
        if args[0] == "exec-identity":
            process.returncode = 0
            return (0 if observed else 1,
                    json.dumps({**identity, "actual_start_evidence": proof}).encode(), "")
        events.append(args[0])
        return 0, b"{}", ""

    def terminate(value):
        value["running"] = False
        return True

    monkeypatch.setattr(container.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(container, "_argv", lambda *a: [])
    monkeypatch.setattr(container, "_helper", helper)
    monkeypatch.setattr(container, "_heartbeat", lambda *a: None)
    monkeypatch.setattr(container, "_sample", lambda *a: {"cgroup": {"memory.events": "oom 0"}})
    monkeypatch.setattr(container, "_terminate", terminate)
    monkeypatch.setattr(container.time, "sleep", lambda *a: None)
    handles = []

    def gated(handle):
        handles.append(handle)
        events.append("gated")

    def actual(handle):
        handles.append(handle)
        events.append("actual")

    result = container.run_managed(boundary, kind="orca", local_workdir=tmp_path, timeout_s=30,
                                   on_started=gated, on_actual_started=actual,
                                   cancel_requested=lambda: False,
                                   on_poll=lambda h: events.append("poll"))
    assert handles[0]["launch_phase"] == "gated_launcher"
    assert "actual_start_evidence" not in handles[0]
    assert events.index("gated") < events.index("gate")
    if observed:
        assert events.index("actual") < events.index("poll")
        assert handles[1]["launch_phase"] == "executed"
        assert result["actual_start_status"] == "confirmed"
        assert result["state"] == "completed"
    else:
        assert "actual" not in events
        assert result["actual_start_status"] == "unconfirmed"
        assert result["reason"] == "orca_actual_start_unconfirmed"
        assert result["state"] == "failed"
