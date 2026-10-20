"""Offline semantic equivalence of the default readable context reductions."""

import copy
import hashlib
import json

import pytest
from test_plan_goal_binding_contract import matches

from orca_agent.context import _compact_planning_schema, _schema
from orca_agent.context_efficiency import (
    context_json,
    expand_observation_references,
    optimize_context_wire,
    reference_observation_sources,
    share_schema_definitions,
)
from orca_agent.proposals import action_parameter_schema
from orca_agent.tools.registry import get_tool


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def native_wire():
    observation = {"name": "frequencies", "value": [-12.5, 0.0, *range(24)],
        "unit": "cm^-1", "validation_status": "unverified", "formula": None,
        "code_sha256": "a" * 64, "actual_conditions": {"method": "B3LYP", "charge": 0},
        "sources": [{"artifact_id": "original_output", "sha256": "b" * 64,
                     "location": "lines 100-160", "attempt_id": "attempt_original"}],
        "limitations": ["未核验科学资格；完整谱保留负值与零模式。"]}
    return {"TASK_CODE_RULES": "Generate task code with the current immutable input manifest.",
        "PLAN_RULES": "Preserve all goals, required evidence and frozen dependencies.",
        "RESPONSE_ENVELOPE": {"action": "revise_plan | call_tool", "parameters": {}, "reason": ""},
        "ACTION_SCHEMAS": {"revise_plan": _schema(action_parameter_schema("revise_plan")),
            "call_tool": {"type": "object", "properties": {"step_id": {"type": "string"}},
                          "required": ["step_id"], "additionalProperties": False}},
        "TOOLS": [],
        "AUTHORITY": {"response_contract": "decision-intent-2", "run_id": "run_original",
            "decision_purpose": {"kind": "planning", "allowed_actions": ["revise_plan", "call_tool"]},
            "request": {"original_text": "保留完整频率及单位，不补算。", "conditions": {"charge": 0}},
            "permission": {"code_execution": True, "version": 1},
            "budget_limits": {"orca_starts": 1}, "cumulative_usage": {"orca_starts_reserved": 1}},
        "CONTROL": {"pending_step_ids": ["frozen_step"],
            "validation_error": {"category": "ProposalError", "requirement": "retain dependencies"}},
        "DATA": {"results": [{"result_id": "result_original", "operation_status": "completed",
            "unqualified_observation_records": [copy.deepcopy(observation)]}],
            "untrusted": {"KEYS": {"$1": "charge"}, "@": 0, "nested": [False, 1, 1.0, -0.0]},
            "delivery": {"references": {"source_original": {
                "run_id": "run_original", "result_id": "result_original"}},
                "facts": [{"ref": "fact_frequency", "kind": "answer", "source_ref": "source_original",
                    "value": {"kind": "evidence_observation", "observation": copy.deepcopy(observation)}}]}}}


def optimize(wire):
    return optimize_context_wire(wire, simplify_schema=_compact_planning_schema)


def test_shared_schema_subtrees_expand_exactly_and_keep_local_refs_and_literals():
    common = {"type": "object", "properties": {f"key_{i}": {"type": "string"}
                                             for i in range(20)},
              "required": ["key_0"], "additionalProperties": False}
    schema = {"type": "object", "properties": {"one": copy.deepcopy(common),
        "two": copy.deepcopy(common), "literal": {"const": copy.deepcopy(common)},
        "local": {"$ref": "#/$defs/item"}}, "$defs": {"item": {"type": "number"}}}
    wire = {"ACTION_SCHEMAS": {"first": schema, "second": copy.deepcopy(schema)},
            "TOOLS": [{"name": "test", "parameters": copy.deepcopy(common)}]}
    before = copy.deepcopy(wire)
    shared = share_schema_definitions(wire)
    assert "SCHEMA_DEFINITIONS" in shared

    def restore(node):
        if isinstance(node, dict):
            if set(node) == {"$ref"} and node["$ref"].startswith("#/SCHEMA_DEFINITIONS/"):
                return copy.deepcopy(shared["SCHEMA_DEFINITIONS"][node["$ref"].rsplit("/", 1)[-1]])
            return {key: value if key in {"const", "enum", "default", "examples"} else restore(value)
                    for key, value in node.items()}
        return [restore(value) for value in node] if isinstance(node, list) else node

    assert restore(shared["ACTION_SCHEMAS"]) == wire["ACTION_SCHEMAS"]
    assert restore(shared["TOOLS"]) == wire["TOOLS"]
    assert wire == before and len(context_json(shared)) < len(context_json(wire))


