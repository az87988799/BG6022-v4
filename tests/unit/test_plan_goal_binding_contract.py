"""Offline plan binding contracts; historical actual replies remain rejected."""

import copy
import hashlib
import itertools
import json
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_agent import ScriptedTransport, make_run
from test_proposals import single, source

from orca_agent import agent
from orca_agent.context import _schema
from orca_agent.models import Goal, InputRef, OutputBinding, Plan, Request, Step
from orca_agent.proposals import (
    ProposalError,
    ProposedPlan,
    action_parameter_schema,
    goal_target_shapes,
    materialize_plan,
    plan_structure_schema,
    validate_action_parameters,
)

FIXTURE = Path(__file__).parents[1] / "fixtures/phase_b/repair-cycle-v23-rejections.json"
EVIDENCE = {"run_id": "source_run", "result_id": "source_result", "port": "energy"}


def matches(schema, value, root):
    """Independent JSON Schema subset, fail closed on new validation keywords.

    The matrix below tests the outer disjoint target forms with valid existing
    EvidenceRef values. It does not claim to add EvidenceRef's existing runtime
    source/permission checks to JSON Schema.
    """
    assert set(schema) <= {"$ref", "$defs", "type", "properties", "additionalProperties",
                           "required", "minProperties", "maxProperties", "anyOf", "pattern", "items",
                           "minItems", "maxItems", "propertyNames", "allOf", "oneOf", "if", "then",
                           "const", "enum", "minLength", "maxLength"}
    if "$ref" in schema:
        assert schema["$ref"].startswith("#/$defs/")
        return matches(root["$defs"][schema["$ref"].split("/")[-1]], value, root)
    kinds = {"object": (dict,), "array": (list,), "string": (str,), "null": (type(None),),
             "integer": (int,), "number": (int, float), "boolean": (bool,)}
    if "type" in schema:
        declared = schema["type"]
        declared = declared if isinstance(declared, list) else [declared]
        if not any(type(value) in kinds[kind] for kind in declared):
            return False
    if "anyOf" in schema and not any(matches(branch, value, root) for branch in schema["anyOf"]):
        return False
    if any(not matches(branch, value, root) for branch in schema.get("allOf", [])):
        return False
    if "oneOf" in schema and sum(matches(branch, value, root) for branch in schema["oneOf"]) != 1:
        return False
    if "if" in schema and matches(schema["if"], value, root) and not matches(schema["then"], value, root):
        return False
    if "const" in schema and value != schema["const"] or "enum" in schema and value not in schema["enum"]:
        return False
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        return False
    if isinstance(value, str) and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", float("inf")):
        return False
    if isinstance(value, dict):
        if (not set(schema.get("required", [])) <= value.keys()
                or not schema.get("minProperties", 0) <= len(value) <= schema.get("maxProperties", float("inf"))):
            return False
        for key, item in value.items():
            if "propertyNames" in schema and not matches(schema["propertyNames"], key, root):
                return False
            rule = schema.get("properties", {}).get(key, schema.get("additionalProperties", {}))
            if rule is False or isinstance(rule, dict) and not matches(rule, item, root):
                return False
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", float("inf")):
            return False
        if any(not matches(schema.get("items", {}), item, root) for item in value):
            return False
    return True


@pytest.mark.parametrize("fields", [tuple(names) for length in range(4)
    for names in itertools.combinations(("step_key", "evidence", "gap"), length)])
@pytest.mark.parametrize("action", ["initial_plan", "revise_plan"])
def test_generated_closed_union_matches_exactly_one_binding_shape(fields, action):
    values = {"step_key": "measure", "evidence": EVIDENCE, "gap": "Missing usable evidence."}
    target = {"port": "energy", **{name: values[name] for name in fields}}
    proposed = {"steps": [{"key": "measure", "tool": "orca.sp"}], "goal_map": {"energy": target}}
    schema = _schema(action_parameter_schema(action))
    target_schema = schema["properties"]["goal_map"]["additionalProperties"]
    branches = target_schema["anyOf"]
    # Each branch is closed and requires a different source field. Thus this
    # generated anyOf is strictly exclusive without a second handwritten rule.
    assert len(branches) == 3
    assert sum(matches(branch, target, schema) for branch in branches) == (1 if len(fields) == 1 else 0)
    assert matches(schema, proposed, schema) is (len(fields) == 1)
    if len(fields) == 1:
        validate_action_parameters(action, proposed)
    else:
        with pytest.raises(ProposalError) as caught:
            validate_action_parameters(action, proposed)
        detail = caught.value.detail
        assert detail["code"] == "goal_target_shape"
        assert detail["path"] == ["parameters", "goal_map", "energy"]
        assert all(error["loc"][:3] == detail["path"] for error in detail["errors"])
        assert not any("GoalTarget" in str(error["loc"]) for error in detail["errors"])


