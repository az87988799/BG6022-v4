"""Approved cap migration preserves all historical and unknown cost offline."""

import copy
import json

import pytest

from orca_agent.store import Store, sha256_file
from tests.helpers import phase_b_budget as budget
from tests.helpers.phase_b_budget_amendment import apply_approved_limits
from tests.unit.test_phase_b_budget import bind_model, make_run, record, settle_model


@pytest.fixture
def legacy(tmp_path, monkeypatch):
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
    budget.reference._save(book.ledger.path, value)
    budget.reference._save(budget.reference.DELIVERED_SNAPSHOT, {
        "limits": budget.reference.ORIGINAL_LIMITS, "entries": {}})
    before = book.ledger.path.read_bytes()
    files = {p: p.read_bytes() for p in (book.ledger.root / "agent-budget").rglob("*.json")}
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
        k: v for k, v in original.items() if k != "limits"}
    assert after["model_usage"]["known_tokens"] == 50
    assert after["model_usage"]["unknown_tokens"] == 150
    assert after["model_usage"]["http_requests"] == 2
    assert all(p.read_bytes() == content for p, content in files.items())
    directory = book.ledger.root / "budget-amendments" / budget.reference.LIMIT_APPROVAL_ID
    assert (directory / "before.json").read_bytes() == before
    assert receipt["before_sha256"] == sha256_file(directory / "before.json")
    migrated = book.ledger.path.read_bytes()
    assert apply_approved_limits(book, execute=True) == receipt
    assert book.ledger.path.read_bytes() == migrated
    # Reconciliation of an existing unknown reservation remains legal; the
    # migration itself neither settles it nor credits its reserved cost back.
    settle_model(book, store, run, unknown, known=True)
    assert book.snapshot()["model_usage"]["http_requests"] == 2
    assert book.snapshot()["model_usage"]["tokens"] == 100


@pytest.mark.parametrize("point", ["after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_interrupted_migration_replays_same_bytes_and_receipt(legacy, point):
    book, _, _, _, _, before, files = legacy

    def crash(phase):
        if phase == point:
            raise OSError("offline controlled publication crash")

    with pytest.raises(OSError):
        apply_approved_limits(book, execute=True, fault=crash)
    if point != "after_ledger_publication":
        assert book.ledger.path.read_bytes() == before
        with pytest.raises(budget.ReferenceBlocked, match="migration"):
            book.snapshot()
    first = apply_approved_limits(book, execute=True)
    assert apply_approved_limits(book, execute=True) == first
    assert book.snapshot()["model_usage"]["tokens"] == 200
    assert all(p.read_bytes() == content for p, content in files.items())


@pytest.mark.parametrize("fault", ["approval", "before", "receipt", "entry_deleted", "known_refund", "remove_authority"])
def test_changed_approval_or_baseline_never_grants_budget(legacy, tmp_path, monkeypatch, fault):
    book, _, _, known, _, _, _ = legacy
    apply_approved_limits(book, execute=True)
    directory = book.ledger.root / "budget-amendments" / budget.reference.LIMIT_APPROVAL_ID
    if fault == "approval":
        changed = tmp_path / "changed-approval.json"
        changed.write_text("{}")
        monkeypatch.setattr(budget.reference, "LIMIT_APPROVAL", changed)
    elif fault in {"before", "receipt"}:
        path = directory / ("before.json" if fault == "before" else "amendment.json")
        path.write_bytes(path.read_bytes() + b" ")
    else:
        data = json.loads(book.ledger.path.read_text(encoding="utf-8"))
        if fault == "entry_deleted":
            data["model_records"].pop(known["id"])
        elif fault == "known_refund":
            data["model_records"][known["id"]]["settled_record"]["total_tokens"] = 0
        else:
            data["limit_authority"] = budget.reference.initial_limit_authority()
        budget.reference._save(book.ledger.path, data)
    with pytest.raises(budget.ReferenceBlocked):
        book.snapshot()
    with pytest.raises(budget.ReferenceBlocked):
        apply_approved_limits(book, execute=True)


def test_approval_profile_keeps_original_case_and_formal_subcaps_immutable():
    approval = budget.reference.limit_approval()
    path = budget.reference.PROJECT / "tests/fixtures/phase_b/cases.json"
    assert sha256_file(path) == "ed3ed06a5027468388cdced66a9ce321889897c28289d8a7e02dd95050af8b30"
    assert approval["approved_limits"] == budget.LIMITS
    assert budget.LIMITS["orca_starts"] == {"reference": 16, "formal": 48, "development": 48, "total": 112}
    assert budget.LIMITS["model"] == {"http_requests": 1004, "tokens": 6500000, "usd": 10}