@pytest.mark.parametrize("columnar", [False, True])
def test_shared_source_metadata_keeps_relative_conditions_and_source_locations_exact(columnar):
    core = {"artifact_id": "source_artifact", "sha256": "a" * 64,
            "run_id": "run_original", "result_id": "result_original",
            "provenance": {"contract": "provenance-1", "detail": "shared source detail " * 20}}
    records = [{"name": f"observation_{i}", "value": i,
                "sources": [{**copy.deepcopy(core), "location": {"line": i + 1},
                             "actual_conditions": {"observation_actual_conditions_ref": "."}}],
                "actual_conditions": {"charge": i}} for i in range(3)]
    row = {"result_id": "result_original", "unqualified_observation_records": records}
    results = {"columns": list(row), "rows": [list(row.values())]} if columnar else [row]
    wire = {"DATA": {"results": results}}
    before = copy.deepcopy(wire)
    shared = reference_observation_sources(wire)
    assert "observation_sources" in shared["DATA"]
    result = shared["DATA"]["results"]
    shown = result["rows"][0][1] if columnar else result[0]["unqualified_observation_records"]
    for original, projected in zip(records, shown, strict=True):
        source, = projected["sources"]
        reference = source["observation_source_ref"]
        assert {**shared["DATA"]["observation_sources"][reference],
                **{key: value for key, value in source.items() if key != "observation_source_ref"}
                } == original["sources"][0]
        assert source["actual_conditions"]["observation_actual_conditions_ref"] == "."
        assert projected["actual_conditions"] == original["actual_conditions"]
    assert wire == before and len(context_json(shared)) < len(context_json(wire))


def test_visible_duplicate_uses_actual_result_identity_and_expands_without_fact_changes():
    wire = native_wire()
    before = copy.deepcopy(wire)
    optimized = optimize(wire)
    observation = optimized["DATA"]["results"][0]["unqualified_observation_records"][0]
    assert observation == {"name": "frequencies", "delivery_fact_ref": "fact_frequency",
                           "field": "observation"}
    restored = expand_observation_references(optimized)
    for section in ("AUTHORITY", "CONTROL", "DATA"):
        assert canonical(restored[section]) == canonical(before[section])
    assert digest(restored["DATA"]) == digest(before["DATA"])
    assert len(context_json(optimized).encode("utf-8")) < len(context_json(wire).encode("utf-8"))
    assert canonical(wire) == canonical(before)


@pytest.mark.parametrize("mismatch", [
    "run", "result", "source_run_missing", "source_result_missing", "result_missing",
    "all_run_identity_missing", "fact_missing", "reference_missing", "delivery_missing",
    "unit", "validation", "source_hash", "value_type", "number_type", "signed_zero",
])
def test_observation_deduplication_requires_exact_visible_value_and_source_identity(mismatch):
    wire = native_wire()
    delivery = wire["DATA"]["delivery"]
    source = delivery["references"]["source_original"]
    result = wire["DATA"]["results"][0]
    duplicate = delivery["facts"][0]["value"]["observation"]
    if mismatch == "run":
        source["run_id"] = "run_other"
    elif mismatch == "result":
        source["result_id"] = "result_other"
    elif mismatch == "source_run_missing":
        source.pop("run_id")
    elif mismatch == "source_result_missing":
        source.pop("result_id")
    elif mismatch == "result_missing":
        result.pop("result_id")
    elif mismatch == "all_run_identity_missing":
        wire["AUTHORITY"].pop("run_id")
        source.pop("run_id")
    elif mismatch == "fact_missing":
        delivery["facts"] = []
    elif mismatch == "reference_missing":
        delivery["references"] = {}
    elif mismatch == "delivery_missing":
        wire["DATA"].pop("delivery")
    elif mismatch == "unit":
        duplicate["unit"] = None
    elif mismatch == "validation":
        duplicate["validation_status"] = "passed"
    elif mismatch == "source_hash":
        duplicate["sources"][0]["sha256"] = "c" * 64
    elif mismatch == "value_type":
        duplicate["actual_conditions"]["charge"] = False
    elif mismatch == "number_type":
        duplicate["actual_conditions"]["charge"] = 0.0
    elif mismatch == "signed_zero":
        duplicate["value"][1] = -0.0
    before = copy.deepcopy(wire)
    optimized = optimize(wire)
    assert canonical(optimized["DATA"]) == canonical(before["DATA"])
    assert "delivery_fact_ref" not in optimized["DATA"]["results"][0]["unqualified_observation_records"][0]
    assert canonical(wire) == canonical(before)


