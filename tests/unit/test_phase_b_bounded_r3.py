"""One disabled r3 package and synthetic sixth amendment; no external calls."""

import copy
import json

import pytest

from orca_agent.store import sha256_file
from tests.helpers import phase_b_bounded_package as package
from tests.helpers import phase_b_model_evaluation as models
from tests.unit.test_phase_b_bounded_package import approved as approved
from tests.unit.test_phase_b_bounded_renewal import (
    OPEN_GUARD,
)
from tests.unit.test_phase_b_bounded_renewal import (
    candidate as candidate,
)
from tests.unit.test_phase_b_bounded_renewal import (
    renewal_approved as renewal_approved,
)
from tests.unit.test_phase_b_budget import bind_model, record, settle_model
from tests.unit.test_phase_b_budget_amendment import legacy as legacy

R3 = package.R3_LABEL


def test_r3_proposal_has_no_authority_and_retains_both_historical_scopes(tmp_path, monkeypatch, capsys):
    assert package.reference.R3_APPROVAL_SHA256 is None
    assert not package.reference.R3_APPROVAL.exists()
    for label, path in ((package.LABEL, package.reference.BOUNDED_APPROVAL),
                        (package.RENEWAL_LABEL, package.reference.RENEWAL_APPROVAL)):
        assert package.scope(package=label) == package.reference._json(path)["development_package"]
    monkeypatch.setattr(package, "R3_ROOT", tmp_path / "r3")
    assert package.main(["proposal", "--package", R3]) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["approval_pinned"] is False
    scope = value["scope"]
    assert scope == package.scope(package=R3)
    assert scope["package_id"] == R3 and scope["supersedes_package"] == package.RENEWAL_LABEL
    assert scope["maximum_model_usage"] == {"http_requests": 112, "tokens": 800000}
    assert 321 + 112 + 48 + 16 + 624 == scope["proposed_limits"]["model"]["http_requests"]
    assert 1005807 + 800000 + 288000 + 96000 + 4704000 == scope["proposed_limits"]["model"]["tokens"]
    assert scope["previous_limits"] == package.reference.RENEWAL_LIMITS
    assert scope["proposed_limits"]["model"]["usd"] == 10
    assert scope["proposed_limits"]["orca_starts"] == scope["previous_limits"]["orca_starts"]
    assert scope["preserved_old_remaining"] == package.scope()["preserved_old_remaining"]
    assert scope["prior_package_disposition"] == {
        "retained_model_slots": {package.DIAGNOSTICS[0]: "failed"},
        "cancelled_model_slots": list(package.MODEL_SLOTS[1:]),
        "cancelled_structure_queries": 2, "cancelled_structure_preparations": 2,
        "cancelled_reference_starts": 1, "cancelled_development_starts": 6,
        "retained_usage": {"http_requests": 1, "tokens": 4256, "orca_starts": 0},
        "reuse_prior_runs_or_passes": False,
    }
    assert scope["reference_id"] == "bounded-20261008-r3-methane-prepared-sp"
    assert not list(tmp_path.iterdir())


