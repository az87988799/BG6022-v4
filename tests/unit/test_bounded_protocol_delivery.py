"""Portable protocol failures and shared delivery facts; no real model/science."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.delivery import collect_goal_facts
from orca_agent.llm import (
    ModelReply,
    ModelUsage,
    _parse_response,
    prepare_request,
    proposal_recovery_kind,
)
from orca_agent.model_usage import (
    current_basis,
    final_explanation_budget,
    send_model,
    validate_final_explanation_capacity,
)
from orca_agent.proposals import action_parameter_schema, validate_action_parameters
from orca_agent.report import build_report
from orca_agent.store import BudgetExceeded
from tests.helpers.phase_b_grading import classify_grade, model_response_evidence
from tests.unit.test_agent import ScriptedTransport, initial_proposal, make_run
from tests.unit.test_context import objects, payload
from tests.unit.test_model_usage import ScriptedTransport as ReplyTransport
from tests.unit.test_report_extended import source as source

FIXTURE = json.loads((Path(__file__).resolve().parents[1]
                     / "fixtures/phase_b/bounded-protocol-failures.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", FIXTURE["responses"], ids=lambda case: case["case"])
def test_portable_reply_shapes_are_never_repaired_into_fake_json(case):
    request = prepare_request([{"role": "user", "content": "Return JSON."}])
    raw = json.dumps({"model": "deepseek-flash", "choices": [{"finish_reason": case["finish_reason"],
        "message": {"content": case["content"]}}], "usage": {"prompt_tokens": 10,
        "completion_tokens": 20, "total_tokens": 30}}).encode("utf-8")
    reply = _parse_response(request, raw, 200, "synthetic")
    assert reply.proposal is None and reply.error_category == case["error"]
    assert reply.raw_content == case["content"]
    assert proposal_recovery_kind(asdict(reply)) == case["recovery"]
    assert FIXTURE["provenance"]["kind"] == "synthetic"


def test_empty_truncation_is_one_durable_failure_across_two_resumes(tmp_path):
    store, run, _ = make_run(tmp_path)

    class EmptyTruncated:
        sends = 0

        def send(self, prepared, *, reserve, settle):
            self.sends += 1
            ticket = reserve(prepared)
            reply = ModelReply(request_hash=prepared.request_hash, error_category="truncated", raw_content="",
                               finish_reason="length", usage=ModelUsage(50, 2000, 2050),
                               response_model="offline-fake", http_status=200)
            settle(ticket, reply)
            return reply

    transport = EmptyTruncated()
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "failed" and transport.sends == 1
    assert stopped.usage.model_calls == 1 and stopped.usage.model_tokens_used == 2050
    assert stopped.decisions[-1]["parameters"]["recovery_kind"] == "fatal"
    before = store.path(f"runs/{run.id}/model/{stopped.model_records[0]['id']}.response.json").read_bytes()
    for _ in range(2):
        again = agent.execute(store, Config(), run.id, resume=True, transport=transport)
        assert again.usage == stopped.usage and transport.sends == 1
        assert not again.attempts and not again.calls
    assert store.path(f"runs/{run.id}/model/{stopped.model_records[0]['id']}.response.json").read_bytes() == before
    assert not build_report(store, stopped)["user_goal_complete"]


@pytest.mark.parametrize("action,values", [
    ("stop", {"reason": "done", "passed": True}), ("stop", {"reason": []}),
    ("clarify", {"questions": ["what?"], "unresolved": []}),
    ("clarify", {"questions": ["what?"], "unresolved": ["gap"], "permission": True}),
    ("call_tool", {"step_id": "ready", "parameters": {}}),
])
def test_action_schema_declares_object_and_runtime_rejects_extra_or_wrong_shapes(action, values):
    with pytest.raises(ValueError):
        validate_action_parameters(action, values)
    schema = action_parameter_schema(action)
    assert schema["type"] == "object"
    if action in {"stop", "clarify"}:
        assert schema["additionalProperties"] is False


@pytest.mark.parametrize("action,values", [("stop", {}), ("stop", {"reason": "done"}),
    ("clarify", {"questions": ["Which field?"], "unresolved": ["field_query"]}),
    ("call_tool", {"step_id": "ready"}), ("call_tool", {"tool": "evidence.value", "parameters": {}})])
def test_declared_action_examples_round_trip(action, values):
    validate_action_parameters(action, json.loads(json.dumps(values)))


def test_final_explanation_reserves_only_explicit_contract_and_allows_last_final_call():
    request, run = objects(scientific=False)
    run.budget.corrections_per_proposal = 1
    request.conditions["explain_results"] = False
    assert final_explanation_budget(request, run)["future_answer_calls"] == 0
    request.conditions["explain_results"] = True
    assert final_explanation_budget(request, run)["future_answer_calls"] == 1
    run.budget.model_calls = 1
    with pytest.raises(BudgetExceeded, match="final explanation"):
        validate_final_explanation_capacity(request, run)
    assert build_context(request, run)  # Projection/replay does not reserve budget.
    run.goal_status = {goal.id: "satisfied" for goal in request.goals}
    assert validate_final_explanation_capacity(request, run, final_only=True)["can_send_before_final"]
    prepared = build_context(request, run)
    assert payload(prepared)["AUTHORITY"]["remaining"]["final_answer_calls"] == 0
    assert list(payload(prepared)["ACTION_PARAMETERS"]) == ["stop"]


@pytest.mark.parametrize("calls", [2, 8])
def test_representative_eight_call_budget_delivers_required_model_explanation(tmp_path, calls):
    store, old, _ = make_run(tmp_path)
    request = store.load_request(old).model_copy(deep=True)
    request.conditions["explain_results"] = True
    run = store.create_run(request, None, old.permission, old.budget.model_copy(update={"model_calls": calls}))
    run.agent_enabled = True
    store.save_run(run)
    completed = agent.execute(store, Config(), run.id,
        transport=ScriptedTransport(initial_proposal, {"action": "stop", "reason": "The bound observed value is 1; no scientific qualification."}))
    assert completed.state == "completed" and completed.usage.model_calls == 2
    assert build_report(store, completed)["user_goal_complete"]
    assert completed.usage.orca_starts_actual == 0


def test_final_slot_guard_prevents_reservation_without_increasing_old_budget(tmp_path):
    store, original, _ = make_run(tmp_path, model_calls=1)
    request = store.load_request(original).model_copy(deep=True)
    request.conditions["explain_results"] = True
    run = store.create_run(request, None, original.permission, original.budget)
    run.agent_enabled = True
    store.save_run(run)
    transport = ScriptedTransport(initial_proposal)
    ended = agent.execute(store, Config(), run.id, transport=transport)
    assert ended.state == "budget_exhausted" and not transport.sent
    assert ended.usage.model_calls == ended.usage.model_tokens_unknown == ended.usage.model_tokens_used == 0
    assert not ended.model_records and ended.budget.model_calls == 1
    report = build_report(store, ended)
    assert not report["user_goal_complete"] and report["goal_facts"][0]["gaps"]


def test_context_and_report_share_actual_observation_answer_and_unknown_unit(tmp_path):
    store, run, _ = make_run(tmp_path)
    completed = agent.execute(store, Config(), run.id, transport=ScriptedTransport(initial_proposal))
    request, plan = store.load_request(completed), store.load_plan(completed)
    facts = collect_goal_facts(store, completed, request, plan)
    results = [store.load_result(completed.id, identifier) for identifier in completed.result_ids]
    projected = payload(build_context(request, completed, plan, results=results, feedback={"goal_facts": facts}))
    assert build_report(store, completed)["goal_facts"] == facts
    import copy
    expected = copy.deepcopy(facts)
    expected[0]["answer"]["observation"]["source"]["imported_from"] = "[path redacted; use registered reference]"
    assert projected["DATA"]["goal_facts"] == expected
    answer = facts[0]["answer"]
    assert answer["observation"]["value"] == 1 and answer["unit"] is None
    assert answer["scientific_qualification"] is False and facts[0]["goal_complete"]


def test_current_unknown_withholds_answer_but_keeps_historical_source_conditions(source):
    case = next(item for item in FIXTURE["semantic_and_delivery"] if item["case"] == "v09-current-unknown")
    source.request.charge = case["current_charge"]
    source.request.conditions_source["charge"] = "unknown"
    row = collect_goal_facts(source.store, source.run, source.request, source.plan)[0]
    report = build_report(source.store, source.run)
    assert report["goal_facts"] == [row]
    assert row["current_conditions"]["request"]["charge"] is None
    assert row["source_conditions"]["charge"] == case["source_charge"]
    assert row["answer"] is None and row["goal_complete"] is False
    assert source.result.qualified_outputs["energy"].value == -74.96299


def test_portable_n06_registration_preserves_both_unmet_scientific_goals(tmp_path):
    from test_semantic_control import candidate
    from test_semantic_goal_binding_production import molecular_run

    from orca_agent.semantic_notices import notice_choices

    case = next(item for item in FIXTURE["semantic_and_delivery"] if item["case"] == "n06-registration-only")
    store, run = molecular_run(tmp_path, case["text"], geometries=False)
    clause = case["text"].split("。")[0]
    choices = notice_choices()
    values = candidate(store, run, kind="normalize", goals=[
        {"key": "geometry", "port": "optimized_geometry", "text_basis": clause, "system_refs": []},
        {"key": "energy", "port": "energy", "text_basis": clause,
         "system_refs": [], "geometry_relation": "optimized"}],
        unresolved=["unsupported_system:ethanol"],
        notices=["乙醇优化结构及优化后电子能均已登记；当前范围不支持乙醇且未登记几何，本轮不计算。",
                 choices["unsupported_system"], choices["missing_geometry"]])
    stopped = agent.execute(store, Config(), run.id,
        transport=ScriptedTransport({"action": "normalize_request", "parameters": values}))
    report = build_report(store, stopped)
    assert stopped.state == "paused" and report["communication"]["registration_complete"]
    assert report["communication"]["delivery_scope"] == case["scope"]
    assert not report["communication"]["awaiting_reply"]
    assert report["user_goal_complete"] is case["scientific_complete"]
    assert {row["port"] for row in report["goal_facts"]} == {"optimized_geometry", "energy"}
    assert all(row["answer"] is None and not row["goal_complete"] for row in report["goal_facts"])
    assert not stopped.calls and not stopped.attempts
    for _ in range(2):
        again = agent.execute(store, Config(), stopped.id, resume=True, transport=ScriptedTransport())
        assert again.usage == stopped.usage and not again.calls and not again.attempts


def test_two_truncated_http_receipts_are_distinct_from_zero_accepted_proposals(tmp_path):
    store, run, _ = make_run(tmp_path)
    prepared = prepare_request([{"role": "user", "content": "Synthetic receipt replay; return JSON."}])
    for number in range(2):
        reply = ModelReply(request_hash=prepared.request_hash, error_category="truncated", raw_content="",
                           finish_reason="length", response_model="deepseek-flash", http_status=200,
                           response_hash=str(number) * 64, usage=ModelUsage(10, 20, 30))
        send_model(store, run, prepared, ReplyTransport(reply), basis=current_basis(store, run), logical_id=f"synthetic_{number}")
        run.decisions.append({"id": run.model_records[-1]["id"], "action": "rejected",
                              "basis": current_basis(store, run)})
        store.save_run(run)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    evidence = model_response_evidence(store, run)
    assert evidence["http_evidence_present"] is True and evidence["http_records"] == 2
    assert evidence["accepted_proposals"] == evidence["accepted_final_responses"] == 0
    assert evidence["rejected_proposals"] == 2 and evidence["present"] is False
    assert evidence["protocol_delivery_status"] == "failed"
    assert classify_grade({"protocol_delivery": {"status": "failed"}, "status": "not_verified"}) == "failed"
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
