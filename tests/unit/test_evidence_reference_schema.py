"""Generated EvidenceRef constraints agree with the existing runtime invariant."""

import copy
import itertools
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_plan_goal_binding_contract import matches

from orca_agent.context import _schema
from orca_agent.models import EvidenceRef
from orca_agent.proposals import ProposalError, action_parameter_schema, validate_action_parameters


@pytest.mark.parametrize("producer,run,result,artifact,digest", list(itertools.product(
    (None, "step_existing"), (None, "run_existing"), (None, "result_existing"),
    (None, "artifact_existing"), (None, "", "a" * 64))))
def test_reference_schema_matches_runtime_including_legacy_empty_digest(producer, run, result, artifact, digest):
    values = {"producer_step_id": producer, "run_id": run, "result_id": result,
              "artifact_id": artifact, "sha256": digest, "port": "prepared_geometry"}
    schema = _schema(EvidenceRef.model_json_schema())
    expected = bool(producer and not any((run, result, artifact, digest)) or not producer and run and result)
    assert matches(schema, values, schema) == expected
    if expected:
        parsed = EvidenceRef.model_validate(values)
        assert parsed.model_dump(exclude_unset=True) == values
    else:
        with pytest.raises(ValidationError):
            EvidenceRef.model_validate(values)


@pytest.mark.parametrize("values", [{}, {"artifact_id": "artifact_existing", "sha256": "a" * 64},
    {"run_id": "run_existing"}, {"result_id": "result_existing"}, {"producer_step_id": ""},
    {"producer_step_id": "step_existing", "run_id": "run_existing"}])
def test_missing_or_mixed_identity_is_rejected_by_generated_schema(values):
    schema = _schema(EvidenceRef.model_json_schema())
    assert not matches(schema, values, schema)
    with pytest.raises(ValidationError):
        EvidenceRef.model_validate(values)


def test_actual_live012_file_only_goal_binding_gets_specific_identity_correction():
    fixture = Path(__file__).parents[1] / "fixtures/code_contracts/live012-file-only-evidence.json"
    proposal = json.loads(fixture.read_text(encoding="utf-8"))["proposal"]
    before = copy.deepcopy(proposal)
    schema = _schema(action_parameter_schema("revise_plan"))
    assert not matches(schema, proposal["parameters"], schema)
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", proposal["parameters"])
    detail = caught.value.detail
    assert detail["evidence_reference_shapes"] == EvidenceRef.model_json_schema()["oneOf"]
    assert "run_id and result_id" in detail["evidence_instruction"]
    assert "artifact_id/sha256 alone" in detail["evidence_instruction"]
    assert "completed Step" in detail["evidence_instruction"]
    file_id = proposal["parameters"]["goal_map"]["goal_prepared_geometry"]["evidence"]["artifact_id"]
    assert file_id not in json.dumps(detail)
    assert proposal == before


@pytest.mark.parametrize("evidence", [
    {"producer_step_id": "existing_step", "port": "prepared_geometry"},
    {"run_id": "existing_run", "result_id": "existing_result", "port": "prepared_geometry"},
])
def test_legal_existing_binding_stays_legal_and_serialization_is_unchanged(evidence):
    plan = {"steps": [{"key": "existing_step"}], "goal_map": {
        "goal_prepared_geometry": {"evidence": evidence, "port": "prepared_geometry"}}}
    schema = _schema(action_parameter_schema("revise_plan"))
    assert matches(schema, plan, schema)
    validate_action_parameters("revise_plan", plan)
    assert EvidenceRef.model_validate(evidence).model_dump(exclude_unset=True) == evidence


def test_stored_step_metadata_gets_proposal_shape_feedback_without_echoing_values():
    proposal = {"steps": [{"id": "step_private", "logical_id": "logical_private",
                           "immutable_frozen": True}], "goal_map": {}}
    before = copy.deepcopy(proposal)
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", proposal)
    detail = caught.value.detail
    assert "key" in detail["step_fields"] and "id" not in detail["step_fields"]
    assert "{key:existing_step_id}" in detail["step_instruction"]
    assert "logical_key" in detail["step_instruction"]
    assert "step_private" not in json.dumps(detail)
    assert "logical_private" not in json.dumps(detail)
    assert proposal == before


def test_stored_step_goal_route_does_not_get_irrelevant_evidence_branch_feedback():
    proposal = {"steps": [{"key": "existing_step"}], "goal_map": {
        "goal_prepared_geometry": {"step_id": "existing_step", "port": "prepared_geometry"}}}
    before = copy.deepcopy(proposal)
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", proposal)
    detail = caught.value.detail
    assert "step_key" in detail["step_target_instruction"]
    assert "evidence_instruction" not in detail
    assert "evidence_reference_shapes" not in detail
    assert proposal == before


@pytest.mark.parametrize("index", range(3))
def test_live013_rejected_revisions_get_only_relevant_static_shape_feedback(index):
    fixture = Path(__file__).parents[1] / "fixtures/code_contracts/live013-runtime-plan-fields.json"
    proposal = json.loads(fixture.read_text(encoding="utf-8"))["replies"][index]["proposal"]
    before = copy.deepcopy(proposal)
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", proposal["parameters"])
    detail = caught.value.detail
    if index == 0:
        assert "{key:existing_step_id}" in detail["step_instruction"]
    elif index == 1:
        assert "step_key" in detail["step_target_instruction"]
        assert "evidence_instruction" not in detail
    else:
        assert "cannot be mixed" in detail["evidence_instruction"]
    assert proposal == before
