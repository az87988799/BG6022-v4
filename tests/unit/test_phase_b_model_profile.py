"""Explicit evaluation modes remain frozen without HTTP, ORCA, or live budgets."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_phase_b_budget import bind_model, budget, make_run, record, settle_model
from test_phase_b_budget import setup as setup
from test_phase_b_freeze import execution_freeze as execution_freeze
from test_phase_b_freeze import frozen as frozen
from test_phase_b_joint_driver import existing as existing
from test_phase_b_model_evaluation import driver as driver

from orca_agent.config import Config
from orca_agent.store import StoreError, sha256_file
from tests.helpers import phase_b_formal_ops as ops
from tests.helpers import phase_b_freeze as gate
from tests.helpers import phase_b_joint as joint


@pytest.mark.parametrize("profile", ["disabled", "thinking_low"])
def test_slot_mode_is_frozen_before_http_and_cannot_change(driver, profile):
    options = {"category": "development", "freeze_label": "explicit-mode", "model_profile": profile}
    store, run, metadata, directory = driver.prepare("V-01/synonym-single-point", 1, **options)
    assert metadata["model_profile"] == driver._read(directory / "reservation.json")["model_profile"] == profile
    assert driver.prepare("V-01/synonym-single-point", 1, **options)[1].id == run.id
    protected = [*directory.glob("*.json"), store.path(f"runs/{run.id}/run.json")]
    before = {path: path.read_bytes() for path in protected}
    changed = {**options, "model_profile": "thinking_low" if profile == "disabled" else "disabled"}
    with pytest.raises(StoreError, match="profile cannot change"):
        driver.prepare("V-01/synonym-single-point", 1, **changed)
    assert {path: path.read_bytes() for path in protected} == before
    assert not run.model_records and not run.attempts and run.usage.model_calls == 0
    assert not run.permission.scientific_execution and run.budget.orca_starts == 0


def test_legacy_slot_missing_mode_remains_disabled_without_rewriting(driver):
    options = {"category": "development", "freeze_label": "legacy-mode"}
    _, run, metadata, directory = driver.prepare("V-01/synonym-single-point", 1, **options)
    metadata.pop("model_profile")
    driver._write(directory / "metadata.json", metadata)
    binding = driver._read(directory / "ready.json")
    binding["metadata_sha256"] = sha256_file(directory / "metadata.json")
    driver._write(directory / "ready.json", binding)
    reservation = driver._read(directory / "reservation.json")
    reservation.pop("model_profile")
    driver._write(directory / "reservation.json", reservation)
    before = {path: path.read_bytes() for path in directory.glob("*.json")}
    assert driver.prepare("V-01/synonym-single-point", 1, **options)[1].id == run.id
    with pytest.raises(StoreError, match="profile cannot change"):
        driver.prepare("V-01/synonym-single-point", 1, model_profile="thinking_low", **options)
    assert {path: path.read_bytes() for path in before} == before


@pytest.mark.parametrize("profile", ["auto", "enabled", True, None])
def test_invalid_mode_rejected_before_preparation(driver, profile):
    with pytest.raises(ValidationError):
        driver.prepare("V-01/synonym-single-point", 1, category="development", model_profile=profile)
    assert not driver.ROOT.exists() and not driver.STORE_ROOT.exists()


@pytest.mark.parametrize("profile", ["disabled", "thinking_low"])
def test_formal_validation_and_agent_receive_the_same_mode_without_more_budget(driver, monkeypatch, profile):
    validated = []
    original = driver._module

    def module(name, filename):
        loaded = original(name, filename)
        if filename != "phase_b_freeze.py":
            return loaded

        def validate(label, *, config):
            validated.append(config.model_dump(mode="json"))
            return loaded.validate_freeze(label, config=config)
        return SimpleNamespace(validate_freeze=validate)

    monkeypatch.setattr(driver, "_module", module)
    monkeypatch.setattr(driver.budget, "AcceptanceBudget", lambda _: SimpleNamespace(snapshot=lambda: {}))
    called = []

    def execute(store, config, run_id, **kwargs):
        called.append(config.model_dump(mode="json"))
        assert called[-1] == validated[-1]
        assert config.orca_path is None and config.mpi_path is None
        run = store.load_run(run_id)
        assert run.budget.model_calls == 4 and run.budget.orca_starts == 0
        assert run.budget.output_tokens == 2000
        assert not run.permission.scientific_execution and not run.model_records
        run.state = "failed"
        store.save_run(run)
        return run

    monkeypatch.setattr(driver.agent, "execute", execute)
    report = driver.evaluate("V-01/synonym-single-point", 1, allow_live=True, model_profile=profile)
    assert report["status"] == "not_verified" and called[0]["model_profile"] == profile
    assert driver.evaluate("V-01/synonym-single-point", 1, allow_live=True, model_profile=profile) == report
    assert len(called) == 1
    with pytest.raises(StoreError, match="profile cannot change"):
        driver.evaluate("V-01/synonym-single-point", 1, allow_live=True, resume=True,
                        model_profile="thinking_low" if profile == "disabled" else "disabled")
    assert len(called) == 1


def test_explicit_cli_mode_reaches_prepare_but_never_grants_live_execution(driver, capsys):
    assert driver.main(["--variant", "V-01/synonym-single-point", "--category", "development",
                        "--freeze-label", "mode-cli", "--model-profile", "thinking_low", "--prepare"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["http_executed"] is False
    assert driver._read(driver._slot("V-01/synonym-single-point", 1, "development", "mode-cli") /
                        "metadata.json")["model_profile"] == "thinking_low"
    with pytest.raises(StoreError, match="explicit --live-model"):
        driver.main(["--variant", "V-01/synonym-single-point", "--category", "development",
                     "--freeze-label", "mode-cli", "--model-profile", "thinking_low", "--execute"])


@pytest.mark.parametrize("resume", [False, True])
def test_legacy_joint_metadata_cannot_switch_mode(existing, resume):
    path, _, _, _ = existing
    before = path.read_bytes()
    with pytest.raises(ValueError, match="profile cannot change"):
        joint.run_case("repair_success", "formal", path.stem, live_model=True, live_orca=True,
                       resume=resume, model_profile="thinking_low")
    assert path.read_bytes() == before


def test_joint_resume_uses_explicit_frozen_mode(existing, monkeypatch):
    path, metadata, expected, _ = existing
    metadata["model_profile"] = "thinking_low"
    path.write_text(json.dumps(metadata), encoding="utf-8")
    before = path.read_bytes()
    seen = []
    monkeypatch.setattr(joint, "AcceptanceBudget", lambda _: "offline-no-accounting")

    def execute(store, config, identity, *, resume, batch):
        seen.append((config.model_profile, identity, resume, batch))
        return expected

    monkeypatch.setattr(joint, "execute", execute)
    monkeypatch.setattr(joint, "build_report", lambda *_: {"offline_only": True})
    monkeypatch.setattr(joint, "render_report", lambda _: "offline only")
    actual, _ = joint.run_case("repair_success", "formal", path.stem, live_model=True, live_orca=True,
                               resume=True, model_profile="thinking_low")
    assert actual is expected
    assert seen == [("thinking_low", expected.id, True, "offline-no-accounting")]
    assert path.read_bytes() == before


def test_explicit_config_selection_is_validated_and_does_not_use_an_ambient_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCA_AGENT_MODEL_PROFILE", "thinking_low")
    assert gate.evaluation_config().model_profile == "disabled"
    assert gate.evaluation_config(model_profile="thinking_low").model_profile == "thinking_low"
    with pytest.raises(ValidationError):
        gate.evaluation_config(model_profile="auto")
    path = tmp_path / "science.toml"
    path.write_text('model_profile = "thinking_low"\n', encoding="utf-8")
    assert gate.evaluation_config(science=True, config_path=path).model_profile == "thinking_low"
    assert gate.evaluation_config(science=True, config_path=path, model_profile="disabled").model_profile == "disabled"


def test_formal_freeze_binds_profile_and_old_missing_field_is_static_only(execution_freeze):
    _, path, record, _ = execution_freeze
    low = gate.evaluation_config(model_profile="thinking_low")
    with pytest.raises(ValueError, match="configuration differs"):
        gate.validate_freeze("formal-test", config=low)
    record["configuration"]["model"] = low.model_dump(mode="json")
    path.write_text(json.dumps(record), encoding="utf-8")
    assert gate.validate_freeze("formal-test", config=low)
    with pytest.raises(ValueError, match="configuration differs"):
        gate.validate_freeze("formal-test")
    record["configuration"]["model"].pop("model_profile")
    path.write_text(json.dumps(record), encoding="utf-8")
    old = path.read_bytes()
    assert gate.validate_freeze("formal-test", execution=False)
    for config in (Config(), low):
        with pytest.raises(ValueError, match="configuration differs"):
            gate.validate_freeze("formal-test", config=config)
    assert path.read_bytes() == old


@pytest.mark.parametrize("profile,configured,expected", [
    ("disabled", None, "disabled"), ("thinking_low", None, "thinking_low"),
    (None, "thinking_low", "thinking_low"), ("disabled", "thinking_low", "disabled"),
])
def test_freeze_writer_binds_both_effective_configs_before_postwrite_validation(
        tmp_path, monkeypatch, profile, configured, expected):
    # Isolated complete freeze inputs; no live account, doctor, or actual Git write.
    coverage_name = "docs/acceptance/phase-b/coverage.json"
    revision_name = "docs/acceptance/phase-b/coverage-repair-v2.json"
    coverage = {"entries": [{"evidence_requirement": "real_model_with_frozen_evidence",
                              "formal_slots": [{"variant_id": "isolated", "repetition": 1}]}]}
    revision = {"final_matrix_complete": True, "development_gates_verified": True,
                "execution_budget_review_complete": True, "final_slot_count": 1}
    names = sorted([coverage_name, revision_name])
    for name, value in [(coverage_name, coverage), (revision_name, revision)]:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    target = tmp_path / "freeze.json"
    config_path = None
    if configured is not None:
        config_path = tmp_path / "explicit-science.toml"
        config_path.write_text(f'model_profile = "{configured}"\n', encoding="utf-8")
    monkeypatch.setattr(ops, "PROJECT", tmp_path)
    monkeypatch.setattr(gate, "PROJECT", tmp_path)
    monkeypatch.setattr(ops, "FREEZE", target)
    monkeypatch.setattr(ops, "FIXED", set(names))
    monkeypatch.setattr(ops, "git", lambda *args: ("\0".join(names).encode() if args == ("ls-files", "-z")
                        else b"a" * 40 if args == ("rev-parse", "HEAD") else b""))
    monkeypatch.setattr(ops, "execution_files", lambda: names)
    monkeypatch.setattr(ops, "runtime_environment", lambda: {"offline_fixture": True})
    monkeypatch.setattr(ops, "execution_budget_authority", lambda: {"offline_fixture": True})
    monkeypatch.setattr(ops, "science_environment", lambda config: {"model_profile": config.model_profile})
    checked = []

    def validate(label, *, config):
        value = json.loads(target.read_text(encoding="utf-8"))
        assert value["configuration"]["model"] == config.model_dump(mode="json")
        assert value["configuration"]["science"]["model_profile"] == expected
        assert value["science_environment"]["model_profile"] == expected
        checked.append(config.model_profile)
        return {"freeze_label": label}

    monkeypatch.setattr(ops, "validate_freeze", validate)
    ops.freeze("explicit-mode", with_science=True, config_path=config_path, model_profile=profile)
    assert checked == [expected]


@pytest.mark.parametrize("profile", [None, "disabled", "thinking_low"])
@pytest.mark.parametrize("known", [False, True])
def test_batch_profile_binding_preserves_legacy_shape_cost_and_immutable_receipts(setup, profile, known):
    book, store = setup
    run, _, _ = make_run(store)
    item = record()
    if profile is not None:
        item["model_profile"] = profile
    book.reserve_model(run, item)
    bind_model(store, run, item)
    settle_model(book, store, run, item, known=known)
    snapshot = book.snapshot()
    basis = snapshot["model_records"][item["id"]]["record"]
    assert ("model_profile" in basis) == (profile is not None)
    assert basis.get("model_profile", "disabled") == (profile or "disabled")
    assert snapshot["model_usage"]["tokens"] == (50 if known else 150)
    files = [path for path in book.ledger.root.rglob("*") if path.is_file() and path.suffix != ".lock"]
    before = {path: path.read_bytes() for path in files}
    book.settle_model(run, item)
    assert book.snapshot() == snapshot and {path: path.read_bytes() for path in files} == before


@pytest.mark.parametrize("change", ["delete", "other_mode"])
def test_batch_rejects_changed_mode_binding_without_refunding_known_cost(setup, change):
    book, store = setup
    run, _, _ = make_run(store)
    item = {**record(), "model_profile": "thinking_low"}
    book.reserve_model(run, item)
    bind_model(store, run, item)
    settle_model(book, store, run, item)
    ledger_before = book.ledger.path.read_bytes()
    if change == "delete":
        item.pop("model_profile")
    else:
        item["model_profile"] = "disabled"
    store._write_json(f"runs/{run.id}/run.json", run)
    with pytest.raises(budget.ReferenceBlocked, match="binding"):
        book.snapshot()
    assert book.ledger.path.read_bytes() == ledger_before
