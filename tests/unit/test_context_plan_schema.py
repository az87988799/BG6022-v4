"""Equivalent projection of the same typed Plan schema; no model or Tool calls."""

import copy
import itertools
import json

import pytest
from test_plan_goal_binding_contract import EVIDENCE, matches
from test_planning import message, records, revised

from orca_agent.context import (
    _compact_planning_schema,
    _exhaustive_action_schema,
    _frozen_completed,
    _planning_display_metadata,
    _schema,
)
from orca_agent.models import (
    Attempt,
    Goal,
    OutputBinding,
    Plan,
    Proposal,
    Request,
    Run,
    Step,
    ToolCall,
)
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.proposals import ProposalError, action_parameter_schema, validate_action_parameters


@pytest.mark.parametrize("action", ["initial_plan", "revise_plan"])
def test_compact_plan_schema_agrees_on_all_binding_combinations_and_field_errors(action):
    original = _schema(action_parameter_schema(action))
    before = copy.deepcopy(original)
    compact = _compact_planning_schema(original)
    assert original == before
    assert len(json.dumps(compact)) < len(json.dumps(original))
    values = {"step_key": "s", "evidence": EVIDENCE, "gap": "Missing evidence."}
    exercised = 0
    for length in range(4):
        for names in itertools.combinations(values, length):
            target = {"port": "energy", **{name: values[name] for name in names}}
            alternatives = [target, {**target, "extra": 1}, {**target, "port": None},
                            {key: value for key, value in target.items() if key != "port"}]
            for name in names:
                alternatives.extend([{**target, name: None}, {**target, name: 7}])
            for altered in alternatives:
                proposed = {"steps": [{"key": "s", "tool": "orca.sp"}], "goal_map": {"energy": altered}}
                expected = matches(original, proposed, original)
                assert matches(compact, proposed, compact) == expected
                try:
                    validate_action_parameters(action, proposed)
                    accepted = True
                except ProposalError:
                    accepted = False
                assert accepted == expected
                exercised += 1
    assert exercised == 56


def test_conflicting_field_types_are_not_merged():
    schema = {"anyOf": [
        {"type": "object", "properties": {"value": {"type": "string"}},
         "required": ["value"], "additionalProperties": False},
        {"type": "object", "properties": {"value": {"type": "integer"}},
         "required": ["value"], "additionalProperties": False},
    ]}
    result = _compact_planning_schema(schema)
    assert result == schema
    for value in ({"value": "x"}, {"value": 2}, {"value": True}, {"value": None}, {}, {"extra": "x"}):
        assert matches(result, value, result) == matches(schema, value, schema)


@pytest.mark.parametrize("common", [(), ("port", "stage")])
def test_exact_property_count_keeps_one_source_with_zero_or_two_common_fields(common):
    rules = {"port": {"type": "string", "pattern": "^energy$"}, "stage": {"type": "integer"},
             "step_key": {"type": "string", "pattern": "^step_"},
             "evidence": {"type": "object", "properties": {"result_id": {"type": "string"}},
                          "required": ["result_id"], "additionalProperties": False},
             "gap": {"type": "string", "minLength": 1}}
    values = {"port": "energy", "stage": 2, "step_key": "step_one",
              "evidence": {"result_id": "result_one"}, "gap": "missing"}
    sources = ("step_key", "evidence", "gap")
    schema = {"anyOf": [{"type": "object", "additionalProperties": False,
        "properties": {name: rules[name] for name in (*common, source)},
        "required": [*common, source]} for source in sources]}
    before = copy.deepcopy(schema)
    result = _compact_planning_schema(schema)
    assert schema == before and "anyOf" not in result
    assert result["additionalProperties"] is False
    assert result["minProperties"] == result["maxProperties"] == len(common) + 1
    assert set(result.get("required", [])) == set(common)
    assert result["properties"] == {name: rules[name] for name in (*common, *sources)}
    names = (*common, *sources)
    for length in range(len(names) + 1):
        for fields in itertools.combinations(names, length):
            value = {name: values[name] for name in fields}
            expected = set(common) <= set(fields) and len(set(fields) & set(sources)) == 1
            assert matches(schema, value, schema) == expected
            assert matches(result, value, result) == expected
            # Presence count cannot bypass a field's type or common constraint.
            for field in fields:
                changed = {**value, field: None}
                assert not matches(schema, changed, schema)
                assert not matches(result, changed, result)
            assert not matches(result, {**value, "unregistered": "extra"}, result)
    if common:
        wrong_common = {**{name: values[name] for name in common}, "step_key": "step_one", "port": "other"}
        assert not matches(schema, wrong_common, schema) and not matches(result, wrong_common, result)


