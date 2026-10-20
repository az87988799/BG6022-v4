"""P3 offline purpose, capacity and planning reservations; no live transport."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from orca_agent import context, model_usage
from orca_agent.decision_purpose import (
    prepared_decision_purpose,
    relevant_result_ids,
    terminal_actions,
    terminal_results_only,
)
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.llm import prepare_request
from orca_agent.model_usage import current_basis, send_model, validate_delivery_margin
from orca_agent.models import Plan, Request, Result, Run
from orca_agent.proposals import validate_action_parameters
from orca_agent.store import BudgetExceeded, StoreError
from tests.unit.test_context import objects, payload
from tests.unit.test_model_usage import ScriptedTransport

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/v06-delivery-capacity"


@pytest.fixture
def replay():
    raw = (FIXTURE / "input.json").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_bytes())
    assert hashlib.sha256(raw).hexdigest() == provenance["input_file"]["sha256"]
    assert len(raw) == provenance["input_file"]["bytes"]
    values = json.loads(raw)["inputs"]
    for name, model in (("request", Request), ("run", Run), ("plan", Plan)):
        values[name] = model.model_validate(values[name])
    values["results"] = [Result.model_validate(value) for value in values["results"]]
    values["now"] = datetime.fromisoformat(values["now"])
    return values


def refresh(snapshot):
    snapshot["fingerprint"] = context._hash({key: value for key, value in snapshot.items() if key != "fingerprint"})


def expand_schema(node):
    """Independent inverse for the documented column serialization."""
    if isinstance(node, list):
        tag, *values = node
        if tag == "p":
            return {"properties": {key: expand_schema(value) for key, value in values[0].items()}}
        if tag in ("c", "e", "r"):
            return {dict(c="const", e="enum", r="$ref")[tag]: values[0]}
        kind, columns = {
            "o": ("object", ["properties", "required", "additionalProperties", "minProperties", "maxProperties"]),
            "O": (None, ["properties", "required", "additionalProperties", "minProperties", "maxProperties"]),
            "a": ("array", ["items", "minItems", "maxItems"]),
            "s": ("string", ["minLength", "maxLength"]),
        }[tag]
        node = {**({"type": kind} if kind is not None else {}),
                **{key: value for key, value in zip(columns, values) if value is not None}}
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key in ("properties", "$defs", "patternProperties"):
            value = {name: expand_schema(child) for name, child in value.items()}
        elif key in ("items", "if", "then", "additionalProperties", "propertyNames"):
            value = expand_schema(value)
        elif key in ("oneOf", "allOf", "anyOf"):
            value = [expand_schema(child) for child in value]
        result[key] = value
    return result


def validation_schema(schema):
    """Independent canonical form for the intake schema's limited rewrites.

    Resolve local references and annotations, then express finite unions and
    nullable strings uniformly. Every other validation keyword stays literal.
    This deliberately rejects unsupported reference scopes rather than guessing.
    """
    def encoded(value):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def visit(node, references=()):
        if not isinstance(node, dict):
            return copy.deepcopy(node)
        assert "$id" not in node and "$dynamicRef" not in node
        value = {key: copy.deepcopy(child) for key, child in node.items()
                 if key not in {"title", "description", "default", "examples", "$defs"}}
        if "$ref" in value:
            reference = value["$ref"]
            assert set(value) == {"$ref"}
            assert reference.startswith("#/$defs/") and reference not in references
            target = schema
            for part in reference[2:].split("/"):
                target = target[part.replace("~1", "/").replace("~0", "~")]
            return visit(target, (*references, reference))
        for key, child in value.items():
            if key in {"properties", "patternProperties", "dependentSchemas"}:
                value[key] = {name: visit(rule, references) for name, rule in child.items()}
            elif key in {"items", "additionalProperties", "propertyNames", "if", "then", "else", "not"}:
                value[key] = visit(child, references)
            elif key in {"anyOf", "oneOf", "allOf", "prefixItems"}:
                value[key] = [visit(rule, references) for rule in child]
        if "const" in value and "enum" not in value:
            value["enum"] = [value.pop("const")]
        # An enum of strings/null already restricts the value to these types;
        # do not drop contradictory types or apply Python's bool/int equality.
        kinds = value.get("type")
        kinds = [kinds] if isinstance(kinds, str) else kinds
        literals = value.get("enum")
        if literals and kinds and all(
                (type(item) is str and "string" in kinds) or (item is None and "null" in kinds)
                for item in literals):
            value.pop("type")
        alternatives = value.get("anyOf")
        if set(value) == {"anyOf"} and alternatives:
            if all(set(branch) == {"enum"} or branch == {"type": ["null"]}
                   for branch in alternatives):
                value = {"enum": [item for branch in alternatives
                                  for item in branch.get("enum", [None])]}
            elif len(alternatives) == 2:
                nulls = [branch for branch in alternatives if branch == {"type": ["null"]}]
                others = [branch for branch in alternatives if branch != {"type": ["null"]}]
                if (len(nulls) == len(others) == 1 and others[0].get("type") == ["string"]
                        and set(others[0]) <= {"type", "pattern", "minLength", "maxLength"}):
                    value = {**others[0], "type": ["null", "string"]}
        if "type" in value:
            kinds = value["type"]
            value["type"] = sorted(set([kinds] if isinstance(kinds, str) else kinds))
        if "enum" in value:
            value["enum"] = [json.loads(item) for item in sorted({encoded(item) for item in value["enum"]})]
        if "required" in value:
            value["required"] = sorted(value["required"])
        for key in ("anyOf", "oneOf", "allOf"):
            if key in value:
                value[key] = sorted(value[key], key=encoded)
        return value

    return visit(schema)


def assert_native_schemas_equivalent(schemas, original_schemas):
    """Check source and displayed validation languages with independent values."""
    from tests.unit.test_plan_goal_binding_contract import EVIDENCE, matches

    assert set(schemas) == set(original_schemas)
    def without_defaults(node):
        # A default annotation does not validate values. Leave literal
        # const/enum values and property names untouched.
        if not isinstance(node, dict):
            return node
        result = {key: value for key, value in node.items() if key != "default"}
        for key, value in result.items():
            if key in {"properties", "$defs", "patternProperties"}:
                result[key] = {name: without_defaults(child) for name, child in value.items()}
            elif key in {"items", "if", "then", "additionalProperties", "propertyNames"}:
                result[key] = without_defaults(value)
            elif key in {"anyOf", "oneOf", "allOf"}:
                result[key] = [without_defaults(child) for child in value]
        return result
    delivery = {"version": "terminal-delivery-1", "snapshot_ref": "current", "goal_explanations": [{
        "goal_ref": "g1", "fact_refs": ["f1"], "explanation_ref": "e1", "blocker_refs": [],
        "next_action_ref": "n1"}]}
    examples = {
        "call_tool": [{"step_id": "pending"}, {"tool": "evidence.value", "parameters": {}}],
        "clarify": [{"questions": ["Which basis?"], "unresolved": ["basis"]}],
        "stop": [{"delivery": delivery}, {"delivery": delivery, "reason": "Evidence remains bounded."}],
        "initial_plan": [{"steps": [{"key": "s", "tool": "orca.sp"}],
                          "goal_map": {"energy": {"step_key": "s", "port": "energy"}}}],
        "revise_plan": [{"steps": [{"key": "s", "tool": "orca.sp"}],
                         "goal_map": {"energy": {"gap": "No evidence.", "port": "energy"}}}],
    }
    for action, actual_schema in schemas.items():
        if action == "normalize_request":
            assert validation_schema(actual_schema) == validation_schema(original_schemas[action])
            continue
        if action not in examples:
            # Immediate Tool schemas now remain literal, including annotations
            # and limits outside this small independent Plan-schema matcher.
            assert actual_schema == original_schemas[action]
            continue
        original_schema = without_defaults(original_schemas[action])
        valid = examples[action]
        if action == "call_tool":
            fields = original_schema["properties"]
            valid = [{"step_id": fields["step_id"].get("enum", ["pending"])[0]}]
            if "tool" in fields:
                valid.append({"tool": fields["tool"].get("enum", ["evidence.value"])[0], "parameters": {}})
        invalid = [{}, None, [], 0, ""]
        for example in valid:
            assert matches(original_schema, example, original_schema)
            invalid += [{**example, "extra": True}]
            invalid += [{key: value for key, value in example.items() if key != field} for field in example]
            invalid += [{**example, field: None} for field in example]
        if action in {"initial_plan", "revise_plan"}:
            for target in ({"port": "energy"}, {"port": "energy", "evidence": EVIDENCE, "gap": "both"},
                           {"port": None, "gap": "missing"}):
                invalid.append({**valid[0], "goal_map": {"energy": target}})
            invalid.append({**valid[0], "steps": []})
        if action == "call_tool":
            invalid += [{"step_id": "pending", "tool": "evidence.value", "parameters": {}},
                        {"step_id": "pending", "parameters": {}}, {"tool": "evidence.value"}]
        if action == "stop":
            invalid += [{"delivery": {**delivery, "version": "invented"}},
                        {"delivery": {**delivery, "goal_explanations": []}},
                        {"delivery": {**delivery, "goal_explanations": [{"goal_ref": "g1"}]}}]
        if action == "clarify":
            invalid += [{"questions": [], "unresolved": ["basis"]},
                        {"questions": ["Which basis?"] * 6, "unresolved": ["basis"]}]
        for value in [*valid, *invalid]:
            actual = without_defaults(actual_schema)
            assert matches(actual, value, actual) == matches(original_schema, value, original_schema)


def test_intake_schema_equivalence_expands_refs_and_only_redundant_constraints():
    original = {"type": "object", "properties": {
        "literal": {"$ref": "#/$defs/nullable"},
        "text": {"anyOf": [{"type": "string", "pattern": "^a", "minLength": 1, "maxLength": 4},
                          {"type": "null"}], "default": None},
        "enum": {"const": {"$ref": "#/$defs/raw", "default": ["literal"]}}},
        "required": ["text", "literal"], "additionalProperties": False,
        "$defs": {"nullable": {"anyOf": [{"type": "string", "enum": ["a", "b"]},
                                       {"type": "null"}], "default": None}}}
    equivalent = copy.deepcopy(original)
    equivalent.pop("$defs")
    equivalent["properties"]["literal"] = {"enum": ["b", None, "a"]}
    equivalent["properties"]["text"] = {"type": ["null", "string"],
                                         "pattern": "^a", "minLength": 1, "maxLength": 4}
    equivalent["required"].reverse()
    before = copy.deepcopy((original, equivalent))
    assert_native_schemas_equivalent({"normalize_request": equivalent}, {"normalize_request": original})
    assert (original, equivalent) == before
    assert validation_schema(original)["properties"]["enum"] == {
        "enum": [{"$ref": "#/$defs/raw", "default": ["literal"]}]}


@pytest.mark.parametrize("change", [
    "field", "required", "additional", "minimum", "maximum", "pattern", "enum", "type",
    "source_refs_min", "source_refs_max", "property_names", "literal",
])
def test_intake_schema_equivalence_rejects_changed_validation_rules(change):
    original = {"type": "object", "properties": {
        "quantity": {"type": "string", "minLength": 1, "maxLength": 20, "pattern": "^energy"},
        "source_refs": {"type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"enum": ["s0", "s1"]}},
        "literal": {"const": {"default": 1}}},
        "required": ["quantity"], "additionalProperties": False,
        "propertyNames": {"pattern": "^[a-z_]+$"}}
    altered = copy.deepcopy(original)
    if change == "field":
        altered["properties"].pop("source_refs")
    elif change == "required":
        altered["required"] = []
    elif change == "additional":
        altered["additionalProperties"] = True
    elif change in {"minimum", "maximum", "pattern"}:
        altered["properties"]["quantity"].pop({"minimum": "minLength", "maximum": "maxLength",
                                              "pattern": "pattern"}[change])
    elif change == "enum":
        altered["properties"]["source_refs"]["items"]["enum"].append("unauthorized")
    elif change == "type":
        altered["properties"]["source_refs"]["items"]["type"] = "integer"
    elif change.startswith("source_refs"):
        altered["properties"]["source_refs"].pop("minItems" if change.endswith("min") else "maxItems")
    elif change == "property_names":
        altered.pop("propertyNames")
    else:
        altered["properties"]["literal"]["const"]["default"] = 2
    with pytest.raises(AssertionError):
        assert_native_schemas_equivalent({"normalize_request": altered}, {"normalize_request": original})


def decoded(prepared):
    value = payload(prepared)
    if "SCHEMA_COLUMNS" in value and "PROPOSAL_SCHEMA" in value:
        value["PROPOSAL_SCHEMA"] = expand_schema(value["PROPOSAL_SCHEMA"])
    return value


@pytest.mark.parametrize("seconds", [None, 1790.001, 1000.001, 99.999, 10.001, 0.001])
def test_actual_v06_all_required_facts_fit_eight_thousand_without_relabeling_failure(replay, seconds):
    before = copy.deepcopy(replay)
    if seconds is not None:
        replay["now"] = replay["run"].deadline - timedelta(seconds=seconds)
    prepared = context.build_context(**replay)
    wire = decoded(prepared)
    assert prepared.input_token_bound <= 8000
    assert prepared.output_token_bound == 2000
    assert prepared_decision_purpose(prepared).kind == "terminal"
    assert wire["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"] == ["stop"]
    assert context._hash(wire["PROPOSAL_SCHEMA"]) == wire["SCHEMA_SHA256"]
    source = replay["delivery_snapshot"]
    facts = {row["ref"]: row for row in wire["DATA"]["delivery"]["facts"]}
    for fact in source["facts"]:
        assert facts[fact["ref"]]["value"] == fact["value"]
        assert facts[fact["ref"]]["kind"] == fact["kind"]
    members = next(fact["value"] for fact in facts.values() if fact["kind"] == "members")
    assert len(members) == 5 and sum(not member["required"] for member in members) == 2
    assert all(row["value"]["complete"] is False for row in facts.values() if row["kind"] == "goal_status")
    assert source["fingerprint"] == wire["AUTHORITY"]["delivery_snapshot_fingerprint"]
    assert replay["run"] == before["run"] and replay["run"].state == "failed"
    assert replay["request"] == before["request"] and replay["delivery_snapshot"] == before["delivery_snapshot"]


def test_terminal_correction_long_ids_chinese_provenance_and_two_source_messages_fit_declared_boundary(replay):
    # Synthetic derivative of the real input; this is not a new model result.
    request = replay["request"]
    request.original_text += "保持所有来源和条件。" * 35
    request.messages = [{"id": "message_one", "text": "方法为 RHF，电荷为零。" * 8},
                        {"id": "message_two", "text": "只解释已有结果，不追加计算。" * 8}]
    snapshot = replay["delivery_snapshot"]
    snapshot["request_text"] = request.original_text
    snapshot["goals"][0]["text_evidence"] = {"first": {"message_id": "message_one"},
                                               "second": {"message_id": "message_two"}}
    replay["feedback"]["validation_error"] = {"requirement": "All required blockers must be cited.",
                                                 "missing_blocker_refs": ["b4", "b5"]}
    refresh(snapshot)
    prepared = context.build_context(**replay)
    wire = decoded(prepared)
    assert prepared.input_token_bound <= 10000
    assert wire["DATA"]["delivery"]["request_text"] == request.original_text
    assert [message["text"] for message in wire["DATA"]["source_messages"]] == [m["text"] for m in request.messages]
    assert wire["CONTROL"]["validation_error"] == replay["feedback"]["validation_error"]


def test_unrelated_processed_history_does_not_grow_terminal_context(replay):
    before = context.build_context(**replay)
    result = replay["results"][0]
    unrelated = [result.model_copy(update={"id": "unrelated_" + str(i), "step_id": "old_" + str(i)}) for i in range(200)]
    replay["run"].result_ids.extend(item.id for item in unrelated)
    replay["run"].processed_feedback.extend(item.id for item in unrelated)
    replay["results"].extend(unrelated)
    after = context.build_context(**replay)
    assert after.canonical_body == before.canonical_body
    assert relevant_result_ids(replay["run"], replay["plan"], snapshot=replay["delivery_snapshot"],
                               feedback_ids=replay["feedback"]["new_result_ids"]) == [result.id]


def test_complete_explanation_selects_only_current_sources_after_large_read_authoring_history():
    from types import SimpleNamespace as NS

    request, _ = objects()
    request.conditions["explain_results"] = True
    reads = [NS(tool="evidence.text", step_id=f"read_{i}", result_id=f"read_result_{i}", state="completed")
             for i in range(34)]
    writer = NS(tool="workspace.write", step_id="writer", result_id="writer_result", state="completed")
    current = [NS(tool="python.execute", step_id=name, result_id=name + "_result", state="completed")
               for name in ("main", "producer", "other")]
    failed_step = NS(id="failed", logical_id="failed_logical", tool="python.execute")
    failed = NS(step_id="failed", logical_id="failed_logical", tool="python.execute",
        execution_kind="python", state="failed", finished_at=datetime.now(),
        result_id="failed_result", frozen_step=failed_step)
    ids = [row.result_id for row in [*reads, writer]] + ["failed_result", "irrelevant_feedback"] + [
        row.result_id for row in current]
    run = NS(id="run", permission=NS(code_execution=True, scientific_execution=False,
                                    allowed_tools=["python.execute"]),
        calls=[*reads, writer, *current], attempts=[failed], result_ids=ids,
        goal_evidence={request.goals[0].id: NS(run_id="run", result_id="main_result")})
    producer_ref = NS(run_id=None, result_id=None, producer_step_id="producer")
    plan = NS(goal_map={request.goals[0].id: NS(evidence=None, step_id="main")}, steps=[
        NS(id="main", depends_on=[], inputs={"source": producer_ref}),
        NS(id="producer", depends_on=[], inputs={}),
        NS(id="unrelated_pending", depends_on=["writer"], inputs={})])
    snapshot = {"goals": [{"goal_id": request.goals[0].id, "goal_complete": True}],
        "references": {"s0": {"run_id": "run", "result_id": "other_result"}}}
    before = copy.deepcopy((run, plan))
    assert terminal_results_only(request, snapshot)
    assert not terminal_results_only(request, snapshot, pending_messages=True)
    selected = relevant_result_ids(run, plan, snapshot=snapshot,
        feedback_ids=["irrelevant_feedback"], terminal_only=True)
    assert selected == ["main_result", "producer_result", "other_result"]
    original = relevant_result_ids(run, plan, snapshot=snapshot, feedback_ids=["irrelevant_feedback"])
    assert len(original) > 32
    assert {"read_result_0", "writer_result", "failed_result", "irrelevant_feedback"} <= set(original)
    snapshot["goals"][0]["goal_complete"] = False
    assert not terminal_results_only(request, snapshot)
    assert (run, plan) == before


def test_capacity_overflow_refuses_instead_of_summarizing_required_original(replay):
    replay["request"].original_text += "必要条件。" * 800
    replay["delivery_snapshot"]["request_text"] = replay["request"].original_text
    refresh(replay["delivery_snapshot"])
    with pytest.raises((context.ContextLimitError, ValueError), match="(bound|envelope)"):
        context.build_context(**replay)


def test_snapshot_science_fingerprint_is_unchanged_by_model_settlement_and_time(replay):
    snapshot = copy.deepcopy(replay["delivery_snapshot"])
    before = context.build_context(**replay)
    replay["run"].usage.model_calls += 1
    replay["run"].usage.model_tokens_used += 75
    replay["run"].usage.decision_rounds += 1
    replay["now"] += timedelta(seconds=10)
    after = context.build_context(**replay)
    assert after.request_hash != before.request_hash
    assert replay["delivery_snapshot"] == snapshot
    assert decoded(after)["AUTHORITY"]["delivery_snapshot_fingerprint"] == snapshot["fingerprint"]


def terminal_run(tmp_path, *, remaining_seconds=1800):
    from orca_agent.store import Store
    request, run = objects(scientific=False)
    request.geometry_artifact_id = None
    request.conditions["explain_results"] = True
    permission = run.permission.model_copy(update={"allowed_tools": [], "artifact_ids": []})
    store = Store(tmp_path / "data")
    run = store.create_run(request, None, permission, run.budget.model_copy(update={"run_seconds": remaining_seconds}))
    run.usage.model_calls = 7
    run.usage.decision_rounds = 11
    store.save_run(run)
    snapshot = collect_delivery_snapshot(store, run, request)
    return store, run, request, snapshot


def test_unmet_goal_uses_last_slot_and_saves_exact_snapshot_without_future_double_count(tmp_path):
    store, run, request, snapshot = terminal_run(tmp_path)
    before_budget = run.budget.model_copy(deep=True)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    purpose = prepared_decision_purpose(prepared)
    margin = validate_delivery_margin(request, run, purpose=purpose, prepared=prepared)
    assert margin["future_answer_calls"] == margin["future_answer_tokens"] == 0
    assert run.usage.model_calls == 7 and run.usage.model_tokens_used == run.usage.model_tokens_unknown == 0
    transport = ScriptedTransport()
    send_model(store, run, prepared, transport, basis=current_basis(store, run), logical_id="last_slot",
               delivery_snapshot=snapshot)
    assert transport.sends == 1 and run.usage.model_calls == 8
    assert run.usage.model_tokens_used == 15 and run.usage.model_tokens_unknown == 0
    record = run.model_records[-1]
    raw = store.path(f"runs/{run.id}/model/{record['id']}.delivery.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == record["delivery_snapshot_sha256"]
    assert json.loads(raw) == snapshot and run.budget == before_budget


def test_snapshot_unknown_token_occupancy_rejects_before_reservation_or_transmission(tmp_path):
    store, run, request, _ = terminal_run(tmp_path)
    run.usage.model_tokens_unknown = run.budget.model_tokens - 1
    store.save_run(run)
    snapshot = collect_delivery_snapshot(store, run, request)
    before = run.model_copy(deep=True)
    durable_before = store.path(f"runs/{run.id}/run.json").read_bytes()
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    assert prepared.input_token_bound <= run.budget.input_tokens and prepared.reserved_tokens > 1
    assert run == before
    transport = ScriptedTransport()
    with pytest.raises(BudgetExceeded, match="token"):
        send_model(store, run, prepared, transport, basis=current_basis(store, run), logical_id="unknown_occupied",
                   delivery_snapshot=snapshot)
    assert transport.sends == 0 and not run.model_records
    assert run == before and store.load_run(run.id) == before
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == durable_before
    assert not list(store.path(f"runs/{run.id}/model").glob("*.request.json"))


@pytest.mark.parametrize("elapsed,allowed", [(0.01, True), (2, False)])
def test_short_terminal_deadline_has_bounded_submission_margin(tmp_path, monkeypatch, elapsed, allowed):
    store, run, request, snapshot = terminal_run(tmp_path, remaining_seconds=10)
    now = run.deadline - timedelta(seconds=10)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot, now=now)
    assert prepared.timeout_seconds == 9
    monkeypatch.setattr(model_usage, "utc_now", lambda: now + timedelta(seconds=elapsed))
    transport = ScriptedTransport()
    if allowed:
        send_model(store, run, prepared, transport, basis=current_basis(store, run), logical_id="short",
                   delivery_snapshot=snapshot)
        assert transport.sends == 1
    else:
        with pytest.raises(BudgetExceeded, match="time budget"):
            send_model(store, run, prepared, transport, basis=current_basis(store, run), logical_id="late",
                       delivery_snapshot=snapshot)
        assert transport.sends == 0 and run.usage.model_calls == 7


@pytest.mark.parametrize("deficit", ["calls", "tokens", "unknown_tokens", "rounds", "seconds"])
def test_nonterminal_preflight_preserves_actual_usage_and_all_four_margins(deficit):
    request, run = objects()
    request.conditions["explain_results"] = True
    before = run.usage.model_copy(deep=True)
    validate_delivery_margin(request, run)
    assert run.usage == before
    if deficit == "calls":
        run.usage.model_calls = run.budget.model_calls
    elif deficit == "tokens":
        run.usage.model_tokens_used = run.budget.model_tokens - 11999
    elif deficit == "unknown_tokens":
        run.usage.model_tokens_unknown = run.budget.model_tokens - 11999
    elif deficit == "rounds":
        run.usage.decision_rounds = run.budget.decision_rounds
    else:
        run.deadline = model_usage.utc_now() + timedelta(seconds=59)
    before = run.usage.model_copy(deep=True)
    with pytest.raises(BudgetExceeded, match="final explanation"):
        validate_delivery_margin(request, run)
    assert run.usage == before


def test_new_marker_and_missing_snapshot_cannot_take_legacy_send_path(tmp_path):
    store, run, request, snapshot = terminal_run(tmp_path)
    transport = ScriptedTransport()
    bare = prepare_request([{"role": "user", "content": "JSON"}], prompt_version=context.PROMPT_VERSION)
    with pytest.raises(StoreError, match="explicit decision purpose"):
        send_model(store, run, bare, transport, basis=current_basis(store, run), logical_id="bare")
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    with pytest.raises(StoreError, match="exact prepared delivery snapshot"):
        send_model(store, run, prepared, transport, basis=current_basis(store, run), logical_id="missing")
    assert transport.sends == 0 and not run.model_records
    historical_projection = context.build_context(request, run, action_parameters={"stop": {}})
    with pytest.raises(StoreError, match="(contract|snapshot|purpose)"):
        send_model(store, run, historical_projection, transport, basis=current_basis(store, run), logical_id="legacy_projection")
    assert transport.sends == 0 and not run.model_records


def test_extra_and_missing_contract_fields_remain_invalid_after_schema_roundtrip(replay):
    wire = decoded(context.build_context(**replay))
    assert context._hash(wire["PROPOSAL_SCHEMA"]) == wire["SCHEMA_SHA256"]
    valid = context._terminal_example(replay["delivery_snapshot"])
    validate_action_parameters("stop", valid, terminal_required=True)
    for invalid in ({}, {**valid, "extra": True}, {"delivery": {**valid["delivery"], "extra": True}}):
        with pytest.raises(ValueError):
            validate_action_parameters("stop", invalid, terminal_required=True)
    invalid = copy.deepcopy(valid)
    invalid["delivery"]["goal_explanations"][0].pop("fact_refs")
    with pytest.raises(ValueError):
        validate_action_parameters("stop", invalid, terminal_required=True)


def test_real_unknown_is_clarification_but_scientific_failure_is_not(tmp_path):
    store, run, request, snapshot = terminal_run(tmp_path)
    assert prepared_decision_purpose(context.build_context(request, run, delivery_snapshot=snapshot)).allowed_actions == ("stop",)
    request.charge = None
    snapshot = collect_delivery_snapshot(store, run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    assert prepared_decision_purpose(prepared).allowed_actions == ("clarify", "stop")


@pytest.mark.parametrize("gap", ["missing:environment", "unknown:electronic_state", "field:temperature_K",
    "unconfirmed:standard_state", "missing:environment:water", "field:multiplicity"])
def test_explicit_unknowns_follow_intake_condition_field_contract(gap):
    request, run = objects(scientific=False)
    request.goals[0].unresolved = [gap]
    assert terminal_actions(request, run) == ("clarify", "stop")


@pytest.mark.parametrize("field", ["environment", "electronic_state"])
def test_explicit_null_effective_physical_condition_requires_clarification(field):
    request, run = objects(scientific=False)
    request.conditions[field] = None
    assert terminal_actions(request, run) == ("clarify", "stop")


def test_absent_optional_thermodynamic_fields_do_not_invent_a_question():
    request, run = objects(scientific=False)
    assert "temperature_K" not in request.conditions and "standard_state" not in request.conditions
    assert terminal_actions(request, run) == ("stop",)


def test_passed_check_counts_retain_rules_and_every_nonpassing_detail(replay):
    snapshot = copy.deepcopy(replay["delivery_snapshot"])
    ref = next(key for key, value in snapshot["references"].items() if value.get("checks"))
    snapshot["references"][ref]["checks"][0].update(status="failed", detail="synthetic_hash_mismatch")
    projected = context._public_delivery(snapshot)["references"][ref]["checks_by_status_at_rule"]
    assert sum(value for value in projected.values() if isinstance(value, int)) == 8
    failed = next(value for key, value in projected.items() if ":details" in key)
    assert failed[0] == {key: snapshot["references"][ref]["checks"][0][key]
                         for key in ("name", "status", "detail", "rule_version")}


def test_planning_schema_transport_and_raw_array_sidecar_report_roundtrip(tmp_path, monkeypatch):
    from orca_agent.config import load_config
    from orca_agent.report import build_report
    from orca_agent.store import Store
    from tests.unit.test_phase_b_model_cases import (
        CASES,
        test_v07_all_requested_reads_precede_completion_within_frozen_model_budget,
    )

    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    initialize = CASES.initialize_agent
    current_profile = load_config(Path("config.code.example.toml"))
    def initialize_current_capacity(store, config, request, permission, budget, **kwargs):
        config = config.model_copy(update={"model": config.model.model_copy(update={
            "input_tokens": current_profile.model.input_tokens})})
        budget = budget.model_copy(update={"input_tokens": current_profile.model.input_tokens,
            "model_tokens": current_profile.text.budget.model_tokens})
        return initialize(store, config, request, permission, budget, **kwargs)
    monkeypatch.setattr(CASES, "initialize_agent", initialize_current_capacity)
    prepared_requests = []
    prepare = context.prepare_request
    def capture(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_requests.append(prepared)
        return prepared
    monkeypatch.setattr(context, "prepare_request", capture)
    # This schema/sidecar regression freezes the current capacity before Run
    # creation. Its original 4 HTTP/3 read and full completion assertions stay;
    # historical fixture bytes and the separate small-envelope guards stay.
    test_v07_all_requested_reads_precede_completion_within_frozen_model_budget(store, "array-location")
    run_path, = store.path("runs").glob("*/run.json")
    run = store.load_run(run_path.parent.name)
    by_hash = {request.request_hash: request for request in prepared_requests}
    requests = [by_hash[record["request_hash"]] for record in run.model_records]
    assert len(requests) == 4 and all(request.input_token_bound <= current_profile.model.input_tokens
                                    for request in requests)
    assert any("KEYS" in json.loads(request.body()["messages"][1]["content"]) for request in requests)
    for request, record in zip(requests, run.model_records, strict=True):
        native = json.loads(request.body()["messages"][1]["content"])["AUTHORITY"]
        assert native["basis"] == record["basis"]
        assert native["contract_required"] is True
        assert native["decision_purpose"] == prepared_decision_purpose(request).as_dict()
        wire = decoded(request)
        if "SCHEMA_SHA256" in wire:
            assert context._hash(wire["PROPOSAL_SCHEMA"]) == wire["SCHEMA_SHA256"]
        raw = store.path(f"runs/{run.id}/model/{record['id']}.delivery.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == record["delivery_snapshot_sha256"]
        full = json.loads(raw)
        assert native["delivery_snapshot_fingerprint"] == full["fingerprint"]
        for fact in wire["DATA"]["delivery"]["facts"]:
            if fact["kind"] != "answer":
                continue
            observation = fact["value"].get("observation", {})
            descriptor = observation.get("value")
            if not isinstance(descriptor, dict) or descriptor.get("displayed") is not False:
                continue
            original_fact = next(item for item in full["facts"] if item["ref"] == fact["ref"])
            original = original_fact["value"]["observation"]
            assert descriptor["snapshot_path"] == "." and descriptor["type"] == "array"
            assert descriptor["sha256"] == context._hash(original["value"])
            assert descriptor["bytes"] == len(context._json(original["value"]).encode("utf-8"))
            assert descriptor["length"] == len(original["value"])
            for key in ("path", "coverage", "conditions", "units", "scientific_status"):
                assert observation[key] == original[key]
            for key in observation["snapshot_fields"]:
                assert key in original  # Same immutable fact/path, no guessed binding.
    full = build_report(store, run)["delivery"]
    array = next(fact["value"]["observation"]["value"] for fact in full["facts"]
                 if fact["kind"] == "answer" and isinstance(fact["value"].get("observation", {}).get("value"), list))
    result = next(store.load_result(run.id, identifier) for identifier in run.result_ids
                  if isinstance(store.load_result(run.id, identifier).observations.get("value_observation", {}).get("value"), list))
    assert context._json(array).encode("utf-8") == context._json(result.observations["value_observation"]["value"]).encode("utf-8")
    assert run.usage.orca_starts_actual == 0


def test_key_encoding_preserves_native_authority_and_literal_marker_shaped_data():
    authority = {"basis": {"request_version": 1}, "related_results": [],
                 "decision_purpose": {"kind": "terminal", "allowed_actions": ["stop"]},
                 "contract_required": True, "delivery_snapshot_fingerprint": "a" * 64}
    raw_data = [{"repeated_long_key_name": {"$0": "untrusted", "@": 0}, "sequence": index} for index in range(20)]
    original = {"AUTHORITY": authority, "DATA": {"raw": raw_data}}
    encoded = context._terminal_key_encoding(context._share_strings(original, share_lists=True))
    prepared = prepare_request([{"role": "system", "content": "Synthetic JSON codec test"},
                                {"role": "user", "content": json.dumps(encoded)}])
    assert payload(prepared)["DATA"] == original["DATA"]
    assert encoded["AUTHORITY"] == authority


@pytest.mark.parametrize("input_tokens", [12000, 64000])
def test_input_chain_grouped_condition_provenance_roundtrips_without_store_decoder(tmp_path, monkeypatch, input_tokens):
    from orca_agent import config as configuration
    from orca_agent.store import Store
    from tests.unit.test_structure_input_chain import (
        test_text_to_model_plan_to_prepared_input_uses_the_single_feedback_loop,
    )

    # Non-code profiles retain their historical 12k display and validation
    # boundary; the larger profile must preserve the same provenance too.
    load = configuration.load_config
    def frozen_capacity(*args, **kwargs):
        config = load(*args, **kwargs)
        config.text.budget.input_tokens = input_tokens
        return config.model_copy(update={"model": config.model.model_copy(update={"input_tokens": input_tokens})})
    monkeypatch.setattr(configuration, "load_config", frozen_capacity)
    requests = []
    prepare = context.prepare_request
    source_schemas = []
    source_schema_by_hash = {}
    native_schemas = []
    native_schema_by_hash = {}
    encoded_sources = {}
    final_native_schemas = []
    final_native_schema_by_hash = {}
    grouped_native_provenance = []
    grouped_native_by_hash = {}
    schema_columns = context._schema_columns
    def capture_schema(schema):
        if "action" in schema.get("properties", {}):
            source_schemas.append(copy.deepcopy(schema))
        encoded = schema_columns(schema)
        key = json.dumps(encoded, sort_keys=True)
        if key in encoded_sources:
            assert encoded_sources[key] == schema
        encoded_sources[key] = copy.deepcopy(schema)
        return encoded
    monkeypatch.setattr(context, "_schema_columns", capture_schema)
    share_strings = context._share_strings
    def capture_final_schemas(value, **kwargs):
        if ("ACTION_SCHEMAS" in value
                and value.get("SCHEMA_COLUMNS", "").startswith("ACTION_SCHEMAS.*")):
            final_native_schemas.append({name: copy.deepcopy(
                encoded_sources.get(json.dumps(schema, sort_keys=True), schema))
                for name, schema in value["ACTION_SCHEMAS"].items()})
            grouped_native_provenance.append(isinstance(value["AUTHORITY"]["request"].get("condition_evidence"), list))
        return share_strings(value, **kwargs)
    monkeypatch.setattr(context, "_share_strings", capture_final_schemas)
    def capture(*args, **kwargs):
        candidate = json.loads(args[0][1]["content"])
        if "ACTION_SCHEMAS" in candidate and "SHARED_STRINGS" not in candidate:
            native_schemas.append(copy.deepcopy(candidate["ACTION_SCHEMAS"]))
        prepared = prepare(*args, **kwargs)
        requests.append(prepared)
        if "SCHEMA_COLUMNS" in candidate and "PROPOSAL_SCHEMA" in candidate:
            source_schema_by_hash[prepared.request_hash] = copy.deepcopy(source_schemas[-1])
        if "ACTION_SCHEMAS" in candidate:
            native_schema_by_hash[prepared.request_hash] = copy.deepcopy(native_schemas[-1])
            final_native_schema_by_hash[prepared.request_hash] = copy.deepcopy(
                final_native_schemas[-1] if candidate.get("SCHEMA_COLUMNS", "").startswith("ACTION_SCHEMAS.*")
                else native_schemas[-1])
            grouped_native_by_hash[prepared.request_hash] = (grouped_native_provenance[-1]
                if candidate.get("SCHEMA_COLUMNS", "").startswith("ACTION_SCHEMAS.*") else False)
        return prepared
    monkeypatch.setattr(context, "prepare_request", capture)
    test_text_to_model_plan_to_prepared_input_uses_the_single_feedback_loop(tmp_path, monkeypatch)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run_file, = store.path("runs").glob("*/run.json")
    run = store.load_run(run_file.parent.name)
    sent_hashes = {record["request_hash"] for record in run.model_records}
    sent = [prepared for prepared in requests if prepared.request_hash in sent_hashes]
    assert len(sent) == 4
    grouped = []
    normalization_requests = 0
    for prepared in sent:
        assert prepared.input_token_bound <= input_tokens
        wire = decoded(prepared)  # Only documented wire; no Store in decoder.
        request_view = wire["AUTHORITY"].get("request", {})
        if not (request_view.get("condition_evidence") or request_view.get("condition_evidence_ref")):
            continue
        current = store.load_request_revision(run, wire["AUTHORITY"]["basis"]["request_version"])
        if "condition_evidence" in request_view:
            assert request_view["condition_evidence"] == current.condition_evidence
        else:
            assert request_view["condition_evidence_ref"] == {
                "source": "immutable Request.condition_evidence", "sha256": context._hash(current.condition_evidence)}
        assert wire["AUTHORITY"]["request"]["conditions_source"] == current.conditions_source
        goal_evidence = [copy.deepcopy(goal.get("text_evidence", {})) for goal in request_view["goals"]]
        for evidence in goal_evidence:
            if "text_basis_ref" in evidence:
                assert evidence.pop("text_basis_ref") == "AUTHORITY.user_originals[0].text"
                evidence["text_basis"] = wire["AUTHORITY"]["user_originals"][0]["text"]
        assert goal_evidence == [goal.text_evidence for goal in current.goals]
        if wire["AUTHORITY"]["decision_purpose"]["kind"] == "normalization":
            normalization_requests += 1
            assert current.normalization_status == "pending"
            assert current.condition_evidence == {"workflow_policy": {
                "version": "simplified-1", "source": "run_configuration"}}
            assert any(item["text"] == current.original_text for item in wire["AUTHORITY"]["user_originals"])
            assert "normalize_request" in wire["ACTION_SCHEMAS"]
            assert "delivery" not in wire["DATA"]
        else:
            record = next(item for item in run.model_records if item["request_hash"] == prepared.request_hash)
            full = json.loads(store.path(f"runs/{run.id}/model/{record['id']}.delivery.json").read_bytes())
            original_conditions = {fact["ref"]: fact["value"] for fact in full["facts"] if fact["kind"] == "conditions"}
            assert {fact["ref"]: fact["value"] for fact in wire["DATA"]["delivery"]["facts"]
                    if fact["kind"] == "conditions"} == original_conditions
            grouped.append(prepared.input_token_bound)
        if wire["AUTHORITY"].get("response_contract") == "decision-intent-2":
            from orca_agent.proposals import DecisionIntent

            raw = json.loads(prepared.body()["messages"][1]["content"])
            assert raw["AUTHORITY"]["response_contract"] == "decision-intent-2"
            assert set(wire["RESPONSE_ENVELOPE"]) == {"action", "parameters", "reason"}
            if grouped_native_by_hash[prepared.request_hash]:
                assert "condition_evidence: fields=name:value;rest shared." in prepared.body()["messages"][0]["content"]
            # Exact independent decoding is compared to the actual input of
            # column encoding, not a second call to the production compactor.
            assert wire["ACTION_SCHEMAS"] == final_native_schema_by_hash[prepared.request_hash]
            assert_native_schemas_equivalent(wire["ACTION_SCHEMAS"], native_schema_by_hash[prepared.request_hash])
            intent_schema = DecisionIntent.model_json_schema()
            assert intent_schema["additionalProperties"] is False
            assert set(intent_schema["required"]) == {"action", "parameters", "reason"}
            intent = {"action": "call_tool", "parameters": {"step_id": "pending"}, "reason": "Audit."}
            DecisionIntent.model_validate(intent)
            for invalid in ({**intent, "type": "decision"}, {key: value for key, value in intent.items() if key != "action"}):
                with pytest.raises(ValueError):
                    DecisionIntent.model_validate(invalid)
        else:
            assert wire["PROPOSAL_SCHEMA"] == source_schema_by_hash[prepared.request_hash]
            assert wire["PROPOSAL_SCHEMA"]["additionalProperties"] is False
            assert wire["PROPOSAL_SCHEMA"]["minProperties"] == 8
    assert normalization_requests == 1 and len(grouped) == 3
    assert run.usage.structure_preparations == 1
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0
    (tmp_path / "capacity.json").write_text(json.dumps({"kind": "offline_synthetic",
        "sent_input_bounds": [request.input_token_bound for request in sent], "grouped": grouped}), encoding="utf-8")


@pytest.mark.parametrize("values,origins,referenced", [
    ({"charge": 0}, {"charge": "request.conditions"}, True),
    ({"charge": False}, {"charge": "request.conditions"}, False),
    ({"charge": 0}, {"charge": "system:water"}, False),
    ({"charge": None}, {"charge": "request.unknown"}, False),
])
def test_current_condition_join_requires_exact_value_and_origin(values, origins, referenced):
    fact = {"ref": "f1", "kind": "conditions", "value": {"current": {"water": values},
        "sources": {"water": origins}, "source": {"charge": 1}, "source_evidence": {"charge": "original"},
        "geometry_relation": "optimized"}}
    original = copy.deepcopy(fact)
    projected = context._reference_current_conditions({"facts": [fact]}, {"conditions": {"charge": 0}})
    value = projected["facts"][0]["value"]
    assert ("current_request_fields" in value) is referenced
    for key in ("source", "source_evidence", "geometry_relation"):
        assert value[key] == original["value"][key]
    if not referenced:
        assert fact == original


@pytest.mark.parametrize("molecule,tool,ports,variant", [
    ("water_opt", "orca.opt", ["energy", "optimized_geometry"], "ordinary"),
    ("water_opt", "orca.opt", ["energy", "optimized_geometry"], "correction"),
    ("water_opt", "orca.opt", ["energy", "optimized_geometry"], "long_identity_and_source"),
    ("methane_sp", "orca.sp", ["energy"], "ordinary"),
])
def test_fixed_science_terminal_shapes_fit_with_all_outputs_and_source_conditions(tmp_path, molecule, tool, ports, variant):
    """Synthetic stdout/current checker shape; never a new real science pass."""
    import shutil

    from orca_agent.goals import validate_goal_evidence
    from orca_agent.models import (
        BudgetLimits,
        Goal,
        InputRef,
        OutputBinding,
        PermissionSnapshot,
        Step,
        SystemInput,
    )
    from orca_agent.orca.adapter import prepare_input
    from orca_agent.store import Store
    from orca_agent.tools.calculation import collect_result
    from orca_agent.versions import CURRENT_CHECK_VERSION
    from tests.unit.test_optimization_final_stage import _case_text
    from tests.unit.test_orca import synthetic_output

    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    geometry = store.import_artifact(Path(__file__).parents[1] / "fixtures/phase_a" / molecule / "geometry.xyz", "initial_geometry")
    request = Request(original_text="Synthetic capacity test: deliver the requested results at frozen conditions.",
        systems=[SystemInput(id="target", geometry_artifact_id=geometry.id)],
        conditions={"explain_results": True}, goals=[Goal(id=port, port=port, system_ids=["target"],
            minimum_check_version=CURRENT_CHECK_VERSION,
            conditions={"geometry_relation": "optimized" if tool == "orca.opt" else "fixed_initial"}) for port in ports])
    if variant == "long_identity_and_source":
        request.original_text += " Report both optimized geometry and its electronic energy under explicitly selected RHF gas-phase conditions."
        request.conditions.update(environment="gas_phase", electronic_state="RHF")
        request.conditions_source.update(environment="explicit", electronic_state="explicit")
        for goal in request.goals:
            goal.id = "goal_" + goal.port + "_0123456789abcdef0123456789abcdef"
    step = Step(id="calculation", logical_id="calculation", tool=tool, system_id="target", geometry=InputRef(artifact_id=geometry.id))
    plan = Plan(request_id=request.id, steps=[step], goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)
                                                           for goal in request.goals})
    run = store.create_run(request, plan, PermissionSnapshot(scientific_execution=True, model_execution=True,
        artifact_ids=[geometry.id]), BudgetLimits(orca_starts=1, model_calls=8, model_tokens=48000, input_tokens=12000, output_tokens=2000))
    attempt = store.reserve_attempt(run, step, geometry.id)
    work = store.path(attempt.directory)
    prepare_input(work, store.artifact_path(geometry.id), step.parameters, tool)
    stdout = _case_text(work / "geometry.xyz", "valid_final_evaluation") if tool == "orca.opt" else synthetic_output(work / "geometry.xyz")
    (work / "stdout.out").write_text(stdout, encoding="utf-8")
    shutil.copyfile(work / "geometry.xyz", work / "job.xyz")
    result = collect_result(store, run, step, attempt, {"state": "completed", "reason": "synthetic capacity fixture; no process"})
    assert set(result.qualified_outputs) == set(ports)
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id, started=False, termination_confirmed=True)
    for goal in request.goals:
        assert validate_goal_evidence(store, run, request, goal, result)
        run.goal_status[goal.id] = "satisfied"
    store.save_run(run)
    full = collect_delivery_snapshot(store, run, request, plan)
    feedback = {"validation_error": {"category": "invalid_terminal_contract", "message": "Missing required fact_refs."}} if variant == "correction" else {}
    prepared = context.build_context(request, run, plan=plan, results=[result], delivery_snapshot=full, feedback=feedback)
    (tmp_path / "capacity.json").write_text(json.dumps({"kind": "offline_synthetic", "variant": variant,
        "input_token_bound": prepared.input_token_bound, "output_token_bound": prepared.output_token_bound}), encoding="utf-8")
    wire = decoded(prepared)
    assert prepared.input_token_bound <= 10000 and prepared.output_token_bound == 2000
    assert prepared_decision_purpose(prepared).kind == "terminal"
    facts = {fact["ref"]: fact for fact in wire["DATA"]["delivery"]["facts"]}
    for fact in full["facts"]:
        if fact["kind"] in {"answer", "conditions", "goal_status", "criterion", "members"}:
            assert facts[fact["ref"]]["value"] == context._safe(fact["value"])
        if fact["kind"] == "checks":
            for port, checks in fact["value"].items():
                visible = facts[fact["ref"]]["value"][port]
                assert len(visible) == len(checks)
                for shown, original in zip(visible, checks, strict=True):
                    for key in ("name", "status", "detail", "rule_version"):
                        assert shown[key] == original[key]
                    for key in ("value", "geometry", "rows", "threshold_rows", "cycle_number", "final_evaluation_lines"):
                        if key in original["source"]:
                            assert shown["source"][key] == original["source"][key]
    assert all(goal["goal_complete"] for goal in full["goals"])
    assert run.usage.orca_starts_actual == run.usage.postprocess_starts == 0
