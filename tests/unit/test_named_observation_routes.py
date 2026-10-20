"""Tool declarations match real named-value collectors; no task is executed."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orca_agent.models import Attempt, Plan, Request, Result, Run, RunConfiguration, fingerprint
from orca_agent.planning import PlanningError, _goal_bindings, validate_revision
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.store import Store
from orca_agent.task_observations import assess
from orca_agent.tools.registry import declared_outputs, get_tool, supports_goal_output

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live026-native-goal-output-route.json"


@pytest.fixture
def actual_route():
    raw = json.loads(FIXTURE.read_bytes())
    request = Request.model_validate(raw["request"])
    plan = Plan.model_validate(raw["plan"], context={"historical_output_declarations": True})
    run = Run(request_id=request.id, request_version=request.version,
              plan_id=plan.id, plan_version=plan.version,
              configuration=RunConfiguration(workflow_policy="simplified-1",
                  observation_delivery_policy="reported-observation-2"))
    return raw, request, plan, run


def test_tool_capability_controls_named_outputs_and_schema_guidance(actual_route):
    _, request, plan, _ = actual_route
    native = next(step for step in plan.steps if step.id == plan.goal_map[request.goals[0].id].step_id)
    python = next(step for step in plan.steps if step.tool == "python.execute")
    assert native.parameters.observation_names == ["energy"]
    assert declared_outputs(native) == ["observations", "artifacts"]
    assert "energy" in declared_outputs(native, historical=True)
    assert "energy_difference" in declared_outputs(python)
    assert not supports_goal_output(native, request.goals[0])
    assert supports_goal_output(python, request.goals[2])
    for name, capability in [("orca.run", "none"), ("python.execute", "task_manifest")]:
        tool = get_tool(name)
        assert tool.check_contract["generic_observations"] is True
        assert tool.check_contract["named_observation_collector"] == capability
    description = get_tool("orca.run").parameter_schema["properties"]["observation_names"]["description"]
    assert "historical" in description and "raw" in description


@pytest.mark.parametrize("port", ["observations", "artifacts"])
def test_manifest_collection_is_a_value_only_when_explicitly_declared(actual_route, port):
    _, request, plan, run = actual_route
    goal = request.goals[2].model_copy(update={"port": port})
    request = request.model_copy(update={"goals": [goal]})
    adapter = next(step for step in plan.steps if step.tool == "python.execute")
    plan = plan.model_copy(update={"goal_map": {
        goal.id: plan.goal_map[goal.id].model_copy(update={"port": port})}})
    assert port in declared_outputs(adapter)  # Raw dependencies remain legal.
    assert not supports_goal_output(adapter, goal)
    with pytest.raises(PlanningError, match="cannot collect"):
        _goal_bindings(request, plan, run)
    adapter.parameters.observation_names.append(port)
    assert supports_goal_output(adapter, goal)
    _goal_bindings(request, plan, run)


def test_activated_historical_plan_loads_but_new_plan_rejects_false_value_route(actual_route, tmp_path):
    raw, request, plan, run = actual_route
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    for kind, value in [("request", raw["request"]), ("plan", raw["plan"])]:
        path = store.path(f"runs/{run.id}/{kind}-revisions/{value['version']}.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(value), encoding="utf-8")
    loaded = store.load_plan(run)
    assert fingerprint(loaded) == fingerprint(plan)
    assert loaded.goal_map[request.goals[0].id] == plan.goal_map[request.goals[0].id]
    with pytest.raises(ValidationError, match="does not produce"):
        Plan.model_validate(raw["plan"])
    with pytest.raises(PlanningError) as caught:
        _goal_bindings(request, loaded, run)
    assert caught.value.detail["code"] == "goal_output_route_mismatch"


def test_explicit_manifest_route_is_admitted_with_unprovided_goal_left_as_gap(actual_route):
    raw, request, _, run = actual_route
    candidate = copy.deepcopy(raw["plan"])
    adapter = next(step for step in candidate["steps"] if step["tool"] == "python.execute")
    adapter["parameters"]["observation_names"].append("energy")
    candidate["goal_map"][request.goals[0].id] = {"step_id": adapter["id"], "port": "energy"}
    candidate["goal_map"][request.goals[1].id] = {"port": "energy", "gap": "Sourced water-energy observation pending."}
    plan = Plan.model_validate(candidate)
    _goal_bindings(request, plan, run)
    assert run.result_ids == [] and run.attempts == []
    assert run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("port", ["energy", "observations", "artifacts"])
def test_native_named_or_collection_route_gets_real_adapter_feedback(actual_route, port):
    _, request, plan, run = actual_route
    goal = request.goals[0].model_copy(update={"port": port})
    request = request.model_copy(update={"goals": [goal]})
    target = plan.goal_map[goal.id].model_copy(update={"port": port})
    plan = plan.model_copy(update={"goal_map": {goal.id: target}})
    run.permission = run.permission.model_copy(update={"code_execution": True,
        "allowed_tools": list(dict.fromkeys(step.tool for step in plan.steps))})
    reader = SimpleNamespace(load_plan=lambda _: plan, load_request=lambda _: request)
    proposal = {"steps": [{"key": step.id} for step in plan.steps],
                "goal_map": {goal.id: {"step_key": target.step_id, "port": port}}}
    with pytest.raises(ProposalError) as caught:
        materialize_plan(reader, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "goal_output_route_mismatch"
    route = detail["valid_route"]
    assert route["selected_producer"]["tool"] == "orca.run"
    assert "python.execute" in route["available_observation_tools"]
    assert route["observation_adapters"][0]["required_observation_name"] == port
    assert "sourced observation" in route["observation_instruction"]
    with pytest.raises(PlanningError) as commit_error:
        _goal_bindings(request, plan, run)
    assert commit_error.value.detail["code"] == "goal_output_route_mismatch"


def test_raw_completed_attempt_is_not_a_manifest_and_identity_remains_strict(actual_route):
    _, request, plan, run = actual_route
    goal = request.goals[0]
    step = next(step for step in plan.steps if step.id == plan.goal_map[goal.id].step_id)
    attempt = Attempt(step_id=step.id, logical_id=step.logical_id, number=2, tool=step.tool,
        state="completed", execution_kind="orca", frozen_step=step, request_version=request.version,
        plan_version=plan.version, input_fingerprint="a" * 64, directory="fixture/never-executed")
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="completed", observations={"evidence_collection_complete": True})
    attempt.result_id = result.id
    run.attempts = [attempt]
    run.result_ids = [result.id]
    before = copy.deepcopy(run.model_dump(mode="json"))
    outcome = assess(None, run, request, goal, result)
    assert outcome["reasons"] == ["generic_observation_requires_task_manifest"]
    assert outcome["scientific_qualification"] is False
    assert run.model_dump(mode="json") == before
    attempt.request_version -= 1
    assert assess(None, run, request, goal, result)["reasons"] == [
        "generic_observation_attempt_purpose_mismatch"]


def test_initial_raw_collection_route_is_rejected_before_file_access(actual_route, tmp_path):
    _, request, plan, _ = actual_route
    goal = request.goals[0].model_copy(update={"port": "artifacts"})
    request = request.model_copy(update={"goals": [goal]})
    plan = plan.model_copy(update={"goal_map": {
        goal.id: plan.goal_map[goal.id].model_copy(update={"port": "artifacts"})}})
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    with pytest.raises(PlanningError) as caught:
        store.create_run(request, plan)
    assert caught.value.detail["code"] == "goal_output_route_mismatch"


def test_historical_invalid_route_can_be_replaced_with_explicit_gaps(actual_route):
    _, request, plan, run = actual_route
    candidate = plan.model_copy(deep=True)
    candidate.version += 1
    for goal in request.goals:
        candidate.goal_map[goal.id] = {"port": goal.port, "gap": "Sourced observation pending."}
    candidate = Plan.model_validate(candidate.model_dump(warnings=False))
    run.permission = run.permission.model_copy(update={"code_execution": True,
        "scientific_execution": True, "artifact_writes": True,
        "allowed_tools": list(dict.fromkeys(step.tool for step in plan.steps)),
        "artifact_ids": [entry.artifact_id for step in plan.steps
            for entry in [getattr(step.parameters, "code", None), getattr(step.parameters, "input", None),
                *getattr(getattr(step.parameters, "inputs", None), "entries", [])]
            if getattr(entry, "artifact_id", None)]})
    run.budget = run.budget.model_copy(update={"file_writes": 6, "analysis_executions": 1})
    before = copy.deepcopy(run.model_dump(mode="json"))
    validate_revision(request, plan, request, candidate, run)
    assert run.model_dump(mode="json") == before
