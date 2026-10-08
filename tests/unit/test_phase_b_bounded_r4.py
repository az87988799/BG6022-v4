"""Disabled r4 scope and synthetic seventh amendment; no real calls or quota."""

import json

import pytest

from orca_agent.store import sha256_file
from tests.helpers import phase_b_bounded_package as package
from tests.unit.test_phase_b_bounded_package import approved as approved
from tests.unit.test_phase_b_bounded_r3 import r3_approved as r3_approved
from tests.unit.test_phase_b_bounded_renewal import OPEN_GUARD
from tests.unit.test_phase_b_bounded_renewal import renewal_approved as renewal_approved
from tests.unit.test_phase_b_budget import bind_model, record, settle_model
from tests.unit.test_phase_b_budget_amendment import legacy as legacy

R4 = package.R4_LABEL


def test_r4_proposal_is_disabled_and_preserves_all_three_approved_scopes(tmp_path, monkeypatch, capsys):
    assert package.reference.R4_APPROVAL_SHA256 is None
    assert not package.reference.R4_APPROVAL.exists()
    for label, path in ((package.LABEL, package.reference.BOUNDED_APPROVAL),
                        (package.RENEWAL_LABEL, package.reference.RENEWAL_APPROVAL),
                        (package.R3_LABEL, package.reference.R3_APPROVAL)):
        assert package.scope(package=label) == package.reference._json(path)["development_package"]
    monkeypatch.setattr(package, "R4_ROOT", tmp_path / "r4")
    assert package.main(["proposal", "--package", R4]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["approval_pinned"] is False
    scope = output["scope"]
    assert scope == package.scope(package=R4)
    assert scope["package_id"] == R4 and scope["supersedes_package"] == package.R3_LABEL
    assert scope["maximum_model_usage"] == {"http_requests": 112, "tokens": 800000}
    assert 324 + 112 + 48 + 16 + 624 == scope["proposed_limits"]["model"]["http_requests"]
    assert 1018912 + 800000 + 288000 + 96000 + 4704000 == scope["proposed_limits"]["model"]["tokens"]
    assert scope["previous_limits"] == package.reference.R3_LIMITS
    assert scope["proposed_limits"]["model"]["usd"] == 10
    assert scope["proposed_limits"]["orca_starts"] == scope["previous_limits"]["orca_starts"]
    assert scope["preserved_old_remaining"] == package.scope()["preserved_old_remaining"]
    assert scope["prior_package_disposition"] == {
        "retained_model_slots": {package.DIAGNOSTICS[0]: "passed", package.DIAGNOSTICS[1]: "failed"},
        "cancelled_model_slots": list(package.MODEL_SLOTS[2:]),
        "cancelled_structure_queries": 2, "cancelled_structure_preparations": 2,
        "cancelled_reference_starts": 1, "cancelled_development_starts": 6,
        "retained_usage": {"http_requests": 3, "tokens": 13105, "orca_starts": 0},
        "reuse_prior_runs_or_passes": False,
    }
    assert scope["reference_id"] == "bounded-20261008-r4-methane-prepared-sp"
    assert scope["diagnostics"] + scope["raw_gates"] == list(package.MODEL_SLOTS)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def r4_approved(r3_approved, tmp_path, monkeypatch):
    book, store, run, known, unknown, r2_item = r3_approved
    package.apply_limits(execute=True, package=package.R3_LABEL)
    # Retain three fully charged calls from an r3 package whose second slot
    # failed. A new package cannot refund these or consume the old unknown cost.
    r3_items = []
    for index, prompt, completion in ((4, 2999, 1686), (5, 3602, 478), (6, 3901, 439)):
        item = record(index, prompt=prompt, completion=completion)
        book.reserve_model(run, item)
        bind_model(store, run, item)
        settle_model(book, store, run, item, prompt=prompt, completion=completion)
        r3_items.append(item)
    # Synthetic historical migration only; the real r4 operator is now closed.
    monkeypatch.setattr(package, "_assert_open", lambda label: None if label == R4 else OPEN_GUARD(label))
    path = tmp_path / "synthetic-r4-approval.json"
    value = {"approval_id": package.reference.R4_APPROVAL_ID, "status": "user_approved",
        "previous_limits": package.reference.R3_LIMITS, "approved_limits": package.reference.R4_LIMITS,
        "previous_approval_id": package.reference.R3_APPROVAL_ID,
        "previous_approval_sha256": package.reference.R3_APPROVAL_SHA256,
        "development_package": package.scope(package=R4),
        "approval_baseline": {"ledger_sha256": sha256_file(book.ledger.path)}}
    package.reference._save(path, value)
    monkeypatch.setattr(package.reference, "R4_APPROVAL", path)
    monkeypatch.setattr(package.reference, "R4_APPROVAL_SHA256", sha256_file(path))
    return book, store, run, known, unknown, r2_item, r3_items


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_seventh_amendment_keeps_failed_calls_unknown_cost_and_all_prior_receipts(r4_approved, point):
    book, _, _, _, _, r2_item, r3_items = r4_approved
    before = book.snapshot()
    files = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    def fault(where):
        if where == point:
            raise OSError("synthetic r4 interruption")
    if point:
        with pytest.raises(OSError, match="synthetic r4 interruption"):
            package.apply_limits(execute=True, package=R4, fault=fault)
    receipt = package.apply_limits(execute=True, package=R4)
    after = book.snapshot()
    assert after["limits"] == package.reference.R4_LIMITS
    assert {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["unknown_tokens"] == 150
    assert sum(item["total_tokens"] for item in r3_items) == 13105
    assert all(item["id"] in after["model_records"] for item in [r2_item, *r3_items])
    assert all(path.read_bytes() == content for path, content in files.items())
    assert receipt["previous_limit_authority"]["approval_id"] == package.reference.R3_APPROVAL_ID
    assert package.apply_limits(execute=True, package=R4) == receipt
    assert book.snapshot() == after
    assert list(book.ledger.root.rglob("batch-ledger.json")) == [book.ledger.path]


@pytest.mark.parametrize("change", ["pin", "status", "scope", "limits", "baseline", "missing_baseline", "chain"])
def test_r4_approval_cannot_inherit_prior_permission_or_expand_scope(r4_approved, change, monkeypatch):
    book, *_ = r4_approved
    path = package.reference.R4_APPROVAL
    original = book.ledger.path.read_bytes()
    value = package.reference._json(path)
    if change == "scope":
        value["development_package"]["repetitions"]["water_opt"] = 4
    elif change == "limits":
        value["approved_limits"]["model"]["http_requests"] += 1
    elif change == "baseline":
        value["approval_baseline"]["ledger_sha256"] = "0" * 64
    elif change == "missing_baseline":
        value.pop("approval_baseline")
    elif change == "chain":
        value["previous_approval_sha256"] = package.reference.RENEWAL_APPROVAL_SHA256
    else:
        value["status"] = "proposal_only"
    package.reference._save(path, value)
    if change != "pin":
        monkeypatch.setattr(package.reference, "R4_APPROVAL_SHA256", sha256_file(path))
    with pytest.raises(package.reference.ReferenceBlocked):
        package.apply_limits(execute=True, package=R4)
    assert book.ledger.path.read_bytes() == original
    assert not (book.ledger.root / "budget-amendments" / package.reference.R4_APPROVAL_ID).exists()


@pytest.mark.parametrize("change", ["failed_record", "old_record", "r3_receipt", "r2_receipt", "r1_receipt", "current_receipt", "before"])
def test_seventh_authority_recursively_rejects_loss_of_prior_evidence(r4_approved, change):
    book, _, _, known, _, _, r3_items = r4_approved
    package.apply_limits(execute=True, package=R4)
    if change in {"failed_record", "old_record"}:
        value = book.snapshot()
        del value["model_records"][r3_items[-1]["id"] if change == "failed_record" else known["id"]]
        package.reference._save(book.ledger.path, value)
    else:
        approval_id = {"r3_receipt": package.reference.R3_APPROVAL_ID,
            "r2_receipt": package.reference.RENEWAL_APPROVAL_ID,
            "r1_receipt": package.reference.BOUNDED_APPROVAL_ID}.get(change, package.reference.R4_APPROVAL_ID)
        path = book.ledger.root / "budget-amendments" / approval_id / (
            "before.json" if change == "before" else "amendment.json")
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(package.reference.ReferenceBlocked):
        book.snapshot()


def test_existing_budget_cannot_adopt_r4_caps_without_new_authority(r3_approved):
    book, *_ = r3_approved
    package.apply_limits(execute=True, package=package.R3_LABEL)
    value = book.snapshot()
    value["limits"] = package.reference.R4_LIMITS
    package.reference._save(book.ledger.path, value)
    with pytest.raises(package.reference.ReferenceBlocked, match="explicit human approval"):
        book.snapshot()
