"""Formal joint metadata reuse is checked without HTTP, ORCA or live accounting."""

import json
from types import SimpleNamespace

import pytest

from tests.helpers import phase_b_freeze, phase_b_joint


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
    monkeypatch.setattr(phase_b_freeze, "validate_freeze", lambda _: frozen.copy())
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
