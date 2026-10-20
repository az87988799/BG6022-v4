"""Finite execution/collection boundaries with synthetic outcomes, no ORCA."""

import time
from datetime import timedelta
from threading import RLock
from types import SimpleNamespace

import pytest

from orca_agent.backends import container
from orca_agent.config import Config
from orca_agent.models import RunConfiguration, utc_now
from orca_agent.tools import managed
from tests.unit.test_managed_attempt_store import make_task


@pytest.mark.parametrize("policy,running,wants_checkpoint", [
    ("simplified-1", False, False),
    ("simplified-1", True, True),
    (None, False, True),
])
def test_settled_child_collection_has_finite_independent_deadline(
    tmp_path, monkeypatch, policy, running, wants_checkpoint,
):
    configuration = RunConfiguration(workflow_policy=policy)
    run = SimpleNamespace(id="run", deadline=utc_now() + timedelta(seconds=100),
                          budget=SimpleNamespace(artifact_count=4))
    store = SimpleNamespace(
        run_configuration=lambda _: configuration, control_lock=lambda _: RLock(),
        read_signal=lambda _: None)
    boundary = {"running": True, "handle": {"launch_id": "fake"}}
    observed = []
    monkeypatch.setattr("orca_agent.natural.ensure_scientific_environment", lambda *a: None)
    monkeypatch.setattr(managed, "_control", lambda *a: None)
    monkeypatch.setattr(managed, "_retain_input_copies", lambda *a: None)
    monkeypatch.setattr(managed, "_collection_limits", lambda *a: {"seconds": 9})
    monkeypatch.setattr(container, "stage", lambda *a, **k: None)

    def execute(*args, **kwargs):
        boundary["running"] = running
        return {"state": "timed_out" if not running else "completed", "reason": "synthetic",
                "resource_usage": {}, "exit_code": None}

    def collect(*args, limits, checkpoint):
        observed.append((limits["seconds"], checkpoint is not None))

    monkeypatch.setattr(container, "run_managed", execute)
    monkeypatch.setattr(container, "collect", collect)
    result = managed._launch_container_orca(
        store, run, None, None, {"resources": {"timeout_seconds": 10}}, tmp_path, tmp_path,
        on_started=lambda _: None, execution_handle={}, on_prepared=lambda _: None,
        parent_handle={"container_id": "fake"}, boundary=boundary,
        deadline=time.monotonic() + 100, fault=None, on_actual_started=None)
    assert observed == [(9, wants_checkpoint)]
    assert "collection_error" not in result


def test_unknown_child_never_enters_read_only_collection(tmp_path, monkeypatch):
    run = SimpleNamespace(id="run", deadline=utc_now() + timedelta(seconds=100),
                          budget=SimpleNamespace(artifact_count=4))
    store = SimpleNamespace(
        run_configuration=lambda _: RunConfiguration(workflow_policy="simplified-1"),
        control_lock=lambda _: RLock(), read_signal=lambda _: None)
    monkeypatch.setattr("orca_agent.natural.ensure_scientific_environment", lambda *a: None)
    monkeypatch.setattr(managed, "_control", lambda *a: None)
    monkeypatch.setattr(managed, "_retain_input_copies", lambda *a: None)
    monkeypatch.setattr(container, "stage", lambda *a, **k: None)
    monkeypatch.setattr(container, "run_managed", lambda *a, **k: {
        "state": "unknown", "reason": "termination_unconfirmed"})
    monkeypatch.setattr(container, "collect", lambda *a, **k: pytest.fail("unknown collection"))
    result = managed._launch_container_orca(
        store, run, None, None, {"resources": {"timeout_seconds": 10}}, tmp_path, tmp_path,
        on_started=lambda _: None, execution_handle={}, on_prepared=lambda _: None,
        parent_handle={"container_id": "fake"}, boundary={"running": True},
        deadline=time.monotonic() + 100, fault=None, on_actual_started=None)
    assert result["state"] == "unknown"


def test_changed_source_after_reservation_settles_as_known_not_started(tmp_path, monkeypatch):
    from orca_agent import runner

    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    monkeypatch.setattr("orca_agent.file_references.resolve_step_files",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("source changed")))
    result, outcome = managed.execute(store, run, step, attempt, Config(data_root=store.root))
    assert outcome["state"] == "failed" and outcome["not_started"]
    assert "source changed" in outcome["reason"]
    store.save_result(result)
    runner._settle(store, run, attempt, result, outcome)
    assert store.environment_lease() is None
    assert run.attempts[0].state == "failed" and not run.attempts[0].started
    assert run.attempts[0].result_id == result.id
