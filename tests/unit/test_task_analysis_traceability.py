"""Offline provenance/condition checks; no scientific execution is simulated."""

import json

import pytest

from orca_agent.models import Observation, ObservationSource, Result, fingerprint
from orca_agent.task_observations import assess, collect
from tests.unit.test_task_observation_conditions import archived_case


def declaration(store, run, result, tmp_path, observations, extra_files=None):
    attempt = run.attempts[0]
    path = tmp_path / "analysis-observations.json"
    path.write_text(json.dumps({"version": 1, "observations": observations}), encoding="utf-8")
    manifest = store.import_artifact(path, "task_output", run_id=run.id, attempt_id=attempt.id)
    result.source["files"]["task/observations.json"] = {
        "artifact_id": manifest.id, "sha256": manifest.sha256}
    result.source["files"].update(extra_files or {})
    result.artifact_ids.append(manifest.id)
    diagnostics = []
    result.observation_records = collect(store, run, attempt.frozen_step, attempt,
                                         result.source["files"], diagnostics=diagnostics)
    result.diagnostics.extend(diagnostics)
    return result


def source_item(**extra):
    return {"name": "frequencies", "value": [1.0], "unit": "cm^-1",
            "sources": [{"filename": "stdout.out", "internal_request_key": "native"}], **extra}


def test_formula_products_and_all_source_conditions_remain_unqualified(tmp_path):
    store, run, request, goal, result = archived_case(tmp_path)
    path = tmp_path / "rankings.csv"
    path.write_text("rank,energy\n1,0\n", encoding="ascii")
    artifact = store.import_artifact(path, "task_output", run_id=run.id, attempt_id=run.attempts[0].id)
    declaration(store, run, result, tmp_path, [source_item(formula="delta = E - min(E)",
        artifacts=["rankings.csv"])], {"task/rankings.csv": {
            "artifact_id": artifact.id, "sha256": artifact.sha256}})
    observation, = result.observation_records
    assert observation.formula == "delta = E - min(E)"
    assert observation.artifact_ids == [artifact.id]
    assert observation.sources[0].actual_conditions == run.internal_calls[0].actual_conditions
    assert observation.validation_status == "unverified" and not result.qualified_outputs
    assert assess(store, run, request, goal, result)["status"] == "passed"


@pytest.mark.parametrize("change", ["geometry", "method", "stage"])
def test_comparison_preserves_different_geometries_and_exposes_protocol_conflicts(tmp_path, change):
    store, run, request, goal, result = archived_case(tmp_path)
    original = run.internal_calls[0]
    actual = {**original.actual_conditions, "geometry_sha256": "e" * 64,
              "input_geometry_sha256": "e" * 64}
    if change == "method":
        actual["method"] = "PBE0"
    elif change == "stage":
        actual["run_type"] = "optimization_and_analytic_frequencies"
    run.internal_calls.append(original.model_copy(deep=True, update={
        "request_key": "candidate", "input_fingerprint": "f" * 64, "actual_conditions": actual}))
    item = source_item()
    item["sources"].append({"filename": "stdout.out", "internal_request_key": "candidate"})
    declaration(store, run, result, tmp_path, [item])
    observation, = result.observation_records
    assert len(observation.sources) == 2
    assert observation.sources[1].actual_conditions == actual
    assert "input_geometry_sha256" not in observation.actual_conditions
    assessment = assess(store, run, request, goal, result)
    if change == "geometry":
        assert observation.validation_status == "unverified"
        assert assessment["status"] == "passed", assessment
    else:
        assert observation.validation_status == "failed"
        assert assessment["status"] == "unresolved"
        assert any(check.name == "source_conditions_consistent" and check.status == "failed"
                   for check in observation.checks)
    assert not result.qualified_outputs


def test_bad_additional_declaration_does_not_erase_valid_output_or_lower_mandatory_evidence(tmp_path):
    store, run, request, goal, result = archived_case(tmp_path)
    run.attempts[0].frozen_step.parameters.observation_names.append("additional_quantity")
    declaration(store, run, result, tmp_path, [source_item(), source_item(name="additional_quantity",
        sources=[{"filename": "missing.out", "internal_request_key": "native"}])])
    assert [item.name for item in result.observation_records] == ["frequencies"]
    assert result.diagnostics[0]["name"] == "additional_quantity"
    assert assess(store, run, request, goal, result)["status"] == "passed"
    goal.minimum_evidence.append("converged_scf@1")
    assert assess(store, run, request, goal, result)["status"] == "unresolved"
    assert result.observation_records[0].value == [1.0]


def test_additive_traceability_defaults_preserve_historical_result_fingerprint():
    result = Result(run_id="run", operation_status="completed", observation_records=[Observation(
        name="new property", value=1.0, sources=[ObservationSource(artifact_id="raw", sha256="a" * 64)])])
    historical = result.model_dump(mode="json")
    for item in historical["observation_records"]:
        item.pop("formula", None)
        item.pop("artifact_ids", None)
        for source in item["sources"]:
            source.pop("actual_conditions", None)
    assert fingerprint(result) == fingerprint(historical)
    result.observation_records[0].formula = "x = 1"
    assert fingerprint(result) != fingerprint(historical)


def test_historical_observation_remains_deliverable_without_rewriting_its_metadata(tmp_path):
    store, run, request, goal, result = archived_case(tmp_path)
    historical = result.model_dump(mode="json")
    for item in historical["observation_records"]:
        for source in item["sources"]:
            source.pop("actual_conditions", None)
    result = Result.model_validate(historical)
    before = result.model_dump(mode="json")
    assessment = assess(store, run, request, goal, result)
    assert assessment["status"] == "passed", assessment
    assert assessment["scientific_qualification"] is False and not result.qualified_outputs
    assert assessment["current_use"]["source_conditions"] == run.internal_calls[0].actual_conditions
    assert result.model_dump(mode="json") == before
