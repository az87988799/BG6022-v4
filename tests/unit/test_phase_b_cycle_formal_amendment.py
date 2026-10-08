"""Synthetic formal additions preserve the applied original cycle and costs."""

import copy

import pytest

from tests.helpers import phase_b_bounded_package as package
from tests.helpers import phase_b_budget as budget
from tests.helpers import phase_b_cycle_formal_amendment as amendment
from tests.helpers import phase_b_repair_cycle as cycle
from tests.unit.test_phase_b_repair_cycle import approved as approved
from tests.unit.test_phase_b_repair_cycle import cycle_approved as cycle_approved
from tests.unit.test_phase_b_repair_cycle import legacy as legacy
from tests.unit.test_phase_b_repair_cycle import r3_approved as r3_approved
from tests.unit.test_phase_b_repair_cycle import renewal_approved as renewal_approved


def test_formal_addition_is_disabled_without_explicit_reviewed_record(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "no-ledger")
    with pytest.raises(budget.ReferenceBlocked, match="explicit execute"):
        amendment.apply()
    with pytest.raises(budget.ReferenceBlocked, match="approval path and exact SHA"):
        amendment.apply(execute=True)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def formal_approved(cycle_approved, tmp_path, monkeypatch):
    book, *_ = cycle_approved
    package.apply_limits(execute=True, package=cycle.CYCLE_ID)
    document = tmp_path / "synthetic-formal-proposal.md"
    document.write_text("Synthetic approval fixture; no actual authorization or execution.")
    monkeypatch.setattr(amendment, "PROPOSAL_PATH", str(document))
    proposed = amendment.proposal()
    value = {"schema_version": 1, "approval_id": amendment.APPROVAL_ID, "status": "user_approved",
        "user_statement": "synthetic test authorization", "question_text": "synthetic test question", "proposal": proposed}
    path = tmp_path / "synthetic-formal-approval.json"
    budget.reference._save(path, value)
    return book, path, value


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_formal_addition_preserves_all_history_unknown_cost_and_original_cycle(formal_approved, point):
    book, path, value = formal_approved
    before = book.snapshot()
    original_scope, original_pin = cycle.scope(), budget.reference.CYCLE_APPROVAL_SHA256
    old_bytes = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    options = {"approval_path": path, "approval_sha256": budget.reference.sha256_file(path), "execute": True}
    def fault(stage):
        if stage == point:
            raise OSError("synthetic formal publication interruption")
    if point:
        with pytest.raises(OSError, match="synthetic"):
            amendment.apply(**options, fault=fault)
    receipt = amendment.apply(**options)
    after = book.snapshot()
    assert {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["unknown_tokens"] == 150
    assert after["limits"] == value["proposal"]["approved_limits"]
    assert after["limits"]["orca_starts"] == before["limits"]["orca_starts"]
    assert after["limits"]["model"]["usd"] == before["limits"]["model"]["usd"] == 20
    assert receipt["previous_limit_authority"]["approval_id"] == budget.reference.CYCLE_APPROVAL_ID
    assert cycle.scope() == original_scope and budget.reference.CYCLE_APPROVAL_SHA256 == original_pin
    assert all(p.read_bytes() == raw for p, raw in old_bytes.items())
    assert amendment.apply(**options) == receipt
    assert budget.reference.is_cycle_ledger(after)
    with pytest.raises(budget.ReferenceBlocked, match="unknown"):
        cycle._no_unknown(after, cycle._state(after))


@pytest.mark.parametrize("field", ["sha", "usd", "development", "e2e", "missing_user", "manifest"])
def test_formal_addition_rejects_unapproved_dimensions_or_identity(formal_approved, field):
    book, path, value = formal_approved
    before = book.ledger.path.read_bytes()
    changed = copy.deepcopy(value)
    if field == "usd":
        changed["proposal"]["approved_limits"]["model"]["usd"] += 1
    elif field == "development":
        changed["proposal"]["formal_allocations"]["formal-1"]["development"] = 1
    elif field == "e2e":
        changed["proposal"]["formal_allocations"]["formal-e2e-1"] = changed["proposal"]["formal_allocations"]["formal-1"]
    elif field == "missing_user":
        changed["user_statement"] = ""
    elif field == "manifest":
        changed["proposal"]["formal_manifest_sha256"]["formal-1"] = "0" * 64
    budget.reference._save(path, changed)
    digest = "0" * 64 if field == "sha" else budget.reference.sha256_file(path)
    with pytest.raises(budget.ReferenceBlocked):
        amendment.apply(approval_path=path, approval_sha256=digest, execute=True)
    assert book.ledger.path.read_bytes() == before
    assert not (book.ledger.root / "budget-amendments" / amendment.APPROVAL_ID).exists()
