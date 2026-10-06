"""Isolated prelaunch accounting faults; never use live ledgers or processes."""

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.models import Result
from orca_agent.store import Store, StoreError
from orca_agent.tools import electronic
from tests.unit.test_phase_b_budget import bind_model, budget, make_run, record, settle_model


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "no-delivered.json")
    store = Store(tmp_path / "data", environment_root=tmp_path / "isolated-environment")
    book = budget.AcceptanceBudget(store)
    run, step, geometry = make_run(store, science=True)
    store._write_json(f"runs/{run.id}/environment.json", {"orca": {"version": "6.1.1"}, "offline_fixture_only": True})
    return store, book, run, step, geometry


def callback(book, run, step, geometry):
    return lambda draft: book.reserve_science(run, step, geometry.id, draft_attempt=draft)


def assert_other_model_reservation_works(book, store):
    other, _, _ = make_run(store, "development")
    item = record("after-reconciliation")
    book.reserve_model(other, item)
    bind_model(store, other, item)
    settle_model(book, store, other, item, known=False)


def test_environment_busy_creates_no_batch_ticket_and_resumes_without_batch_poison(setup, monkeypatch):
    store, book, blocked, step, geometry = setup
    owner, owner_step, owner_geometry = make_run(store, science=True)
    owner_attempt = store.reserve_attempt(owner, owner_step, owner_geometry.id)
    owner_lease = store.environment_lease()
    callbacks = []

    def offline_entry(store, run, step, attempt, config, fault=None):
        callbacks.append(attempt.id)
        outcome = {"state": "failed", "not_started": True, "handle": None,
                   "resource_usage": {}, "reason": "offline test execution entry, no engine invoked"}
        return Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                      operation_status="failed", source={"execution": outcome}), outcome

    monkeypatch.setattr(electronic, "execute", offline_entry)
    first = agent.execute(store, Config(), blocked.id, batch=book)
    assert first.state == "failed" and not first.attempts and not callbacks
    assert first.usage.orca_starts_reserved == 0
    assert not book.snapshot().get("agent_science")
    assert store.environment_lease() == owner_lease
    assert_other_model_reservation_works(book, store)
    store.finish_attempt(owner, owner_attempt.id, state="not_started", started=False, termination_confirmed=True)
    recovered = agent.execute(store, Config(), blocked.id, batch=book, resume=True)
    assert len(callbacks) == len(recovered.attempts) == 1
    science = book.snapshot()["agent_science"]
    assert len(science) == 1
    entry = next(iter(science.values()))
    assert entry["state"] == "known" and entry["orca_starts_reserved"] == 1 and entry["orca_starts_actual"] == 0


@pytest.mark.parametrize("fault", ["paused", "different_step", "changed_permission", "existing_directory"])
def test_precondition_rejection_runs_before_batch_callback(setup, fault):
    store, book, run, step, geometry = setup
    if fault == "paused":
        store.signal(run.id, "pause")
    elif fault == "different_step":
        step = step.model_copy(update={"id": "not-the-plan-step"})
    elif fault == "changed_permission":
        permission = run.permission.model_copy(update={"scientific_execution": False})
        store._write_json(f"runs/{run.id}/permission.json", permission)
    else:
        store.path(f"runs/{run.id}/steps/{step.id}/attempt-001").mkdir(parents=True)
    with pytest.raises(StoreError):
        store.reserve_attempt(run, step, geometry.id, before_reserve=callback(book, run, step, geometry))
    assert not store.load_run(run.id).attempts and not book.snapshot().get("agent_science")


@pytest.mark.parametrize("window", ["before_intent", "after_intent", "after_lease", "after_run"])
def test_prepersistence_crash_restores_exact_attempt_as_not_started_and_keeps_reservation(setup, monkeypatch, window):
    store, book, run, step, geometry = setup
    original_write, original_save = store._write_json, store.save_run
    fired = False

    def crash_write(relative, value, **kwargs):
        nonlocal fired
        if not fired and relative.endswith("/intent.json") and window == "before_intent":
            fired = True
            raise OSError("offline pre-intent filesystem failure")
        original_write(relative, value, **kwargs)
        if not fired and relative.endswith("/intent.json") and window == "after_intent":
            fired = True
            raise OSError("offline post-intent filesystem failure")

    def crash_save(value):
        nonlocal fired
        if not fired and value.attempts and window == "after_lease":
            fired = True
            raise OSError("offline lease-published Run-save failure")
        original_save(value)
        if not fired and value.attempts and window == "after_run":
            fired = True
            raise OSError("offline Run-published response loss")

    monkeypatch.setattr(store, "_write_json", crash_write)
    monkeypatch.setattr(store, "save_run", crash_save)
    with pytest.raises(OSError):
        store.reserve_attempt(run, step, geometry.id, before_reserve=callback(book, run, step, geometry))
    assert fired
    monkeypatch.setattr(store, "_write_json", original_write)
    monkeypatch.setattr(store, "save_run", original_save)
    reserved = next(iter(book.snapshot()["agent_science"].values()))
    ticket, draft_id = reserved["id"], reserved["prelaunch_attempt"]["id"]
    original_receipt = book._directory("agent_science", ticket) / "reservation.json"
    receipt_bytes = original_receipt.read_bytes()
    current = store.load_run(run.id)
    for repetition in range(2):
        assert book.reconcile_science(current) == {ticket: "known"}
        value = store.load_run(run.id)
        assert len(value.attempts) == 1 and value.attempts[0].id == draft_id
        assert value.attempts[0].state == "not_started" and not value.attempts[0].started
        assert value.usage.orca_starts_reserved == 1 and value.usage.orca_starts_actual == 0
        assert value.usage.logical_attempts[step.logical_id] == 1
        assert not store.environment_lease()
        if repetition == 0:
            settled_bytes = book.ledger.path.read_bytes()
        else:
            assert book.ledger.path.read_bytes() == settled_bytes
        assert original_receipt.read_bytes() == receipt_bytes
    entry = book.snapshot()["agent_science"][ticket]
    assert entry["orca_starts_reserved"] == 1 and entry["orca_starts_actual"] == 0
    assert len(entry["settlements"]) == 1
    assert_other_model_reservation_works(book, store)


