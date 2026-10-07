"""Untrusted condition dictionaries cannot equate bool/float with integer state."""

import pytest

from orca_agent.applicability import canonical_condition, effective_conditions
from orca_agent.goals import goal_evidence_assessment, validate_goal_evidence
from orca_agent.models import InputRef, OutputBinding, PermissionSnapshot, Plan, Step, SystemInput
from orca_agent.store import Store
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_current_applicability import _optimization_run, _synthetic_result
from tests.unit.test_dispatch import archived_energy, comparison_run

INVALID = [("charge", False), ("charge", 0.0), ("multiplicity", True), ("multiplicity", 1.0)]


@pytest.fixture
def archived(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run, result = archived_energy(store)
    return store, run, store.load_request(run), result


def conditions_at(request, scope):
    if scope == "system":
        return request.systems[0].conditions
    if scope == "goal":
        return request.goals[0].conditions
    return request.conditions


@pytest.mark.parametrize("scope", ["request", "system", "goal"])
@pytest.mark.parametrize("name,value", INVALID)
def test_persisted_invalid_conditions_block_plan_and_archived_sp(archived, scope, name, value):
    store, source_run, request, result = archived
    original = store.load_result(source_run.id, result.id).model_dump_json()
    request.systems = [SystemInput(id="water", geometry_artifact_id=request.geometry_artifact_id)]
    request.goals[0].system_ids = ["water"]
    conditions_at(request, scope)[name] = value
    permission = PermissionSnapshot(scientific_execution=True, artifact_ids=[request.geometry_artifact_id],
                                    result_ids=[result.id])
    current = store.create_run(request, None, permission)
    persisted = store.load_request(store.load_run(current.id))
    assert type(conditions_at(persisted, scope)[name]) is type(value)
    assessment = goal_evidence_assessment(store, current, persisted, persisted.goals[0], result)
    assert assessment["status"] == "unresolved"
    assert "unknown_condition:" + name in assessment["reasons"]
    with pytest.raises(ValueError, match="condition:" + name):
        store.create_run(persisted, store.load_plan(source_run), permission)
    assert store.load_result(source_run.id, result.id).model_dump_json() == original
    assert current.usage.orca_starts_actual == source_run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("name,value", INVALID)
def test_invalid_actual_condition_cannot_reuse_qualified_source(archived, name, value):
    store, run, request, original = archived
    persisted_original = store.load_result(run.id, original.id).model_dump_json()
    candidate = original.model_copy(deep=True)
    candidate.source["conditions"][name] = value
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], candidate)
    assert assessment["status"] == "unresolved"
    assert "source_conditions_missing_or_outside_profile:" + name in assessment["reasons"]
    assert candidate.qualified_outputs == original.qualified_outputs
    assert validate_goal_evidence(store, run, request, request.goals[0], original)
    assert store.load_result(run.id, original.id).model_dump_json() == persisted_original
    assert run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("scope", ["request", "system", "goal"])
def test_derived_current_purpose_cannot_equate_invalid_numeric_constraints(archived, scope):
    store, source_run, _, source = archived
    run, step = comparison_run(store, source_run, source, systems=[SystemInput(id="water")])
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    request = store.load_request(run)
    assert validate_goal_evidence(store, run, request, request.goals[0], result)
    for name, value in INVALID:
        invalid = request.model_copy(deep=True)
        conditions_at(invalid, scope)[name] = value
        assessment = goal_evidence_assessment(store, run, invalid, invalid.goals[0], result)
        assert assessment["status"] == "unresolved"
        assert "unknown_condition:" + name in assessment["reasons"]
    assert result.qualified_outputs["energy_difference"].value == 0
    assert run.usage.orca_starts_actual == source_run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("scope", ["request", "system", "goal"])
def test_opt_history_and_consumption_require_integer_current_conditions(tmp_path, scope):
    store, request, source_plan, permission, initial = _optimization_run(tmp_path)
    source_run = store.create_run(request, source_plan, permission)
    optimized = _synthetic_result(store, source_run, source_plan.steps[0], initial.id)
    persisted_optimized = store.load_result(source_run.id, optimized.id).model_dump_json()
    artifact = optimized.qualified_outputs["optimized_geometry"].artifact_id
    downstream = _synthetic_result(store, source_run, source_plan.steps[1], artifact)
    step = Step(id="new_sp", logical_id="new_sp", tool="orca.sp", system_id="water",
                geometry=InputRef(artifact_id=artifact))
    current_plan = Plan(request_id=request.id, steps=[step],
                        goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    permission.artifact_ids.append(artifact)
    permission.result_ids.extend([optimized.id, downstream.id])
    for name, value in INVALID:
        invalid = request.model_copy(deep=True)
        conditions_at(invalid, scope)[name] = value
        current = store.create_run(invalid, None, permission)
        invalid = store.load_request(current)
        for source in (optimized, downstream):
            assessed = goal_evidence_assessment(store, current, invalid, invalid.goals[0], source)
            assert assessed["status"] == "unresolved"
            assert "unknown_condition:" + name in assessed["reasons"]
        with pytest.raises(ValueError, match="condition:" + name):
            store.create_run(invalid, current_plan, permission)
        assert not current.attempts and current.usage.orca_starts_actual == 0

    # Exact integer constraints retain the complete cross-Run Opt -> SP path.
    conditions_at(request, scope).update(charge=0, multiplicity=1)
    current = store.create_run(request, current_plan, permission)
    persisted = store.load_request(current)
    assert effective_conditions(persisted, persisted.goals[0], "water")["status"] == "passed"
    for source in (optimized, downstream):
        assert validate_goal_evidence(store, current, persisted, persisted.goals[0], source)
    output = _synthetic_result(store, current, step, artifact)
    assert validate_goal_evidence(store, current, persisted, persisted.goals[0], output)
    assert store.load_result(source_run.id, optimized.id).model_dump_json() == persisted_optimized
    assert current.usage.orca_starts_actual == source_run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("name", ["charge", "multiplicity"])
@pytest.mark.parametrize("value", [None, "unknown"])
def test_existing_unknown_integer_conditions_remain_unresolved(name, value):
    assert canonical_condition(name, value) is None
