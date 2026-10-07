"""The formal gate checks exact frozen inputs before any live reservation."""

import json
from types import SimpleNamespace

import pytest

from tests.helpers import phase_b_freeze as gate


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    source = tmp_path / "agent.py"
    raw = tmp_path / "job.out"
    source.write_bytes(b"value = 1\r\n")
    raw.write_bytes(b"ORCA\r\r\n")
    path = tmp_path / "freeze.json"
    record = {"freeze_label": "formal-test", "code_commit": "a" * 40,
              "files": {"agent.py": gate.freeze_hash(source, source_text=True),
                        "job.out": gate.freeze_hash(raw)},
              "source_lf_normalization": ["agent.py"]}
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(gate, "PROJECT", tmp_path)
    monkeypatch.setattr(gate, "FREEZE", path)
    return tmp_path, path, record


def test_source_checkout_line_endings_do_not_relabel_raw_evidence(frozen):
    root, _, _ = frozen
    (root / "agent.py").write_bytes(b"value = 1\n")
    assert gate.validate_freeze("formal-test", execution=False)["code_commit"] == "a" * 40
    (root / "job.out").write_bytes(b"ORCA\r\n")
    with pytest.raises(ValueError, match="job.out"):
        gate.validate_freeze("formal-test")


def test_code_edit_and_different_label_are_rejected(frozen):
    root, _, _ = frozen
    with pytest.raises(ValueError, match="label differs"):
        gate.validate_freeze("formal-new")
    (root / "agent.py").write_bytes(b"value = 2\n")
    with pytest.raises(ValueError, match="agent.py"):
        gate.validate_freeze("formal-test")


def test_freeze_cannot_reference_a_file_outside_project(frozen):
    root, path, record = frozen
    outside = root.parent / (root.name + "-outside.txt")
    outside.write_bytes(b"external")
    record["files"]["../" + outside.name] = gate.freeze_hash(outside)
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        gate.validate_freeze("formal-test")


def test_historical_freeze_can_only_be_reviewed_statically(frozen):
    assert gate.validate_freeze("formal-test", execution=False)["freeze_label"] == "formal-test"
    with pytest.raises(ValueError, match="static review only"):
        gate.validate_freeze("formal-test")


@pytest.fixture
def execution_freeze(frozen, monkeypatch):
    root, path, record = frozen
    source = root / "src/orca_agent/agent.py"
    source.parent.mkdir(parents=True)
    source.write_text("value = 1\n")
    record["files"]["src/orca_agent/agent.py"] = gate.freeze_hash(source)
    monkeypatch.setattr(gate, "FIXED", {"agent.py", "job.out"})
    environment = {"python_version": "3.11.4", "packages": {"openai": "2.28.0"},
                   "prompt_version": "frozen-test", "token_bound_version": "frozen-token"}
    monkeypatch.setattr(gate, "runtime_environment", lambda: environment)
    monkeypatch.setattr(gate, "validate_product_imports", lambda _: None)
    authority = {"limits": {"model": {"http_requests": 1004}}, "origin": "amendment",
                 "approval_sha256": "b" * 64, "receipt_sha256": "c" * 64}
    monkeypatch.setattr(gate, "execution_budget_authority", lambda: authority)
    record.update(schema_version=2, execution_files=gate.execution_files(),
                  execution_environment=environment.copy(),
                  budget_authority=json.loads(json.dumps(authority)),
                  configuration={"model": gate.evaluation_config().model_dump(mode="json")})
    path.write_text(json.dumps(record))
    return root, path, record, environment


@pytest.mark.parametrize("fault", ["python", "package", "prompt", "token", "added_source", "configuration"])
def test_execution_drift_is_rejected_but_historical_review_still_works(execution_freeze, fault):
    root, path, record, environment = execution_freeze
    assert gate.validate_freeze("formal-test")["freeze_label"] == "formal-test"
    if fault == "python":
        environment["python_version"] = "0.0.0"
    elif fault == "package":
        environment["packages"] = {"openai": "wrong-version"}
    elif fault == "prompt":
        environment["prompt_version"] = "changed"
    elif fault == "token":
        environment["token_bound_version"] = "changed"
    elif fault == "added_source":
        (root / "src/orca_agent/new_source.py").write_text("value = 2\n")
    else:
        record["configuration"]["model"]["data_root"] = "wrong-root"
        path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="differs"):
        gate.validate_freeze("formal-test")
    assert gate.validate_freeze("formal-test", execution=False)["freeze_label"] == "formal-test"


def test_unrelated_document_does_not_change_frozen_execution(execution_freeze):
    root, _, _, _ = execution_freeze
    (root / "README.md").write_text("An unrelated documentation-only revision.")
    assert gate.validate_freeze("formal-test")["freeze_label"] == "formal-test"


@pytest.mark.parametrize("field", ["limits", "approval_sha256", "receipt_sha256"])
def test_budget_approval_or_applied_receipt_drift_blocks_execution_only(execution_freeze, field):
    _, path, record, _ = execution_freeze
    record["budget_authority"][field] = "changed"
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="budget authority differs"):
        gate.validate_freeze("formal-test")
    assert gate.validate_freeze("formal-test", execution=False)["freeze_label"] == "formal-test"


def test_second_budget_authority_does_not_rewrite_or_reactivate_old_freeze(execution_freeze, monkeypatch):
    _, path, record, _ = execution_freeze
    old = path.read_bytes()
    latest = {**record["budget_authority"],
              "limits": {"model": {"http_requests": 1050, "tokens": 6530000, "usd": 10}},
              "approval_id": "repair-budget-supplement-20261007", "receipt_sha256": "d" * 64}
    monkeypatch.setattr(gate, "execution_budget_authority", lambda: latest)
    assert gate.validate_freeze("formal-test", execution=False)["freeze_label"] == "formal-test"
    with pytest.raises(ValueError, match="budget authority differs"):
        gate.validate_freeze("formal-test")
    assert path.read_bytes() == old


def test_formal_manifest_keeps_both_immutable_budget_approvals():
    assert {"docs/acceptance/phase-b/budget-approval-20261007.json",
            "docs/acceptance/phase-b/budget-approval-supplement-20261007.json"} <= gate.FIXED


def test_actual_product_import_cannot_come_from_another_checkout(tmp_path, monkeypatch):
    import sys

    record = {"files": {name: "unused" for name in gate.execution_files()}}
    gate.validate_product_imports(record)
    monkeypatch.setitem(sys.modules, "orca_agent.wrong_checkout",
                        SimpleNamespace(__file__=str(tmp_path / "foreign.py")))
    with pytest.raises(ValueError, match="import origin differs"):
        gate.validate_product_imports(record)


def test_joint_requires_verified_science_and_rejects_binary_change_before_probe(execution_freeze, monkeypatch):
    root, path, record, _ = execution_freeze
    config = gate.evaluation_config(science=True)
    config.orca_path, config.mpi_path = root / "orca.exe", root / "mpi.exe"
    record["configuration"]["science"] = config.model_dump(mode="json")
    path.write_text(json.dumps(record))
    monkeypatch.setattr(gate, "science_environment", lambda _: pytest.fail("binary drift reached a probe"))
    with pytest.raises(ValueError, match="no verified scientific environment"):
        gate.validate_freeze("formal-test", config=config, science=True)
    config.orca_path.write_bytes(b"changed binary")
    config.mpi_path.write_bytes(b"mpi binary")
    record["science_environment"] = {"orca": {"sha256": "a" * 64}, "mpi": {"sha256": "b" * 64}}
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match="executable differs"):
        gate.validate_freeze("formal-test", config=config, science=True)
