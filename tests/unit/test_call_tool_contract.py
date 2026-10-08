"""Shared structural call contract; all runs and replies are offline fixtures."""

import itertools
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
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
    assert "@=literal SHARED_STRINGS[i];" in wire["STRING_ENCODING"]
    decoded = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert decoded["DATA"] == value["DATA"]
    assert decoded["DATA"]["rows"][0]["repeated_long_column_name_two"] == {"@": 0}
    assert decoded["DATA"]["rows"][1]["repeated_long_column_name_two"] == {"@": 1}


def test_copyable_clarify_examples_remain_strings_even_when_data_shares_the_same_literal():
    text = "<1-5 texts,1-1000 chars>"
    value = {"ACTION_PARAMETERS": {"clarify": {"questions": [text], "unresolved": [text]}, "stop": {}},
             "DATA": {"a": text, "b": text, "c": "long repeated evidence " * 8,
                      "d": "long repeated evidence " * 8, "literal_marker": {"@": 1}}}
    before = json.dumps(value, sort_keys=True)
    wire = _share_strings(value)
    assert "SHARED_STRINGS" in wire
    assert wire["ACTION_PARAMETERS"]["clarify"] == value["ACTION_PARAMETERS"]["clarify"]
    assert all(isinstance(item, str) for items in wire["ACTION_PARAMETERS"]["clarify"].values() for item in items)
    decoded = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert decoded["DATA"] == value["DATA"]
    assert json.dumps(value, sort_keys=True) == before


@pytest.mark.parametrize("parent,fields,native_keys", [
    ("ACTION_PARAMETERS", {
        "clarify": {"questions": ["<1-5 texts,1-1000 chars>"], "unresolved": ["<1-5 texts,1-1000 chars>"]},
        "call_tool": {"step_id": "step_registered_long_identifier"}, "stop": {},
    }, ["clarify", "call_tool"]),
    ("AUTHORITY", {
        "basis": {"request_version": 1, "plan_version": 1, "permission_version": 1, "control_generation": 0},
        "related_results": ["result_registered_long_identifier"],
        "goal_status": {"energy": "insufficient_evidence"},
    }, ["basis", "related_results", "goal_status"]),
    ("CONTROL", {"validation_error": {"path": ["parameters"], "requirement": "literal observed diagnostic"}}, None),
])
def test_native_paths_survive_shared_parent_objects_in_untrusted_data(parent, fields, native_keys):
    value = {parent: fields, "DATA": {"copy_one": fields, "copy_two": fields,
             "energy": {"value": -74.9, "unit": "Eh", "multiplicity": None}, "absent": {}}}
    before = json.dumps(value, sort_keys=True)
    wire = _share_strings(value)
    assert "SHARED_STRINGS" in wire
    if native_keys is None:
        assert wire[parent] == fields
    else:
        assert set(wire[parent]) == set(fields)
        assert all(wire[parent][key] == fields[key] for key in native_keys)
    decoded = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert decoded[parent] == fields and decoded["DATA"] == value["DATA"]
    assert json.dumps(value, sort_keys=True) == before


def test_native_action_path_survives_dense_sibling_tabulation():
    plan = {"steps": [{"key": "initial_energy", "tool": "orca.sp", "parameters": {}}],
            "goal_map": {"energy": {"step_key": "initial_energy", "port": "energy"}}}
    value = {"ACTION_PARAMETERS": {"initial_plan": plan, "revise_plan": plan,
             "clarify": {"questions": ["<1-5 texts,1-1000 chars>"], "unresolved": ["<1-5 texts,1-1000 chars>"]},
             "stop": {}}, "DATA": {"one": "repeated scientific text " * 8, "two": "repeated scientific text " * 8}}
    wire = _share_strings(value)
    assert wire["ACTION_PARAMETERS"]["clarify"] == value["ACTION_PARAMETERS"]["clarify"]
    decoded = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert decoded["ACTION_PARAMETERS"] == value["ACTION_PARAMETERS"]
    assert decoded["DATA"] == value["DATA"]


def test_rejected_v15_stop_extra_transport_type_is_not_repaired_or_accepted():
    # Portable shape of the real fourth V07 proposal; offline regression only.
    value = {"type": "json_object", "action": "stop", "request_version": 1, "plan_version": 1,
             "permission_version": 1, "control_generation": 0, "related_results": [],
             "reason": "quantity:dipoleMagnitude;unit:unknown;conditions:unknown;source:raw;limits:unverified;next:stop",
             "parameters": {}}
    raw = json.dumps(value)
    with pytest.raises(ValidationError) as caught:
        Proposal.model_validate(json.loads(raw))
    assert [{"type": e["type"], "loc": e["loc"]} for e in caught.value.errors()] == [
        {"type": "extra_forbidden", "loc": ("type",)}]
    assert json.loads(raw) == value
