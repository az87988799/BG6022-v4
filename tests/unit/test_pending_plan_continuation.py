"""Pending approved work stays tied to current required Goals; no execution."""

from copy import deepcopy
from datetime import datetime, timezone

import pytest

from orca_agent.delivery import delivery_snapshot
from orca_agent.models import (
    Attempt,
    BudgetLimits,
    EvidenceRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Run,
    RunConfiguration,
    Step,
)


def pending_report(*, policy="reported-observation-2", optional=False):
    """Only typed archived receipts are used; files and scientific values are absent."""
    goal = Goal(id="report", port="geometry_report", required=not optional,
        original_text="Optimize the geometry, then analyze the completed evidence.",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")
    goals = [goal]
    if optional:
        goals.insert(0, Goal(id="required", port="energy"))
    request = Request(goals=goals)
    optimize = Step(id="optimize", logical_id="optimize", tool="orca.opt",
                    geometry=InputRef(artifact_id="initial_geometry"))
    analyze = Step(id="analyze", logical_id="analyze", tool="python.execute",
        parameters={"code": {"artifact_id": "analysis_code", "sha256": "a" * 64},
                    "sdk_orca": False, "observation_names": [goal.port]},
        depends_on=[optimize.id],
        inputs={"optimized": EvidenceRef(producer_step_id=optimize.id, port="optimized_geometry")})
    unrelated = Step(id="unrelated", logical_id="unrelated", tool="orca.sp",
                     geometry=InputRef(artifact_id="unrelated_geometry"))
    goal_map = {goal.id: OutputBinding(step_id=analyze.id, port=goal.port)}
    if optional:
        goal_map["required"] = OutputBinding(port="energy", gap="outside this fixture's completed work")
    plan = Plan(request_id=request.id, steps=[optimize, analyze, unrelated], goal_map=goal_map)
    run = Run(request_id=request.id, request_version=request.version,
        plan_id=plan.id, plan_version=plan.version,
        configuration=RunConfiguration(observation_delivery_policy=policy),
        permission=PermissionSnapshot(scientific_execution=True, allow_additional_science=True,
            code_execution=True, artifact_writes=True,
            allowed_tools=["orca.opt", "orca.sp", "python.execute"],
            artifact_ids=["initial_geometry", "analysis_code", "unrelated_geometry"]),
        budget=BudgetLimits(model_calls=8, model_tokens=100000, input_tokens=24000,
            output_tokens=2000, decision_rounds=12, plan_revisions=3,
            orca_starts=4, extra_orca_starts=3, analysis_executions=3, code_retries=2),
        initial_science_steps=[optimize.id, unrelated.id],
        goal_status={item.id: "insufficient_evidence" for item in goals})
    return request, run, plan, goal


def archived_completion(run, step):
    """A synthetic settled receipt exercises matching, never scientific qualification."""
    attempt = Attempt(step_id=step.id, logical_id=step.logical_id, number=1, tool=step.tool,
        execution_kind="python" if step.tool == "python.execute" else "orca",
        input_fingerprint="b" * 64, directory="offline-pending-plan-fixture",
        frozen_step=deepcopy(step), state="completed", request_version=run.request_version,
        plan_version=run.plan_version, permission_version=run.permission.version,
        finished_at=datetime(2026, 10, 21, tzinfo=timezone.utc))
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="completed")
    attempt.result_id = result.id
    run.attempts.append(attempt)
    run.result_ids.append(result.id)
    run.selected_results[step.id] = result.id
    if attempt.execution_kind == "orca":
        run.usage.orca_starts_reserved += 1
        run.usage.orca_starts_actual += 1
    return result


def snapshot(request, run, plan, *, results=None, selections=None):
    return delivery_snapshot(request, run, selections or {}, plan=plan, results=results or {})


def pending_steps(value):
    facts = [item["value"] for item in value["facts"] if item["kind"] == "current_plan_work"]
    if not facts:
        assert all(item["kind"] != "continue_approved_plan" for item in value["next_actions"])
        return []
    fact, = facts
    assert fact["kind"] == "execute_approved_steps"
    assert value["next_actions"][0]["kind"] == "continue_approved_plan"
    return fact["step_ids"]


