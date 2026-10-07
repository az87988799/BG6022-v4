"""Formal joint metadata reuse is checked without HTTP, ORCA or live accounting."""

import json
from types import SimpleNamespace

import pytest

from tests.helpers import phase_b_freeze, phase_b_joint


@pytest.mark.parametrize("case,starts,extra", [
    ("sampling_left", 4, 1), ("sampling_right", 4, 1), ("sampling_stop", 3, 0),
])
def test_sampling_run_budgets_match_frozen_case_allocation(tmp_path, monkeypatch, case, starts, extra):
    from orca_agent import doctor
    from orca_agent.config import Config
    from orca_agent.store import Store

    initialize = phase_b_joint.initialize_agent
    monkeypatch.setattr(doctor, "diagnose", lambda _: pytest.fail("offline preparation must not probe ORCA"))
    monkeypatch.setattr(phase_b_joint, "initialize_agent", lambda *args, **kwargs: initialize(
        *args, **kwargs, defer_environment=True))
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run, _ = phase_b_joint.prepare_case(store, Config(data_root=store.root), case, "development")
    persisted = store.load_run(run.id)
    allocations = json.loads((phase_b_joint.PROJECT / "tests/fixtures/phase_b/cases.json").read_text(
        encoding="utf-8"))["batch_budget"]["formal_allocations"]
    allocation = next(item for item in allocations if item["id"] == case)
    assert persisted.budget.orca_starts == starts == allocation["starts_per_repeat"]
    assert persisted.budget.extra_orca_starts == extra
    assert persisted.budget.model_calls == 8 and persisted.budget.model_tokens == 48000
    assert not persisted.attempts and not persisted.model_records


@pytest.fixture
def existing(tmp_path, monkeypatch):
    frozen = {"freeze_label": "formal-v1", "code_commit": "offline-commit", "freeze_sha256": "a" * 64}
    metadata = {"case": "repair_success", "category": "formal", "freeze": frozen.copy(), "run_id": "offline-run"}
    run = SimpleNamespace(id="offline-run", batch_category="formal")
    directory = tmp_path / "evaluations"
    directory.mkdir()
    path = directory / "formal-v1-repair_success-1.json"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    store = SimpleNamespace(root=tmp_path / "agent", load_run=lambda identity: run)
    monkeypatch.setattr(phase_b_joint, "ROOT", tmp_path)
    monkeypatch.setattr(phase_b_joint, "Store", lambda _: store)
    monkeypatch.setattr(phase_b_freeze, "validate_freeze", lambda _, **kwargs: frozen.copy())
    monkeypatch.setattr(phase_b_joint, "prepare_case", lambda *_: pytest.fail("must reuse existing Run"))
    monkeypatch.setattr(phase_b_joint, "execute", lambda *_args, **_kwargs: pytest.fail("must reject before execution"))
    monkeypatch.setattr(phase_b_joint, "AcceptanceBudget", lambda *_: pytest.fail("must not touch a batch ledger"))
    return path, metadata, run, frozen


@pytest.mark.parametrize("change", ["missing_freeze", "freeze_hash", "commit", "label", "run_category", "metadata_category"])
@pytest.mark.parametrize("resume", [False, True])
def test_existing_formal_identity_cannot_rebind_freeze_or_category(existing, change, resume):
    path, metadata, run, _ = existing
    if change == "missing_freeze":
        metadata.pop("freeze")
    elif change == "freeze_hash":
        metadata["freeze"]["freeze_sha256"] = "b" * 64
    elif change == "commit":
        metadata["freeze"]["code_commit"] = "another-commit"
    elif change == "label":
        metadata["freeze"]["freeze_label"] = "another-label"
    elif change == "run_category":
        run.batch_category = "development"
    else:
        metadata["category"] = "development"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="freeze|category|rebound"):
        phase_b_joint.run_case("repair_success", "formal", path.stem,
                              live_model=True, live_orca=True, resume=resume)
    assert path.read_bytes() == before


def test_same_formal_freeze_reuses_record_without_any_transmission(existing):
    path, metadata, expected, _ = existing
    run, saved = phase_b_joint.run_case("repair_success", "formal", path.stem, live_model=True, live_orca=True)
    assert run is expected and saved == metadata


def test_explicit_resume_keeps_same_freeze_and_same_run(existing, monkeypatch):
    path, metadata, expected, _ = existing
    calls = []
    monkeypatch.setattr(phase_b_joint, "AcceptanceBudget", lambda _: "isolated-offline-accounting")

    def offline_execute(store, config, identity, *, resume, batch):
        calls.append((identity, resume, batch))
        return expected

    monkeypatch.setattr(phase_b_joint, "execute", offline_execute)
    monkeypatch.setattr(phase_b_joint, "build_report", lambda *_: {"offline_fixture_only": True})
    monkeypatch.setattr(phase_b_joint, "render_report", lambda _: "offline fixture")
    run, saved = phase_b_joint.run_case("repair_success", "formal", path.stem,
                                      live_model=True, live_orca=True, resume=True)
    assert run is expected and saved == metadata
    assert calls == [(expected.id, True, "isolated-offline-accounting")]
