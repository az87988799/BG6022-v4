"""Immediate parameters remain copyable without decoding display aliases."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent import context
from orca_agent.context import _compact_native_wire, _schema, _terminal_key_encoding
from orca_agent.proposals import (
    DecisionIntent,
    ProposalError,
    ProposedPlan,
    validate_action_parameters,
)
from orca_agent.readable_context import native_schema
from orca_agent.tools.registry import get_tool, validate_parameters


def test_compact_immediate_schema_keeps_literal_names_defaults_and_numeric_bounds():
    names = ("evidence.text", "evidence.list", "knowledge.search")
    schemas = {name: get_tool(name).parameter_schema for name in names}
    template = {"AUTHORITY": {"basis": {"permission_version": 1}},
        "ACTION_SCHEMAS": {**schemas, "revise_plan": native_schema(ProposedPlan.model_json_schema())},
        "TOOLS": [{"name": name, "immediate": True, "parameters_ref": "ACTION_SCHEMAS." + name} for name in names],
        "DATA": {"results": [{"artifact_id": "artifact_" + str(index), "type": "untrusted",
                              "schema_lookalike": schemas["evidence.text"]} for index in range(20)]}}
    before = copy.deepcopy(template)
    wire = _compact_native_wire(template)
    assert template == before
    for name, schema in schemas.items():
        # Assert the raw wire, not a decoder that could conceal alias leakage.
        assert wire["ACTION_SCHEMAS"][name] == schema
    assert "SHARED_STRINGS" in wire  # DATA still uses existing compact encoding.
    keys = _terminal_key_encoding(template, literal_paths={("ACTION_SCHEMAS", name) for name in names})
    assert "artifact_id" in keys["KEYS"].values()
    assert all(keys["ACTION_SCHEMAS"][name] == schema for name, schema in schemas.items())
    text = wire["ACTION_SCHEMAS"]["evidence.text"]
    assert text["properties"]["start_line"] == get_tool("evidence.text").parameter_schema["properties"]["start_line"]
    assert text["properties"]["lines"] == get_tool("evidence.text").parameter_schema["properties"]["lines"]
    assert wire["ACTION_SCHEMAS"]["knowledge.search"]["properties"]["query"]["maxLength"] == 120
    assert text["required"] == ["artifact_id"] and text["additionalProperties"] is False
    assert "revise_plan" in wire["ACTION_SCHEMAS"]
    assert wire["AUTHORITY"]["basis"] == template["AUTHORITY"]["basis"]


def test_actual_live015_candidates_still_rejected_under_literal_schema():
    path = Path(__file__).parents[1] / "fixtures/code_contracts/live015-invalid-read-bounds.json"
    replies = json.loads(path.read_text(encoding="utf-8"))["replies"]
    assert len(replies) == 3
    for reply in replies:
        schema = get_tool("evidence.text").parameter_schema
        assert schema["properties"]["start_line"]["minimum"] == 1
        assert schema["properties"]["lines"]["maximum"] == 200
        with pytest.raises(ValidationError) as caught:
            validate_parameters("evidence.text", reply["proposal"]["parameters"])
        assert any(error["loc"] == ("start_line",) and error["ctx"]["ge"] == 1 for error in caught.value.errors())
    # These defaults are program-defined existing syntax, not a scientific
    # value or an automatically repaired/accepted model proposal.
    parameters = validate_parameters("evidence.text", {"artifact_id": "registered"})
    assert parameters.start_line == 1 and parameters.lines == 40


def test_noncode_immediate_schema_uses_legacy_reversible_projection_with_same_validation():
    from types import SimpleNamespace

    from test_context import payload

    names = ("evidence.text", "evidence.value", "knowledge.search")
    schemas = {name: get_tool(name).parameter_schema for name in names}
    source = {"ACTION_SCHEMAS": copy.deepcopy(schemas),
        "TOOLS": [{"name": name, "immediate": True, "parameters_ref": "ACTION_SCHEMAS." + name} for name in names],
        "AUTHORITY": {"basis": {"permission_version": 1}},
        "CONTROL": {"pending_step_ids": [], "validation_error": {"category": "ProposalError"}},
        "DATA": {"literal": {"@": 0}, "copies": [schemas["evidence.value"]] * 3}}
    before = copy.deepcopy(source)
    wire = _compact_native_wire(source, schema_columns=True, legacy=True)
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    def expand_references(node, root):
        if isinstance(node, list):
            return [expand_references(value, root) for value in node]
        if not isinstance(node, dict):
            return node
        if set(node) == {"$ref"}:
            assert node["$ref"].startswith("#/$defs/")
            return expand_references(root["$defs"][node["$ref"].split("/")[-1]], root)
        return {key: value if key in {"const", "enum", "default", "examples"}
                else expand_references(value, root) for key, value in node.items() if key != "$defs"}

    projected = {name: _schema(schema) for name, schema in schemas.items()}
    expected = {name: expand_references(schema, schema) for name, schema in projected.items()}
    assert restored["ACTION_SCHEMAS"] == expected
    for field in ("AUTHORITY", "CONTROL", "DATA", "TOOLS"):
        assert restored[field] == source[field]
    assert source == before
    # The historical display omits annotation defaults; the registry still
    # owns execution defaults and rejects values outside the same bounds.
    parameters = validate_parameters("evidence.text", {"artifact_id": "registered"})
    assert parameters.start_line == 1 and parameters.lines == 40
    with pytest.raises(ValidationError):
        validate_parameters("evidence.text", {"artifact_id": "registered", "start_line": 0})


def test_all_native_submission_contracts_and_correction_are_literal():
    schema = ProposedPlan.model_json_schema()
    tool = get_tool("python.execute")
    candidate = {"action": "revise_plan", "parameters": {"steps": [{"key": "prior"}],
        "goal_map": {"goal_geometry": {"step_key": "prior", "port": "prepared_geometry"}}}, "reason": "retain"}
    template = {"RESPONSE_ENVELOPE": {"action": "revise_plan", "parameters": {}, "reason": "<reason>"},
        "ACTION_SCHEMAS": {"revise_plan": schema},
        "TOOLS": [{"name": tool.name, "parameters": tool.parameter_schema},
                  {"name": "orca.run", "parameters": get_tool("orca.run").parameter_schema}],
        "CONTROL": {"validation_error": {"errors": [{"type": "missing", "loc": ["parameters", "code"]}]}},
        "AUTHORITY": {"request": {"goals": [{"id": "goal_geometry", "port": "prepared_geometry"}]},
            "plan": {"goal_map": {"goal_geometry": {"step_id": "prior", "port": "prepared_geometry"}}},
            "permission": {"code_execution": False}},
        "DATA": {"rejected_proposal": {"trust": "untrusted", "proposal": candidate},
            "results": [{"parameters": {}, "reason": "repeat", "port": "prepared_geometry",
                "request": {"goals": [{"id": str(index), "port": "prepared_geometry"}]},
                "plan": {"junk": str(index)}} for index in range(20)]}}
    before = copy.deepcopy(template)
    wire = _compact_native_wire(template)
    assert template == before
    assert wire["RESPONSE_ENVELOPE"] == template["RESPONSE_ENVELOPE"]
    assert wire["CONTROL"] == template["CONTROL"]
    assert wire["DATA"]["rejected_proposal"] == template["DATA"]["rejected_proposal"]
    assert wire["AUTHORITY"]["request"]["goals"] == template["AUTHORITY"]["request"]["goals"]
    assert wire["AUTHORITY"]["plan"]["goal_map"] == template["AUTHORITY"]["plan"]["goal_map"]
    assert wire["AUTHORITY"]["permission"]["code_execution"] is False
    assert "SCHEMA_COLUMNS" not in wire
    plan = wire["ACTION_SCHEMAS"]["revise_plan"]
    step, repair = plan["properties"]["steps"]["items"]["anyOf"]
    assert set(step["properties"]) == set(schema["$defs"]["ProposedStep"]["properties"])
    assert step["required"] == ["key"] and step["additionalProperties"] is False
    assert step["properties"]["logical_key"]["default"] is None
    assert set(repair["properties"]) == {"key", "repair_of", "code"}
    assert repair["required"] == ["key", "repair_of", "code"] and repair["additionalProperties"] is False
    assert repair["properties"]["code"]["properties"]["sha256"]["pattern"] == "^[a-f0-9]{64}$"
    assert isinstance(wire["TOOLS"], list)  # Table encoding must not hide protected child paths.
    python = wire["TOOLS"][0]
    assert python["name"] == "python.execute"
    assert set(python["parameters"]["properties"]) == {"code", "inputs", "observation_names", "conditions"}
    code = python["parameters"]["properties"]["code"]["properties"]
    assert code["sha256"]["pattern"] == "^[a-f0-9]{64}$"
    assert code["version"]["minimum"] == 1 and code["version"]["default"] == 1
    assert code["language"]["default"] == "python"
    manifest = python["parameters"]["properties"]["inputs"]["properties"]["entries"]["items"]
    assert manifest["properties"]["target_path"]["minLength"] == 1
    assert manifest["properties"]["target_path"]["maxLength"] == 240


def test_actual_live016_aliases_and_incomplete_evidence_stay_invalid():
    from tests.unit.test_plan_goal_binding_contract import matches

    path = Path(__file__).parents[1] / "fixtures/code_contracts/live016-contract-alias-replies.json"
    replies = {row["ordinal"]: row["proposal"] for row in json.loads(path.read_text(encoding="utf-8"))["replies"]}
    before = copy.deepcopy(replies)
    for ordinal in (13, 19):
        with pytest.raises(ValidationError):
            DecisionIntent.model_validate(replies[ordinal])
    wire = _compact_native_wire({"ACTION_SCHEMAS": {"revise_plan": ProposedPlan.model_json_schema()}, "TOOLS": []})
    schema = _schema(wire["ACTION_SCHEMAS"]["revise_plan"])
    for ordinal in (14, 18):
        assert not matches(schema, replies[ordinal]["parameters"], schema)
        with pytest.raises(ProposalError):
            validate_action_parameters("revise_plan", replies[ordinal]["parameters"])
    # A correctly spelled proposal can still fail later Goal/source gates.
    # The context projection neither supplies missing bindings nor executes it.
    assert matches(schema, replies[17]["parameters"], schema)
    assert replies == before
    valid = {"steps": [{"key": "existing"}], "goal_map": {
        "geometry": {"step_key": "existing", "port": "prepared_geometry"}}}
    assert matches(schema, valid, schema)
    validate_action_parameters("revise_plan", valid)


def test_annotation_projection_preserves_schema_looking_literal_defaults():
    value = {"type": "object", "properties": {"description": {"type": "string", "default": "literal"}},
        "default": {"title": "literal", "description": "literal", "pattern": "literal"},
        "title": "annotation", "description": "annotation"}
    projected = native_schema(value)
    assert projected["default"] == value["default"]
    assert projected["properties"]["description"] == value["properties"]["description"]


def test_last_capacity_schema_columns_preserve_all_literal_fields_and_defaults():
    from tests.unit.test_decision_purpose_capacity import expand_schema

    template = {"RESPONSE_ENVELOPE": {"action": "revise_plan", "parameters": {}, "reason": "explain"},
        "ACTION_SCHEMAS": {"revise_plan": ProposedPlan.model_json_schema(),
            "evidence.text": get_tool("evidence.text").parameter_schema},
        "TOOLS": [{"name": "python.execute", "parameters": get_tool("python.execute").parameter_schema},
                  {"name": "evidence.text", "immediate": True, "parameters_ref": "ACTION_SCHEMAS.evidence.text"}],
        "DATA": {"untrusted": [{"parameters": "repeat", "observation_names": "repeat"} for _ in range(20)]}}
    full, tight = _compact_native_wire(template), _compact_native_wire(template, schema_columns=True)
    assert tight["RESPONSE_ENVELOPE"] == template["RESPONSE_ENVELOPE"]
    for name, schema in tight["ACTION_SCHEMAS"].items():
        assert expand_schema(schema) == full["ACTION_SCHEMAS"][name]
    assert expand_schema(tight["TOOLS"][0]["parameters"]) == full["TOOLS"][0]["parameters"]
    assert tight["ACTION_SCHEMAS"]["evidence.text"] == get_tool("evidence.text").parameter_schema
    literal = {"default": {"type": "string", "properties": {"title": "literal"}},
               "examples": [{"type": "array", "items": {"type": "integer"}}]}
    assert context._schema_columns(literal) == literal


def test_terminal_compact_response_preserves_native_envelope_and_constraints(tmp_path, monkeypatch):
    from orca_agent.decision_purpose import classify_decision_purpose
    from orca_agent.delivery import collect_delivery_snapshot
    from orca_agent.models import utc_now
    from tests.unit.test_generated_delivery import case

    store, run, request, _ = case(tmp_path, monkeypatch)
    run.budget = run.budget.model_copy(update={"input_tokens": 64000, "model_tokens": 100000, "output_tokens": 2000})
    run.configuration = run.configuration.model_copy(update={"model":
        run.configuration.model.model_copy(update={"input_tokens": 64000})})
    snapshot = collect_delivery_snapshot(store, run, request, store.load_plan(run))
    prepare = context.prepare_request
    calls = []
    def compact_only(messages, **kwargs):
        raw = json.loads(messages[1]["content"])
        calls.append(raw)
        if len(calls) == 1:
            raise ValueError("conservative input token bound exceeds forced-first-view-test")
        return prepare(messages, **kwargs)
    monkeypatch.setattr(context, "prepare_request", compact_only)
    before = run.model_copy(deep=True)
    prepared = context._terminal_context(request, run, snapshot, classify_decision_purpose(["stop"]), {}, (),
        run.control_generation, utc_now(), "disabled")
    raw = json.loads(prepared.body()["messages"][1]["content"])
    assert len(calls) > 1 and "PROPOSAL_SCHEMA" not in raw
    assert set(raw["RESPONSE_ENVELOPE"]) == {"action", "parameters", "reason"}
    stop = raw["ACTION_SCHEMAS"]["stop"]
    assert stop["properties"]["reason"]["maxLength"] == 1000
    assert stop["properties"]["reason"]["default"] == ""
    assert stop["properties"]["delivery"]["properties"]["version"]["const"] == "terminal-delivery-1"
    assert run == before and not run.model_records
