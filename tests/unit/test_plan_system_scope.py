"""New structure Plan scopes; HTTP/preparation mocked, no model or ORCA."""

import copy

import pytest

from orca_agent.input_bindings import prepared_geometry, validate_input_result
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_general_identity_selection import normalized, pubchem
from tests.unit.test_general_preparation_admission import execution
from tests.unit.test_simple_name_resolution import mock_http
from tests.unit.test_structure_tools import mock_generator
from tests.unit.test_workflow_resource_simplification import future_sdk_case


def scope_case(tmp_path):
    store, run = normalized(tmp_path, name="oxidane", automatic=False, execution=execution(),
        state={"method": "HF", "basis": "STO-3G", "electronic_state": "RHF"})
    return store, run, store.load_request(run)


def structure_proposal(request, *, location="parameters", system="system_target"):
    steps = [
        {"key": "resolve", "tool": "structure.resolve", "parameters": {}},
        {"key": "prepare", "tool": "structure.prepare", "parameters": {},
         "inputs": {"identity": {"producer_key": "resolve", "port": "resolved_identity"}}},
    ]
    for step in steps:
        (step["parameters"] if location == "parameters" else step)["system_id"] = system
    return {"steps": steps, "goal_map": {goal.id: {"port": goal.port,
        "gap": "Prepare inputs before scientific observations."} for goal in request.goals}}


@pytest.mark.parametrize("location", ["parameters", "step"])
def test_either_explicit_scope_reaches_consumable_identity_and_prepared_binding(
        tmp_path, monkeypatch, location):
    store, run, request = scope_case(tmp_path)
    supplied = structure_proposal(request, location=location)
    before = copy.deepcopy(supplied)
    plan = materialize_plan(store, run, supplied)
    assert supplied == before  # Materialization must not rewrite the model proposal.
    resolve, prepare = plan.steps
    assert all(step.system_id == step.parameters.system_id == "system_target" for step in plan.steps)
    assert prepare.parameters.charge == request.charge == 0
    assert prepare.parameters.multiplicity == request.multiplicity == 1
    assert prepare.depends_on == [resolve.id]
    run = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="plan")

    # Keep production endpoint, byte bounds, dispatch, Call and Artifact binding;
    # only the network transport and geometry generator are offline substitutes.
    calls = mock_http(monkeypatch, [(200, pubchem("O", "H2O"))])
    generators = mock_generator(monkeypatch)
    identity = execute_call(store, run, resolve.tool, resolve.parameters.model_dump(), step=resolve)
    assert identity.operation_status == "completed", identity.diagnostics
    bound = validate_input_result(store, run, request, identity, "resolved_identity")
    assert bound == run.input_bindings["system_target"]["resolved_identity"]
    assert bound["rule_version"] == "structure-identity-3"
    geometry = execute_call(store, run, prepare.tool, prepare.parameters.model_dump(),
                            step=prepare, results={resolve.id: identity})
    assert geometry.operation_status == "completed", geometry.diagnostics
    geometry_binding = validate_input_result(store, run, request, geometry, "prepared_geometry")
    assert geometry_binding == run.input_bindings["system_target"]["prepared_geometry"]
    assert prepared_geometry(store, run, request, "system_target") == geometry_binding["artifact_id"]
    preparation_call = next(call for call in run.calls if call.id == geometry.call_id)
    assert preparation_call.consumption["identity"]["result_id"] == identity.id
    assert all(call.frozen_step.system_id == call.parameters["system_id"] for call in run.calls)
    assert len(calls) == len(generators) == 1
    assert not run.attempts and run.usage.model_calls == run.usage.orca_starts_actual == 0


def test_single_scope_locations_materialize_to_the_same_conditions_and_dependencies(tmp_path):
    store, run, request = scope_case(tmp_path)
    plans = [materialize_plan(store, run, structure_proposal(request, location=location))
             for location in ("parameters", "step")]
    for plan in plans:
        assert [(step.tool, step.system_id, step.parameters.model_dump()) for step in plan.steps] == [
            ("structure.resolve", "system_target", {"system_id": "system_target"}),
            ("structure.prepare", "system_target",
             {"system_id": "system_target", "charge": 0, "multiplicity": 1}),
        ]
        assert plan.steps[1].depends_on == [plan.steps[0].id]
        assert plan.steps[1].inputs["identity"].producer_step_id == plan.steps[0].id
    assert store.load_plan(run) is None and not run.calls and not run.attempts


