"""Synthetic durable receipts only; these tests never invoke Docker or ORCA."""

import copy
import hashlib
import json

import pytest

from orca_agent import runner
from orca_agent.backends import container
from orca_agent.config import Config
from orca_agent.store import atomic_write
from orca_agent.tools import managed
from tests.unit.test_managed_attempt_store import make_task


def stored_start(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch, tool="orca.run")
    attempt = store.reserve_attempt(run, step)
    gated = {"backend": "linux-container", "launch_phase": "gated_launcher", "kind": "orca",
             "container_id": "c" * 64, "launch_id": "d" * 32,
             "image_id": "sha256:" + "e" * 64, "manifest_sha256": "f" * 64,
             "engine_id": "synthetic-engine", "run_id": run.id, "attempt_id": attempt.id,
             "execution": {"backend": "linux-container", "synthetic_receipt": True},
             "workload_identity": {"pid": 42, "start_ticks": "12345"}}
    actual = {**gated, "launch_phase": "executed", "actual_start_evidence": {
        **gated["workload_identity"], "executable": "/opt/orca/orca",
        "method": "proc_exe_inode_and_pid_start_ticks", "device": 25, "inode": 321}}
    root = store.path(attempt.directory)
    atomic_write(root / "started.json", json.dumps(gated).encode(), immutable=True)
    attempt.execution_handle = gated
    attempt.state = "running"
    store.update_lease_handle(run.id, attempt.id, gated)
    store.save_run(run)
    monkeypatch.setattr(container, "run_managed", lambda *a, **k: pytest.fail("unexpected workload launch"))
    monkeypatch.setattr(container, "prepare_boundary", lambda *a, **k: pytest.fail("unexpected boundary launch"))
    monkeypatch.setattr(managed, "launch_orca", lambda *a, **k: pytest.fail("unexpected ORCA launch"))
    return store, run, step, attempt, gated, actual


@pytest.mark.parametrize("run_handle_saved", [False, True])
@pytest.mark.parametrize("proof_method", ["proc_exe_inode_and_pid_start_ticks",
                                         "trusted_subprocess_exec_error_pipe"])
