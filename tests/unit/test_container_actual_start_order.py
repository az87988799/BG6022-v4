"""Synthetic Docker transport timing, never a real isolation/start proof."""

import json
import time

import pytest

from orca_agent.backends import container


def execute(monkeypatch, *, already_exited=False, proof_available=True, changed_identity=False):
    events, actual = [], []
    handle = {"container_id": "c" * 64, "launch_id": "d" * 32}
    boundary = {"handle": handle, "execution": {}, "deadline": time.monotonic() + 100,
        "last_heartbeat": time.monotonic(), "sample_time": 0, "observed_processes": {},
        "running": True, "last_snapshot": {"cgroup": {}}}
    gated = {"pid": 42, "start_ticks": "123", "uid": 10002, "name": "python3.11"}
    proof = {"pid": 43 if changed_identity else 42, "start_ticks": "123", "executable": "/opt/orca/orca",
             "method": "proc_exe_inode_and_pid_start_ticks", "device": 25, "inode": 321}
    # A retained name-only sample must never become an inode start proof.
    boundary["observed_processes"][(42, "123")] = {**gated, "name": "orca"}

    class Process:
        returncode = 0

        def __init__(self):
            self.first = True

        def poll(self):
            if self.first and not already_exited:
                self.first = False
                return None
            return 0

    def helper(_boundary, _uid, args, **_kwargs):
        events.append(args[0])
        if args[0] == "identity":
            return 0, json.dumps(gated).encode(), ""
        if args[0] == "exec-identity":
            return ((0, json.dumps({**gated, "actual_start_evidence": proof}).encode(), "")
                    if proof_available else (1, b"", "process already exited"))
        return 0, b"", ""

    def sample(_boundary):
        events.append("sample")
        return {"cgroup": {"memory.events": "oom 0\noom_kill 0"}}

    monkeypatch.setattr(container, "_helper", helper)
    monkeypatch.setattr(container, "_sample", sample)
    monkeypatch.setattr(container, "_heartbeat", lambda *_args, **_kwargs: events.append("heartbeat"))
    monkeypatch.setattr(container, "_terminate", lambda _boundary: True)
    monkeypatch.setattr(container, "_argv", lambda *_args: ["synthetic-docker"])
    monkeypatch.setattr(container.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(container.time, "sleep", lambda _: None)
    monkeypatch.setitem(container._ACTIVE, handle["launch_id"], lambda: events.append("parent_controls"))
    result = container.run_managed(boundary, kind="orca", local_workdir=None, timeout_s=30,
        on_started=lambda _handle: events.append("started"), cancel_requested=lambda: False,
        parent_handle=handle, on_actual_started=actual.append)
    return result, events[events.index("gate") + 1:], actual


def test_actual_inode_observation_precedes_expensive_parent_control_and_sampling(monkeypatch):
    result, events, actual = execute(monkeypatch)
    assert events[0] == "exec-identity"
    assert events.index("exec-identity") < events.index("parent_controls")
    assert result["state"] == "completed" and result["actual_start_status"] == "confirmed"
    assert len(actual) == 1
    assert actual[0]["actual_start_evidence"]["method"] == "proc_exe_inode_and_pid_start_ticks"


def test_transport_exit_still_gets_one_final_strong_observation(monkeypatch):
    result, events, actual = execute(monkeypatch, already_exited=True)
    assert events[0] == "exec-identity"
    assert events.count("exec-identity") == 1
    assert events.index("exec-identity") < events.index("quiesce")
    assert result["state"] == "completed" and len(actual) == 1


@pytest.mark.parametrize("already_exited", [False, True])
def test_name_sample_or_zero_exit_never_substitutes_for_missing_inode_proof(monkeypatch, already_exited):
    result, events, actual = execute(monkeypatch, already_exited=already_exited, proof_available=False)
    assert result["state"] == "failed" and result["reason"] == "orca_actual_start_unconfirmed"
    assert result["exit_code"] == 0 and not actual
    assert events.count("exec-identity") == (1 if already_exited else 2)


def test_final_observation_rejects_changed_process_identity(monkeypatch):
    result, _events, actual = execute(monkeypatch, already_exited=True, changed_identity=True)
    assert result["state"] == "failed"
    assert "differs from gated identity" in result["reason"]
    assert not actual
