"""Actual a023 first scientific Plan admission; no generated code is executed."""

import copy
import json
from pathlib import Path

import pytest

from orca_agent.model_usage import current_basis
from orca_agent.models import BudgetLimits, PermissionSnapshot, Request, RunConfiguration
from orca_agent.planning import PlanningError
from orca_agent.proposals import materialize_plan
from orca_agent.store import Store

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live023-first-science-analysis-route.json"


def actual(tmp_path, *, qualified=False):
    fixture = json.loads(FIXTURE.read_text("utf-8"))
    request = Request.model_validate(fixture["request"])
    if qualified:
        for goal in request.goals:
            if goal.port == "energy":
                goal.evidence_contract_version = "legacy-1"
                goal.verification = "qualified"
                goal.minimum_check_version = "orca-hf-2"
    configuration = RunConfiguration.model_validate(fixture["configuration"])
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment", configuration=configuration)
    run = store.create_run(request, None, PermissionSnapshot.model_validate(fixture["permission"]),
        BudgetLimits.model_validate(fixture["budget"]), configuration=configuration,
        science_baseline_policy="first_science_plan")
    return store, run, request, fixture["parameters"]


def test_actual_native_to_python_energy_plan_is_admitted_without_execution(tmp_path):
    store, run, request, parameters = actual(tmp_path)
    proposal = copy.deepcopy(parameters)
    budget = run.budget.model_copy(deep=True)
    plan = materialize_plan(store, run, parameters)
    updated = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="actual_call6_replay")
    assert updated.budget == budget and store.load_request(updated) == request
    assert parameters == proposal and store.load_plan(updated) == plan
    assert not updated.calls and not updated.attempts and not updated.internal_calls
    assert store.environment_lease() is None
    assert updated.usage.orca_starts_actual == updated.usage.orca_starts_reserved == 0
    assert updated.usage.analysis_executions == updated.usage.model_calls == 0
    assert len(updated.initial_science_steps) == 2
    by_id = {step.id: step for step in plan.steps}
    for goal in request.goals:
        if goal.port != "energy":
            continue
        step = by_id[plan.goal_map[goal.id].step_id]
        assert step.tool == "python.execute" and step.parameters.sdk_orca is False
        assert step.parameters.observation_names == ["energy"]
        source = step.parameters.inputs.entries[0]
        assert by_id[source.producer_step_id].tool == "orca.run"
        assert source.producer_step_id in step.depends_on
        assert updated.goal_status[goal.id] != "satisfied"


def test_qualified_energy_still_cannot_be_replaced_by_generic_analysis(tmp_path):
    store, run, _, parameters = actual(tmp_path, qualified=True)
    before = store.load_run(run.id)
    with pytest.raises(PlanningError, match="generic observations cannot replace required scientific qualification"):
        plan = materialize_plan(store, run, parameters)
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="reject_qualified_adapter")
    assert store.load_run(run.id) == before and store.load_plan(run) is None
    assert store.environment_lease() is None


def test_generic_route_still_requires_the_declared_goal_observation(tmp_path):
    store, run, _, parameters = actual(tmp_path)
    step = next(row for row in parameters["steps"] if row["key"] == "extract_gas_energy")
    step["parameters"]["observation_names"] = ["unrelated_count"]
    before = store.load_run(run.id)
    with pytest.raises(ValueError):
        plan = materialize_plan(store, run, parameters)
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="reject_wrong_observation")
    assert store.load_run(run.id) == before and store.load_plan(run) is None
    assert store.environment_lease() is None
