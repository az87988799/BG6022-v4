"""The acceptance driver binds data and budgets, never supplies a solution."""

import copy
import json
from pathlib import Path

import pytest

from tests.helpers import code_tools_acceptance as acceptance
from tests.helpers import code_tools_batch as batch
from tests.helpers.code_tools_freeze import FreezeError

PROJECT = Path(__file__).resolve().parents[2]


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def assets(tmp_path):
    for root in acceptance.ASSET_ROOTS:
        (tmp_path / root).mkdir(parents=True)
    for name in acceptance.REQUIRED:
        write(tmp_path / acceptance.DIRECTORY / name, {})
    return tmp_path


def test_assets_include_new_solver_and_non_python_files(assets):
    before = acceptance.asset_files(assets)
    new = assets / "tests/helpers/injected_solver.py"
    new.write_text("answer = 42", encoding="utf-8")
    data = assets / "scripts/extra-instructions.txt"
    data.write_text("new knowledge", encoding="utf-8")
    after = acceptance.asset_files(assets)
    assert set(after) - set(before) == {
        "tests/helpers/injected_solver.py", "scripts/extra-instructions.txt"}


def test_assets_detect_missing_and_modified_inputs(assets):
    path = assets / acceptance.DIRECTORY / "controls.json"
    before = acceptance.asset_files(assets)
    path.write_text('{"changed":true}', encoding="utf-8")
    assert before != acceptance.asset_files(assets)
    path.unlink()
    with pytest.raises(FreezeError, match="required"):
        acceptance.asset_files(assets)


def test_only_three_exact_report_paths_excluded(assets):
    before = acceptance.asset_files(assets)
    for relative in acceptance.EXCLUDED:
        write(assets / relative, {"result": "mutable"})
    assert acceptance.asset_files(assets) == before
    write(assets / acceptance.DIRECTORY / "reports/solver.json", {"hidden": "not allowed"})
    assert acceptance.asset_files(assets) != before


def test_new_package_initializer_is_detected(assets):
    before = acceptance.asset_files(assets)
    (assets / "tests/__init__.py").write_text("injected = True", encoding="utf-8")
    assert set(acceptance.asset_files(assets)) - set(before) == {"tests/__init__.py"}


def test_helper_import_origin_is_checked(tmp_path):
    acceptance.validate_helper_imports(PROJECT)
    with pytest.raises(FreezeError, match="import origin"):
        acceptance.validate_helper_imports(tmp_path)


@pytest.fixture
def task_project(tmp_path):
    directory = tmp_path / acceptance.DIRECTORY
    real = PROJECT / acceptance.DIRECTORY
    for name in ("public-tasks.json", "controls.json"):
        write(directory / name, acceptance.read_json(real / name))
    write(directory / "holdout-selection.json", {"repetitions": 3, "tasks": [
        {"id": task, "original_text": f"Independent {task}", "reference_protocol": {"id": task},
            "tolerances": {"absolute": 0.1}} for task in ("H1", "H2")]})
    return tmp_path


def test_exact_seeded_inputs_and_separate_references(task_project):
    texts = acceptance.task_texts(task_project)
    assert len(texts) == 60
    public = acceptance.read_json(task_project / acceptance.DIRECTORY / "public-tasks.json")
    text = public["tasks"][0]["original_text"]
    for repeat, seed in enumerate((1701, 1702, 1703), 1):
        key = batch.slot_id("T1", repeat, "formal")
        assert texts[key] == text + "\n\n" + public["seed_policy"]["seed_suffix_template"].format(seed=seed)
        assert batch.text_sha256(texts[key]) != batch.text_sha256(
            texts[batch.slot_id("T1", repeat, "reference")])
    assert texts["control-C06A-001"] == texts["control-C06B-001"]


@pytest.mark.parametrize("change", ["seed", "task", "repeat", "controls", "missing_reference"])
def test_task_scope_changes_are_rejected(task_project, change):
    directory = task_project / acceptance.DIRECTORY
    public = acceptance.read_json(directory / "public-tasks.json")
    if change == "seed":
        public["seed_policy"]["seeds_by_repetition"][0] += 1
    elif change == "task":
        public["tasks"][0]["id"] = "Different"
    elif change == "repeat":
        public["repetitions"] = 4
    elif change == "missing_reference":
        public["tasks"][0]["reference_protocol"] = {}
    else:
        controls = acceptance.read_json(directory / "controls.json")
        controls["cases"].pop()
        write(directory / "controls.json", controls)
    write(directory / "public-tasks.json", public)
    with pytest.raises(FreezeError):
        acceptance.task_texts(task_project)


def test_current_rules_and_all_input_bindings():
    result = acceptance.check_rules(PROJECT)
    assert result["task_slots"] == 60
    assert result["limits"]["rows"] == 60


def test_static_verification_does_not_claim_runtime_reobservation(monkeypatch):
    manifest = {"runtime": {"verified": True, "coverage": "old observation"}}
    evaluation = {"rules": {"task_slots": 60}}
    monkeypatch.setattr(acceptance, "read_json", lambda p: manifest if p.name == "product-freeze.json" else evaluation)
    monkeypatch.setattr(acceptance, "load_config", lambda p: object())
    monkeypatch.setattr(acceptance, "validate_product_manifest", lambda *a, **kw: {
        "status": "matched", "runtime_metadata_verified": True})
    monkeypatch.setattr(acceptance, "build_evaluation_manifest", lambda p: copy.deepcopy(evaluation))
    monkeypatch.setattr(acceptance, "digest", lambda p: "0" * 64)
    result = acceptance.verify(PROJECT)
    assert not result["runtime_metadata_verified"]
    assert not result["runtime_reobserved"]
    assert result["model_calls"] == result["orca_starts"] == 0


def test_evaluation_manifest_tamper_is_rejected(monkeypatch):
    monkeypatch.setattr(acceptance, "read_json", lambda p: {
        "runtime": {"verified": False}} if p.name == "product-freeze.json" else {"assets": {"evil": "x"}})
    monkeypatch.setattr(acceptance, "load_config", lambda p: object())
    monkeypatch.setattr(acceptance, "validate_product_manifest", lambda *a, **kw: {"status": "matched"})
    monkeypatch.setattr(acceptance, "build_evaluation_manifest", lambda p: {"assets": {}})
    with pytest.raises(FreezeError, match="evaluation assets"):
        acceptance.verify(PROJECT)
