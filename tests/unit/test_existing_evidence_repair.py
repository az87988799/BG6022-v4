"""Pure feedback over archived receipt identities; no execution or parser mocks."""

from orca_agent.delivery import delivery_snapshot
from orca_agent.minimum_evidence import assess_minimum_evidence
from orca_agent.models import (
    Attempt,
    BudgetLimits,
    BudgetUsage,
    CodeRef,
    Goal,
    InternalCall,
    Observation,
    OutputBinding,
    PermissionSnapshot,
    Request,
    Result,
    Run,
    Step,
    utc_now,
)


def case():
    goal = Goal(id="report", port="novel_report", minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations")
    request = Request(goals=[goal])
    step = Step(id="analysis", logical_id="analysis", tool="python.execute", parameters={
        "code": CodeRef(artifact_id="script", sha256="a" * 64)})
    attempt = Attempt(id="parent", step_id=step.id, logical_id=step.logical_id, tool=step.tool,
        execution_kind="python", number=1, input_fingerprint="b" * 64, directory="archived",
        state="completed", request_version=request.version, frozen_step=step, finished_at=utc_now())
    native = InternalCall(request_key="completed_source", input_fingerprint="c" * 64,
        state="completed", parent_attempt_id=attempt.id, finished_at=utc_now(),
        actual_conditions={"request_version": request.version, "system_id": None}, artifact_ids=["raw"],
        response={"files": {"stdout.out": {"artifact_id": "raw", "sha256": "d" * 64}}})
    run = Run(request_id=request.id, request_version=request.version,
        permission=PermissionSnapshot(code_execution=True, artifact_writes=True,
            allowed_tools=["workspace.write", "python.execute"]),
        budget=BudgetLimits(orca_starts=4, analysis_executions=8, code_retries=2),
        usage=BudgetUsage(orca_starts_reserved=4, orca_starts_actual=4),
        attempts=[attempt], internal_calls=[native], goal_status={goal.id: "insufficient_evidence"})
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="completed",
        observation_records=[Observation(name=goal.port, value={"report": "archived metadata"})])
    attempt.result_id = result.id
    run.result_ids.append(result.id)
    selection = {"binding": OutputBinding(step_id=step.id, port=goal.port), "result": result, "gaps": [],
        "assessment": {"status": "unresolved", "reasons": ["requested_observation_lacks_completed_native_source"]},
        "source_integrity": {"status": "verified", "artifacts": [], "gaps": []}}
    return request, run, result, {goal.id: selection}


def test_exhausted_orca_budget_still_exposes_existing_source_analysis_without_completing_goal():
    request, run, result, selections = case()
    before = (request.model_dump_json(), run.model_dump_json(), result.model_dump_json())
    snapshot = delivery_snapshot(request, run, selections)
    assert snapshot["next_actions"][0]["kind"] == "repair_existing_evidence"
    repair = next(item["value"] for item in snapshot["facts"] if item["kind"] == "existing_evidence_repair")
    assert repair["new_orca_starts_required"] == 0
    assert repair["source_candidates"][0]["internal_request_key"] == "completed_source"
    assert repair["source_candidates"][0]["files"]["stdout.out"] == {"artifact_id": "raw", "sha256": "d" * 64}
    assert repair["analysis_executions_remaining"] == 8 and repair["code_attempts_remaining"] == 2
    assert not snapshot["goals"][0]["goal_complete"] and snapshot["goals"][0]["answer"] is None
    assert not result.qualified_outputs
    assert "not an authorized analysis" in next(item["text"] for item in snapshot["blockers"]
                                                if item["code"] == "orca_starts_exhausted")
    assert before == (request.model_dump_json(), run.model_dump_json(), result.model_dump_json())


def test_repair_suggestion_requires_permission_analysis_attempt_budget_and_settled_current_purpose():
    for unavailable in ("permission", "analysis_budget", "code_budget", "unsettled", "request_changed"):
        request, run, _, selections = case()
        if unavailable == "permission":
            run.permission.code_execution = False
        elif unavailable == "analysis_budget":
            run.usage.analysis_executions = run.budget.analysis_executions
        elif unavailable == "code_budget":
            run.usage.code_retries = run.budget.code_retries
        elif unavailable == "unsettled":
            run.internal_calls[0].state = "unknown"
        else:
            run.internal_calls[0].actual_conditions["request_version"] += 1
        snapshot = delivery_snapshot(request, run, selections)
        assert snapshot["next_actions"][0]["kind"] == "stop_with_partial_results", unavailable
        assert not any(item["kind"] == "existing_evidence_repair" for item in snapshot["facts"])


def test_missing_scientific_minimum_is_not_reclassified_as_a_source_only_repair():
    request, run, result, selections = case()
    goal = request.goals[0]
    goal.minimum_evidence = ["converged_scf@1"]
    requirements = assess_minimum_evidence(goal, result, purpose_passed=False)
    selections[goal.id]["assessment"]["reasons"].extend(
        item["reason"] + ":" + item["requested"] for item in requirements if item["status"] != "passed")
    snapshot = delivery_snapshot(request, run, selections)
    assert snapshot["next_actions"][0]["kind"] == "stop_with_partial_results"
    assert not snapshot["goals"][0]["goal_complete"]
    assert snapshot["goals"][0]["requested_minimum_evidence"] == ["converged_scf@1"]
    assert not result.qualified_outputs
