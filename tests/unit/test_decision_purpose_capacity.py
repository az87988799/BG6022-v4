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
            "a": ("array", ["items", "minItems", "maxItems"]),
            "s": ("string", ["minLength", "maxLength"]),
        }[tag]
        node = {"type": kind, **{key: value for key, value in zip(columns, values) if value is not None}}
    if not isinstance(node, dict):
        return node
    result = {}
    for key, value in node.items():
        if key in ("properties", "$defs"):
            value = {name: expand_schema(child) for name, child in value.items()}
        elif key in ("items", "if", "then", "additionalProperties", "propertyNames"):
            value = expand_schema(value)
        elif key in ("oneOf", "allOf", "anyOf"):
            value = [expand_schema(child) for child in value]
        result[key] = value
    return result


def decoded(prepared):
    value = payload(prepared)
    if "SCHEMA_COLUMNS" in value:
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
    from orca_agent.report import build_report
    from orca_agent.store import Store
    from tests.unit.test_phase_b_model_cases import (
        test_v07_all_requested_reads_precede_completion_within_frozen_model_budget,
    )

    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    prepared_requests = []
    prepare = context.prepare_request
    def capture(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        prepared_requests.append(prepared)
        return prepared
    monkeypatch.setattr(context, "prepare_request", capture)
    # The existing production Agent test retains its original 4 HTTP/3 read
    # and full user-goal completion assertions. Its transport is synthetic.
    test_v07_all_requested_reads_precede_completion_within_frozen_model_budget(store, "array-location")
    run_path, = store.path("runs").glob("*/run.json")
    run = store.load_run(run_path.parent.name)
    by_hash = {request.request_hash: request for request in prepared_requests}
    requests = [by_hash[record["request_hash"]] for record in run.model_records]
    assert len(requests) == 4 and all(request.input_token_bound <= 12000 for request in requests)
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


def test_input_chain_grouped_condition_provenance_roundtrips_without_store_decoder(tmp_path, monkeypatch):
    from orca_agent.store import Store
    from tests.unit.test_structure_input_chain import (
        test_text_to_model_plan_to_prepared_input_uses_the_single_feedback_loop,
    )

    requests = []
    prepare = context.prepare_request
    def capture(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        requests.append(prepared)
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
    for prepared in sent:
        assert prepared.input_token_bound <= 12000
        wire = decoded(prepared)  # Only documented wire; no Store in decoder.
        if "condition_evidence: fields=" not in prepared.body()["messages"][0]["content"]:
            continue
        current = store.load_request_revision(run, wire["AUTHORITY"]["basis"]["request_version"])
        assert wire["AUTHORITY"]["request"]["condition_evidence"] == current.condition_evidence
        assert wire["AUTHORITY"]["request"]["conditions_source"] == current.conditions_source
        assert [goal.get("text_evidence", {}) for goal in wire["AUTHORITY"]["request"]["goals"]] == [
            goal.text_evidence for goal in current.goals]
        record = next(item for item in run.model_records if item["request_hash"] == prepared.request_hash)
        full = json.loads(store.path(f"runs/{run.id}/model/{record['id']}.delivery.json").read_bytes())
        original_conditions = {fact["ref"]: fact["value"] for fact in full["facts"] if fact["kind"] == "conditions"}
        assert {fact["ref"]: fact["value"] for fact in wire["DATA"]["delivery"]["facts"]
                if fact["kind"] == "conditions"} == original_conditions
        assert context._hash(wire["PROPOSAL_SCHEMA"]) == wire["SCHEMA_SHA256"]
        grouped.append(prepared.input_token_bound)
    assert grouped and run.usage.structure_preparations == 1
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
