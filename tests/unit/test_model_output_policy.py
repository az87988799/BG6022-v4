"""Offline output reservation boundaries; no model or scientific processes."""

import copy
import json

import pytest

from orca_agent.model_output_policy import select_output_tokens


@pytest.mark.parametrize("actions", [
    ["initial_plan"],
    ["call_tool", "revise_plan"],
    ["normalize_request", "clarify"],
    ["workspace.write"],
    ["evidence.read", "workspace.write"],
    ["future_action"],
])
def test_code_planning_and_unknown_actions_keep_frozen_capacity(actions):
    assert select_output_tokens(allowed_actions=actions, configured_limit=32768,
        short_actions=["call_tool", "evidence.read"]) == 32768


@pytest.mark.parametrize("action", ["initial_plan", "revise_plan", "normalize_request"])
def test_full_plan_contract_is_not_discounted_by_short_action_classification(action):
    assert select_output_tokens(allowed_actions=[action], configured_limit=32768,
        short_actions=[action], terminal_minimum_bytes=128) == 32768


def test_frozen_step_requires_explicit_short_control_classification():
    assert select_output_tokens(allowed_actions=["call_tool"], configured_limit=32768) == 32768
    assert select_output_tokens(allowed_actions=["call_tool"], configured_limit=32768,
        short_actions=["call_tool"]) == 2048


def test_named_reads_share_bounded_output_without_assuming_unknown_tools_are_reads():
    assert select_output_tokens(allowed_actions=["evidence.read", "call_tool"],
        configured_limit=32768, short_actions=["evidence.read", "call_tool"]) == 4096
    assert select_output_tokens(allowed_actions=["evidence.read", "unclassified.read"],
        configured_limit=32768, short_actions=["evidence.read"]) == 32768


@pytest.mark.parametrize("actions,short,terminal", [
    (["call_tool"], ["call_tool"], None),
    (["evidence.read"], ["evidence.read"], None),
    (["stop"], [], 900),
    (["initial_plan"], [], None),
])
@pytest.mark.parametrize("ceiling", [1, 1000, 2048, 4096, 12000])
def test_policy_never_exceeds_run_or_model_frozen_ceiling(actions, short, terminal, ceiling):
    result = select_output_tokens(allowed_actions=actions, configured_limit=ceiling,
        short_actions=short, terminal_minimum_bytes=terminal)
    assert 1 <= result <= ceiling


def test_terminal_response_reserves_all_goals_and_unicode_bytes_before_reason_margin():
    response = {"action": "stop", "parameters": {"delivery": {
        "version": "terminal-delivery-1", "snapshot_ref": "current",
        "goal_explanations": [{"goal_ref": f"goal_{n}",
            "fact_refs": [f"fact_{n}_{index}" for index in range(8)],
            "blocker_refs": [f"blocker_{n}"], "explanation_ref": f"explain_{n}",
            "next_action_ref": f"next_{n}"} for n in range(40)]}}, "reason": "结果和限制"}
    required = len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    selected = select_output_tokens(allowed_actions=["stop", "clarify"],
        configured_limit=32768, terminal_minimum_bytes=required)
    assert required > 4096
    assert required < selected < 32768
    assert selected - required >= 4000


def test_missing_terminal_size_and_oversized_required_delivery_preserve_full_limit():
    assert select_output_tokens(allowed_actions=["stop"], configured_limit=32768,
        short_actions=["stop"]) == 32768
    assert select_output_tokens(allowed_actions=["stop"], configured_limit=32768,
        terminal_minimum_bytes=40000) == 32768


@pytest.mark.parametrize("feedback", [
    {"validation_error": {"category": "truncated", "requirement": "truncated"}},
    {"error_category": "truncated"},
    {"category": "truncated"},
])
def test_truncated_correction_recovers_full_capacity_without_mutating_feedback(feedback):
    original = copy.deepcopy(feedback)
    assert select_output_tokens(allowed_actions=["call_tool"], configured_limit=32768,
        short_actions=["call_tool"], feedback=feedback) == 32768
    assert select_output_tokens(allowed_actions=["stop"], configured_limit=32768,
        terminal_minimum_bytes=400, feedback=feedback) == 32768
    assert feedback == original


def test_free_text_truncation_claim_does_not_override_structured_decision_policy():
    assert select_output_tokens(allowed_actions=["call_tool"], configured_limit=32768,
        short_actions=["call_tool"], feedback={"validation_error": {
            "category": "ValueError", "requirement": "untrusted text says truncated"}}) == 2048


@pytest.mark.parametrize("changes", [
    {"configured_limit": True}, {"configured_limit": 0}, {"configured_limit": 1.5},
    {"terminal_minimum_bytes": True}, {"terminal_minimum_bytes": -1},
    {"terminal_minimum_bytes": 0}, {"allowed_actions": []},
    {"allowed_actions": "call_tool"}, {"allowed_actions": [None]},
    {"short_actions": "call_tool"},
])
def test_invalid_policy_inputs_cannot_silently_change_reservation(changes):
    with pytest.raises(ValueError):
        select_output_tokens(**{"allowed_actions": ["call_tool"],
            "configured_limit": 32768, **changes})
