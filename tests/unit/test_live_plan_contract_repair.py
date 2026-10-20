"""Retained failed real proposals; only offline structural materialization runs."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from orca_agent.models import BudgetLimits, PermissionSnapshot, Request, RunConfiguration
from orca_agent.proposals import ProposalError, materialize_plan, validation_error_details
from orca_agent.store import Store
from orca_agent.tools.registry import get_tool
from tests.unit.test_plan_system_scope import scope_case, structure_proposal
from tests.unit.test_simplified_file_references import _case, _proposal
from tests.unit.test_workflow_resource_simplification import future_sdk_case


FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/refreeze4-plan-contract-rejections.json"
DIGEST = "fcb9f57c0c0bfe8214a5fc83fd948f4720d4c86ff033a7bd08c4e2a13edc40f7"


def retained(tmp_path, name, index):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST
    case = next(item for item in json.loads(FIXTURE.read_bytes())["cases"] if item["case"] == name)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(workflow_policy="simplified-1"))
    request = Request.model_validate(case["request"])
    run = store.create_run(request, None, PermissionSnapshot.model_validate(case["permission"]),
                           BudgetLimits.model_validate(case["budget"]))
    return store, run, case["replies"][index]["proposal"]["parameters"]


@pytest.mark.parametrize("index", range(3))
def test_retained_h1_role_and_geometry_forms_materialize_without_executing(tmp_path, index):
    store, run, proposal = retained(tmp_path, "H1", index)
    original = copy.deepcopy(proposal)
    before = store.load_run(run.id).model_dump_json()
    plan = materialize_plan(store, run, proposal)
    resolve, prepare, writer, code = plan.steps
    assert code.geometry is None and not code.inputs
    assert code.parameters.code.producer_step_id == writer.id
    assert code.parameters.code.port == "written_artifact"
    entry, = code.parameters.inputs.entries
    assert entry.producer_step_id == prepare.id and entry.port == "prepared_geometry"
    assert entry.target_path == "hcn.xyz"
    assert code.parameters.sdk_orca is True
    assert prepare.inputs["identity"].producer_step_id == resolve.id
    assert set(code.depends_on) == {prepare.id, writer.id}
    assert store.load_run(run.id).model_dump_json() == before
    assert proposal == original and not run.attempts and not run.calls
    assert store.load_plan(run) is None


@pytest.mark.parametrize("index", range(3))
def test_retained_t3_preserves_goals_and_exposes_actual_port_failure(tmp_path, index):
    store, run, proposal = retained(tmp_path, "T3", index)
    original = copy.deepcopy(proposal)
    try:
        plan = materialize_plan(store, run, proposal)
    except ProposalError as exc:
        detail = exc.detail
        if "errors" in detail:
            error, = detail["errors"]
            role, = proposal["steps"][1]["inputs"]
            assert error["loc"] == ["parameters", "steps", 1, "inputs", role, "port"]
            assert error["reason"] == "producer does not declare the required port"
            definition = get_tool("structure.resolve")
            assert error["allowed_ports"] == definition.output_ports + definition.observation_outputs
            assert error["input_roles"] == get_tool("structure.prepare").input_roles
        else:
            assert detail["path"][1:2] == ["goal_map"]
            assert detail["path"][-1] == "port" and detail["expected"] == "refinement_criterion"
    else:
        assert all(step.system_id == "system_butane" for step in plan.steps[:2])
    assert proposal == original and store.load_plan(run) is None
    assert not run.attempts and not run.calls


@pytest.mark.parametrize("field", ["producer_key", "producer_step_id"])
def test_unique_system_inheritance_and_symbolic_evidence_alias_are_the_same(tmp_path, field):
    store, run, request = scope_case(tmp_path)
    proposal = structure_proposal(request)
    for step in proposal["steps"]:
        step["parameters"].pop("system_id")
    proposal["steps"][1]["inputs"]["identity"] = {field: "resolve", "port": "resolved_identity"}
    plan = materialize_plan(store, run, proposal)
    assert all(step.system_id == step.parameters.system_id == "system_target" for step in plan.steps)
    assert plan.steps[1].inputs["identity"].producer_step_id == plan.steps[0].id


@pytest.mark.parametrize("mutation", ["missing_manifest", "different_producer", "conflicting_code"])
def test_geometry_and_file_aliases_cannot_change_the_declared_source(tmp_path, mutation):
    store, run, proposal = retained(tmp_path, "H1", 2)
    code = proposal["steps"][-1]
    if mutation == "missing_manifest":
        code["parameters"].pop("inputs")
    elif mutation == "different_producer":
        code["geometry"]["producer_key"] = "write_nmr_code"
    else:
        code["inputs"]["code"]["producer_key"] = "prepare_hcn"
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["code"] == (
        "conflicting_file_reference" if mutation == "conflicting_code" else "geometry_manifest_reference_required")
    assert store.load_plan(run) is None and not run.attempts


def test_role_alias_still_rejects_an_unauthorized_artifact(tmp_path):
    store, run, _, _ = _case(tmp_path)
    proposal = _proposal()
    code = proposal["steps"][-1]
    code["parameters"].pop("code")
    code["inputs"] = {"code": {"artifact_id": "not_authorized"}}
    with pytest.raises(ProposalError, match="outside the permission"):
        materialize_plan(store, run, proposal)
    assert store.load_plan(run) is None and not run.attempts


def test_redundant_geometry_does_not_authorize_an_unknown_manifest_file(tmp_path):
    store, run, proposal = retained(tmp_path, "H1", 2)
    code = proposal["steps"][-1]
    code["geometry"] = {"artifact_id": "not_authorized"}
    code["parameters"]["inputs"]["entries"] = [{"artifact_id": "not_authorized",
        "target_path": "hcn.xyz", "purpose": "initial geometry"}]
    with pytest.raises(ProposalError, match="outside the permission"):
        materialize_plan(store, run, proposal)
    assert store.load_plan(run) is None and not run.attempts


def test_multiple_systems_require_an_explicit_scope(tmp_path):
    store, run, request, _ = future_sdk_case(tmp_path, planned=False)
    proposal = structure_proposal(request)
    for step in proposal["steps"]:
        step["parameters"].pop("system_id")
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["errors"][0] == {
        "type": "missing", "loc": ["parameters", "steps", 0, "parameters", "system_id"]}


def test_conflicting_producer_aliases_remain_rejected(tmp_path):
    store, run, request = scope_case(tmp_path)
    proposal = structure_proposal(request)
    proposal["steps"][1]["inputs"]["identity"]["producer_step_id"] = "prepare"
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["code"] == "conflicting_producer_reference"
    assert caught.value.detail["path"] == ["parameters", "steps", 1, "inputs", "identity"]


def test_arbitrary_validator_message_and_input_are_not_published():
    secret = "never-copy-provider-token-abcdef"

    class UntrustedValidator(BaseModel):
        value: str

        @model_validator(mode="after")
        def reject(self):
            raise ValueError(self.value)

    with pytest.raises(ValidationError) as caught:
        UntrustedValidator(value=secret)
    assert validation_error_details(caught.value) == [{"type": "value_error", "loc": []}]
    assert secret not in json.dumps(validation_error_details(caught.value))
