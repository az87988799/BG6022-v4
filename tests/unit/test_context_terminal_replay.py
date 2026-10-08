"""Offline replay of the real V06 failure; no Store, transport or Tool execution."""

import copy
import hashlib
import itertools
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import context
from orca_agent.llm import input_token_upper_bound
from orca_agent.models import Plan, Proposal, Request, Result, Run
from orca_agent.proposals import validate_action_parameters
from tests.unit.test_context import payload

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/v06-feedback-context"


@pytest.fixture
def replay():
    raw = (FIXTURE / "input.json").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_bytes())
    assert provenance["kind"] == "real_derived"
    assert hashlib.sha256(raw).hexdigest() == provenance["input_file"]["sha256"]
    data = json.loads(raw)
    values = copy.deepcopy(data["inputs"])
    for name, model in (("request", Request), ("run", Run), ("plan", Plan)):
        values[name] = model.model_validate(values[name])
    values["results"] = [Result.model_validate(result) for result in values["results"]]
    values["now"] = datetime.fromisoformat(values["now"])
    return data, values


def decoded(messages):
    return payload(SimpleNamespace(body=lambda: {"messages": messages}))


def test_actual_failed_context_fits_without_changing_authority_data_or_tool_contract(replay, monkeypatch):
    data, inputs = replay
    before = copy.deepcopy(data["inputs"])
    with monkeypatch.context() as patch:
        patch.setattr(context, "_compact_terminal_response", lambda wire: wire)
        with pytest.raises(context.ContextLimitError, match="12000 global bound"):
            context.build_context(**inputs)
    prepared = context.build_context(**inputs)
    old_messages = data["legacy_compact_messages"]
    messages = prepared.body()["messages"]
    assert input_token_upper_bound({"messages": old_messages}) == 12372
    assert data["expected"]["corrected_input_token_bound"] == 11983  # Historical v19 bytes stay frozen.
    assert prepared.input_token_bound == 11988  # v21 changes only terminal guidance.
    assert prepared.input_token_bound <= inputs["run"].budget.input_tokens == 12000
    for key, sent in (("legacy", old_messages), ("corrected", messages)):
        assert hashlib.sha256(sent[1]["content"].encode()).hexdigest() == (
            data["expected"][f"{key}_user_message_sha256"])
    old_wire, wire = (json.loads(sent[1]["content"]) for sent in (old_messages, messages))
    for key in ("AUTHORITY", "DATA", "TOOL_CATALOG", "SHARED_STRINGS", "STRING_ENCODING"):
        assert wire[key] == old_wire[key]
    old, new = decoded(old_messages), decoded(messages)
    for key in ("AUTHORITY", "DATA", "TOOL_CATALOG"):
        assert new[key] == old[key]
    assert new["TOOL_CATALOG"][0]["check_contract"] == old["TOOL_CATALOG"][0]["check_contract"]
    assert set(new["PROPOSAL_SCHEMA"]["properties"]) == set(Proposal.model_fields)
    assert "RESPONSE_ENVELOPE" not in wire and "ACTION_PARAMETERS" not in wire
    assert "RESPONSE_ENVELOPE" not in messages[0]["content"]
    assert "JSON per PROPOSAL_SCHEMA; reason per REASON_TEMPLATE." in messages[0]["content"]
    assert wire["REASON_TEMPLATE"] == old_wire["RESPONSE_ENVELOPE"]["reason"]
    analysis = new["DATA"]["results"][0]["unqualified_observations"]["analysis"]
    assert len(analysis["members"]) == 5
    assert [member["status"] for member in analysis["members"]].count("missing") == 2
    assert [member["energy_eh"] for member in analysis["members"][:3]] == [
        -74.962692158239, -74.964906308165, -74.954287966128]
    assert analysis["scientific_status"] == "insufficient_evidence"
    assert analysis["target_width_angstrom"] == 0.12
    assert inputs["run"].state == "failed"  # Replay never relabels the historical run.
    assert inputs["run"].usage.model_calls == 1 and inputs["run"].usage.orca_starts_reserved == 0
    for key, model in (("request", inputs["request"]), ("run", inputs["run"]), ("plan", inputs["plan"])):
        # Historical bytes stay frozen; newly introduced empty local receipt
        # fields are supplied by the same model defaults on both sides.
        assert model == type(model).model_validate(before[key])
    assert [result.model_dump(mode="json") for result in inputs["results"]] == before["results"]
    assert inputs["feedback"] == before["feedback"]


@pytest.mark.parametrize("seconds_remaining", [None, 1790.001, 1000.001, 999.999, 100.001,
                                             99.999, 10.001, 9.999, 1.001, 0.001])
def test_post_result_and_deadline_clock_widths_fit_without_changing_facts(replay, seconds_remaining):
    _, inputs = replay
    # Result.created_at is the earliest recorded time this feedback can exist;
    # the exact failure time is not persisted, so never invent that timestamp.
    inputs["now"] = (inputs["results"][0].created_at if seconds_remaining is None else
                     inputs["run"].deadline - timedelta(seconds=seconds_remaining))
    assert inputs["now"] >= inputs["results"][0].created_at
    prepared = context.build_context(**inputs)
    assert prepared.input_token_bound <= 11988 < inputs["run"].budget.input_tokens
    assert inputs["run"].budget.input_tokens == 12000