@pytest.mark.parametrize("target", [
    {"step_key": "measure", "port": "energy", "gap": None},
    {"step_key": "measure", "port": "energy", "extra": "must not echo"},
    {"step_key": None, "port": "energy"},
    {"step_key": "measure"},
    {"step_id": "already_persisted", "port": "energy"},
    {"gap": 1, "port": "energy"},
    {"evidence": EVIDENCE, "port": None},
])
def test_unknown_missing_and_null_binding_fields_are_not_silently_repaired(target):
    proposed = {"steps": [{"key": "measure", "tool": "orca.sp"}], "goal_map": {"energy": target}}
    before = copy.deepcopy(proposed)
    schema = _schema(action_parameter_schema("initial_plan"))
    assert not matches(schema, proposed, schema)
    with pytest.raises(ProposalError):
        validate_action_parameters("initial_plan", proposed)
    assert proposed == before


@pytest.mark.parametrize("evidence", [EVIDENCE, {"producer_step_id": "existing_step", "port": "energy"}])
def test_existing_evidence_ref_forms_and_historical_output_binding_remain_supported(evidence):
    target = {"evidence": evidence, "port": "energy"}
    proposed = {"steps": [{"key": "measure", "tool": "orca.sp"}], "goal_map": {"energy": target}}
    schema = _schema(action_parameter_schema("initial_plan"))
    assert matches(schema, proposed, schema)
    validate_action_parameters("initial_plan", proposed)
    old = Plan(request_id="request_one", steps=[Step(id="step_one", logical_id="logical_one", tool="orca.sp",
               geometry=InputRef(artifact_id="artifact_one"))],
               goal_map={"energy": OutputBinding(step_id="step_one", port="energy")})
    assert Plan.model_validate(old.model_dump()) == old


def test_mixed_binding_rejection_preserves_store_request_plan_budget_and_payload(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposed = single()
    proposed["goal_map"]["energy"]["gap"] = "Untrusted detail must not be echoed into CONTROL."
    before = copy.deepcopy(proposed)
    run_bytes = store.path(f"runs/{run.id}/run.json").read_bytes()
    request = store.load_request(run).model_dump()
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposed)
    detail = caught.value.detail
    assert detail["path"] == ["parameters", "goal_map", "energy"]
    assert "Untrusted detail" not in str(detail)
    assert detail["binding_shapes"] == [["step_key", "port"], ["evidence", "port"], ["gap", "port"]]
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == run_bytes
    assert store.load_request(run).model_dump() == request and store.load_plan(run) is None
    assert proposed == before and not run.calls and not run.attempts


def test_unknown_goal_and_step_have_paths_without_inventing_request_goals(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposed = single()
    proposed["goal_map"]["diagnostic_is_not_a_goal"] = {"gap": "untrusted", "port": "energy"}
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposed)
    assert caught.value.detail == {
        "requirement": "Use only current Request Goal IDs; a Plan cannot introduce a Goal.",
        "code": "unknown_goal_id", "path": ["parameters", "goal_map"], "allowed_goal_ids": ["energy"]}
    proposed = single()
    proposed["goal_map"]["energy"]["step_key"] = "absent"
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposed)
    assert caught.value.detail["path"] == ["parameters", "goal_map", "energy", "step_key"]
    assert caught.value.detail["code"] == "unknown_goal_step"
    assert store.load_plan(run) is None and [g.id for g in store.load_request(run).goals] == ["energy"]