def test_explicit_result_run_overrides_context_run_when_selecting_visible_source():
    wire = native_wire()
    result = wire["DATA"]["results"][0]
    result["run_id"] = "run_external"
    assert "delivery_fact_ref" not in optimize(wire)["DATA"]["results"][0]["unqualified_observation_records"][0]
    wire["DATA"]["delivery"]["references"]["source_original"]["run_id"] = "run_external"
    optimized = optimize(wire)
    assert optimized["DATA"]["results"][0]["unqualified_observation_records"][0]["delivery_fact_ref"] == "fact_frequency"
    assert digest(expand_observation_references(optimized)["DATA"]) == digest(wire["DATA"])


@pytest.mark.parametrize("action", ["initial_plan", "revise_plan"])
def test_default_plan_simplification_keeps_binding_validation_and_literal_protocol(action):
    wire = native_wire()
    original = _schema(action_parameter_schema(action))
    wire["ACTION_SCHEMAS"] = {action: original}
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"] = [action]
    optimized = optimize(wire)
    projected = optimized["ACTION_SCHEMAS"][action]
    assert len(context_json(projected)) < len(context_json(original))
    for target, expected in [
        ({"step_key": "measure", "port": "energy"}, True),
        ({"gap": "Missing original evidence", "port": "energy"}, True),
        ({"step_key": "measure", "gap": "conflict", "port": "energy"}, False),
        ({"step_key": "measure"}, False),
        ({"step_key": "measure", "port": "energy", "extra": True}, False),
    ]:
        proposed = {"steps": [{"key": "measure", "tool": "orca.sp"}], "goal_map": {"energy": target}}
        assert matches(original, proposed, original) is expected
        assert matches(projected, proposed, projected) is expected
    assert not {"KEYS", "SHARED_STRINGS", "SCHEMA_COLUMNS"}.intersection(optimized)
    assert set(optimized["RESPONSE_ENVELOPE"]) == {"action", "parameters", "reason"}
    assert optimized["TASK_CODE_RULES"] == wire["TASK_CODE_RULES"]
    assert optimized["PLAN_RULES"] == wire["PLAN_RULES"]


def test_immediate_tool_schema_is_unchanged_with_patterns_defaults_and_boundaries():
    wire = native_wire()
    schema = copy.deepcopy(get_tool("evidence.text").parameter_schema)
    wire["ACTION_SCHEMAS"]["evidence.text"] = schema
    wire["TOOLS"] = [{"name": "evidence.text", "immediate": True,
                      "parameters_ref": "ACTION_SCHEMAS.evidence.text"}]
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"].append("evidence.text")
    optimized = optimize(wire)
    assert canonical(optimized["ACTION_SCHEMAS"]["evidence.text"]) == canonical(schema)
    assert optimized["TOOLS"] == wire["TOOLS"]


@pytest.mark.parametrize("guard", ["KEYS", "SCHEMA_COLUMNS", "SHARED_STRINGS", "STRING_ENCODING",
                                   "legacy_contract", "no_native_schema"])
def test_historical_encoded_contexts_keep_their_existing_values_and_decoder(guard):
    wire = native_wire()
    if guard in {"KEYS", "SCHEMA_COLUMNS", "SHARED_STRINGS", "STRING_ENCODING"}:
        wire[guard] = {"marker": "existing decoder"}
    elif guard == "legacy_contract":
        wire["AUTHORITY"]["response_contract"] = "decision-intent-1"
    else:
        wire.pop("ACTION_SCHEMAS")
    before = copy.deepcopy(wire)
    assert canonical(optimize(wire)) == canonical(before)
    assert canonical(wire) == canonical(before)


