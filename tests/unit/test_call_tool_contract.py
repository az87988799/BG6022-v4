"""Shared structural call contract; all runs and replies are offline fixtures."""

import itertools
import json
from types import SimpleNamespace

import pytest
from test_agent import ScriptedTransport, make_run
from test_context import objects, payload

from orca_agent import agent
from orca_agent.context import _share_strings, build_context
from orca_agent.models import Proposal
from orca_agent.proposals import (
    call_tool_instruction,
    call_tool_parameter_shapes,
    call_tool_parameters_schema,
    valid_call_tool_parameters,
)


@pytest.mark.parametrize("values,valid", [
    ({"step_id": "ready"}, True),
    ({"tool": "evidence.value", "parameters": {"path": []}}, True),
    ({"step_id": "ready", "artifact_id": "other", "path": [], "view": "raw"}, False),
    ({"step_id": "ready", "tool": "evidence.value", "parameters": {}}, False),
    ({"tool": "evidence.value", "parameters": {}, "artifact_id": "other"}, False),
    ({"step_id": ["ready"]}, False),
    ({"step_id": None}, False),
    ({"tool": "evidence.value", "parameters": []}, False),
    ({"tool": False, "parameters": {}}, False),
    ({"tool": "evidence.value"}, False),
    ({}, False),
    ([], False),
])
def test_exact_disjoint_call_forms(values, valid):
    assert valid_call_tool_parameters(values) is valid


def test_schema_alternatives_and_runtime_agree_for_all_field_combinations():
    schema = call_tool_parameters_schema()
    assert schema["type"] == "object"
    assert schema["properties"] == {
        "step_id": {"type": "string"}, "tool": {"type": "string"},
        "parameters": {"type": "object"},
    }
    fields = {"step_id": "ready", "tool": "evidence.value", "parameters": {},
              "artifact_id": "unplanned", "path": [], "view": "raw"}
    for count in range(len(fields) + 1):
        for keys in itertools.combinations(fields, count):
            values = {key: fields[key] for key in keys}
            matches = [set(branch["required"]) <= values.keys()
                       and len(values) <= branch["maxProperties"] for branch in schema["oneOf"]]
            assert (sum(matches) == 1) is valid_call_tool_parameters(values)


@pytest.mark.parametrize("immediate", [False, True])
def test_plain_instruction_and_schema_share_exact_field_sets(immediate):
    shapes = call_tool_parameter_shapes(immediate=immediate)
    schema = call_tool_parameters_schema(immediate=immediate)
    alternatives = schema.get("oneOf", [schema])
    assert [branch["required"] for branch in alternatives] == shapes
    instruction = call_tool_instruction(immediate=immediate)
    assert all("{" + ",".join(fields) + "}" in instruction for fields in shapes)
    assert "no inline params" in instruction and "match reason" in instruction
    assert ("catalog.name, never effects" in instruction) is immediate


def test_actual_response_schema_binds_contract_only_to_call_action():
    request, run = objects(scientific=False)
    prepared = build_context(request, run)
    data = payload(prepared)
    assert call_tool_instruction() in prepared.body()["messages"][0]["content"]
    schema = data["PROPOSAL_SCHEMA"]
    assert schema["if"] == {"properties": {"action": {"const": "call_tool"}}}
    assert schema["then"] == {"properties": {"parameters": call_tool_parameters_schema()}}
    assert schema["properties"]["parameters"] == {"type": "object"}
    assert {"initial_plan", "clarify", "stop"} <= set(schema["properties"]["action"]["enum"])
    # Closed envelope plus its exact field count requires every declared field.
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(Proposal.model_fields)
    for count in range(len(Proposal.model_fields) + 1):
        for keys in itertools.combinations(Proposal.model_fields, count):
            assert (len(keys) >= schema["minProperties"]) is (set(keys) == set(Proposal.model_fields))


def test_no_reader_budget_omits_immediate_contract_but_preserves_ready_step(tmp_path):
    store, run, _ = make_run(tmp_path, initial_plan=True)
    run.usage.evidence_reads = run.budget.evidence_reads
    data = payload(build_context(store.load_request(run), run, store.load_plan(run),
                                 feedback={"pending_step_ids": ["read_a"]}))
    contract = data["PROPOSAL_SCHEMA"]["then"]["properties"]["parameters"]
    assert contract == call_tool_parameters_schema(immediate=False)
    assert contract["required"] == ["step_id"] and contract["maxProperties"] == 1
    assert set(contract["properties"]) == {"step_id"}
    assert data["ACTION_PARAMETERS"]["call_tool"] == {"step_id": "read_a"}


def test_no_call_action_has_no_call_contract():
    request, run = objects(scientific=False)
    run.permission.allowed_tools = []
    schema = payload(build_context(request, run))["PROPOSAL_SCHEMA"]
    assert "call_tool" not in schema["properties"]["action"]["enum"]
    assert "if" not in schema and "then" not in schema


def test_hybrid_correction_cannot_override_or_reserve_a_ready_step(tmp_path):
    store, run, artifact = make_run(tmp_path, initial_plan=True)
    plan = store.load_plan(run)
    before = plan.model_dump_json()
    transport = ScriptedTransport(
        {"action": "call_tool", "parameters": {
            "step_id": "read_a", "artifact_id": artifact.id,
            "path": [{"kind": "key", "key": "b"}], "view": "raw"}},
        {"action": "call_tool", "parameters": {"step_id": "read_a"}},
    )
    updated, action, value = agent._decision(store, run, plan, {}, transport, None, None)
    assert action == "step" and value[0].id == "read_a"
    assert value[0].parameters.path[0].key == "a"
    assert store.load_plan(updated).model_dump_json() == before
    assert len(transport.sent) == updated.usage.model_calls == 2
    error = transport.sent[1]["CONTROL"]["validation_error"]
    assert error["category"] == "ProposalError"
    assert error["requirement"]["path"] == ["parameters"]
    assert error["requirement"]["allowed_shapes"] == call_tool_parameter_shapes()
    assert error["requirement"]["requirement"] == call_tool_instruction()
    assert [decision["action"] for decision in updated.decisions] == ["rejected", "call_tool"]
    assert not updated.calls and not updated.attempts and updated.usage.evidence_reads == 0


def test_valid_immediate_shape_still_requires_actual_tool_parameter_validation(tmp_path):
    store, run, artifact = make_run(tmp_path)
    transport = ScriptedTransport(
        {"action": "call_tool", "parameters": {"tool": "evidence.value", "parameters": {}}},
        {"action": "call_tool", "parameters": {"tool": "evidence.value", "parameters": {
            "artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}}},
    )
    updated, action, value = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "query" and value[0]["tool"] == "evidence.value"
    assert transport.sent[1]["CONTROL"]["validation_error"] is not None
    assert len(transport.sent) == updated.usage.model_calls == 2
    assert not updated.calls and not updated.attempts and updated.usage.evidence_reads == 0


def test_two_row_compaction_preserves_literal_null_missing_and_trust_paths():
    value = {"DATA": {"a": "repeated long literal " * 8, "b": "repeated long literal " * 8, "rows": [
        {"repeated_long_column_name_one": "literal", "repeated_long_column_name_two": {"@": 0},
         "repeated_long_column_name_three": None},
        {"repeated_long_column_name_one": "literal", "repeated_long_column_name_two": {"@": 1}},
    ]}}
    wire = _share_strings(value)
    assert "@columns" in str(wire)
    assert "literal SHARED_STRINGS[i]; no recursion" in wire["STRING_ENCODING"]
    decoded = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert decoded["DATA"] == value["DATA"]