def test_pending_required_goal_covers_its_dependencies_and_excludes_unrelated_work():
    request, run, plan, _ = pending_report()
    before = deepcopy((request, run, plan))
    assert pending_steps(snapshot(request, run, plan)) == ["optimize", "analyze"]
    assert (request, run, plan) == before


def test_completed_optimization_leaves_analysis_without_requiring_more_science_or_retry_budget():
    request, run, plan, _ = pending_report()
    result = archived_completion(run, plan.steps[0])
    run.permission.scientific_execution = run.permission.allow_additional_science = False
    run.budget.orca_starts = run.budget.extra_orca_starts = run.budget.code_retries = 0
    assert pending_steps(snapshot(request, run, plan, results={result.step_id: result})) == ["analyze"]


def test_completed_current_steps_do_not_reexecute_to_fill_a_missing_goal_assessment():
    request, run, plan, _ = pending_report()
    results = {step.id: archived_completion(run, step) for step in plan.steps[:2]}
    assert pending_steps(snapshot(request, run, plan, results=results)) == []


def test_optional_goal_pending_work_does_not_prevent_partial_delivery():
    request, run, plan, _ = pending_report(optional=True)
    assert pending_steps(snapshot(request, run, plan)) == []


def test_completed_required_goal_does_not_force_still_declared_work():
    request, run, plan, goal = pending_report()
    run.goal_status[goal.id] = "satisfied"
    selections = {goal.id: {"assessment": {"status": "passed", "reasons": []}}}
    value = snapshot(request, run, plan, selections=selections)
    assert value["goals"][0]["goal_complete"]
    assert pending_steps(value) == []


@pytest.mark.parametrize("policy", [None, "reported-observation-1"])
def test_legacy_snapshots_do_not_gain_a_continuation_requirement(policy):
    request, run, plan, _ = pending_report(policy=policy)
    value = snapshot(request, run, plan)
    assert pending_steps(value) == []
    assert value == delivery_snapshot(request, run, {})


@pytest.mark.parametrize("barrier", ["orca_budget", "analysis_budget", "code_permission"])
def test_required_pending_work_with_a_real_resource_barrier_can_be_reported(barrier):
    request, run, plan, _ = pending_report()
    if barrier == "orca_budget":
        run.budget.orca_starts = 0
    elif barrier == "analysis_budget":
        run.budget.analysis_executions = 0
    else:
        run.permission.code_execution = False
    assert pending_steps(snapshot(request, run, plan)) == []


def test_unknown_execution_requires_reconciliation_instead_of_more_work():
    request, run, plan, _ = pending_report()
    step = plan.steps[0]
    run.attempts.append(Attempt(step_id=step.id, logical_id=step.logical_id,
        number=1, tool=step.tool, input_fingerprint="b" * 64,
        directory="offline-unknown-fixture", frozen_step=deepcopy(step),
        state="unknown", request_version=request.version))
    assert pending_steps(snapshot(request, run, plan)) == []


@pytest.mark.parametrize("stale", ["declaration", "request"])
def test_historical_completion_does_not_consume_current_pending_declaration(stale):
    request, run, plan, _ = pending_report()
    result = archived_completion(run, plan.steps[0])
    if stale == "declaration":
        run.attempts[0].frozen_step.parameters.scf_maxiter += 1
    else:
        request.version = run.request_version = plan.request_version = 2
    assert pending_steps(snapshot(request, run, plan, results={result.step_id: result})) == ["optimize", "analyze"]


def test_model_settlement_does_not_change_the_continuation_snapshot_fingerprint():
    request, run, plan, _ = pending_report()
    initial = snapshot(request, run, plan)
    run.usage.model_calls += 1
    run.usage.decision_rounds += 1
    run.usage.model_tokens_used += 1000
    assert snapshot(request, run, plan) == initial