def test_short_control_rules_preserve_every_state_field_and_unknown_actions_keep_full_rules():
    wire = native_wire()
    wire["ACTION_SCHEMAS"] = {"call_tool": wire["ACTION_SCHEMAS"]["call_tool"]}
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"] = ["call_tool"]
    optimized = optimize(wire)
    assert "frozen Step" in optimized["TASK_CODE_RULES"]
    for section in ("AUTHORITY", "CONTROL", "DATA"):
        assert canonical(expand_observation_references(optimized)[section]) == canonical(wire[section])
    wire["ACTION_SCHEMAS"]["new_action"] = {"type": "object"}
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"].append("new_action")
    assert optimize(wire)["TASK_CODE_RULES"] == wire["TASK_CODE_RULES"]


def test_context_order_stabilizes_contract_prefix_without_mutating_canonical_fact_hash():
    first = native_wire()
    second = dict(reversed(list(copy.deepcopy(first).items())))
    second["AUTHORITY"]["cumulative_usage"]["orca_starts_reserved"] = 2
    second["CONTROL"]["validation_error"] = {"category": "new_error"}
    second["DATA"]["untrusted"]["new_observation"] = "下一轮已有事实"
    before = copy.deepcopy((first, second))
    a, b = context_json(first), context_json(second)
    assert a.split(',"AUTHORITY":', 1)[0] == b.split(',"AUTHORITY":', 1)[0]
    assert a.index('"ACTION_SCHEMAS":') < a.index('"AUTHORITY":')
    assert a.index('"AUTHORITY":') < a.index('"CONTROL":') < a.index('"DATA":')
    assert digest(json.loads(a)) == digest(first)
    assert digest(json.loads(b)) == digest(second)
    assert canonical((first, second)) == canonical(before)
    assert context_json(dict(reversed(list(first.items())))) == a


@pytest.mark.parametrize("effects,keep_rules", [
    (["write_artifact"], True), (["execute_python"], True),
    (None, True), (["read_registered_artifact"], False),
])
def test_immediate_authoring_or_unknown_effects_keep_code_rules(effects, keep_rules):
    wire = native_wire()
    wire["ACTION_SCHEMAS"] = {"immediate_tool": {"type": "object"}}
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"] = ["immediate_tool"]
    wire["TOOLS"] = [{"name": "immediate_tool", "immediate": True, "effects": effects}]
    projected = optimize(wire)
    assert (projected["TASK_CODE_RULES"] == wire["TASK_CODE_RULES"]) is keep_rules


def test_default_build_context_preserves_immediate_schema_and_readable_plan_contract(tmp_path, monkeypatch):
    from test_context_ready_execution import input_context

    run, plan, _, _, wire = input_context(tmp_path, monkeypatch)
    assert wire["AUTHORITY"]["response_contract"] == "decision-intent-2"
    assert "revise_plan" in wire["ACTION_SCHEMAS"]
    assert not {"KEYS", "SHARED_STRINGS", "SCHEMA_COLUMNS"}.intersection(wire)
    immediate = [tool for tool in wire["TOOLS"] if tool.get("immediate")]
    assert immediate
    for tool in immediate:
        assert canonical(wire["ACTION_SCHEMAS"][tool["name"]]) == canonical(
            get_tool(tool["name"]).parameter_schema)
    assert wire["AUTHORITY"]["plan"]["goal_map"] == plan.model_dump(
        mode="json", exclude_none=True)["goal_map"]
    assert "TASK_CODE_RULES" in wire
    assert run.usage.model_calls == 0 and not run.model_records


def prepared_control(*, ceiling=32768, truncated=False):
    from orca_agent.context import _prepare_context_request
    from orca_agent.models import ModelLimits

    wire = native_wire()
    wire["ACTION_SCHEMAS"] = {"call_tool": wire["ACTION_SCHEMAS"]["call_tool"]}
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"] = ["call_tool"]
    if truncated:
        wire["CONTROL"]["validation_error"] = {"category": "truncated", "requirement": "truncated"}
    messages = [{"role": "system", "content": "Return one JSON object."},
                {"role": "user", "content": canonical(wire)}]
    before = copy.deepcopy(messages)
    prepared = _prepare_context_request(messages, max_output_tokens=ceiling,
        limits=ModelLimits(input_tokens=100000, output_tokens=32768))
    assert messages == before
    return prepared, wire


@pytest.mark.parametrize("ceiling,truncated,expected", [(32768, False, 2048),
    (1000, False, 1000), (32768, True, 32768), (1000, True, 1000)])