@pytest.mark.parametrize("index", [0, 1], ids=["resolve", "prepare"])
def test_conflicting_outer_and_parameter_scopes_are_rejected_before_activation(tmp_path, index):
    store, run, request = scope_case(tmp_path)
    proposed = structure_proposal(request)
    proposed["steps"][index]["system_id"] = "conflicting_system"
    before = store.load_run(run.id).model_dump()
    with pytest.raises(ProposalError, match="same requested system") as caught:
        materialize_plan(store, run, proposed)
    assert caught.value.detail["path"] == ["parameters", "steps", index, "parameters", "system_id"]
    assert store.load_run(run.id).model_dump() == before
    assert store.load_plan(run) is None and not run.calls


@pytest.mark.parametrize("value", [None, 7, True, [], {"unexpected": "scope"}])
@pytest.mark.parametrize("index", [0, 1], ids=["resolve", "prepare"])
def test_malformed_parameter_system_remains_a_bounded_proposal_error(tmp_path, index, value):
    store, run, request = scope_case(tmp_path)
    proposed = structure_proposal(request)
    proposed["steps"][index]["parameters"]["system_id"] = value
    before = store.load_run(run.id).model_dump()
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposed)
    expected = ["parameters", "steps", index, "parameters", "system_id"]
    if value is None:
        assert caught.value.detail["errors"] == [{"type": "string_type", "loc": expected}]
    else:
        assert caught.value.detail["path"] == expected
    assert "unexpected" not in str(caught.value.detail)
    assert store.load_run(run.id).model_dump() == before
    assert not run.calls and not run.attempts


@pytest.mark.parametrize("location", ["parameters", "step"])
def test_unknown_system_cannot_be_activated_from_either_scope(tmp_path, location):
    store, run, request = scope_case(tmp_path)
    before = store.load_run(run.id).model_dump()
    with pytest.raises(ProposalError, match="declared in the current Request") as caught:
        plan = materialize_plan(store, run, structure_proposal(
            request, location=location, system="not_requested"))
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="wrong")
    assert caught.value.detail["path"] == ["parameters", "steps", 0, "parameters", "system_id"]
    assert store.load_run(run.id).model_dump() == before and store.load_plan(run) is None


def test_different_requested_system_preparation_cannot_supply_sdk_initial_anchor(tmp_path):
    store, run, request, direct = future_sdk_case(tmp_path, planned=False, producer_system="methane")
    proposed = structure_proposal(request, system="methane")
    script = direct.steps[-1]
    proposed["steps"].append({"key": "script", "tool": script.tool, "system_id": "water",
        "parameters": script.parameters.model_dump(), "depends_on": ["prepare"]})
    proposed["goal_map"] = {"report": {"step_key": "script", "port": "artifacts"}}
    plan = materialize_plan(store, run, proposed)
    assert plan.steps[1].system_id == plan.steps[1].parameters.system_id == "methane"
    assert plan.steps[-1].system_id == "water"
    with pytest.raises(ValueError, match="prepared or registered geometry"):
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="wrong")
    assert store.load_plan(run) is None and not run.calls and not run.attempts


def test_retained_persisted_step_is_not_silently_scoped_or_rewritten(tmp_path):
    store, run, request = scope_case(tmp_path)
    plan = materialize_plan(store, run, structure_proposal(request))
    # A historical shape allowed a null outer system beside a parameter scope.
    # It may be retained as history, but this test does not execute or qualify it.
    plan.steps = [plan.steps[0].model_copy(update={"system_id": None})]
    run = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="plan")
    prior = plan.model_dump()
    retained = materialize_plan(store, run, {
        "steps": [{"key": step.id} for step in plan.steps],
        "goal_map": structure_proposal(request)["goal_map"],
    })
    assert retained.steps == plan.steps
    assert retained.steps[0].system_id is None
    assert retained.steps[0].parameters.system_id == "system_target"
    assert store.load_plan(run).model_dump() == prior and not run.calls