@pytest.mark.parametrize("branch_sources", [(('a', 'b'), ('c', 'd')), (('a',), ('b', 'c')), ((), ('a',))])
def test_non_singleton_source_branches_keep_their_explicit_alternatives(branch_sources):
    common = {"port": {"type": "string"}, "stage": {"type": "string"}}
    schema = {"anyOf": [{"type": "object", "additionalProperties": False,
        "properties": {**common, **dict.fromkeys(sources, {"type": "string"})},
        "required": [*common, *sources]} for sources in branch_sources]}
    result = _compact_planning_schema(schema)
    assert "anyOf" in result
    fields = list(dict.fromkeys(name for branch in schema["anyOf"] for name in branch["properties"]))
    for length in range(len(fields) + 1):
        for selected in itertools.combinations(fields, length):
            value = dict.fromkeys(selected, "value")
            assert matches(result, value, result) == matches(schema, value, schema)


def test_exact_count_optimization_cannot_merge_conflicting_common_field_types():
    schema = {"anyOf": [
        {"type": "object", "additionalProperties": False,
         "properties": {"port": {"type": "string"}, "a": {"type": "string"}}, "required": ["port", "a"]},
        {"type": "object", "additionalProperties": False,
         "properties": {"port": {"type": "integer"}, "b": {"type": "string"}}, "required": ["port", "b"]},
    ]}
    result = _compact_planning_schema(schema)
    assert result == schema
    for port in ("energy", 1, None):
        for sources in ({}, {"a": "value"}, {"b": "value"}, {"a": "value", "b": "value"}):
            value = {"port": port, **sources}
            assert matches(result, value, result) == matches(schema, value, schema)


def test_single_use_definitions_inline_but_shared_constraints_keep_working():
    schema = {"$defs": {
        "Single": {"type": "string", "pattern": "^only$"},
        "Shared": {"type": "string", "pattern": "^same$"},
    }, "type": "object", "properties": {
        "one": {"$ref": "#/$defs/Single"},
        "two": {"$ref": "#/$defs/Shared"},
        "three": {"$ref": "#/$defs/Shared"},
    }, "required": ["one", "two", "three"], "additionalProperties": False}
    before = copy.deepcopy(schema)
    result = _compact_planning_schema(schema)
    assert schema == before and set(result["$defs"]) == {"Shared"}
    assert result["properties"]["one"] == schema["$defs"]["Single"]
    assert result["properties"]["two"] == result["properties"]["three"] == {"$ref": "#/$defs/Shared"}
    for one, two, three in itertools.product(("only", "same", "wrong", None), repeat=3):
        value = {"one": one, "two": two, "three": three}
        assert matches(result, value, result) == matches(schema, value, schema)


@pytest.mark.parametrize("extra_constraint", [{"required": ["a", "absent"]}, {"minProperties": 2}])
def test_unsatisfiable_closed_object_branch_is_not_weakened(extra_constraint):
    schema = {"anyOf": [
        {"type": "object", "properties": {"a": {"type": "string"}},
         "required": ["a"], "minProperties": 1, "additionalProperties": False, **extra_constraint},
        {"type": "object", "properties": {"b": {"type": "string"}},
         "required": ["b"], "additionalProperties": False},
    ]}
    result = _compact_planning_schema(schema)
    for value in ({}, {"a": "x"}, {"b": "x"}, {"a": "x", "b": "y"}, {"a": "x", "absent": "x"}):
        assert matches(result, value, result) == matches(schema, value, schema)


def test_null_string_projection_preserves_pattern_and_does_not_accept_numbers():
    schema = {"anyOf": [{"type": "string", "pattern": "^[abc]+$"}, {"type": "null"}]}
    result = _compact_planning_schema(schema)
    assert result == {"type": ["string", "null"], "pattern": "^[abc]+$"}
    for value in (None, "abc", "", "abd", 0, False, [], {}):
        assert matches(result, value, result) == matches(schema, value, schema)


