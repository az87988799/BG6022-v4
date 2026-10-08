"""Portable query-feedback regressions; scripted replies are not live evidence."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import agent, context, llm, runner
from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.decision_purpose import prepared_decision_purpose
from orca_agent.llm import PreparedRequest
from orca_agent.model_usage import final_explanation_budget, validate_delivery_margin
from orca_agent.models import Plan, Request, Result, Run
from orca_agent.proposals import (
    call_tool_instruction,
    call_tool_parameters_schema,
    valid_call_tool_parameters,
)
from orca_agent.store import BudgetExceeded, Store
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_decision_purpose_capacity import decoded
from tests.unit.test_phase_b_model_cases import CASES

FIXTURE = Path(__file__).parents[1] / "fixtures/phase_b/repair-cycle-v24-query-rejection.json"


def _actual_failure():
    document = json.loads(FIXTURE.read_bytes())
    for response in document["responses"]:
        for name in ("request_file", "response_file", "delivery_file"):
            if name not in response:
                continue
            evidence = response[name]
            raw = evidence["utf8_text"].encode("utf-8")
            assert len(raw) == evidence["bytes"]
            assert hashlib.sha256(raw).hexdigest() == evidence["sha256"]
        assert json.loads(response["raw_content"]) == response["proposal"]
    return document


def _correction_inputs(document):
    run = Run.model_validate(document["run_state"])
    plan = Plan.model_validate(document["accepted_plan"])
    result = Result.model_validate(document["discover_result"])
    response = document["responses"][1]
    diagnostic = response["decision"]["parameters"]
    snapshot = json.loads(response["delivery_file"]["utf8_text"])
    return {
        "request": Request.model_validate(document["request"]), "run": run, "plan": plan,
        "results": [result], "delivery_snapshot": snapshot,
        "feedback": {"new_result_ids": [result.id],
                     "pending_step_ids": [step.id for step in agent._ready(plan, {result.step_id: result})],
                     "validation_error": {"category": diagnostic["error_category"],
                                          "requirement": diagnostic["requirement"]}},
        "relevant_tools": run.permission.allowed_tools,
        "control_generation": run.control_generation,
        "now": datetime.fromisoformat(response["model_record"]["created_at"]) + timedelta(seconds=5),
        "model_profile": "disabled",
    }


@pytest.fixture
def candidates(monkeypatch, tmp_path):
    original = context.prepare_request
    values = []

    def capture(*args, **kwargs):
        error = None
        try:
            prepared = original(*args, **kwargs)
            body = prepared.body()
            bound = prepared.input_token_bound
        except ValueError as exc:
            if str(exc) != "conservative input token bound exceeds 12000":
                raise
            error = exc
            body = {"model": llm.MODEL, "messages": args[0], "stream": False, "temperature": 0,
                    "max_tokens": kwargs["max_output_tokens"], "response_format": {"type": "json_object"},
                    **llm.model_profile_parameters(kwargs["model_profile"])}
            bound = llm.input_token_upper_bound(body)
        wire = json.loads(body["messages"][-1]["content"])
        values.append({"input_bound": bound,
                       "sections": {key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())
                                    for key, value in wire.items()}})
        (tmp_path / "prepared-candidates.json").write_text(json.dumps(values, indent=2), encoding="utf-8")
        (tmp_path / f"candidate-{len(values)}.json").write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        if error:
            raise error
        return prepared

    monkeypatch.setattr(context, "prepare_request", capture)
    return values


def test_original_reply_failed_despite_visible_exact_plan_path_and_names():
    document = _actual_failure()
    response = document["responses"][1]
    original = copy.deepcopy(response["proposal"])
    body = json.loads(response["request_file"]["utf8_text"])
    sent = decoded(SimpleNamespace(body=lambda: body))
    assert response["accepted"] is False
    assert not valid_call_tool_parameters(original["parameters"])
    step = _step_for(sent, "raw_field_observation")
    assert step["id"] == original["parameters"]["step_id"]
    assert step["tool"] == "evidence.value"
    assert step["parameters"]["path"][2:4] == [
        {"kind": "key", "key": "Dipole_Moment"}, {"kind": "index", "index": 0}]
    assert original["parameters"]["parameters"]["path"][3] == {"kind": "key", "key": "dipoleMagnitude"}
    assert all(tool["name"] != "read_registered_artifact" for tool in sent["TOOL_CATALOG"])
    assert all(tool["effects"] == ["read_registered_artifact"] for tool in sent["TOOL_CATALOG"])
    assert "Plan{step_id} only" in body["messages"][0]["content"]
    assert response["proposal"] == original


def test_frozen_uncompacted_request_already_exceeds_post_reply_terminal_margin():
    document = _actual_failure()
    values = _correction_inputs(document)
    response = document["responses"][1]
    record = response["model_record"]
    # This is the persisted SECOND request reused for arithmetic, not a claim
    # that a third request was transmitted or that its bytes were captured.
    prepared = PreparedRequest(
        canonical_body=response["request_file"]["utf8_text"], request_hash=record["request_hash"],
        input_token_bound=record["input_reserved"], output_token_bound=record["output_reserved"],
        timeout_seconds=60, prompt_version=record["prompt_version"],
    )
    assessment = final_explanation_budget(values["request"], values["run"],
        purpose=prepared_decision_purpose(prepared), prepared=prepared, now=values["now"])
    assert assessment["remaining_calls_before_this_request"] == 2
    assert assessment["remaining_decision_rounds_after_this_round"] == 9
    assert assessment["remaining_tokens_before_this_request"] == 25303
    assert assessment["future_answer_tokens"] == 12000
    assert prepared.reserved_tokens == 13359
    with pytest.raises(BudgetExceeded, match="required final explanation"):
        validate_delivery_margin(values["request"], values["run"], purpose=prepared_decision_purpose(prepared),
                                 prepared=prepared, now=values["now"])


def test_actual_correction_uses_smaller_existing_projection_and_keeps_final_margin(candidates):
    document = _actual_failure()
    before = copy.deepcopy(document)
    values = _correction_inputs(document)
    try:
        prepared = build_context(**values)
    except (BudgetExceeded, context.ContextLimitError) as exc:
        pytest.fail(f"{exc}; prepared candidates={candidates}")
    assessment = validate_delivery_margin(values["request"], values["run"],
        purpose=prepared_decision_purpose(prepared), prepared=prepared, now=values["now"])
    assert prepared.input_token_bound <= 11303
    assert assessment["can_send_before_final"] is True
    assert prepared.output_token_bound == 2000
    sent = decoded(prepared)
    assert sent["DATA"]["delivery"]["request_text"] == document["request"]["original_text"]
    expected_error = copy.deepcopy(values["feedback"]["validation_error"])
    repeated_instruction = expected_error["requirement"].pop("requirement")
    assert repeated_instruction == call_tool_instruction()
    assert sent["CONTROL"]["validation_error"] == expected_error
    prompt = prepared.body()["messages"][0]["content"]
    assert "Step{step_id} only;no overrides" in prompt
    assert "Reader{tool,parameters}:tool=catalog.name,not effects" in prompt
    goals = {row["id"]: row for row in sent["AUTHORITY"]["request"]["goals"]}
    for goal in document["request"]["goals"]:
        assert goals[goal["id"]]["conditions"] == goal["conditions"]
    expected = next(step for step in document["accepted_plan"]["steps"]
                    if step["id"] == document["responses"][1]["proposal"]["parameters"]["step_id"])
    assert _step_for(sent, "raw_field_observation")["parameters"]["path"] == expected["parameters"]["path"]
    facts = {fact["ref"]: fact for fact in sent["DATA"]["delivery"]["facts"]}
    for fact in values["delivery_snapshot"]["facts"]:
        shown = copy.deepcopy(facts[fact["ref"]]["value"])
        if fact["kind"] == "checks":
            for port, checks in shown.items():
                for check, original in zip(checks, fact["value"][port], strict=True):
                    if check["source"] == {"snapshot_path": "."}:
                        assert check["rule_version"] == "evidence-read-1"
                        check["source"] = original["source"]
        if fact["kind"] == "answer" and shown.get("kind") == "evidence_observation":
            observation = shown["observation"]
            for key in observation.pop("snapshot_fields", []):
                assert key in {"file", "source", "artifact_id", "sha256"}
                assert key not in observation
                observation[key] = fact["value"]["observation"][key]
        # Only the documented same-fact metadata references are restored from
        # the byte-bound sidecar. Values, path, units, coverage and qualification
        # must be present verbatim, not inferred or substituted from Store.
        assert shown == fact["value"]
    assert document == before


def test_specific_path_correction_cannot_be_dropped_as_generic_call_guidance():
    values = _correction_inputs(_actual_failure())
    values["feedback"]["validation_error"]["requirement"]["requirement"] += (
        " Dipole_Moment is an array: preserve its index 0 before dipoleMagnitude.")
    expected = copy.deepcopy(values["feedback"]["validation_error"])
    prepared = build_context(**values)
    assert decoded(prepared)["CONTROL"]["validation_error"] == expected
    # The stronger diagnostic can still exhaust this exact historical balance;
    # projection never edits it to buy room or relaxes the final-answer reserve.
    with pytest.raises(BudgetExceeded, match="required final explanation"):
        validate_delivery_margin(values["request"], values["run"], purpose=prepared_decision_purpose(prepared),
                                 prepared=prepared, now=values["now"])


def _query_run(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run, _ = CASES.create_request(store, "V-07/array-location", 1)
    # The offline test has no shared live ledger and cannot send a real request.
    run.batch_category = None
    store.save_run(run)
    return store, run


def _initial_plan(data):
    request = data["AUTHORITY"]["request"]
    goals = {goal["id"]: goal for goal in request["goals"]}
    sequence = request["conditions"]["user_query_sequence"]
    keys = {key: f"s{i + 1}" for i, key in enumerate(sequence)}
    return {"action": "initial_plan", "parameters": {
        "steps": [{"key": keys[key], "tool": "evidence.discover" if i == 0 else "evidence.value",
                   "parameters": goals[key]["conditions"]["query"]}
                  for i, key in enumerate(sequence)],
        "goal_map": {key: {"step_key": keys[key], "port": goals[key]["port"]} for key in sequence},
    }}


def _step_for(data, goal_id):
    plan = data["AUTHORITY"]["plan"]
    step_id = plan["goal_map"][goal_id]["step_id"]
    return next(step for step in plan["steps"] if isinstance(step, dict) and step["id"] == step_id)


def _call_contract(data):
    schema = data["PROPOSAL_SCHEMA"]
    return next(branch["properties"]["parameters"] for branch in schema["oneOf"]
                if branch["properties"]["action"] == {"const": "call_tool"})


def _planned_call(goal_id):
    def reply(data):
        step = _step_for(data, goal_id)
        return {"action": "call_tool", "parameters": {"step_id": step["id"]}}
    return reply


class CapturedTransport(ScriptedTransport):
    def __init__(self, *scripts):
        super().__init__(*scripts)
        self.prepared = []

    def send(self, prepared, *, reserve, settle):
        self.prepared.append(prepared)
        return super().send(prepared, reserve=reserve, settle=settle)


def test_v07_four_calls_read_in_user_order_and_publish_exact_raw_value(tmp_path, candidates):
    store, run = _query_run(tmp_path)
    original = store.load_request(run).model_dump(mode="json")
    transport = CapturedTransport(
        _initial_plan, _planned_call("raw_geometry_slice"),
        _planned_call("raw_field_observation"), {"action": "stop", "parameters": {}},
    )
    completed = agent.execute(store, Config(data_root=store.root), run.id, transport=transport)
    assert completed.state == "completed", {"diagnostics": completed.diagnostics, "candidates": candidates}
    assert completed.usage.model_calls == 4 == completed.budget.model_calls
    assert [call.tool for call in completed.calls] == ["evidence.discover", "evidence.value", "evidence.value"]
    assert completed.calls[1].parameters["path"][-1] == {"kind": "slice", "start": 0, "stop": 1}
    assert completed.calls[2].parameters["path"] == original["goals"][0]["conditions"]["query"]["path"]
    assert completed.usage.evidence_reads == 3
    assert not completed.attempts and completed.usage.orca_starts_actual == 0
    assert completed.terminal_deliveries[-1].contract_status == "passed"
    assert store.load_request(completed).model_dump(mode="json") == original
    results = [store.load_result(completed.id, result_id) for result_id in completed.result_ids]
    assert all(not result.qualified_outputs for result in results)
    value = results[-1].observations["value_observation"]
    assert value["scientific_status"] == "unverified" and value["units"] is None
    source = json.loads(store.artifact_path(completed.calls[-1].parameters["artifact_id"]).read_bytes())
    assert value["value"] == source["Geometries"][0]["Dipole_Moment"][0]["dipoleMagnitude"]
    assert all(prepared.input_token_bound <= 12000 for prepared in transport.prepared)
    assert transport.prepared[-1].input_token_bound <= 10000
    # ScriptedTransport settles 75 synthetic tokens; the real pre-send margin
    # still runs, but this is not a worst-case four-request token certificate.
    assert completed.usage.model_tokens_used == 4 * 75

    feedback = decoded(transport.prepared[1])
    contract = _call_contract(feedback)
    steps = {_step_for(feedback, goal)["id"] for goal in ("raw_geometry_slice", "raw_field_observation")}
    assert set(contract["properties"]["step_id"]["enum"]) == steps
    if "pending_step_ids" in feedback.get("CONTROL", {}):
        assert set(feedback["CONTROL"]["pending_step_ids"]) == steps
    assert set(contract["properties"]["tool"]["enum"]) == {"evidence.discover", "evidence.value"}
    assert "read_registered_artifact" not in contract["properties"]["tool"]["enum"]
    assert feedback["ACTION_PARAMETERS"]["call_tool"] == {
        "step_id": _step_for(feedback, "raw_geometry_slice")["id"]}
    assert _step_for(feedback, "raw_field_observation")["parameters"]["path"] == value["path"]
    assert {tuple(branch["required"]) for branch in contract["oneOf"]} == {
        ("step_id",), ("tool", "parameters")}


@pytest.mark.parametrize("mistake", ["hybrid", "effect_name"])
def test_invalid_feedback_call_is_not_executed_or_used_to_override_plan(tmp_path, mistake):
    store, run = _query_run(tmp_path)

    def invalid(data):
        step = _step_for(data, "raw_geometry_slice")
        values = {"tool": "read_registered_artifact", "parameters": copy.deepcopy(step["parameters"])}
        if mistake == "hybrid":
            values["step_id"] = step["id"]
            assert not valid_call_tool_parameters(values)
        return {"action": "call_tool", "parameters": values}

    # Test only the decision boundary after a real offline discover, without
    # pretending the frozen four-call allowance promises every correction.
    runner._goals(store, run, None, {})
    store.save_run(run)
    transport = CapturedTransport(_initial_plan)
    plan_run, action, _ = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "plan"
    plan = store.load_plan(plan_run)
    before = plan.model_dump(mode="json")
    from orca_agent.tools.dispatch import execute_call
    result = execute_call(store, plan_run, plan.steps[0].tool, plan.steps[0].parameters.model_dump(),
                          step=plan.steps[0], results={})
    results = {plan.steps[0].id: result}
    transport = CapturedTransport(invalid, _planned_call("raw_geometry_slice"))
    updated, action, value = agent._decision(store, plan_run, plan, results, transport, None, None)
    assert action == "step" and value[0].id == plan.goal_map["raw_geometry_slice"].step_id
    assert len(updated.calls) == 1 and updated.usage.evidence_reads == 1
    assert store.load_plan(updated).model_dump(mode="json") == before
    assert updated.decisions[-2]["action"] == "rejected"
    assert transport.sent[1]["CONTROL"]["validation_error"]


@pytest.mark.parametrize("step_ids,tool_names,expected_fields", [
    ([], ["evidence.value"], {"tool", "parameters"}),
    (["step_ready"], [], {"step_id"}),
    ([], [], set()),
])
def test_empty_current_choices_remove_only_the_unavailable_call_branch(step_ids, tool_names, expected_fields):
    schema = call_tool_parameters_schema(step_ids=step_ids, tool_names=tool_names)
    if not expected_fields:
        assert schema == {"not": {}}
    else:
        assert set(schema["properties"]) == expected_fields
        assert set(schema["required"]) == expected_fields
        assert schema["maxProperties"] == len(expected_fields)
