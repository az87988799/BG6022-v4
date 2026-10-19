"""Actual failed read proposals and synthetic correction; no live execution."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent import ScriptedTransport, make_run

from orca_agent import agent, runner
from orca_agent.proposals import ProposalError, validate_action_parameters, validation_error_details
from orca_agent.tools.registry import validate_parameters

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live015-invalid-read-bounds.json"


def test_actual_live015_proposals_expose_the_existing_bounds_without_rewriting():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "7156a2869c2adb3345d65028036b1ec7f469ac86ebb78543a1a32596dc552a58")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    before = copy.deepcopy(fixture)
    for index, item in enumerate(fixture["replies"]):
        with pytest.raises(ValidationError) as caught:
            validate_parameters("evidence.text", item["proposal"]["parameters"])
        details = validation_error_details(caught.value)
        by_field = {tuple(error["loc"]): error for error in details}
        assert by_field[("start_line",)] == {
            "type": "greater_than_equal", "loc": ["start_line"], "constraints": {"ge": 1}}
        if index < 2:
            assert by_field[("lines",)]["constraints"] == {"le": 200}
        else:
            assert ("lines",) not in by_field
        assert "artifact_91e6854b9cf64bd58a85746cc743a1c6" not in json.dumps(details)
    assert fixture == before


def test_feedback_drops_values_messages_urls_and_arbitrary_context():
    secret = "private candidate content"

    class ErrorFixture:
        def errors(self, **options):
            assert options == {"include_input": False, "include_url": False}
            return [{"type": "greater_than_equal", "loc": ("value",), "msg": secret,
                     "input": secret, "url": secret,
                     "ctx": {"ge": 1, "le": 200, "min_length": 2, "max_length": 8,
                             "gt": float("inf"), "lt": secret, "multiple_of": True,
                             "error": ValueError(secret), "expected": secret}}]

    assert validation_error_details(ErrorFixture()) == [{"type": "greater_than_equal", "loc": ["value"],
        "constraints": {"ge": 1, "le": 200, "min_length": 2, "max_length": 8}}]


def test_structural_action_feedback_uses_the_same_safe_bounds():
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("clarify", {"questions": [], "unresolved": ["unknown"]})
    detail = caught.value.detail
    error = next(item for item in detail["errors"] if item["loc"] == ["parameters", "questions"])
    assert error["constraints"] == {"min_length": 1}


def test_real_decision_correction_transmits_bounds_without_reserving_a_read(tmp_path):
    store, original, artifact = make_run(tmp_path)
    run = store.create_run(store.load_request(original), None,
        original.permission.model_copy(update={"allowed_tools": ["evidence.value", "evidence.text"]}),
        original.budget, configuration=original.configuration)
    run.agent_enabled = True
    runner._goals(store, run, None, {})
    store.save_run(run)
    assert store.load_run(run.id) == run
    rejected = {"action": "call_tool", "parameters": {"tool": "evidence.text", "parameters": {
        "artifact_id": artifact.id, "start_line": 0, "lines": 250}}}

    def corrected(data):
        errors = data["CONTROL"]["validation_error"]["requirement"]
        assert isinstance(errors, list), data["CONTROL"]["validation_error"]
        bounds = {item["loc"][0]: item["constraints"] for item in errors}
        return {"action": "call_tool", "parameters": {"tool": "evidence.text", "parameters": {
            "artifact_id": artifact.id, "start_line": bounds["start_line"]["ge"],
            "lines": bounds["lines"]["le"]}}}

    transport = ScriptedTransport(rejected, corrected)
    updated, action, value = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "query" and value[0]["parameters"]["start_line"] == 1
    assert value[0]["parameters"]["lines"] == 200
    assert len(transport.sent) == updated.usage.model_calls == 2
    assert updated.usage.model_tokens_used == 150 and updated.usage.model_tokens_unknown == 0
    assert not updated.calls and not updated.attempts and updated.usage.evidence_reads == 0
    assert updated.usage.orca_starts_actual == updated.usage.orca_starts_reserved == 0
    saved = store.load_run(run.id)
    assert saved.decisions[0]["parameters"]["requirement"][0]["constraints"] == {"ge": 1}