def test_prepared_control_request_uses_smaller_limit_and_truncation_restores_frozen_capacity(
    ceiling, truncated, expected,
):
    prepared, original = prepared_control(ceiling=ceiling, truncated=truncated)
    body = prepared.body()
    assert body["max_tokens"] == prepared.output_token_bound == expected
    projected = json.loads(body["messages"][1]["content"])
    for section in ("AUTHORITY", "CONTROL", "DATA"):
        assert canonical(expand_observation_references(projected)[section]) == canonical(original[section])
    assert "delivery_fact_ref" in body["messages"][0]["content"]


def test_output_projection_does_not_hide_invalid_model_frozen_ceiling():
    with pytest.raises(ValueError, match="output token limit"):
        prepared_control(ceiling=32769)


@pytest.mark.parametrize("snapshot_state", ["complete", "truncated", "missing"])
def test_terminal_wrapper_sizes_all_goals_and_preserves_full_capacity_for_uncertainty(snapshot_state):
    from orca_agent.context import _prepare_context_request, _terminal_example
    from orca_agent.models import ModelLimits

    ceiling = 32768
    wire = native_wire()
    wire["ACTION_SCHEMAS"] = {"stop": {"type": "object"}}
    wire["AUTHORITY"]["decision_purpose"]["allowed_actions"] = ["stop"]
    wire["CONTROL"] = {}
    delivery = {"version": "test-terminal-many-goals", "facts": [], "references": {},
        "goals": [{"ref": f"goal_{index}",
            "required_fact_refs": [f"fact_{index}_{number}_" + "完整来源" * 18 for number in range(3)],
            "explanation_refs": [f"explanation_{index}"],
            "required_blocker_refs": [f"blocker_{index}"],
            "next_action_refs": [f"next_{index}"]} for index in range(8)]}
    wire["DATA"] = {"results": [], "delivery": delivery}
    example = {"action": "stop", "parameters": _terminal_example(delivery),
               "reason": "Audit only; delivery selects program facts."}
    minimum_bytes = len(canonical(example).encode("utf-8"))
    assert len(example["parameters"]["delivery"]["goal_explanations"]) == 8
    assert minimum_bytes + 4096 < ceiling
    if snapshot_state == "truncated":
        wire["CONTROL"]["validation_error"] = {"category": "truncated"}
    elif snapshot_state == "missing":
        del delivery["goals"][-1]["explanation_refs"]
    messages = [{"role": "system", "content": "Return one JSON object."},
                {"role": "user", "content": canonical(wire)}]
    before = copy.deepcopy(messages)
    prepared = _prepare_context_request(messages, max_output_tokens=ceiling,
        limits=ModelLimits(input_tokens=100000, output_tokens=ceiling))
    assert messages == before
    assert prepared.body()["max_tokens"] == prepared.output_token_bound
    if snapshot_state == "complete":
        assert minimum_bytes + 4096 <= prepared.output_token_bound < ceiling
    else:
        assert prepared.output_token_bound == ceiling
    projected = json.loads(prepared.body()["messages"][1]["content"])
    assert canonical(projected["DATA"]["delivery"]) == canonical(delivery)


def test_response_usage_exceeding_smaller_reservation_still_fails_with_known_usage():
    from orca_agent.llm import _parse_response

    prepared, _ = prepared_control()
    actual_output = prepared.output_token_bound + 1
    response = {"id": "synthetic_receipt", "model": prepared.model,
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant",
            "content": '{"action":"call_tool","parameters":{"step_id":"frozen_step"},"reason":"run"}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": actual_output,
                  "total_tokens": 100 + actual_output}}
    reply = _parse_response(prepared, canonical(response).encode("utf-8"), 200, "offline")
    assert reply.error_category == "token_bound_exceeded"
    assert reply.proposal is None
    assert reply.usage.completion_tokens == actual_output


@pytest.mark.parametrize("mismatch", [None, "run", "result", "hidden_reference", "hidden_fact"])
def test_redacted_public_source_requires_matching_trusted_snapshot_and_visible_target(mismatch):
    wire = native_wire()
    sources = copy.deepcopy(wire["DATA"]["delivery"]["references"])
    wire["DATA"]["delivery"]["references"]["source_original"] = {"conditions": {"method": "B3LYP"}}
    if mismatch in {"run", "result"}:
        sources["source_original"][mismatch + "_id"] = "foreign_identity"
    elif mismatch == "hidden_reference":
        wire["DATA"]["delivery"]["references"] = {}
    elif mismatch == "hidden_fact":
        wire["DATA"]["delivery"]["facts"] = []
    before = copy.deepcopy((wire, sources))
    optimized = optimize_context_wire(wire, simplify_schema=_compact_planning_schema,
        source_references=sources)
    observed = optimized["DATA"]["results"][0]["unqualified_observation_records"][0]
    assert ("delivery_fact_ref" in observed) is (mismatch is None)
    assert canonical(expand_observation_references(optimized)["DATA"]) == canonical(wire["DATA"])
    assert canonical((wire, sources)) == canonical(before)


