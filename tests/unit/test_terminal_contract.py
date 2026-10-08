"""Portable adversarial terminal contracts. No network or scientific execution."""

import copy

import pytest

from orca_agent.proposals import ProposalError, action_parameter_schema, validate_action_parameters
from orca_agent.terminal import validate_terminal_explanation


def snapshot(*, complete=False, two_goals=False):
    """Synthetic program facts: the old V06 failure shape, not real new evidence."""
    result = {"version": "terminal-delivery-1", "fingerprint": "synthetic",
              "goals": [], "facts": [], "blockers": [], "explanations": [], "next_actions": []}
    for index in range(1, 3 if two_goals else 2):
        goal = f"g{index}"
        facts = [f"f{index}members", f"f{index}criterion", f"f{index}goal"]
        blockers = [] if complete else [f"b{index}permission", f"b{index}budget"]
        explanation = f"e{index}complete" if complete else f"e{index}insufficient"
        action = f"n{index}stop"
        result["goals"].append({"ref": goal, "required": True, "required_fact_refs": facts,
            "required_blocker_refs": blockers, "explanation_refs": [explanation],
            "next_action_refs": [action]})
        for ref in facts:
            result["facts"].append({"ref": ref, "goal_ref": goal})
        for ref in blockers:
            result["blockers"].append({"ref": ref, "goal_refs": [goal], "value": 0})
        result["explanations"].append({"ref": explanation, "goal_ref": goal, "fact_refs": facts})
        result["next_actions"].append({"ref": action, "goal_refs": [goal], "blocker_refs": blockers,
                                      "kind": "stop_with_results", "requires": []})
    return result


def valid_parameters(facts):
    return {"delivery": {"version": "terminal-delivery-1", "snapshot_ref": "current",
        "goal_explanations": [{"goal_ref": goal["ref"], "fact_refs": goal["required_fact_refs"],
            "explanation_ref": goal["explanation_refs"][0],
            "blocker_refs": goal["required_blocker_refs"],
            "next_action_ref": goal["next_action_refs"][0]} for goal in facts["goals"]]}}


@pytest.mark.parametrize("complete", [False, True])
def test_positive_and_partial_relations_are_distinct_valid_deliveries(complete):
    facts = snapshot(complete=complete, two_goals=True)
    original = copy.deepcopy(facts)
    answer = validate_terminal_explanation(valid_parameters(facts), facts)
    assert len(answer["goal_explanations"]) == 2
    assert all(("complete" in row["explanation_ref"]) == complete for row in answer["goal_explanations"])
    assert facts == original


@pytest.mark.parametrize("field,value,code", [
    ("fact_refs", ["f1members", "f1goal"], "terminal_fact_scope"),
    ("fact_refs", ["f1members", "f1criterion", "f1goal", "f2goal"], "terminal_fact_scope"),
    ("fact_refs", ["f1members", "f1criterion", "f1goal", "f1goal"], "terminal_fact_scope"),
    ("blocker_refs", ["b1permission"], "terminal_blocker_coverage"),
    ("blocker_refs", ["b1permission", "b1budget", "b2budget"], "terminal_blocker_coverage"),
    ("explanation_ref", "e1complete", "terminal_incompatible_relation"),
    ("explanation_ref", "e2insufficient", "terminal_incompatible_relation"),
    ("next_action_ref", "n2stop", "terminal_incompatible_next_action"),
    ("next_action_ref", "run_more_orca", "terminal_incompatible_next_action"),
])
def test_legal_ids_do_not_authorize_wrong_goal_or_inference(field, value, code):
    facts = snapshot(two_goals=True)
    params = copy.deepcopy(valid_parameters(facts))
    params["delivery"]["goal_explanations"][0][field] = value
    with pytest.raises(ProposalError) as caught:
        validate_terminal_explanation(params, facts)
    assert caught.value.detail["code"] == code


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown"])
def test_every_required_goal_is_covered_once(mutation):
    facts = snapshot(two_goals=True)
    params = copy.deepcopy(valid_parameters(facts))
    rows = params["delivery"]["goal_explanations"]
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(copy.deepcopy(rows[0]))
    else:
        rows[0]["goal_ref"] = "g_missing"
    with pytest.raises(ProposalError) as caught:
        validate_terminal_explanation(params, facts)
    assert caught.value.detail["code"] == "terminal_goal_coverage"


@pytest.mark.parametrize("field,value", [("goal_complete", True), ("energy", -74.9),
                                          ("permission", True), ("reason", "everything passed")])
def test_model_cannot_inject_undeclared_claims_into_verified_explanation(field, value):
    params = valid_parameters(snapshot())
    params["delivery"]["goal_explanations"][0][field] = value
    with pytest.raises(ProposalError):
        validate_terminal_explanation(params, snapshot())


def test_historical_reason_is_readable_but_cannot_pass_new_terminal_schema():
    old = {"reason": "goal evidence is sufficient to answer within sampled range"}
    validate_action_parameters("stop", old)
    with pytest.raises(ProposalError):
        validate_action_parameters("stop", old, terminal_required=True)
    assert "delivery" in action_parameter_schema("stop", terminal_required=True)["required"]


def test_even_empty_blockers_are_explicit_so_accepted_delivery_equals_raw_model_payload():
    facts = snapshot(complete=True)
    params = valid_parameters(facts)
    assert validate_terminal_explanation(params, facts) == params["delivery"]
    del params["delivery"]["goal_explanations"][0]["blocker_refs"]
    with pytest.raises(ProposalError):
        validate_terminal_explanation(params, facts)
    assert "blocker_refs" in action_parameter_schema("stop", terminal_required=True)["$defs"]["GoalExplanation"]["required"]


def test_free_reason_is_retained_as_audit_not_copied_to_verified_claims():
    facts = snapshot()
    params = valid_parameters(facts)
    params["reason"] = "Synthetic intentionally wrong prose: all science succeeded."
    validated = validate_terminal_explanation(params, facts)
    assert "reason" not in validated
    assert validated["goal_explanations"][0]["explanation_ref"] == "e1insufficient"
    assert params["reason"].endswith("succeeded.")
