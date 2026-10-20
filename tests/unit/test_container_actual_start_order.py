"""Synthetic Docker transport timing, never a real isolation/start proof."""

import json
import time

import pytest

from orca_agent.backends import container


def execute(monkeypatch, tmp_path, *, already_exited=False, proof_available=True,
            changed_identity=False, receipt_override=None):
    events, actual = [], []
    handle = {"container_id": "c" * 64, "launch_id": "d" * 32}
    boundary = {"handle": handle, "execution": {}, "deadline": time.monotonic() + 100,
        "last_heartbeat": time.monotonic(), "sample_time": 0, "observed_processes": {},
        "running": True, "last_snapshot": {"cgroup": {}}}
    gated = {"pid": 42, "start_ticks": "123", "uid": 10002, "name": "python3.11"}
    proof = {"pid": 43 if changed_identity else 42, "start_ticks": "123", "executable": "/opt/orca/orca",
             "method": "trusted_subprocess_exec_error_pipe", "device": 25, "inode": 321,
             "exec_error_pipe": "closed_without_error", "child_exit_code": 126}
    # A retained name-only sample must never become an inode start proof.
    boundary["observed_processes"][(42, "123")] = {**gated, "name": "orca"}

    class Process:
        returncode = 126

        def __init__(self):
            self.first = True

        def poll(self):
            if self.first and not already_exited:
                self.first = False
                return None
            return 126

    transport = None

    def popen(*_args, **kwargs):
        nonlocal transport
        transport = kwargs["stdout"]
        return Process()

    def helper(_boundary, _uid, args, **_kwargs):
        events.append(args[0])
        if args[0] == "identity":
            return 0, json.dumps(gated).encode(), ""
        if args[0] == "exec-identity":
            pytest.fail("host must not race /proc through another Docker exec")
        if args[0] == "gate" and proof_available:
            receipt = {"version": 1, "kind": "orca", "identity": gated,
                       "status": "confirmed", "actual_start_evidence": proof}
            raw = receipt_override if receipt_override is not None else json.dumps(receipt).encode() + b"\n"
            transport.write(raw)
            transport.flush()
        return 0, b"", ""

    def sample(_boundary):
        events.append("sample")
        return {"cgroup": {"memory.events": "oom 0\noom_kill 0"}}

    monkeypatch.setattr(container, "_helper", helper)
    monkeypatch.setattr(container, "_sample", sample)
    monkeypatch.setattr(container, "_heartbeat", lambda *_args, **_kwargs: events.append("heartbeat"))
    monkeypatch.setattr(container, "_terminate", lambda _boundary: True)
    monkeypatch.setattr(container, "_argv", lambda *_args: ["synthetic-docker"])
    monkeypatch.setattr(container.subprocess, "Popen", popen)
    monkeypatch.setattr(container.time, "sleep", lambda _: None)
    monkeypatch.setitem(container._ACTIVE, handle["launch_id"], lambda: events.append("parent_controls"))
    result = container.run_managed(boundary, kind="orca", local_workdir=tmp_path / "task", timeout_s=30,
        on_started=lambda _handle: events.append("started"), cancel_requested=lambda: False,
        parent_handle=handle, on_actual_started=lambda handle: (actual.append(handle), events.append("actual")))
    return result, events[events.index("gate") + 1:], actual


def test_durable_exec_receipt_precedes_parent_control_and_sampling(monkeypatch, tmp_path):
    result, events, actual = execute(monkeypatch, tmp_path)
    assert events[0] == "actual"
    assert events.index("actual") < events.index("parent_controls")
    assert result["state"] == "failed" and result["actual_start_status"] == "confirmed"
    assert result["reason"] == "nonzero_exit_code"
    assert len(actual) == 1
    assert actual[0]["actual_start_evidence"]["method"] == "trusted_subprocess_exec_error_pipe"


def test_fast_exit_126_is_confirmed_after_transport_already_exited(monkeypatch, tmp_path):
    result, events, actual = execute(monkeypatch, tmp_path, already_exited=True)
    assert events[0] == "actual"
    assert events.index("actual") < events.index("quiesce")
    assert result["state"] == "failed" and result["exit_code"] == 126 and len(actual) == 1
    raw = (tmp_path / "orca-launch-transport.json").read_bytes()
    assert json.loads(raw)["actual_start_evidence"] == actual[0]["actual_start_evidence"]
    assert actual[0]["launch_receipt_sha256"] == container.hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("already_exited", [False, True])
def test_name_sample_never_substitutes_for_missing_receipt(monkeypatch, tmp_path, already_exited):
    result, events, actual = execute(monkeypatch, tmp_path, already_exited=already_exited, proof_available=False)
    assert result["state"] == "failed" and result["reason"] == "orca_actual_start_unconfirmed"
    assert result["exit_code"] == 126 and not actual
    assert "exec-identity" not in events


def test_final_observation_rejects_changed_process_identity(monkeypatch, tmp_path):
    result, _events, actual = execute(monkeypatch, tmp_path, already_exited=True, changed_identity=True)
    assert result["state"] == "failed"
    assert "differs from gated identity" in result["reason"]
    assert not actual


@pytest.mark.parametrize("raw", [b'{"status":"confirmed"}', b'{}\n{}\n', b'x' * 8193])
def test_incomplete_multiple_or_oversized_records_never_confirm_start(monkeypatch, tmp_path, raw):
    result, _events, actual = execute(monkeypatch, tmp_path, already_exited=True, receipt_override=raw)
    assert result["actual_start_status"] == "unconfirmed" and not actual
    assert result["not_started"] is False


def test_bound_exec_failure_receipt_records_not_started(monkeypatch, tmp_path):
    raw = json.dumps({"version": 1, "kind": "orca", "identity": {"pid": 42, "start_ticks": "123"},
                     "status": "not_started", "reason": "native_exec_rejected", "error": {
                         "type": "PermissionError", "errno": 13,
                         "source": "subprocess_child_exec_error_pipe"}}).encode() + b"\n"
    result, _events, actual = execute(monkeypatch, tmp_path, already_exited=True, receipt_override=raw)
    assert result["actual_start_status"] == "not_started" and result["not_started"] is True
    assert result["state"] == "failed" and result["reason"] == "orca_exec_failed" and not actual