def test_agent_resume_twice_collects_no_start_proof_without_new_execution_or_refund(setup, monkeypatch):
    store, book, run, step, geometry = setup
    run.budget.orca_starts = 1
    store._write_json(f"runs/{run.id}/budget.json", run.budget)
    store._write_json(f"runs/{run.id}/run.json", run)
    original = store.save_run
    monkeypatch.setattr(store, "save_run", lambda _: (_ for _ in ()).throw(OSError("offline lease/Run gap")))
    with pytest.raises(OSError):
        store.reserve_attempt(run, step, geometry.id, before_reserve=callback(book, run, step, geometry))
    monkeypatch.setattr(store, "save_run", original)
    monkeypatch.setattr(electronic, "execute", lambda *_args: pytest.fail("no scientific entry may run"))
    monkeypatch.setattr(agent.local, "reconcile", lambda *_args: pytest.fail("no process identity was created"))
    for repetition in range(2):
        resumed = agent.execute(store, Config(), run.id, batch=book, resume=True)
        assert resumed.state == "budget_exhausted"
        assert len(resumed.attempts) == len(resumed.result_ids) == 1
        attempt = resumed.attempts[0]
        assert attempt.state == "not_started" and attempt.result_id and not attempt.started
        assert not store.load_result(run.id, attempt.result_id).qualified_outputs
        assert resumed.usage.orca_starts_reserved == 1 and resumed.usage.orca_starts_actual == 0
        assert not resumed.model_records and not store.environment_lease()
        if repetition == 0:
            ledger_bytes = book.ledger.path.read_bytes()
        else:
            assert book.ledger.path.read_bytes() == ledger_bytes
    proof = store.path(f"{resumed.attempts[0].directory}/prelaunch-aborted.json")
    proof.write_text('{"changed":true}')
    with pytest.raises(budget.ReferenceBlocked, match="binding"):
        book.snapshot()


def test_partial_intent_recovery_never_releases_another_runs_lease(setup, monkeypatch):
    store, book, run, step, geometry = setup
    original = store._write_json

    def before_intent(relative, value, **kwargs):
        if relative.endswith("/intent.json"):
            raise OSError("offline failure before intent")
        return original(relative, value, **kwargs)

    monkeypatch.setattr(store, "_write_json", before_intent)
    with pytest.raises(OSError):
        store.reserve_attempt(run, step, geometry.id, before_reserve=callback(book, run, step, geometry))
    monkeypatch.setattr(store, "_write_json", original)
    other, other_step, other_geometry = make_run(store, science=True)
    store.reserve_attempt(other, other_step, other_geometry.id)
    lease = store.environment_lease()
    assert list(book.reconcile_science(run).values()) == ["known"]
    assert store.environment_lease() == lease


@pytest.mark.parametrize("tamper", ["startup_file", "intent", "lease_handle", "run_counter"])
def test_ambiguous_or_changed_boundary_is_never_settled_as_zero(setup, monkeypatch, tamper):
    store, book, run, step, geometry = setup
    original = store.save_run
    monkeypatch.setattr(store, "save_run", lambda _: (_ for _ in ()).throw(OSError("offline save failure")))
    with pytest.raises(OSError):
        store.reserve_attempt(run, step, geometry.id, before_reserve=callback(book, run, step, geometry))
    monkeypatch.setattr(store, "save_run", original)
    entry = next(iter(book.snapshot()["agent_science"].values()))
    directory = entry["prelaunch_attempt"]["directory"]
    if tamper == "startup_file":
        store._write_json(f"{directory}/started.json", {"pid": 123})
    elif tamper == "intent":
        store._write_json(f"{directory}/intent.json", {"changed": True})
    elif tamper == "lease_handle":
        store.update_lease_handle(run.id, entry["prelaunch_attempt"]["id"], {"job_name": "unverified"})
    else:
        current = store.load_run(run.id)
        current.usage.orca_starts_reserved += 1
        store._write_json(f"runs/{run.id}/run.json", current)
    before = book.ledger.path.read_bytes()
    with pytest.raises(StoreError):
        book.reconcile_science(store.load_run(run.id))
    assert book.ledger.path.read_bytes() == before
    assert book.snapshot()["agent_science"][entry["id"]]["state"] == "reserved"
    assert not store.path(f"{directory}/prelaunch-aborted.json").exists()