def test_recovery_after_positive_start_receipt_counts_once_without_replay(
    tmp_path, monkeypatch, run_handle_saved, proof_method,
):
    store, run, step, attempt, gated, actual = stored_start(tmp_path, monkeypatch)
    root = store.path(attempt.directory)
    if proof_method == "trusted_subprocess_exec_error_pipe":
        actual["actual_start_evidence"].update(method=proof_method,
            exec_error_pipe="closed_without_error", child_exit_code=126)
        # The private child identity is sampled while running; the host sees
        # the same PID/start ticks sleeping at its gate. State is not identity.
        raw = json.dumps({"version": 1, "kind": "orca", "identity": {
                              **gated["workload_identity"], "state": "R (running)"},
                          "status": "confirmed",
                          "actual_start_evidence": actual["actual_start_evidence"]}).encode() + b"\n"
        atomic_write(root / "orca-launch-transport.json", raw, immutable=True)
        actual["launch_receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    atomic_write(root / "actual-start.json", json.dumps(actual).encode(), immutable=True)
    if run_handle_saved:
        attempt.execution_handle = actual
        store.update_lease_handle(run.id, attempt.id, actual)
        store.save_run(run)
    original_gated = (root / "started.json").read_bytes()
    original_actual = (root / "actual-start.json").read_bytes()
    observed = []

    def reconcile(handle):
        observed.append(handle)
        return {"state": "terminated", "reason": "synthetic exact container stopped",
                "active_processes": 0}

    monkeypatch.setattr(container, "reconcile", reconcile)
    monkeypatch.setattr(container, "boundary_from_handle", lambda handle, **kw: {"handle": handle})
    monkeypatch.setattr(container, "collect", lambda *a, **kw: {})
    config = Config(data_root=store.root)
    assert runner._recover(store, config, run, store.load_plan(run)) is True
    persisted = store.load_run(run.id)
    assert observed and all(handle == actual for handle in observed)
    assert persisted.usage.orca_starts_reserved == persisted.usage.orca_starts_actual == 1
    assert persisted.attempts[0].started is True
    assert persisted.attempts[0].state == "failed"
    assert persisted.usage.resource_usage_complete is False
    assert store.environment_lease() is None
    assert (root / "started.json").read_bytes() == original_gated
    assert (root / "actual-start.json").read_bytes() == original_actual
    assert not (root / "execution.json").exists()
    assert runner._recover(store, config, persisted, store.load_plan(persisted)) is True
    assert store.load_run(run.id).usage.orca_starts_actual == 1
    assert len(observed) == 2  # One termination check before collection and one after it.


@pytest.mark.parametrize("field,value", [
    ("container_id", "0" * 64),
    ("launch_id", "1" * 32),
    ("attempt_id", "another-attempt"),
    ("execution", {"backend": "linux-container", "synthetic_receipt": "different"}),
    ("workload_identity.pid", 43),
    ("workload_identity.start_ticks", "12346"),
    ("actual_start_evidence.executable", "/usr/bin/python3.11"),
    ("actual_start_evidence.method", "unproven_claim"),
    ("actual_start_evidence.pid", 43),
    ("actual_start_evidence.start_ticks", "12346"),
    ("actual_start_evidence", None),
])
def test_recovery_rejects_conflicting_actual_start_and_retains_lease_and_budget(
    tmp_path, monkeypatch, field, value,
):
    store, run, step, attempt, gated, actual = stored_start(tmp_path, monkeypatch)
    changed = copy.deepcopy(actual)
    if "." in field:
        section, key = field.split(".")
        changed[section][key] = value
    else:
        changed[field] = value
    path = store.path(attempt.directory) / "actual-start.json"
    path.write_text(json.dumps(changed), encoding="utf-8")
    original = path.read_bytes()
    lease = store.environment_lease()
    monkeypatch.setattr(container, "reconcile", lambda *a, **kw: pytest.fail("invalid receipt reconciled"))
    monkeypatch.setattr(container, "collect", lambda *a, **kw: pytest.fail("invalid receipt collected"))
    with pytest.raises(ValueError, match="actual-start receipt differs"):
        runner._recover(store, Config(data_root=store.root), run, store.load_plan(run))
    persisted = store.load_run(run.id)
    assert persisted.usage.orca_starts_reserved == 1
    assert persisted.usage.orca_starts_actual == 0
    assert persisted.attempts[0].finished_at is None
    assert persisted.attempts[0].execution_handle == gated
    assert store.environment_lease() == lease
    assert path.read_bytes() == original
    assert not persisted.result_ids


@pytest.mark.parametrize("damage", ["signal_exit", "missing_transport", "changed_transport", "wrong_scope"])
def test_exec_pipe_recovery_rejects_unbound_or_nonpositive_evidence(tmp_path, monkeypatch, damage):
    store, run, step, attempt, gated, actual = stored_start(tmp_path, monkeypatch)
    actual["actual_start_evidence"].update(method="trusted_subprocess_exec_error_pipe",
        exec_error_pipe="closed_without_error", child_exit_code=-9 if damage == "signal_exit" else 126)
    raw = json.dumps({"version": 1, "kind": "orca", "identity": {
                          **gated["workload_identity"], "state": "R (running)"},
                      "status": "confirmed", "actual_start_evidence": actual["actual_start_evidence"]
                      }).encode() + b"\n"
    actual["launch_receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    if damage == "wrong_scope":
        actual["orca_scope"] = "unrelated-native-call"
    root = store.path(attempt.directory)
    if damage != "missing_transport":
        atomic_write(root / "orca-launch-transport.json", raw + (b" " if damage == "changed_transport" else b""),
                     immutable=True)
    atomic_write(root / "actual-start.json", json.dumps(actual).encode(), immutable=True)
    monkeypatch.setattr(container, "reconcile", lambda *a, **kw: pytest.fail("invalid receipt reconciled"))
    with pytest.raises(ValueError, match="actual-start receipt differs"):
        runner._recover(store, Config(data_root=store.root), run, store.load_plan(run))
    persisted = store.load_run(run.id)
    assert persisted.usage.orca_starts_actual == 0 and persisted.usage.orca_starts_reserved == 1
    assert persisted.attempts[0].finished_at is None and store.environment_lease() is not None


def test_recovery_persists_complete_launcher_proof_before_reconciliation(tmp_path, monkeypatch):
    store, run, step, attempt, gated, actual = stored_start(tmp_path, monkeypatch)
    root = store.path(attempt.directory)
    actual["actual_start_evidence"].update(method="trusted_subprocess_exec_error_pipe",
        exec_error_pipe="closed_without_error", child_exit_code=126)
    raw = json.dumps({"version": 1, "kind": "orca", "identity": gated["workload_identity"],
                      "status": "confirmed", "actual_start_evidence": actual["actual_start_evidence"]
                      }).encode() + b"\n"
    atomic_write(root / "orca-launch-transport.json", raw, immutable=True)
    actual["launch_receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    assert not (root / "actual-start.json").exists()
    observed = []

    def reconcile(handle):
        # The recovered proof is durable before any further managed operation.
        observed.append(handle)
        assert json.loads((root / "actual-start.json").read_bytes()) == actual == handle
        return {"state": "terminated", "reason": "synthetic exact container stopped",
                "active_processes": 0}

    monkeypatch.setattr(container, "reconcile", reconcile)
    monkeypatch.setattr(container, "boundary_from_handle", lambda handle, **kw: {"handle": handle})
    monkeypatch.setattr(container, "collect", lambda *a, **kw: {})
    assert runner._recover(store, Config(data_root=store.root), run, store.load_plan(run)) is True
    persisted = store.load_run(run.id)
    assert persisted.usage.orca_starts_actual == persisted.usage.orca_starts_reserved == 1
    assert persisted.attempts[0].started is True
    assert (root / "orca-launch-transport.json").read_bytes() == raw
    assert len(observed) == 2


@pytest.mark.parametrize("source", ["task_file", "incomplete", "wrong_identity"])
def test_recovery_never_promotes_task_incomplete_or_mismatched_transport(tmp_path, monkeypatch, source):
    store, run, step, attempt, gated, actual = stored_start(tmp_path, monkeypatch)
    root = store.path(attempt.directory)
    actual["actual_start_evidence"].update(method="trusted_subprocess_exec_error_pipe",
        exec_error_pipe="closed_without_error", child_exit_code=126)
    identity = {**gated["workload_identity"]}
    if source == "wrong_identity":
        identity["pid"] += 1
    raw = json.dumps({"version": 1, "kind": "orca", "identity": identity,
                      "status": "confirmed", "actual_start_evidence": actual["actual_start_evidence"]
                      }).encode() + b"\n"
    target = root / "task" if source == "task_file" else root
    target.mkdir(exist_ok=True)
    atomic_write(target / "orca-launch-transport.json", raw[:-1] if source == "incomplete" else raw,
                 immutable=True)

    def reconcile(handle):
        assert handle == gated and not (root / "actual-start.json").exists()
        return {"state": "terminated", "reason": "synthetic exact container stopped",
                "active_processes": 0}

    monkeypatch.setattr(container, "reconcile", reconcile)
    monkeypatch.setattr(container, "boundary_from_handle", lambda handle, **kw: {"handle": handle})
    monkeypatch.setattr(container, "collect", lambda *a, **kw: {})
    if source == "wrong_identity":
        with pytest.raises(ValueError, match="actual-start receipt differs"):
            runner._recover(store, Config(data_root=store.root), run, store.load_plan(run))
    else:
        assert runner._recover(store, Config(data_root=store.root), run, store.load_plan(run)) is True
    persisted = store.load_run(run.id)
    assert persisted.usage.orca_starts_actual == 0 and persisted.usage.orca_starts_reserved == 1
    assert not (root / "actual-start.json").exists()
