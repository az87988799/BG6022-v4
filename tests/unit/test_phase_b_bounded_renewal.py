"""Historical r2 approval and current r4 preparation guards; no live model, network, OPI or ORCA."""

import copy
import json
from types import SimpleNamespace

import pytest

from orca_agent.config import Config
from orca_agent.store import sha256_file
from tests.helpers import phase_b_bounded_package as package
from tests.helpers import phase_b_model_evaluation as models
from tests.unit.test_phase_b_bounded_package import approved as approved
from tests.unit.test_phase_b_budget_amendment import legacy as legacy

OPEN_GUARD = package._assert_open
R2 = package.RENEWAL_LABEL
R3 = package.R3_LABEL
R4 = package.R4_LABEL


def test_renewal_proposal_is_unapproved_and_preserves_exact_old_scope(tmp_path, monkeypatch, capsys):
    old = json.loads(package.reference.BOUNDED_APPROVAL.read_text(encoding="utf-8"))
    assert package.scope() == old["development_package"]
    monkeypatch.setattr(package.reference, "RENEWAL_APPROVAL_SHA256", None)
    assert package.reference.RENEWAL_APPROVAL_SHA256 is None
    monkeypatch.setattr(package, "RENEWAL_ROOT", tmp_path / "r2")
    assert package.main(["proposal", "--package", R2]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["approval_pinned"] is False
    assert output["scope"] == package.scope(package=R2)
    scope = output["scope"]
    assert scope["package_id"] == R2 and scope["supersedes_package"] == package.LABEL
    assert scope["maximum_model_usage"] == {"http_requests": 112, "tokens": 800000}
    assert 320 + 112 + 48 + 16 + 624 == scope["proposed_limits"]["model"]["http_requests"]
    assert 1001551 + 800000 + 288000 + 96000 + 4704000 == scope["proposed_limits"]["model"]["tokens"]
    assert scope["proposed_limits"]["orca_starts"] == scope["previous_limits"]["orca_starts"]
    assert scope["prior_package_disposition"]["cancelled_model_slots"] == list(package.MODEL_SLOTS[2:])
    assert scope["prior_package_disposition"]["reuse_prior_runs_or_passes"] is False
    assert scope["reference_id"] != package.scope()["reference_id"]
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("operation", ["apply", "freeze", "model", "resolve", "prepare", "reference", "science"])
@pytest.mark.parametrize("label", [package.LABEL, R2, R3, R4])
def test_old_package_is_closed_despite_original_approval(tmp_path, monkeypatch, operation, label):
    monkeypatch.setattr(package, "ROOT", tmp_path / "old")
    monkeypatch.setattr(package, "RENEWAL_ROOT", tmp_path / "r2")
    monkeypatch.setattr(package, "R3_ROOT", tmp_path / "r3")
    monkeypatch.setattr(package, "_approval", lambda **_: pytest.fail("old package must close before approval"))
    with pytest.raises(package.reference.ReferenceBlocked, match="closed"):
        package.main([operation, "--package", label, "--execute", "--live-model", "--live-orca",
            "--live-network", "--live-opi", "--variant", package.MODEL_SLOTS[0], "--system", "water", "--repetition", "1"])
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("label", [package.LABEL, R2, R3, R4])
@pytest.mark.parametrize("entry", ["prepare", "evaluate"])
def test_low_level_entry_cannot_bypass_closed_or_unapproved_package(tmp_path, monkeypatch, label, entry):
    monkeypatch.setattr(models, "ROOT", tmp_path / "models")
    monkeypatch.setattr(package, "R4_ROOT", tmp_path / "r4")
    monkeypatch.setattr(package.reference, "R4_APPROVAL_SHA256", None)
    monkeypatch.setattr(models.agent, "execute", lambda *_, **__: pytest.fail("no HTTP or Run execution"))
    kwargs = {"allow_live": True} if entry == "evaluate" else {}
    with pytest.raises(package.reference.ReferenceBlocked, match="closed|explicit human approval"):
        getattr(models, entry)(package.MODEL_SLOTS[0], 1, category="development", freeze_label=label, **kwargs)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("changed", ["variant", "repetition", "category", "profile", "resume"])
def test_renewal_rejects_extra_identity_and_resume_before_approval(changed, monkeypatch):
    # Test the archived shape guard independently from its new permanent close.
    monkeypatch.setattr(package, "_assert_open", lambda label: None if label == R4 else OPEN_GUARD(label))
    options = {"variant": package.MODEL_SLOTS[0], "repetition": 1, "category": "development",
               "package": R4, "model_profile": "disabled", "resume": False}
    key, value = {"variant": ("variant", "V-01/success"), "repetition": ("repetition", 2),
        "category": ("category", "formal"), "profile": ("model_profile", "thinking_low"),
        "resume": ("resume", True)}[changed]
    options[key] = value
    monkeypatch.setattr(package, "_execution_gate", lambda **_: pytest.fail("reject identity before gate"))
    with pytest.raises(package.reference.ReferenceBlocked, match="outside this fixed package"):
        package.guard_model_slot(**options)


@pytest.fixture
def renewal_approved(approved, tmp_path, monkeypatch):
    book, *rest = approved
    package.apply_limits(execute=True)  # synthetic fourth amendment baseline
    # Only this synthetic historical migration can audit the now-closed r2 path.
    monkeypatch.setattr(package, "_assert_open", lambda label: None if label == R2 else OPEN_GUARD(label))
    path = tmp_path / "synthetic-renewal-approval.json"
    value = {"approval_id": package.reference.RENEWAL_APPROVAL_ID, "status": "user_approved",
        "previous_limits": package.reference.BOUNDED_LIMITS, "approved_limits": package.reference.RENEWAL_LIMITS,
        "previous_approval_id": package.reference.BOUNDED_APPROVAL_ID,
        "previous_approval_sha256": package.reference.BOUNDED_APPROVAL_SHA256,
        "development_package": package.scope(package=R2),
        "approval_baseline": {"ledger_sha256": sha256_file(book.ledger.path)}}
    package.reference._save(path, value)
    monkeypatch.setattr(package.reference, "RENEWAL_APPROVAL", path)
    monkeypatch.setattr(package.reference, "RENEWAL_APPROVAL_SHA256", sha256_file(path))
    return book, *rest


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_fifth_amendment_preserves_every_cost_and_prior_receipt(renewal_approved, point):
    book, *_ = renewal_approved
    before = book.snapshot()
    files = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    def fault(where):
        if where == point:
            raise OSError("synthetic renewal interruption")
    if point:
        with pytest.raises(OSError):
            package.apply_limits(execute=True, package=R2, fault=fault)
    receipt = package.apply_limits(execute=True, package=R2)
    after = book.snapshot()
    assert after["limits"] == package.reference.RENEWAL_LIMITS
    assert {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["unknown_tokens"] == 150
    assert all(path.read_bytes() == content for path, content in files.items())
    assert package.apply_limits(execute=True, package=R2) == receipt
    assert book.snapshot() == after
    assert not (book.ledger.root / R2 / "batch-ledger.json").exists()


@pytest.mark.parametrize("change", ["pin", "scope", "baseline", "missing_baseline", "old_record", "prior_receipt"])
def test_renewal_approval_cannot_rewrite_history_or_expand_scope(renewal_approved, change, monkeypatch):
    book, _, _, known, _ = renewal_approved
    path = package.reference.RENEWAL_APPROVAL
    if change in {"pin", "scope", "baseline", "missing_baseline"}:
        value = package.reference._json(path)
        if change == "scope":
            value["development_package"]["repetitions"]["water_opt"] = 4
        elif change == "baseline":
            value["approval_baseline"]["ledger_sha256"] = "0" * 64
        elif change == "missing_baseline":
            value.pop("approval_baseline")
        else:
            value["status"] = "proposal_only"
        package.reference._save(path, value)
        if change != "pin":
            # A new pin alone cannot loosen the exact fixed package/baseline.
            monkeypatch.setattr(package.reference, "RENEWAL_APPROVAL_SHA256", sha256_file(path))
        with pytest.raises(package.reference.ReferenceBlocked):
            package.apply_limits(execute=True, package=R2)
    else:
        package.apply_limits(execute=True, package=R2)
        if change == "old_record":
            value = book.snapshot()
            del value["model_records"][known["id"]]
            package.reference._save(book.ledger.path, value)
        else:
            old = book.ledger.root / "budget-amendments" / package.reference.BOUNDED_APPROVAL_ID / "amendment.json"
            old.write_bytes(old.read_bytes() + b" ")
        with pytest.raises(package.reference.ReferenceBlocked):
            book.snapshot()


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    # Exercise the archived operator's invariants with an explicit synthetic
    # bypass; production r4 remains closed at every entry point.
    monkeypatch.setattr(package, "_assert_open", lambda label: None if label == R4 else OPEN_GUARD(label))
    root = tmp_path / "r4"
    monkeypatch.setattr(package, "R4_ROOT", root)
    monkeypatch.setattr(models, "ROOT", tmp_path / "evaluations")
    monkeypatch.setattr(package, "_approval", lambda **_: {"development_package": package.scope(package=R4)})
    monkeypatch.setattr(package.reference, "R4_APPROVAL_SHA256", "synthetic-approved-pin")
    monkeypatch.setattr(package.freeze, "runtime_environment", lambda: {"python": "synthetic-runtime"})
    monkeypatch.setattr(package.importlib.metadata, "version", lambda _: "synthetic-rdkit")
    monkeypatch.setattr(package, "_source_files", lambda: {"source.py": "synthetic-source-hash"})
    orca, mpi = tmp_path / "orca.exe", tmp_path / "mpi.exe"
    orca.write_bytes(b"never executed")
    mpi.write_bytes(b"never executed")
    config = Config(orca_path=orca, mpi_path=mpi)
    monkeypatch.setattr(package.freeze, "evaluation_config", lambda **_: config)
    snapshot = {"limits": copy.deepcopy(package.reference.R4_LIMITS)}
    monkeypatch.setattr(package.budget, "AcceptanceBudget", lambda _: SimpleNamespace(snapshot=lambda: snapshot))
    monkeypatch.setattr(package, "Store", lambda _: SimpleNamespace())
    monkeypatch.setattr(package, "_no_unknown_package_cost", lambda **_: None)
    record = {"scope": package.scope(package=R4), "source_files": package._source_files(),
        "runtime": {"python": "synthetic-runtime", "rdkit_version": "synthetic-rdkit"},
        "configuration": config.model_dump(mode="json"), "approval_sha256": "synthetic-approved-pin",
        "binaries": {"orca": {"path": str(orca), "sha256": sha256_file(orca)},
                     "mpi": {"path": str(mpi), "sha256": sha256_file(mpi)}}}
    package._save(root / "candidate.json", record)
    return root, snapshot, record


@pytest.mark.parametrize("entry", ["prepare", "evaluate"])
@pytest.mark.parametrize("change", ["candidate_missing", "source", "budget", "binary", "previous_failed"])
def test_direct_model_entry_requires_current_candidate_budget_and_prior_pass(candidate, monkeypatch, entry, change):
    root, snapshot, record = candidate
    if change == "candidate_missing":
        (root / "candidate.json").unlink()
    elif change == "source":
        monkeypatch.setattr(package, "_source_files", lambda: {"source.py": "changed"})
    elif change == "budget":
        snapshot["limits"] = package.reference.BOUNDED_LIMITS
    elif change == "binary":
        from pathlib import Path
        Path(record["binaries"]["orca"]["path"]).write_bytes(b"changed")
    monkeypatch.setattr(models, "regrade", lambda *_, **__: {"status": "failed"})
    if change == "previous_failed":
        prior = models._slot(package.MODEL_SLOTS[0], 1, "development", R4)
        package._save(prior / "metadata.json", {"bounded_candidate_sha256": sha256_file(root / "candidate.json")})
    monkeypatch.setattr(models.cases, "create_request", lambda *_, **__: pytest.fail("no Run allocation after failed gate"))
    kwargs = {"allow_live": True} if entry == "evaluate" else {}
    with pytest.raises((package.reference.ReferenceBlocked, FileNotFoundError)):
        getattr(models, entry)(package.MODEL_SLOTS[1], 1, category="development", freeze_label=R4, **kwargs)


def test_low_level_guard_uses_only_new_label_previous_evidence(candidate, monkeypatch):
    root, _, _ = candidate
    observed = []
    def regrade(variant, rep, **options):
        observed.append((variant, rep, options))
        return {"status": "passed"}
    monkeypatch.setattr(models, "regrade", regrade)
    prior = models._slot(package.MODEL_SLOTS[0], 1, "development", R4)
    package._save(prior / "metadata.json", {"bounded_candidate_sha256": sha256_file(root / "candidate.json")})
    digest = package.guard_model_slot(package.MODEL_SLOTS[1], 1, category="development",
        package=R4, model_profile="disabled")
    assert digest == sha256_file(root / "candidate.json")
    assert observed == [(package.MODEL_SLOTS[0], 1, {"category": "development", "freeze_label": R4})]


def test_unknown_model_cost_in_new_package_stops_other_slots(tmp_path, monkeypatch):
    monkeypatch.setattr(models, "ROOT", tmp_path / "evaluations")
    monkeypatch.setattr(package, "R4_ROOT", tmp_path / "r4")
    slot = models._slot(package.MODEL_SLOTS[0], 1, "development", R4)
    package._save(slot / "ready.json", {"run_id": "new-unknown-run"})
    observed = []
    def load_run(run_id):
        observed.append(run_id)
        return SimpleNamespace(state="paused", model_records=[{"status": "unknown"}], attempts=[], calls=[])
    monkeypatch.setattr(package, "Store", lambda _: SimpleNamespace(load_run=load_run))
    with pytest.raises(package.reference.ReferenceBlocked, match="unknown cost/process"):
        package._no_unknown_package_cost(package=R4)
    assert observed == ["new-unknown-run"]


@pytest.mark.parametrize("label", [package.LABEL, R2, R3, R4])
def test_old_regrade_remains_read_only_after_package_closure(tmp_path, monkeypatch, label):
    monkeypatch.setattr(models, "ROOT", tmp_path / "evaluations")
    variant = package.MODEL_SLOTS[1]
    slot = models._slot(variant, 1, "development", label)
    metadata = {"variant_id": variant, "repetition": 1, "category": "development",
                "freeze_label": label, "run_id": "historical-run"}
    package._save(slot / "metadata.json", metadata)
    package._save(slot / "ready.json", {"run_id": "historical-run", "metadata_sha256": sha256_file(slot / "metadata.json")})
    monkeypatch.setattr(models, "Store", lambda _: SimpleNamespace(load_run=lambda _: SimpleNamespace(batch_category="development")))
    monkeypatch.setattr(models.cases, "evaluate_response", lambda *_, **__: {"status": "incomplete_or_failed"})
    monkeypatch.setattr(package, "_execution_gate", lambda **_: pytest.fail("audit must not need execution gate"))
    assert models.regrade(variant, 1, category="development", freeze_label=label)["status"] == "incomplete_or_failed"
    assert not (slot / "reservation.json").exists()


def test_new_model_slot_binds_candidate_and_rejects_replacement(candidate, tmp_path, monkeypatch):
    from orca_agent.store import Store, StoreError
    root, _, record = candidate
    monkeypatch.setattr(models, "ROOT", tmp_path / "evaluations")
    monkeypatch.setattr(models, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(models, "Store", lambda path: Store(path, environment_root=tmp_path / "environment"))
    store, run, metadata, slot = models.prepare(package.MODEL_SLOTS[0], 1,
        category="development", freeze_label=R4)
    assert metadata["bounded_candidate_sha256"] == sha256_file(root / "candidate.json")
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert not run.model_records and not run.attempts
    repeated = models.prepare(package.MODEL_SLOTS[0], 1, category="development", freeze_label=R4)
    assert repeated[1].id == run.id
    # Even a replacement whose current fields pass the source gate cannot bind
    # an existing Run to new candidate bytes.
    record["commit"] = "another-offline-candidate"
    package.reference._save(root / "candidate.json", record)
    with pytest.raises(StoreError, match="immutable package candidate"):
        models.prepare(package.MODEL_SLOTS[0], 1, category="development", freeze_label=R4)
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert models._read(slot / "metadata.json") == metadata


def test_prior_pass_cannot_come_from_another_candidate(candidate, monkeypatch):
    root, _, _ = candidate
    prior = models._slot(package.MODEL_SLOTS[0], 1, "development", R4)
    package._save(prior / "metadata.json", {"bounded_candidate_sha256": "different-candidate"})
    monkeypatch.setattr(models, "regrade", lambda *_, **__: pytest.fail("mismatched candidate cannot count as prior pass"))
    with pytest.raises(package.reference.ReferenceBlocked, match="another package candidate"):
        package.guard_model_slot(package.MODEL_SLOTS[1], 1, category="development",
            package=R4, model_profile="disabled")


def test_recorded_human_renewal_approval_binds_unchanged_proposal_and_scope():
    value = package.reference.renewal_approval()
    assert value["user_statement"] == "批准，继续"
    assert value["response_mode"] == "subsequent free-form user turn; not an option selection"
    assert value["development_package"] == package.scope(package=R2)
    assert value["previous_approval_sha256"] == package.reference.BOUNDED_APPROVAL_SHA256
    assert value["decision_document_sha256"] == sha256_file(package.PROJECT / value["decision_document"])
    assert value["approval_baseline"]["ledger_sha256"] == "15f07f9cf0f86619c4be382349411e33f32c6d34e83bd195626b5fceb0acbe51"
    assert value["application_status_at_recording"] == "not_applied"
    assert value["execution_status_at_recording"] == "not_executed"
    assert value["acceptance_status"] == "not_verified"