def _matches(schema, value):
    """Interpret only the JSON Schema keywords present in these two envelopes.

    Unknown keywords fail this test helper instead of silently passing. This
    checks the emitted schemas independently of Pydantic's defaulted envelope.
    """
    assert set(schema) <= {"type", "const", "enum", "allOf", "oneOf", "if", "then",
        "required", "minProperties", "maxProperties", "properties", "additionalProperties",
        "minLength", "maxLength", "minItems", "maxItems", "items"}
    if "type" in schema and not isinstance(value, {"object": dict, "string": str, "array": list}[schema["type"]]):
        return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False
    if any(not _matches(branch, value) for branch in schema.get("allOf", [])):
        return False
    if "oneOf" in schema and sum(_matches(branch, value) for branch in schema["oneOf"]) != 1:
        return False
    if "if" in schema and _matches(schema["if"], value) and not _matches(schema["then"], value):
        return False
    if isinstance(value, dict):
        if (not set(schema.get("required", [])) <= value.keys()
                or not schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", float("inf"))):
            return False
        properties = schema.get("properties", {})
        for key, item in value.items():
            rule = properties.get(key, schema.get("additionalProperties", {}))
            if rule is False or (isinstance(rule, dict) and not _matches(rule, item)):
                return False
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
        return False
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            return False
        if any(not _matches(schema.get("items", {}), item) for item in value):
            return False
    return True


@pytest.mark.parametrize("action,parameters,valid", [
    ("stop", {}, True), ("stop", {"reason": "Insufficient samples."}, True),
    ("stop", {"reason": []}, False), ("stop", {"extra": True}, False),
    ("stop", {"reason": "x" * 1001}, False),
    ("clarify", {"questions": ["Which scope?"], "unresolved": ["scope"]}, True),
    ("clarify", {}, False), ("clarify", {"questions": ["Which scope?"]}, False),
    ("clarify", {"unresolved": ["scope"]}, False),
    ("clarify", {"questions": ["Which scope?"], "unresolved": ["scope"], "extra": True}, False),
    ("clarify", {"questions": [], "unresolved": ["scope"]}, False),
    ("clarify", {"questions": [""], "unresolved": ["scope"]}, False),
    ("clarify", {"questions": [False], "unresolved": ["scope"]}, False),
    ("clarify", {"questions": ["Which scope?"] * 6, "unresolved": ["scope"]}, False),
])
def test_old_and_compact_action_parameter_schemas_agree_with_program_validation(replay, action, parameters, valid):
    data, inputs = replay
    schemas = [decoded(data["legacy_compact_messages"])["PROPOSAL_SCHEMA"],
               payload(context.build_context(**inputs))["PROPOSAL_SCHEMA"]]
    proposal = {"action": action, "reason": "No remaining authorized science.", "parameters": parameters,
        "request_version": 1, "plan_version": 1, "permission_version": 1, "control_generation": 0,
        "related_results": inputs["run"].result_ids}
    for schema in schemas:
        assert _matches(schema, proposal) is valid
    if valid:
        validate_action_parameters(action, parameters)
    else:
        with pytest.raises(ValueError):
            validate_action_parameters(action, parameters)


@pytest.mark.parametrize("action", ["clarify", "stop"])
def test_both_actions_still_require_exactly_the_eight_envelope_fields(replay, action):
    data, inputs = replay
    old = decoded(data["legacy_compact_messages"])["PROPOSAL_SCHEMA"]
    new = payload(context.build_context(**inputs))["PROPOSAL_SCHEMA"]
    assert {key: value for key, value in old.items() if key != "allOf"} == {
        key: value for key, value in new.items() if key != "oneOf"}
    assert new["oneOf"] == [{"properties": {**rule["if"]["properties"], **rule["then"]["properties"]}}
                             for rule in old["allOf"]]
    proposal = {"action": action, "reason": "Insufficient evidence.", "parameters": (
        {"questions": ["Which scope?"], "unresolved": ["scope"]} if action == "clarify" else {}),
        "request_version": 1, "plan_version": 1, "permission_version": 1, "control_generation": 0,
        "related_results": inputs["run"].result_ids}
    for count in range(len(proposal) + 1):
        for keys in itertools.combinations(proposal, count):
            candidate = {key: proposal[key] for key in keys}
            assert _matches(old, candidate) is (count == 8)
            assert _matches(new, candidate) is (count == 8)
    for schema in (old, new):
        assert not _matches(schema, {**proposal, "extra": True})
        assert not _matches(schema, {**proposal, "parameters": []})
        assert not _matches(schema, {**proposal, "action": "call_tool"})


@pytest.mark.parametrize("change", ["extra_action", "one_action", "missing_branch", "overlap", "condition", "consequence"])
def test_nonexhaustive_or_nonterminal_schema_is_not_rewritten(replay, change):
    data, _ = replay
    wire = json.loads(data["legacy_compact_messages"][1]["content"])
    schema = wire["PROPOSAL_SCHEMA"]
    if change == "extra_action":
        schema["properties"]["action"]["enum"].append("call_tool")
    elif change == "one_action":
        schema["properties"]["action"]["enum"] = ["stop"]
    elif change == "missing_branch":
        schema["allOf"].pop()
    elif change == "overlap":
        schema["allOf"][1] = copy.deepcopy(schema["allOf"][0])
    elif change == "condition":
        schema["allOf"][0]["if"]["required"] = ["action"]
    else:
        schema["allOf"][0]["then"]["properties"]["reason"] = {"const": "other"}
    assert context._compact_terminal_response(wire) is wire


def test_nonempty_control_and_parameter_schemas_survive_terminal_compaction(replay):
    data, _ = replay
    wire = json.loads(data["legacy_compact_messages"][1]["content"])
    wire["CONTROL"] = {"validation_error": {"requirement": "Exact declared parameters."}}
    wire["PARAMETER_SCHEMAS"] = {"retained": {"type": "object"}}
    compact = context._compact_terminal_response(wire)
    assert compact["CONTROL"] == wire["CONTROL"]
    assert compact["PARAMETER_SCHEMAS"] == wire["PARAMETER_SCHEMAS"]