@pytest.fixture
def r3_approved(renewal_approved, tmp_path, monkeypatch):
    book, store, run, known, unknown = renewal_approved
    package.apply_limits(execute=True, package=package.RENEWAL_LABEL)
    # Model a failed, fully charged r2 call in the same synthetic ledger. It is
    # retained alongside older known and unknown costs, never retried or erased.
    r2_item = record(3, prompt=2974, completion=1282)
    book.reserve_model(run, r2_item)
    bind_model(store, run, r2_item)
    settle_model(book, store, run, r2_item, prompt=2974, completion=1282)
    monkeypatch.setattr(package, "_assert_open", OPEN_GUARD)
    path = tmp_path / "synthetic-r3-approval.json"
    value = {"approval_id": package.reference.R3_APPROVAL_ID, "status": "user_approved",
        "previous_limits": package.reference.RENEWAL_LIMITS, "approved_limits": package.reference.R3_LIMITS,
        "previous_approval_id": package.reference.RENEWAL_APPROVAL_ID,
        "previous_approval_sha256": package.reference.RENEWAL_APPROVAL_SHA256,
        "development_package": package.scope(package=R3),
        "approval_baseline": {"ledger_sha256": sha256_file(book.ledger.path)}}
    package.reference._save(path, value)
    monkeypatch.setattr(package.reference, "R3_APPROVAL", path)
    monkeypatch.setattr(package.reference, "R3_APPROVAL_SHA256", sha256_file(path))
    return book, store, run, known, unknown, r2_item


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_sixth_amendment_keeps_failed_and_unknown_costs_and_all_prior_receipts(r3_approved, point):
    book, _, _, _, _, r2_item = r3_approved
    before = book.snapshot()
    files = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    def fault(where):
        if where == point:
            raise OSError("synthetic r3 interruption")
    if point:
        with pytest.raises(OSError, match="synthetic r3 interruption"):
            package.apply_limits(execute=True, package=R3, fault=fault)
    receipt = package.apply_limits(execute=True, package=R3)
    after = book.snapshot()
    assert after["limits"] == package.reference.R3_LIMITS
    assert {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["unknown_tokens"] == 150
    assert r2_item["id"] in after["model_records"]
    assert after["model_usage"]["tokens"] >= 4256 + 150
    assert all(path.read_bytes() == content for path, content in files.items())
    assert receipt["previous_limit_authority"]["approval_id"] == package.reference.RENEWAL_APPROVAL_ID
    assert package.apply_limits(execute=True, package=R3) == receipt
    assert book.snapshot() == after
    assert list(book.ledger.root.rglob("batch-ledger.json")) == [book.ledger.path]


@pytest.mark.parametrize("change", ["pin", "status", "scope", "limits", "baseline", "missing_baseline", "chain"])
def test_r3_approval_cannot_inherit_old_permission_or_expand_scope(r3_approved, change, monkeypatch):
    book, *_ = r3_approved
    path = package.reference.R3_APPROVAL
    original_ledger = book.ledger.path.read_bytes()
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
        value["previous_approval_sha256"] = package.reference.BOUNDED_APPROVAL_SHA256
    else:
        value["status"] = "proposal_only"
    package.reference._save(path, value)
    if change != "pin":
        monkeypatch.setattr(package.reference, "R3_APPROVAL_SHA256", sha256_file(path))
    with pytest.raises(package.reference.ReferenceBlocked):
        package.apply_limits(execute=True, package=R3)
    assert book.ledger.path.read_bytes() == original_ledger
    assert not (book.ledger.root / "budget-amendments" / package.reference.R3_APPROVAL_ID).exists()


@pytest.mark.parametrize("change", ["failed_record", "old_record", "r2_receipt", "r1_receipt", "current_receipt", "before"])
def test_sixth_authority_recursively_rejects_loss_of_historical_cost(r3_approved, change):
    book, _, _, known, _, r2_item = r3_approved
    package.apply_limits(execute=True, package=R3)
    if change in {"failed_record", "old_record"}:
        value = book.snapshot()
        del value["model_records"][r2_item["id"] if change == "failed_record" else known["id"]]
        package.reference._save(book.ledger.path, value)
    else:
        approval_id = {"r2_receipt": package.reference.RENEWAL_APPROVAL_ID,
            "r1_receipt": package.reference.BOUNDED_APPROVAL_ID}.get(change, package.reference.R3_APPROVAL_ID)
        path = book.ledger.root / "budget-amendments" / approval_id / (
            "before.json" if change == "before" else "amendment.json")
        path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(package.reference.ReferenceBlocked):
        book.snapshot()


def test_old_candidate_and_passes_cannot_supply_r3_prior_gate(candidate, monkeypatch):
    root, _, record = candidate
    for label in (package.LABEL, package.RENEWAL_LABEL):
        old_slot = models._slot(package.MODEL_SLOTS[0], 1, "development", label)
        package._save(old_slot / "metadata.json", {"bounded_candidate_sha256": sha256_file(root / "candidate.json")})
        package._save(old_slot / "grade.json", {"status": "passed"})
    monkeypatch.setattr(models, "regrade", lambda *_, **__: pytest.fail("old package pass cannot qualify r3"))
    with pytest.raises(FileNotFoundError):
        package.guard_model_slot(package.MODEL_SLOTS[1], 1, category="development", package=R3, model_profile="disabled")
    old_record = copy.deepcopy(record)
    old_record["scope"] = package.scope(package=package.RENEWAL_LABEL)
    package.reference._save(root / "candidate.json", old_record)
    with pytest.raises(package.reference.ReferenceBlocked, match="candidate"):
        package.guard_model_slot(package.MODEL_SLOTS[0], 1, category="development", package=R3, model_profile="disabled")