@pytest.mark.parametrize("target_kind", ["future", "gap"])
def test_missing_goal_correction_is_neutral_and_can_keep_a_future_route_or_gap(tmp_path, target_kind):
    store, run, artifact = make_run(tmp_path, two_goals=True)
    proposed = {"steps": [{"key": "read_a", "tool": "evidence.value", "parameters": {
        "artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}}],
        "goal_map": {"a": {"step_key": "read_a", "port": "value_observation"}}}

    def corrected(data):
        detail = data["CONTROL"]["validation_error"]["requirement"]
        assert detail["missing_goals"] == [{"goal_id": "b", "port": "value_observation"}]
        assert detail["binding_shapes"] == goal_target_shapes()
        assert "route, not completion" in detail["requirement"]
        values = copy.deepcopy(proposed)
        if target_kind == "future":
            values["steps"].append({"key": "read_b", "tool": "evidence.value", "parameters": {
                "artifact_id": artifact.id, "path": [{"kind": "key", "key": "b"}]}})
            values["goal_map"]["b"] = {"step_key": "read_b", "port": "value_observation"}
        else:
            values["goal_map"].update(detail["gap_bindings"])
        return {"action": "initial_plan", "parameters": values}

    request = store.load_request(run).model_dump()
    budget = run.budget.model_dump()
    transport = ScriptedTransport({"action": "initial_plan", "parameters": proposed}, corrected)
    updated, action, _ = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "plan" and len(transport.sent) == 2
    assert store.load_request(updated).model_dump() == request and updated.budget.model_dump() == budget
    assert updated.usage.model_calls == 2 and updated.usage.model_tokens_used == 150
    assert updated.usage.plan_revisions == 0 and not updated.calls and not updated.attempts
    assert all(value != "satisfied" for value in updated.goal_status.values())
    target = store.load_plan(updated).goal_map["b"]
    assert (target.step_id is not None) is (target_kind == "future")
    assert (target.gap is not None) is (target_kind == "gap")


def test_actual_v23_replies_stay_unmodified_and_fail_their_distinct_contracts(tmp_path):
    fixture_bytes = FIXTURE.read_bytes()
    fixture = json.loads(fixture_bytes)
    actual = fixture["runs"][1]
    responses = actual["responses"]
    assert len(responses) == 2
    for reply in responses:
        assert hashlib.sha256(reply["raw_content"].encode()).hexdigest() == reply["raw_content_sha256"]
        assert json.loads(reply["raw_content"]) == reply["proposal"]
    first, second = [copy.deepcopy(reply["proposal"]["parameters"]) for reply in responses]
    # The first has a legal binding shape but a diagnostic as Goal ID. Validate
    # against a synthetic isolated Request with the actual requested identity.
    ProposedPlan.model_validate(first)
    store, original, _, _, _ = source(tmp_path)
    request = Request(goals=[Goal(id="finite_sample_internal_minimum", port="sampling",
                                  minimum_check_version="finite-sampling-1")])
    run = store.create_run(request, None, original.permission, original.budget)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, first)
    assert caught.value.detail["missing_goals"] == [
        {"goal_id": "finite_sample_internal_minimum", "port": "sampling"}]
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, second)
    assert caught.value.detail["code"] == "goal_target_shape"
    assert caught.value.detail["path"] == ["parameters", "goal_map", "finite_sample_internal_minimum"]
    assert all(error["loc"] for error in caught.value.detail["errors"])
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert store.load_plan(run) is None and not run.calls and not run.attempts
    assert FIXTURE.read_bytes() == fixture_bytes


def test_invalid_nested_evidence_is_localized_without_echoing_rejected_values():
    proposed = single()
    proposed["goal_map"]["energy"] = {"port": "energy", "evidence": {
        "run_id": "source", "result_id": "source_result", "producer_step_id": "future"}}
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("initial_plan", proposed)
    assert any(item["loc"] == ["parameters", "goal_map", "energy", "evidence"]
               for item in caught.value.detail["errors"])
    assert "future" not in json.dumps(caught.value.detail.get("errors"))
    with pytest.raises(ValidationError):
        ProposedPlan.model_validate(proposed)


def test_public_plan_structure_changes_only_two_expansion_layers_and_declares_scope():
    original = action_parameter_schema("initial_plan")
    full_before = copy.deepcopy(original)
    projected = plan_structure_schema()
    expected = copy.deepcopy(original)
    expected["properties"]["steps"]["items"] = {"type": "object"}
    expected["$defs"]["EvidenceRef"] = {"type": "object"}
    # This definition becomes unreachable only because steps.items is opaque.
    expected["$defs"].pop("ProposedStep")
    expected["description"] = (
        "Structural projection only: Step fields and EvidenceRef contents are validated separately.")
    assert projected == expected
    assert action_parameter_schema("initial_plan") == original == full_before
    assert original["properties"]["steps"]["minItems"] == projected["properties"]["steps"]["minItems"] == 1
    assert original["properties"]["steps"]["maxItems"] == projected["properties"]["steps"]["maxItems"] == 8
    assert projected["properties"]["goal_map"] == original["properties"]["goal_map"]
    for name in ("FutureGoalTarget", "EvidenceGoalTarget", "GapGoalTarget"):
        assert projected["$defs"][name] == original["$defs"][name]
        assert projected["$defs"][name]["additionalProperties"] is False
        assert projected["$defs"][name]["properties"]["port"]["pattern"] == r"^[A-Za-z][A-Za-z0-9_]{0,63}$"


@pytest.mark.parametrize("nested_error", ["step_field", "evidence_field", "evidence_source"])
def test_structural_projection_does_not_claim_or_replace_nested_runtime_validation(nested_error):
    proposed = single()
    if nested_error == "step_field":
        proposed["steps"][0]["unexpected"] = "not allowed by the full Step type"
    else:
        evidence = {**EVIDENCE, "unexpected": "not permitted"} if nested_error == "evidence_field" else {}
        proposed["goal_map"]["energy"] = {"evidence": evidence, "port": "energy"}
    schema = _schema(plan_structure_schema())
    assert matches(schema, proposed, schema)
    with pytest.raises(ProposalError):
        validate_action_parameters("initial_plan", proposed)


@pytest.mark.parametrize("count", [0, 1, 8, 9])
def test_structural_projection_retains_step_count_and_target_exclusivity(count):
    schema = _schema(plan_structure_schema())
    proposed = single()
    proposed["steps"] *= count
    assert matches(schema, proposed, schema) is (1 <= count <= 8)
    proposed["goal_map"]["energy"]["gap"] = "mixed shape is forbidden even in the structural projection"
    assert not matches(schema, proposed, schema)
