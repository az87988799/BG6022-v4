"""The live evaluation driver is inert by default and cannot reset a used slot."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.store import StoreError, sha256_file

PROJECT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fixed_model_driver_test", PROJECT / "tests/helpers/phase_b_model_evaluation.py")
DRIVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRIVER)


def activate(driver, label="formal-v1", commit="offline-test"):
    path = driver.PROJECT / "docs/acceptance/phase-b/formal-freeze.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"freeze_label": label, "code_commit": commit,
                               "files": {"offline-fixture": commit}}), encoding="utf-8")
    return path


@pytest.fixture
def driver(tmp_path, monkeypatch):
    monkeypatch.setattr(DRIVER, "ROOT", tmp_path / "evaluations")
    monkeypatch.setattr(DRIVER, "STORE_ROOT", tmp_path / "store")
    monkeypatch.setattr(DRIVER, "PROJECT", tmp_path / "project")
    activate(DRIVER)

    def validate(label, **kwargs):
        path = DRIVER.PROJECT / "docs/acceptance/phase-b/formal-freeze.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        if record["freeze_label"] != label:
            raise ValueError("formal evaluation label differs from the active freeze")
        return {"code_commit": record["code_commit"], "freeze_label": label, "freeze_sha256": sha256_file(path)}

    module = DRIVER._module
    monkeypatch.setattr(DRIVER, "_module", lambda name, filename:
        SimpleNamespace(validate_freeze=validate)
        if filename == "phase_b_freeze.py" else module(name, filename))
    return DRIVER


def test_live_gate_precedes_all_preparation(driver):
    with pytest.raises(StoreError, match="explicit --live-model"):
        driver.evaluate("V-01/synonym-single-point", 1)
    assert not driver.ROOT.exists()


def test_repeated_prepare_returns_exact_run_and_three_slots_stay_distinct(driver):
    first = driver.prepare("V-01/synonym-single-point", 1)
    repeated = driver.prepare("V-01/synonym-single-point", 1)
    second = driver.prepare("V-01/synonym-single-point", 2)
    assert first[1].id == repeated[1].id != second[1].id
    assert first[2] == repeated[2]
    assert not first[1].model_records and not first[1].attempts


def test_development_is_separate_and_only_active_formal_freeze_may_prepare(driver):
    development = driver.prepare("V-01/synonym-single-point", 1, category="development", freeze_label="probe-v1")
    activate(driver, "final-v1")
    formal = driver.prepare("V-01/synonym-single-point", 1, freeze_label="final-v1")
    assert development[1].id != formal[1].id
    assert development[1].batch_category == "development"
    with pytest.raises(ValueError, match="active freeze"):
        driver.prepare("V-01/synonym-single-point", 1, freeze_label="final-v2")


def test_formal_label_cannot_rebind_existing_slots_to_a_different_code_freeze(driver):
    store, run, _, directory = driver.prepare("V-01/synonym-single-point", 1)
    run_before = store.path(f"runs/{run.id}/run.json").read_bytes()
    metadata_before = (directory / "metadata.json").read_bytes()
    activate(driver, commit="changed-code")
    with pytest.raises(StoreError, match="already bound"):
        driver.prepare("V-01/synonym-single-point", 1)
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == run_before
    assert (directory / "metadata.json").read_bytes() == metadata_before


def test_new_valid_formal_freeze_preserves_old_runs_snapshots_and_shared_costs(driver, tmp_path, monkeypatch):
    from test_phase_b_budget import record

    monkeypatch.setattr(driver.budget.reference, "BATCH_ROOT", tmp_path / "shared-batch")
    monkeypatch.setattr(driver.budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "absent-delivered.json")
    store, original, metadata, directory = driver.prepare("V-01/synonym-single-point", 1)
    book = driver.budget.AcceptanceBudget(store)
    reserved = record(71)
    book.reserve_model(original, reserved)
    original.model_records.append(reserved)
    store._write_json(f"runs/{original.id}/run.json", original)  # Explicit offline reservation fixture.
    budget_before = book.snapshot()
    ledger_before = book.ledger.path.read_bytes()
    run_before = store.path(f"runs/{original.id}/run.json").read_bytes()
    frozen_before = {path: path.read_bytes() for path in (driver.ROOT / "formal-freezes").iterdir()}
    # Preserve the old driver anchor too; it is never rewritten by migration.
    legacy = driver.ROOT / "formal-freeze.json"
    old_anchor = driver._read(driver.ROOT / "formal-freezes/formal-v1.anchor.json")
    driver._write(legacy, {key: old_anchor[key] for key in (
        "freeze_label", "spec_sha256", "variant_ids", "repetitions")}, immutable=True)
    legacy_before = legacy.read_bytes()
    activate(driver, "formal-v2", "repaired-code")
    next_store, replacement, revised, _ = driver.prepare("V-01/synonym-single-point", 1, freeze_label="formal-v2")
    assert replacement.id != original.id and next_store.root == store.root
    assert revised["freeze_sha256"] != metadata["freeze_sha256"]
    assert driver.budget.AcceptanceBudget(next_store).snapshot() == budget_before
    assert book.ledger.path.read_bytes() == ledger_before
    assert store.path(f"runs/{original.id}/run.json").read_bytes() == run_before
    assert driver._read(directory / "metadata.json") == metadata
    assert all(path.read_bytes() == content for path, content in frozen_before.items())
    assert legacy.read_bytes() == legacy_before
    assert len(list((driver.ROOT / "formal-freezes").glob("*.anchor.json"))) == 2
    # Historical review is still possible without activating or replaying its old Run.
    assert driver.regrade("V-01/synonym-single-point", 1)["status"] == "not_verified"


@pytest.mark.parametrize("mutation", ["missing", "changed"])
def test_new_formal_batch_requires_every_prior_exact_freeze_snapshot(driver, mutation):
    driver.prepare("V-01/synonym-single-point", 1)
    snapshot = driver.ROOT / "formal-freezes/formal-v1.freeze.json"
    if mutation == "missing":
        snapshot.unlink()
    else:
        snapshot.write_text('{"tampered":true}', encoding="utf-8")
    activate(driver, "formal-v2", "repaired-code")
    with pytest.raises(StoreError, match="prior formal freeze snapshot"):
        driver.prepare("V-01/synonym-single-point", 1, freeze_label="formal-v2")
    assert len(list((driver.STORE_ROOT / "runs").iterdir())) == 1


def test_legacy_single_anchor_migrates_only_under_its_exact_original_active_freeze(driver):
    first = driver.prepare("V-01/synonym-single-point", 1)
    archive = driver.ROOT / "formal-freezes"
    identity = driver._read(archive / "formal-v1.anchor.json")
    legacy = driver.ROOT / "formal-freeze.json"
    driver._write(legacy, {key: identity[key] for key in (
        "freeze_label", "spec_sha256", "variant_ids", "repetitions")}, immutable=True)
    legacy_bytes = legacy.read_bytes()
    for path in archive.iterdir():
        path.unlink()  # Only the old driver layout remains in this temporary fixture.
    repeated = driver.prepare("V-01/synonym-single-point", 1)
    assert repeated[1].id == first[1].id and repeated[2] == first[2]
    assert legacy.read_bytes() == legacy_bytes
    activate(driver, "formal-v2", "repaired-code")
    assert driver.prepare("V-01/synonym-single-point", 1, freeze_label="formal-v2")[1].id != first[1].id
    assert legacy.read_bytes() == legacy_bytes


def test_deleting_an_old_anchor_cannot_make_its_formal_history_disappear(driver):
    driver.prepare("V-01/synonym-single-point", 1)
    (driver.ROOT / "formal-freezes/formal-v1.anchor.json").unlink()
    activate(driver, "formal-v2", "repaired-code")
    with pytest.raises(StoreError, match="prior formal batch"):
        driver.prepare("V-01/synonym-single-point", 1, freeze_label="formal-v2")
    assert len(list((driver.STORE_ROOT / "runs").iterdir())) == 1


def test_interrupted_preparation_never_creates_second_run(driver, monkeypatch):
    original = driver.cases.create_request

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("offline interruption after Run creation")

    monkeypatch.setattr(driver.cases, "create_request", crash)
    with pytest.raises(RuntimeError, match="interruption"):
        driver.prepare("V-01/synonym-single-point", 1)
    with pytest.raises(StoreError, match="interrupted"):
        driver.prepare("V-01/synonym-single-point", 1)
    assert len(list((driver.STORE_ROOT / "runs").iterdir())) == 1


def test_source_gap_never_calls_agent_or_budget(driver, monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("source gap must not send or reserve")

    monkeypatch.setattr(driver.agent, "execute", denied)
    monkeypatch.setattr(driver.budget, "AcceptanceBudget", denied)
    report = driver.evaluate("V-09/compatible-water", 1, allow_live=True)
    assert report["fixture_gaps"] and report["status"] == "not_verified"
    assert not report["http_requests"]


def test_finished_trajectory_regrades_without_reexecuting(driver, monkeypatch):
    calls = []

    class OfflineBatch:
        def __init__(self, store):
            self.store = store

        def snapshot(self):
            return {}

    def offline_execute(store, config, run_id, **kwargs):
        calls.append(run_id)
        assert config.orca_path is None and config.mpi_path is None
        run = store.load_run(run_id)
        run.state = "failed"
        store.save_run(run)
        return run

    monkeypatch.setattr(driver.budget, "AcceptanceBudget", OfflineBatch)
    monkeypatch.setattr(driver.agent, "execute", offline_execute)
    first = driver.evaluate("V-01/synonym-single-point", 1, allow_live=True)
    repeated = driver.evaluate("V-01/synonym-single-point", 1, allow_live=True)
    assert len(calls) == 1 and first == repeated
    assert first["status"] == "not_verified"
    assert driver.regrade("V-01/synonym-single-point", 1) == first


def test_tampered_metadata_and_wrong_review_are_rejected(driver, tmp_path):
    _, _, metadata, directory = driver.prepare("V-01/synonym-single-point", 1)
    review = driver.review_template(metadata)
    assert review["all_proposal_facts_passed"] is None
    assert review["semantic_review_passed"] is None
    review["run_id"] = "wrong_run"
    review_path = tmp_path / "review.json"
    review_path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(StoreError, match="different evaluation"):
        driver.regrade("V-01/synonym-single-point", 1, review_path=review_path)
    metadata["repetition"] = 3
    (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(StoreError, match="metadata changed"):
        driver.prepare("V-01/synonym-single-point", 1)


def test_default_cli_lists_frozen_ids_without_creating_state(driver, capsys):
    assert driver.main([]) == 0
    output = json.loads(capsys.readouterr().out)
    assert len(output["variants"]) == 40 and output["http_executed"] is False
    assert len(driver.cases.fixed_variant_ids()) == 25
    assert set(driver.cases.fixed_variant_ids()) < set(output["variants"])
    assert set(driver.cases.sampling_intent_variant_ids(real_model_only=True)) <= set(output["variants"])
    assert not (set(driver.cases.sampling_intent_variant_ids())
                - set(driver.cases.sampling_intent_variant_ids(real_model_only=True))) & set(output["variants"])
    assert not driver.ROOT.exists()
