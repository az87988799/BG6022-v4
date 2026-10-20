"""Actual rejected delivery proposal and explicit offline controls; no task execution."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_agent import ScriptedTransport, initial_proposal, make_run

from orca_agent import agent
from orca_agent.models import Plan, Request, Run
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.proposals import materialize_plan

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live033-goal-evidence-rule.json"


def case():
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "62735596ef13a4c0c946e3edb337d595ab3dcee514785005061c81b1e973de98"
    data = json.loads(raw)
    request = Request.model_validate(data["request"])
    prior = Plan.model_validate(data["plan"])
    run = Run.model_validate(data["run_context"])
    def missing_result(run_id, result_id):
        assert run_id == run.id and result_id in run.result_ids
        # This historical fixture contains Request/Plan/Run, not Result bodies.
        # Do not invent settled evidence: the proposal supplies every Step.
        raise FileNotFoundError("Result body is not included in this fixture")

    store = SimpleNamespace(load_plan=lambda _: prior, load_request=lambda _: request,
                            load_result=missing_result)
    return data, request, prior, run, store


def rejected():
    data, request, prior, run, store = case()
    proposal = data["proposal"]["parameters"]
    before = copy.deepcopy((request, prior, run, proposal))
    proposed = materialize_plan(store, run, proposal)
    with pytest.raises(PlanningError, match="concrete goal evidence lacks") as caught:
        validate_revision(request, prior, request, proposed, run, authorized_artifacts=data["authorized_artifacts"])
    assert (request, prior, run, proposal) == before
    return caught.value


def test_actual_live033_rule_rejection_identifies_contract_and_existing_route():
    detail = rejected().detail
    assert detail["goal_id"] == "goal_ethanol_optimized_geometry"
    assert detail["path"] == ["parameters", "goal_map", detail["goal_id"], "evidence", "rule_version"]
    assert detail["expected_rule_version"] == "generic-observation-1"
    assert detail["actual_rule_version"] is None
    assert detail["step_target_shape"] == {
        "step_key": "step_8b30c4eb08584a17876b1dbc45ed14be", "port": "optimized_geometry"}
    assert "reported-with-limitations" in detail["observation_contract"]
    assert "remain unverified" in detail["observation_contract"]
    assert len(json.dumps(detail)) > 300


def test_test_authored_step_routes_pass_without_changing_frozen_steps_or_qualification():
    data, request, prior, run, store = case()
    proposal = copy.deepcopy(data["proposal"]["parameters"])
    for goal, target in list(proposal["goal_map"].items()):
        if "evidence" in target:
            proposal["goal_map"][goal] = {
                "step_key": target["evidence"]["producer_step_id"], "port": target["port"]}
    before = copy.deepcopy((request, prior, run, proposal))
    proposed = materialize_plan(store, run, proposal)
    validate_revision(request, prior, request, proposed, run, authorized_artifacts=data["authorized_artifacts"])
    assert proposed.steps == prior.steps
    assert (request, prior, run, proposal) == before
    assert run.goal_status["goal_ethanol_optimized_geometry"] == "insufficient_evidence"
    assert request.goals[1].verification == "reported_with_limitations"


@pytest.mark.parametrize("mutation", ["wrong_port", "wrong_rule", "qualified_step_route", "qualified_evidence"])
def test_rule_feedback_does_not_relax_port_rule_or_explicit_qualification(mutation):
    data, request, prior, run, store = case()
    proposal = copy.deepcopy(data["proposal"]["parameters"])
    goal_id = "goal_ethanol_optimized_geometry"
    target = proposal["goal_map"][goal_id]
    goal = next(g for g in request.goals if g.id == goal_id)
    if mutation == "wrong_port":
        target["evidence"]["port"] = "different_quantity"
    elif mutation == "wrong_rule":
        target["evidence"]["rule_version"] = "unrelated-rule-1"
    else:
        goal.verification = "qualified"
        goal.minimum_check_version = "optimization-1"
        goal.evidence_contract_version = "legacy-1"
        if mutation == "qualified_step_route":
            proposal["goal_map"][goal_id] = {
                "step_key": target["evidence"]["producer_step_id"], "port": target["port"]}
    before = copy.deepcopy((request, prior, run, proposal))
    proposed = materialize_plan(store, run, proposal)
    with pytest.raises(PlanningError) as caught:
        validate_revision(request, prior, request, proposed, run, authorized_artifacts=data["authorized_artifacts"])
    if mutation == "qualified_step_route":
        assert str(caught.value) == "generic observations cannot replace required scientific qualification"
    elif mutation == "wrong_port":
        assert str(caught.value) == "concrete goal evidence changes the requested physical quantity"
    elif mutation == "wrong_rule":
        assert caught.value.detail["actual_rule_version"] == "unrelated-rule-1"
    elif mutation == "qualified_evidence":
        assert caught.value.detail["expected_rule_version"] == "optimization-1"
        assert "observation_contract" not in caught.value.detail
    assert (request, prior, run, proposal) == before


@pytest.mark.parametrize("label", ["x" * 20000, "bad rule\nsecret-like text", "file:///private/rule"])
def test_invalid_rule_diagnostic_is_bounded_without_mutating_proposal_or_relaxing_rejection(label):
    data, request, prior, run, store = case()
    proposal = copy.deepcopy(data["proposal"]["parameters"])
    proposal["goal_map"]["goal_ethanol_optimized_geometry"]["evidence"]["rule_version"] = label
    before = copy.deepcopy((request, prior, run, proposal))
    proposed = materialize_plan(store, run, proposal)
    with pytest.raises(PlanningError, match="concrete goal evidence lacks") as caught:
        validate_revision(request, prior, request, proposed, run, authorized_artifacts=data["authorized_artifacts"])
    detail = caught.value.detail
    assert detail["actual_rule_version"] == {
        "omitted": "unsafe_or_oversized_rule_label", "chars": len(label),
        "sha256": hashlib.sha256(label.encode()).hexdigest()}
    assert len(json.dumps(detail)) < 2000
    assert (request, prior, run, proposal) == before
    assert proposed.goal_map["goal_ethanol_optimized_geometry"].evidence.rule_version == label


@pytest.mark.parametrize("trusted", [True, False])
def test_real_decision_feedback_preserves_planning_detail_and_ignores_arbitrary_exception_detail(
        tmp_path, monkeypatch, trusted):
    error = rejected()
    if not trusted:
        error = ValueError("ordinary validation failure")
        error.detail = {"untrusted_extra": "must not be copied"}
    store, run, _ = make_run(tmp_path)

    def reject(*args, **kwargs):
        raise error

    monkeypatch.setattr(agent, "materialize_plan", reject)

    def verify_feedback(data):
        detail = data["CONTROL"]["validation_error"]["requirement"]
        assert detail == (error.detail if trusted else "ordinary validation failure")
        return {"action": "stop"}

    updated, action, _ = agent._decision(
        store, run, None, {}, ScriptedTransport(initial_proposal, verify_feedback), None, None)
    assert action == "stop" and updated.usage.model_calls == 2
    assert not updated.calls and not updated.attempts
    assert updated.decisions[0]["parameters"]["requirement"] == (
        error.detail if trusted else "ordinary validation failure")
