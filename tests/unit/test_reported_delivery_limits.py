"""Terminal delivery cannot hide the unverified obligations of a sourced report."""

from copy import deepcopy

import pytest

from orca_agent.delivery import delivery_snapshot
from orca_agent.models import BudgetLimits, Goal, Observation, ObservationSource, PermissionSnapshot, Request, Result, Run
from orca_agent.proposals import ProposalError
from orca_agent.terminal import validate_terminal_explanation
from tests.unit.test_terminal_contract import valid_parameters


def reported_snapshot(*, reported=True):
    goal = Goal(id="candidate_report", port="candidate_structures",
        original_text="保存至少8个候选并报告筛选阈值。", minimum_evidence=["至少8个候选"],
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")
    request = Request(original_text=goal.original_text, goals=[goal])
    run = Run(id="run_report", request_id=request.id, request_version=1, plan_id="plan_report", plan_version=1,
        permission=PermissionSnapshot(), budget=BudgetLimits(), goal_status={goal.id: "satisfied"})
    result = Result(id="result_report", run_id=run.id, step_id="step_report", operation_status="completed",
        observation_records=[Observation(name=goal.port, value={"count": 8},
            sources=[ObservationSource(artifact_id="artifact_manifest", sha256="a" * 64)])])
    assessment = {"status": "passed", "reasons": [], "minimum_evidence": [{
        "requested": "至少8个候选", "status": "unverified", "reason": "no_registered_evaluator",
        "blocking": False, "source": "explicit"}]}
    if reported:
        assessment["verification_basis"] = "reported_delivery"
    selections = {goal.id: {"result": result, "assessment": assessment,
        "source_integrity": {"status": "verified", "artifacts": [], "gaps": []}}}
    return delivery_snapshot(request, run, selections)


def test_reported_delivery_carries_mandatory_unverified_limits_and_distinct_relation():
    snapshot = reported_snapshot()
    goal, = snapshot["goals"]
    assert goal["goal_complete"] and goal["scientific_qualification"] is False
    assert goal["answer"]["scientific_qualification"] is False
    limits, = [fact for fact in snapshot["facts"] if fact["kind"] == "verification_limits"]
    assert limits["ref"] in goal["required_fact_refs"]
    assert limits["value"]["scientific_qualification"] is False
    assert limits["value"]["minimum_evidence"][0]["requested"] == "至少8个候选"
    assert limits["value"]["minimum_evidence"][0]["status"] == "unverified"
    assert snapshot["explanations"][0]["relation"] == "reported_observation_delivered"
    assert validate_terminal_explanation(valid_parameters(snapshot), snapshot)


def test_model_cannot_omit_verification_limits_from_final_explanation():
    snapshot = reported_snapshot()
    limits = next(fact for fact in snapshot["facts"] if fact["kind"] == "verification_limits")
    parameters = deepcopy(valid_parameters(snapshot))
    parameters["delivery"]["goal_explanations"][0]["fact_refs"].remove(limits["ref"])
    with pytest.raises(ProposalError) as caught:
        validate_terminal_explanation(parameters, snapshot)
    assert caught.value.detail["code"] == "terminal_fact_scope"


def test_legacy_assessment_does_not_gain_new_reported_semantics():
    snapshot = reported_snapshot(reported=False)
    assert "verification_basis" not in snapshot["goals"][0]
    assert not any(fact["kind"] == "verification_limits" for fact in snapshot["facts"])
    assert snapshot["explanations"][0]["relation"] == "current_goal_satisfied"