def planning_envelope():
    schema = _schema(Proposal.model_json_schema())
    schema.pop("required", None)
    schema["minProperties"] = len(Proposal.model_fields)
    actions = ["initial_plan", "call_tool", "clarify", "stop"]
    schema["properties"]["action"] = {"enum": actions}
    schema["allOf"] = []
    for action in actions:
        parameters = _schema(action_parameter_schema(action))
        if definitions := parameters.pop("$defs", None):
            schema.setdefault("$defs", {}).update(definitions)
        schema["allOf"].append({"if": {"properties": {"action": {"const": action}}},
                                "then": {"properties": {"parameters": parameters}}})
    return schema


def test_exhaustive_action_union_matches_original_closed_eight_field_envelope():
    schema = planning_envelope()
    before = copy.deepcopy(schema)
    merged = _exhaustive_action_schema(schema)
    result = _compact_planning_schema(merged)
    assert schema == before and "allOf" not in merged and "oneOf" in merged
    examples = {
        "initial_plan": {"steps": [{"key": "s", "tool": "orca.sp"}],
                         "goal_map": {"energy": {"step_key": "s", "port": "energy"}}},
        "call_tool": {"step_id": "read_one"},
        "clarify": {"questions": ["Which method?"], "unresolved": ["method unknown"]},
        "stop": {"reason": "No useful allowed work remains."},
    }
    for action, values in itertools.product(examples, examples.values()):
        value = {"action": action, "parameters": values, "reason": "offline schema equivalence",
                 "request_version": 1, "plan_version": None, "permission_version": 1,
                 "control_generation": 0, "related_results": []}
        variants = [value, {**value, "extra": True}]
        variants.extend({key: item for key, item in value.items() if key != omitted} for omitted in value)
        for changed in variants:
            assert matches(result, changed, result) == matches(schema, changed, schema)
    # A top-level call_tool condition may coexist with the other allOf rules.
    top_level = copy.deepcopy(schema)
    call = top_level["allOf"].pop(1)
    top_level.update(call)
    top_result = _exhaustive_action_schema(top_level)
    assert {json.dumps(branch, sort_keys=True) for branch in top_result["oneOf"]} == {
        json.dumps(branch, sort_keys=True) for branch in merged["oneOf"]}
    assert {key: value for key, value in top_result.items() if key != "oneOf"} == {
        key: value for key, value in merged.items() if key != "oneOf"}


@pytest.mark.parametrize("mutation", ["missing_rule", "duplicate_rule", "extra_action", "condition_constraint",
                                      "consequence_constraint", "action_not_required", "not_object", "else"])
def test_incomplete_or_nonstandard_action_rules_are_kept_verbatim(mutation):
    schema = planning_envelope()
    if mutation == "missing_rule":
        schema["allOf"].pop()
    elif mutation == "duplicate_rule":
        schema["allOf"][0] = copy.deepcopy(schema["allOf"][1])
    elif mutation == "extra_action":
        schema["properties"]["action"]["enum"].append("revise_plan")
    elif mutation == "condition_constraint":
        schema["allOf"][0]["if"]["required"] = ["action"]
    elif mutation == "consequence_constraint":
        schema["allOf"][0]["then"]["properties"]["reason"] = {"const": "unchanged constraint"}
    elif mutation == "action_not_required":
        schema.pop("minProperties")
    elif mutation == "not_object":
        schema["type"] = "array"
    elif mutation == "else":
        schema.update(schema["allOf"].pop())
        schema["else"] = {"properties": {"reason": {"const": "must preserve alternate constraint"}}}
    assert _exhaustive_action_schema(schema) is schema


def display_authority(request, plan, run):
    """Derived plain display data; these are never the source model objects."""
    return {"request": request.model_dump(mode="json"), "plan": plan.model_dump(mode="json"),
            "basis": {"request_version": request.version, "plan_version": run.plan_version,
                      "permission_version": run.permission.version, "control_generation": 0},
            "permission": run.permission.model_dump(mode="json"),
            "budget_limits": run.budget.model_dump(mode="json"),
            "delivery_snapshot_fingerprint": "immutable_snapshot_reference"}


@pytest.mark.parametrize("state", Attempt.model_json_schema()["properties"]["state"]["enum"])
def test_every_reserved_attempt_state_keeps_display_logical_identity(state):
    request, plan, run = records(attempted=True)
    run.permission.allowed_repairs.clear()
    run.attempts[0].state = state
    originals = [model.model_dump(mode="json") for model in (request, plan, run)]
    authority = display_authority(request, plan, run)
    expected = copy.deepcopy(authority)
    expected["request"].pop("id")
    expected["request"].pop("version")
    _planning_display_metadata(authority, run)
    assert authority == expected
    assert authority["plan"]["steps"][0]["logical_id"] == run.attempts[0].logical_id
    assert [model.model_dump(mode="json") for model in (request, plan, run)] == originals


