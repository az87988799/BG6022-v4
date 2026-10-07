"""Approved cap migration preserves all historical and unknown cost offline."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from orca_agent.store import Store, sha256_file
from tests.helpers import phase_b_budget as budget
from tests.helpers.phase_b_budget_amendment import apply_approved_limits
from tests.unit.test_phase_b_budget import bind_model, make_run, record, settle_model


@pytest.fixture
def legacy(tmp_path, monkeypatch):
    """An isolated first migration with known and unknown durable reservations."""
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "delivered.json")
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    book = budget.AcceptanceBudget(store)
    run, _, _ = make_run(store, "development")
    known, unknown = record(), record(1)
    for item in (known, unknown):
        book.reserve_model(run, item)
        bind_model(store, run, item)
        settle_model(book, store, run, item, known=item is known)
    value = book.snapshot()
    value["limits"] = copy.deepcopy(budget.reference.ORIGINAL_LIMITS)
    del value["limit_authority"]
    first = book.ledger.root / "budget-amendments" / budget.reference.LIMIT_APPROVAL_ID
    budget.reference._save(first / "before.json", value, immutable=True)
    first_receipt = {
        "schema_version": 1, "approval_id": budget.reference.LIMIT_APPROVAL_ID,
        "approval_sha256": budget.reference.LIMIT_APPROVAL_SHA256,
        "previous_limits": budget.reference.ORIGINAL_LIMITS,
        "approved_limits": budget.reference.LIMITS,
        "before_sha256": sha256_file(first / "before.json"),
        "preserved_model_usage": value["model_usage"],
        "preserved_entry_counts": {kind: len(value.get(kind, {}))
                                   for kind in ("entries", "model_records", "agent_science")},
        "applied_at": "2026-10-07T00:00:00+00:00",
    }
    budget.reference._save(first / "amendment.json", first_receipt, immutable=True)
    value["limits"] = copy.deepcopy(budget.reference.LIMITS)
    value["limit_authority"] = {**budget.reference.initial_limit_authority(), "origin": "amendment",
                                "receipt_sha256": sha256_file(first / "amendment.json")}
    budget.reference._save(book.ledger.path, value)
    budget.reference._save(budget.reference.DELIVERED_SNAPSHOT, {
        "limits": budget.reference.ORIGINAL_LIMITS, "entries": {}})
    before = book.ledger.path.read_bytes()
    files = {p: p.read_bytes() for directory in (book.ledger.root / "agent-budget", first, store.root)
             for p in directory.rglob("*.json")}
    return book, store, run, known, unknown, before, files


def test_legacy_cap_cannot_be_spent_under_new_limit_without_explicit_migration(legacy):
    book, _, run, _, _, before, _ = legacy
    for action in (book.snapshot, lambda: book.reserve_model(run, record(2)),
                   lambda: apply_approved_limits(book)):
        with pytest.raises(budget.ReferenceBlocked, match="migration|explicit"):
            action()
    assert book.ledger.path.read_bytes() == before


def test_approved_migration_preserves_receipts_unknown_cost_and_is_idempotent(legacy):
    book, store, run, _, unknown, before, files = legacy
    receipt = apply_approved_limits(book, execute=True)
    after = book.snapshot()
    original = json.loads(before)
    assert after["limits"] == budget.LIMITS
    assert {k: v for k, v in after.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in original.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["known_tokens"] == 50
    assert after["model_usage"]["unknown_tokens"] == 150
    assert after["model_usage"]["http_requests"] == 2
    assert all(p.read_bytes() == content for p, content in files.items())
    directory = book.ledger.root / "budget-amendments" / budget.reference.SUPPLEMENT_APPROVAL_ID
    assert (directory / "before.json").read_bytes() == before
    assert receipt["before_sha256"] == sha256_file(directory / "before.json")
    assert receipt["previous_limit_authority"] == original["limit_authority"]
    migrated = book.ledger.path.read_bytes()
    assert apply_approved_limits(book, execute=True) == receipt
    assert book.ledger.path.read_bytes() == migrated
    # Reconciliation of an existing unknown reservation remains legal; the
    # migration itself neither settles it nor credits its reserved cost back.
    settle_model(book, store, run, unknown, known=True)
    assert book.snapshot()["model_usage"]["http_requests"] == 2
    assert book.snapshot()["model_usage"]["tokens"] == 100
    after_settlement = book.ledger.path.read_bytes()
    assert apply_approved_limits(book, execute=True) == receipt
    assert book.ledger.path.read_bytes() == after_settlement


@pytest.mark.parametrize("point", ["after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_interrupted_migration_replays_same_bytes_and_receipt(legacy, point):
    book, _, run, _, _, before, files = legacy

    def crash(phase):
        if phase == point:
            raise OSError("offline controlled publication crash")

    with pytest.raises(OSError):
        apply_approved_limits(book, execute=True, fault=crash)
    if point != "after_ledger_publication":
        assert book.ledger.path.read_bytes() == before
        with pytest.raises(budget.ReferenceBlocked, match="migration"):
            book.snapshot()
        with pytest.raises(budget.ReferenceBlocked, match="migration"):
            book.reserve_model(run, record(2))
    first = apply_approved_limits(book, execute=True)
    assert apply_approved_limits(book, execute=True) == first
    assert book.snapshot()["model_usage"]["tokens"] == 200
    assert all(p.read_bytes() == content for p, content in files.items())


@pytest.mark.parametrize("fault", ["approval", "first_approval", "before", "receipt", "first_before",
                                  "first_receipt", "entry_deleted", "known_refund", "remove_authority"])
def test_changed_approval_or_baseline_never_grants_budget(legacy, tmp_path, monkeypatch, fault):
    book, _, _, known, _, _, _ = legacy
    apply_approved_limits(book, execute=True)
    directory = book.ledger.root / "budget-amendments" / budget.reference.SUPPLEMENT_APPROVAL_ID
    if fault in {"approval", "first_approval"}:
        changed = tmp_path / "changed-approval.json"
        changed.write_text("{}")
        monkeypatch.setattr(budget.reference, "SUPPLEMENT_APPROVAL" if fault == "approval" else "LIMIT_APPROVAL", changed)
    elif fault in {"before", "receipt", "first_before", "first_receipt"}:
        if fault.startswith("first_"):
            directory = book.ledger.root / "budget-amendments" / budget.reference.LIMIT_APPROVAL_ID
        path = directory / ("before.json" if fault.endswith("before") else "amendment.json")
        path.write_bytes(path.read_bytes() + b" ")
    else:
        data = json.loads(book.ledger.path.read_text(encoding="utf-8"))
        if fault == "entry_deleted":
            data["model_records"].pop(known["id"])
        elif fault == "known_refund":
            data["model_records"][known["id"]]["settled_record"]["total_tokens"] = 0
        else:
            data["limit_authority"] = budget.reference.active_limit_authority()
        budget.reference._save(book.ledger.path, data)
    with pytest.raises(budget.ReferenceBlocked):
        book.snapshot()
    with pytest.raises(budget.ReferenceBlocked):
        apply_approved_limits(book, execute=True)


def test_approval_profile_keeps_original_case_and_formal_subcaps_immutable():
    approval = budget.reference.limit_approval()
    supplement = budget.reference.supplement_approval()
    path = budget.reference.PROJECT / "tests/fixtures/phase_b/cases.json"
    assert sha256_file(path) == "ed3ed06a5027468388cdced66a9ce321889897c28289d8a7e02dd95050af8b30"
    assert approval["approved_limits"] == budget.reference.LIMITS == supplement["previous_limits"]
    assert supplement["approved_limits"] == budget.LIMITS == budget.reference.ACTIVE_LIMITS
    assert budget.LIMITS["orca_starts"] == {"reference": 16, "formal": 48, "development": 48, "total": 112}
    assert budget.reference.LIMITS["model"] == {"http_requests": 1004, "tokens": 6500000, "usd": 10}
    assert budget.LIMITS["model"] == {"http_requests": 1050, "tokens": 6530000, "usd": 10}


def test_original_bare_limits_cannot_skip_first_applied_approval(legacy):
    book, _, run, _, _, _, _ = legacy
    path = book.ledger.root / "budget-amendments" / budget.reference.LIMIT_APPROVAL_ID / "before.json"
    book.ledger.path.write_bytes(path.read_bytes())
    original = book.ledger.path.read_bytes()
    for action in (book.snapshot, lambda: book._snapshot_unlocked(allow_legacy_limits=True),
                   lambda: book.reserve_model(run, record(2)), lambda: apply_approved_limits(book, execute=True)):
        with pytest.raises(budget.ReferenceBlocked, match="migration"):
            action()
    assert book.ledger.path.read_bytes() == original
    assert not (book.ledger.root / "budget-amendments" / budget.reference.SUPPLEMENT_APPROVAL_ID).exists()


def test_first_approval_can_be_audited_without_granting_latest_limits(legacy):
    book, _, _, _, _, before, files = legacy
    assert book._snapshot_unlocked(allow_legacy_limits=True)["limits"] == budget.reference.LIMITS
    with pytest.raises(budget.ReferenceBlocked, match="migration"):
        book.snapshot()
    assert book.ledger.path.read_bytes() == before
    assert all(path.read_bytes() == content for path, content in files.items())


def test_initial_authority_cannot_hide_first_migration_history_without_delivered_snapshot(legacy):
    book, _, _, _, _, _, _ = legacy
    budget.reference.DELIVERED_SNAPSHOT.unlink()
    value = json.loads(book.ledger.path.read_text(encoding="utf-8"))
    value.update(limits=budget.LIMITS, limit_authority=budget.reference.active_limit_authority())
    budget.reference._save(book.ledger.path, value)
    with pytest.raises(budget.ReferenceBlocked, match="migration receipt"):
        book.snapshot()


def test_second_migration_does_not_initialize_a_missing_batch(legacy):
    book, _, _, _, _, _, _ = legacy
    book.ledger.path.unlink()
    with pytest.raises(budget.ReferenceBlocked, match="missing"):
        apply_approved_limits(book, execute=True)


def test_first_authority_binding_cannot_be_replaced_inside_second_receipt(legacy):
    book, _, _, _, _, _, _ = legacy
    apply_approved_limits(book, execute=True)
    path = book.ledger.root / "budget-amendments" / budget.reference.SUPPLEMENT_APPROVAL_ID / "amendment.json"
    receipt = budget.reference._json(path)
    receipt["previous_limit_authority"]["receipt_sha256"] = "0" * 64
    budget.reference._save(path, receipt)
    current = budget.reference._json(book.ledger.path)
    current["limit_authority"]["receipt_sha256"] = sha256_file(path)
    budget.reference._save(book.ledger.path, current)
    with pytest.raises(budget.ReferenceBlocked, match="baseline"):
        book.snapshot()


def test_migration_excludes_new_reservations_until_atomic_publication(legacy):
    book, store, run, _, _, _, files = legacy
    competitor = budget.AcceptanceBudget(store)
    snapshot_saved, release, reserve_started = Event(), Event(), Event()
    item = record(2)

    def pause(phase):
        if phase == "after_original_snapshot":
            snapshot_saved.set()
            assert release.wait(5)

    def reserve():
        reserve_started.set()
        competitor.reserve_model(run, item)

    with ThreadPoolExecutor(max_workers=2) as workers:
        migration = workers.submit(apply_approved_limits, book, execute=True, fault=pause)
        assert snapshot_saved.wait(5)
        reservation = workers.submit(reserve)
        try:
            assert reserve_started.wait(5)
            assert not reservation.done()
        finally:
            release.set()
        receipt = migration.result(timeout=5)
        reservation.result(timeout=5)
    bind_model(store, run, item)
    assert book.snapshot()["model_usage"]["http_requests"] == 3
    after = book.ledger.path.read_bytes()
    assert apply_approved_limits(book, execute=True) == receipt
    assert book.ledger.path.read_bytes() == after
    assert all(path.read_bytes() == content for path, content in files.items()
               if not path.is_relative_to(store.root))