@pytest.mark.parametrize("source_matches", [True, False])
def test_actual_archived_observation_uses_visible_reference_in_default_build_context(source_matches):
    from test_bounded_live_observation_context import feedback

    from orca_agent.context import build_context
    from orca_agent.models import (
        BudgetLimits,
        Goal,
        ModelLimits,
        OutputBinding,
        PermissionSnapshot,
        Plan,
        Request,
        Run,
        RunConfiguration,
        Step,
    )

    result = feedback()
    # This complete value fits the normal preview. The larger ranking is
    # intentionally windowed and must not be treated as an exact duplicate.
    observation = next(row for row in result.observation_records if row.name == "conformer_search")
    result = result.model_copy(update={"observation_records": [observation], "observations": {}})
    goal = Goal(id="goal_ranking", port=observation.name,
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")
    request = Request(goals=[goal], method="B3LYP", basis="6-31G",
        conditions={"explain_results": True, "electronic_state": "RKS", "environment": "gas_phase"})
    step = Step(id=result.step_id, logical_id="analysis", tool="python.execute", parameters={
        "code": {"artifact_id": "script_artifact", "sha256": observation.code_sha256},
        "observation_names": [observation.name]})
    plan = Plan(request_id=request.id, steps=[step],
        goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)})
    run = Run(id=result.run_id, request_id=request.id, request_version=request.version,
        plan_id=plan.id, plan_version=plan.version, result_ids=[result.id],
        permission=PermissionSnapshot(model_execution=True, code_execution=True,
            artifact_writes=True, allowed_tools=["python.execute", "workspace.write"]),
        budget=BudgetLimits(model_calls=4, model_tokens=300000, input_tokens=100000,
            output_tokens=2000, plan_revisions=2, decision_rounds=8),
        configuration=RunConfiguration(model=ModelLimits(input_tokens=100000, output_tokens=2000)))
    run.goal_status = {goal.id: "insufficient_evidence"}
    snapshot = {"version": "test-bounded-feedback", "fingerprint": "a" * 64,
        "basis": {"request_id": request.id, "request_version": request.version,
            "plan_id": plan.id, "plan_version": plan.version, "permission_version": run.permission.version,
            "control_generation": 0}, "request_text": request.original_text, "communication": {},
        "resources": {"permission": {}, "limits": {}, "remaining": {}, "usage": {}},
        "references": {"s0": {"run_id": run.id if source_matches else "different_run", "result_id": result.id}},
        "goals": [{"goal_id": goal.id, "ref": "g0", "port": goal.port,
            "required_fact_refs": ["f0"], "explanation_refs": ["e0"],
            "required_blocker_refs": [], "next_action_refs": ["n0"]}],
        "facts": [{"ref": "f0", "goal_ref": "g0", "source_ref": "s0", "kind": "answer",
            "value": {"kind": "evidence_observation", "observation": observation.model_dump(mode="json")}}],
        "explanations": [], "blockers": [], "next_actions": []}
    before = copy.deepcopy((request, run, plan, result, snapshot))
    prepared = build_context(request, run, plan, results=[result], delivery_snapshot=snapshot,
        feedback={"new_result_ids": [result.id], "pending_step_ids": []})
    wire = json.loads(prepared.body()["messages"][1]["content"])
    assert not {"KEYS", "SHARED_STRINGS", "SCHEMA_COLUMNS"}.intersection(wire)
    visible = wire["DATA"]["results"][0]["unqualified_observation_records"][0]
    assert ("delivery_fact_ref" in visible) is source_matches
    assert wire["DATA"]["delivery"]["references"]["s0"] == {}
    assert canonical(expand_observation_references(wire)["DATA"]["results"][0]["unqualified_observation_records"][0]) == canonical(
        observation.model_dump(mode="json"))
    assert (request, run, plan, result, snapshot) == before
    assert run.usage.model_calls == run.usage.orca_starts_actual == 0
