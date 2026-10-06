"""Current user purpose stays distinct from a source's original qualification."""

from types import SimpleNamespace

import pytest

from orca_agent.goals import validate_goal_evidence
from orca_agent.models import Check, EvidenceRef, Goal, OutputBinding, Plan, Request, Result, Step
from orca_agent.natural import _missing_information
from orca_agent.store import Store, StoreError
from tests.unit.test_agent import make_run
from tests.unit.test_dispatch import archived_energy


def test_fixed_initial_energy_does_not_accept_optimization_energy(tmp_path, monkeypatch):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source_run, result = archived_energy(store)
    request = store.load_request(source_run)
    goal = Goal(id="fixed", port="energy", minimum_check_version="orca-hf-2",
                conditions={"geometry_relation": "fixed_initial"})
    assert validate_goal_evidence(store, source_run, request, goal, result)
    optimization = source_run.model_copy(deep=True)
    optimization.attempts[0].tool = "orca.opt"
    original = store.load_run
    monkeypatch.setattr(store, "load_run", lambda value: optimization if value == source_run.id else original(value))
    assert not validate_goal_evidence(store, source_run, request, goal, result)


def test_historical_analysis_does_not_require_a_new_initial_geometry():
    request = Request(geometry_artifact_id=None, charge=None, multiplicity=None, method=None, basis=None,
        goals=[Goal(id="comparison", port="energy_difference", minimum_check_version="energy-compare-1")])
    normalized = _missing_information(request)
    assert normalized.normalization_status == "normalized"
    assert normalized.goals[0].unresolved == []


def test_fixed_initial_geometry_cannot_be_replaced_by_a_planned_optimized_dependency(tmp_path, monkeypatch):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run, result = archived_energy(store)
    other = tmp_path / "other.xyz"
    other.write_text("3\nOther geometry\nO 0 0 0\nH 0 .8 .6\nH 0 -.8 .6\n")
    geometry = store.import_artifact(other, "initial_geometry")
    request = store.load_request(run).model_copy(update={"geometry_artifact_id": geometry.id})
    plan = SimpleNamespace(steps=[SimpleNamespace(id=result.step_id,
        geometry=SimpleNamespace(producer_step_id="optimization"))])
    monkeypatch.setattr(store, "load_plan", lambda _: plan)
    goal = Goal(id="fixed", port="energy", minimum_check_version="orca-hf-2",
                conditions={"geometry_relation": "fixed_initial"})
    assert not validate_goal_evidence(store, run, request, goal, result)


def test_applied_action_history_cannot_be_erased_or_rewritten(tmp_path):
    store, run, _ = make_run(tmp_path)
    run.applied_decisions.append("decision_original")
    store.save_run(run)
    for replacement in ([], ["decision_other"]):
        edited = run.model_copy(deep=True)
        edited.applied_decisions = replacement
        with pytest.raises(StoreError, match="history"):
            store.save_run(edited)


def test_partial_observations_require_an_explicit_observation_goal(tmp_path):
    store, run, artifact = make_run(tmp_path)
    call = store.reserve_call(run, "evidence.value", {"artifact_id": artifact.id})
    result = Result(run_id=run.id, call_id=call.id, operation_status="completed",
        observations={"value_observation": {"status": "partial", "value": [1], "omitted": 5}},
        checks={"value_observation": [Check(name="bounded_evidence_read", status="passed",
                                             rule_version="evidence-read-1")]})
    request = store.load_request(run)
    goal = Goal(id="partial", port="value_observation", minimum_check_version="evidence-read-1")
    assert not validate_goal_evidence(store, run, request, goal, result)
    goal.conditions["accept_partial_observations"] = True
    assert validate_goal_evidence(store, run, request, goal, result)
    assert not result.qualified_outputs


def test_empty_search_does_not_satisfy_a_goal_requiring_an_actual_excerpt(tmp_path):
    store, run, _ = make_run(tmp_path)
    goal = Goal(id="excerpt", port="search_hits", minimum_check_version="evidence-read-1",
                conditions={"require_nonempty_matches": True})
    result = Result(run_id=run.id, operation_status="completed",
        observations={"search_hits": {"status": "observed", "matches": []}},
        checks={"search_hits": [Check(name="bounded_evidence_read", status="passed",
                                      rule_version="evidence-read-1")]})
    assert not validate_goal_evidence(store, run, store.load_request(run), goal, result)


def test_verified_immediate_query_can_fill_a_plan_gap(tmp_path):
    from orca_agent import runner
    from orca_agent.tools.dispatch import execute_call

    store, original, artifact = make_run(tmp_path)
    request = store.load_request(original)
    step = Step(id="unrelated", logical_id="unrelated", tool="evidence.value",
                parameters={"artifact_id": artifact.id})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"a": OutputBinding(port="value_observation", gap="query after import")})
    run = store.create_run(request, plan, original.permission, original.budget)
    result = execute_call(store, run, "evidence.value", request.goals[0].conditions["query"])
    run.goal_evidence["a"] = EvidenceRef(run_id=run.id, result_id=result.id,
                                        port="value_observation", rule_version="evidence-read-1")
    assert runner._goals(store, run, plan, {})
    assert run.goal_status == {"a": "satisfied"}
