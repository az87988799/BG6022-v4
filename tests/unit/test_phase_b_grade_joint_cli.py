"""Offline CLI metadata binding: no transmission, calculation or Store creation by grading."""

import json

import pytest

from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import Store, sha256_file
from tests.helpers import phase_b_grade_joint as grade


@pytest.fixture
def evaluation(tmp_path):
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    request = Request(original_text="Synthetic grading input; no scientific evidence.",
                      goals=[Goal(id="read", port="value_observation", minimum_check_version="evidence-read-1")])
    run = store.create_run(request, None, PermissionSnapshot(), BudgetLimits(orca_starts=0, model_calls=0))
    run.batch_category = "development"
    store.save_run(run)
    metadata = {"run_id": run.id, "case": "repair_success", "category": "development"}
    path = tmp_path / "metadata.json"
    return store, run, metadata, path


def invoke(evaluation, *, metadata=True):
    store, run, values, path = evaluation
    args = [values["case"], run.id, "--data-root", str(store.root)]
    if metadata:
        path.write_text(json.dumps(values), encoding="utf-8")
        args.extend(["--metadata", str(path)])
    return grade.main(args)


def test_cli_passes_metadata_to_actual_grader_without_mutating_run(evaluation, capsys):
    store, run, _, _ = evaluation
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    invoke(evaluation)
    report = json.loads(capsys.readouterr().out)
    assert next(c for c in report["checks"] if c["name"] == "evaluation_identity")["passed"]
    assert not report["passed"]  # An identity match never invents missing science.
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before


@pytest.mark.parametrize("field, value", [("run_id", "another_run"), ("case", "sampling_stop"),
                                         ("category", "formal")])
def test_cli_rejects_metadata_from_another_evaluation(evaluation, capsys, field, value):
    store, run, metadata, path = evaluation
    metadata[field] = value
    path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        grade.main(["repair_success", run.id, "--data-root", str(store.root), "--metadata", str(path)])
    assert exc.value.code == 2 and "metadata" in capsys.readouterr().err


def test_cli_raw_methane_cannot_omit_metadata(evaluation, capsys):
    evaluation[2]["case"] = "methane_opt_control"
    with pytest.raises(SystemExit) as exc:
        invoke(evaluation, metadata=False)
    assert exc.value.code == 2 and "requires --metadata" in capsys.readouterr().err


@pytest.mark.parametrize("payload", [None, [], "raw_text"])
def test_cli_null_or_nonobject_metadata_cannot_bypass_raw_gate(evaluation, capsys, payload):
    store, run, _, path = evaluation
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        grade.main(["methane_opt_control", run.id, "--data-root", str(store.root), "--metadata", str(path)])
    assert exc.value.code == 2 and "metadata" in capsys.readouterr().err


@pytest.mark.parametrize("missing", ["input_form", "raw_bundle_path", "raw_bundle_sha256"])
def test_cli_raw_metadata_cannot_drop_entry_proof(evaluation, capsys, missing):
    metadata = evaluation[2]
    metadata.update(case="methane_opt_control", input_form="raw_text",
                    raw_bundle_path="unread-placeholder.json", raw_bundle_sha256="a" * 64)
    metadata.pop(missing)
    with pytest.raises(SystemExit) as exc:
        invoke(evaluation)
    assert exc.value.code == 2 and "raw input bundle" in capsys.readouterr().err


def test_cli_raw_proof_reaches_optimized_energy_gate_instead_of_silently_skipping_it(evaluation, capsys):
    store, run, metadata, _ = evaluation
    bundle = store.path("synthetic-raw-input.json")
    bundle.write_text(json.dumps({"text": store.load_request(run).original_text, "conditions": {}}), encoding="utf-8")
    metadata.update(case="methane_opt_control", input_form="raw_text",
                    raw_bundle_path=str(bundle), raw_bundle_sha256=sha256_file(bundle))
    invoke(evaluation)
    report = json.loads(capsys.readouterr().out)
    checks = {c["name"]: c["passed"] for c in report["checks"]}
    assert checks["evaluation_identity"]
    assert checks["raw_intake_and_optimized_energy_target"] is False
    assert report["passed"] is False


def test_cli_other_structured_cases_keep_optional_metadata(evaluation, capsys):
    invoke(evaluation, metadata=False)
    report = json.loads(capsys.readouterr().out)
    assert report["case"] == "repair_success" and not report["passed"]