@pytest.mark.parametrize("state", ToolCall.model_json_schema()["properties"]["state"]["enum"])
def test_every_reserved_call_state_keeps_display_logical_identity(state):
    request = Request(id="request_read", goals=[Goal(id="a", port="value_observation",
        minimum_check_version="evidence-read-1")])
    step = Step(id="read_one", logical_id="read_logical", tool="evidence.value",
                parameters={"artifact_id": "artifact_read", "path": [{"kind": "key", "key": "a"}]})
    plan = Plan(id="plan_read", request_id=request.id, steps=[step],
                goal_map={"a": OutputBinding(step_id=step.id, port="value_observation")})
    call = ToolCall(tool=step.tool, parameters=step.parameters.model_dump(mode="json"), step_id=step.id,
                    state=state, request_version=1, frozen_step=step.model_copy(deep=True))
    run = Run(request_id=request.id, request_version=1, plan_id=plan.id, plan_version=1, calls=[call])
    assert not run.permission.allowed_repairs
    originals = [model.model_dump(mode="json") for model in (request, plan, run)]
    authority = display_authority(request, plan, run)
    expected = copy.deepcopy(authority)
    expected["request"].pop("id")
    expected["request"].pop("version")
    _planning_display_metadata(authority, run)
    assert authority == expected
    assert authority["plan"]["steps"][0]["logical_id"] == step.logical_id
    assert [model.model_dump(mode="json") for model in (request, plan, run)] == originals


@pytest.mark.parametrize("repair_permission", [False, True])
def test_only_never_reserved_nonrepair_display_metadata_is_omitted(repair_permission):
    request, plan, run = records()
    if not repair_permission:
        run.permission.allowed_repairs.clear()
    assert not run.attempts and not run.calls
    originals = [model.model_dump(mode="json") for model in (request, plan, run)]
    authority = display_authority(request, plan, run)
    expected = copy.deepcopy(authority)
    expected["request"].pop("id")
    expected["request"].pop("version")
    if not repair_permission:
        expected["plan"]["steps"][0].pop("logical_id")
    _planning_display_metadata(authority, run)
    assert authority == expected
    assert authority["basis"]["request_version"] == request.version
    assert authority["plan"]["goal_map"] == originals[1]["goal_map"]
    assert [model.model_dump(mode="json") for model in (request, plan, run)] == originals


def test_failed_reservation_needs_logical_identity_for_trusted_user_geometry_change_without_repairs():
    request, plan, run = records(attempted=True)
    run.permission.allowed_repairs.clear()
    assert run.attempts[0].state == "failed"
    # Completed-result compaction is deliberately narrower than reservation.
    # Using this empty set for metadata omission would hide a required identity.
    assert _frozen_completed(plan, run, []) == {}
    authority = display_authority(request, plan, run)
    original_run = run.model_dump(mode="json")
    original_plan = plan.model_dump(mode="json")
    _planning_display_metadata(authority, run)
    logical_id = authority["plan"]["steps"][0]["logical_id"]
    user_request = request.model_copy(deep=True, update={"version": 2, "geometry_artifact_id": "geometry2",
                                                       "messages": [message()]})
    replacement = revised(plan, scf=1, logical_id=logical_id)
    replacement.request_version = user_request.version
    replacement.steps[0].geometry.artifact_id = "geometry2"
    validate_revision(request, plan, user_request, replacement, run, user_update=True)
    replacement.steps[0].logical_id = "invented_identity"
    with pytest.raises(PlanningError, match="additional scientific Steps are not authorized"):
        validate_revision(request, plan, user_request, replacement, run, user_update=True)
    replacement.steps[0].logical_id = logical_id
    replacement.steps[0].id = plan.steps[0].id
    replacement.goal_map["energy"].step_id = plan.steps[0].id
    with pytest.raises(PlanningError, match="reserved Step inputs and parameters are immutable"):
        validate_revision(request, plan, user_request, replacement, run, user_update=True)
    assert run.model_dump(mode="json") == original_run and plan.model_dump(mode="json") == original_plan
