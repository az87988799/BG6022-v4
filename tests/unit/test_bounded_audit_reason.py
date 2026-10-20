"""Audit prose may be normalized without changing execution parameters."""

import copy

import pytest
from pydantic import ValidationError

from orca_agent.proposals import DecisionIntent, StopParameters, validate_action_parameters


@pytest.mark.parametrize("length", [0, 1000, 1001, 1163, 20000])
def test_reason_bound_preserves_original_action_and_parameters(length):
    raw = {"action": "call_tool", "parameters": {"step_id": "step_unchanged"},
           "reason": "由" * length}
    before = copy.deepcopy(raw)
    intent = DecisionIntent.model_validate(raw)
    assert raw == before
    assert intent.action == raw["action"] and intent.parameters == raw["parameters"]
    assert len(intent.reason) == min(length, 1000)
    if length > 1000:
        assert "reason shortened" in intent.reason
    else:
        assert intent.reason == raw["reason"]


def test_stop_returns_canonical_reason_without_mutating_raw_input():
    raw = {"reason": "x" * 1001}
    canonical = validate_action_parameters("stop", raw)
    assert len(canonical["reason"]) == 1000 and len(raw["reason"]) == 1001
    assert StopParameters.model_validate(canonical).reason == canonical["reason"]


@pytest.mark.parametrize("bad", [None, True, 1, [], {"reason": "x"}])
def test_wrong_reason_type_still_rejected(bad):
    with pytest.raises(ValidationError):
        DecisionIntent.model_validate({"action": "stop", "parameters": {}, "reason": bad})
    with pytest.raises(ValidationError):
        StopParameters.model_validate({"reason": bad})


def test_unknown_execution_envelope_fields_still_rejected():
    with pytest.raises(ValidationError):
        DecisionIntent.model_validate({"action": "call_tool", "parameters": {},
                                       "reason": "x" * 1001, "bypass_checks": True})
