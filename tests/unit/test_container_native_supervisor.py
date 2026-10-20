"""Synthetic launcher contract checks; no Docker, ORCA or isolation claims."""

import errno
import json
import subprocess
from types import SimpleNamespace

import pytest

from orca_agent.backends.container_runtime import helper

IDENTITY = {"pid": 42, "start_ticks": "123"}
FIXED = SimpleNamespace(st_dev=25, st_ino=321)


@pytest.mark.parametrize("code", [0, 126, 255])
def test_fast_normal_exit_retains_positive_exec_pipe_evidence(monkeypatch, code):
    monkeypatch.setattr(helper, "executable_identity", lambda _identity: (_ for _ in ()).throw(
        ProcessLookupError("short child already exited")))
    receipt = helper.native_start_receipt(SimpleNamespace(poll=lambda: code), IDENTITY, FIXED)
    assert receipt["status"] == "confirmed"
    assert receipt["identity"] == IDENTITY
    assert receipt["actual_start_evidence"] == {
        "method": "trusted_subprocess_exec_error_pipe", "executable": "/opt/orca/orca",
        "device": 25, "inode": 321, **IDENTITY, "exec_error_pipe": "closed_without_error",
        "child_exit_code": code}


@pytest.mark.parametrize("code", [-9, -15, -11])
def test_signal_without_loaded_executable_proof_remains_unconfirmed(monkeypatch, code):
    monkeypatch.setattr(helper, "executable_identity", lambda _identity: (_ for _ in ()).throw(
        ProcessLookupError("child gone")))
    receipt = helper.native_start_receipt(SimpleNamespace(poll=lambda: code), IDENTITY, FIXED)
    assert receipt["status"] == "unconfirmed"
    assert "actual_start_evidence" not in receipt


def test_loaded_executable_proof_precedes_later_signal(monkeypatch):
    proof = {**IDENTITY, "method": "proc_exe_inode_and_pid_start_ticks",
             "executable": "/opt/orca/orca", "device": 25, "inode": 321}
    monkeypatch.setattr(helper, "executable_identity", lambda _identity: proof)
    receipt = helper.native_start_receipt(SimpleNamespace(poll=lambda: -9), IDENTITY, FIXED)
    assert receipt["status"] == "confirmed" and receipt["actual_start_evidence"] == proof


def simulate_supervisor(monkeypatch, tmp_path, *, failure=None):
    closed, calls = [], []
    monkeypatch.setattr(helper.os, "pipe2", lambda _flags: (100, 101), raising=False)
    monkeypatch.setattr(helper.os, "O_CLOEXEC", 0, raising=False)
    monkeypatch.setattr(helper.os, "close", closed.append)
    monkeypatch.setattr(helper.os, "read", lambda _fd, _limit: json.dumps(IDENTITY).encode())
    monkeypatch.setattr(helper, "Path", lambda _path: SimpleNamespace(stat=lambda: FIXED))
    monkeypatch.setattr(helper, "gated_child", lambda *args: calls.append(("gated_child", args)))
    monkeypatch.setattr(helper, "executable_identity", lambda _identity: (_ for _ in ()).throw(
        ProcessLookupError("short child already exited")))

    def popen(argv, **kwargs):
        calls.append(("popen", argv, kwargs))
        kwargs["preexec_fn"]()
        if failure is not None:
            raise failure
        return SimpleNamespace(pid=42, poll=lambda: 126, wait=lambda: 126)

    monkeypatch.setattr(helper.subprocess, "Popen", popen)
    return closed, calls


def test_supervisor_keeps_management_output_out_of_child_fds(monkeypatch, tmp_path, capsys):
    closed, calls = simulate_supervisor(monkeypatch, tmp_path)
    code = helper.supervise_orca(tmp_path, "orca", 30, {"LANG": "C.UTF-8"})
    assert code == 126
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "confirmed" and receipt["identity"] == IDENTITY
    _, argv, kwargs = calls[0]
    assert argv == ["/opt/orca/orca", "job.inp"]
    assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["close_fds"] is True
    assert kwargs["pass_fds"] == (101,)
    assert kwargs["stdout"].name == str(tmp_path / "stdout.out")
    assert kwargs["stderr"].name == str(tmp_path / "stderr.txt")
    assert calls[1] == ("gated_child", (tmp_path, "orca", 30, 101))
    assert 100 in closed and 101 in closed


@pytest.mark.parametrize("failure", [
    FileNotFoundError(errno.ENOENT, "not exposed", "/opt/orca/orca"),
    PermissionError(errno.EACCES, "not exposed", "/opt/orca/orca"),
    subprocess.SubprocessError("Exception occurred in preexec_fn."),
])
def test_only_child_exec_pipe_failure_proves_not_started(monkeypatch, tmp_path, capsys, failure):
    simulate_supervisor(monkeypatch, tmp_path, failure=failure)
    assert helper.supervise_orca(tmp_path, "orca", 30, {}) == 126
    raw = capsys.readouterr().out
    receipt = json.loads(raw)
    assert receipt["status"] == "not_started"
    assert receipt["error"]["source"] == "subprocess_child_exec_error_pipe"
    assert "not exposed" not in raw


def test_parent_pipe_error_never_becomes_a_not_started_receipt(monkeypatch, tmp_path, capsys):
    simulate_supervisor(monkeypatch, tmp_path, failure=OSError(errno.EIO, "parent transport"))
    with pytest.raises(OSError, match="parent transport"):
        helper.supervise_orca(tmp_path, "orca", 30, {})
    assert capsys.readouterr().out == ""
