"""Portable regression of r3 V06's failed interpretation, never a model replay."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import context
from orca_agent.llm import input_token_upper_bound, prepare_request
from orca_agent.models import Check, Goal, QualifiedOutput, Result, utc_now
from tests.unit.test_context import objects, payload

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/v06-terminal-outcome"


@pytest.fixture
def actual():
    raw = (FIXTURE / "input.json").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_bytes())
    assert provenance["kind"] == "real_derived"
    assert hashlib.sha256(raw).hexdigest() == provenance["input_file"]["sha256"]
    return json.loads(raw)


def decoded(messages):
    return payload(SimpleNamespace(body=lambda: {"messages": messages}))


def test_actual_failure_is_retained_with_budget_and_goal_facts_visible(actual):
    first, last = actual["original_http"]
    assert json.loads(first["raw_content"])["action"] == "initial_plan"
    final = json.loads(last["raw_content"])
    assert final["action"] == "stop"
    assert "goal evidence is sufficient" in final["reason"]
    assert "required evidence is complete" in final["reason"]
    assert actual["recorded_review"]["classification"] == "failed"
    assert actual["recorded_review"]["budget_limit_disclosed"] is False
    assert actual["program_facts"]["qualified_outputs"] == {}
    assert actual["program_facts"]["goal_satisfied"] is False
    data = decoded(last["request"]["messages"])
    authority = data["AUTHORITY"]
    assert authority["goal_status"] == {"finite_sample_internal_minimum": "insufficient_evidence"}
    assert authority["budget_limits"]["orca_starts"] == authority["budget_limits"]["extra_orca_starts"] == 0
    assert authority["permission"]["scientific_execution"] is False
    assert authority["permission"]["allow_additional_science"] is False
    analysis = data["DATA"]["results"][0]["unqualified_observations"]["analysis"]
    assert analysis["goal_satisfied"] is False and analysis["reason"] == "span_too_wide"
    assert analysis["neighbor_span_angstrom"] == 0.16000000000042536 > analysis["target_width_angstrom"] == 0.12
    assert [member["status"] for member in analysis["members"]].count("qualified") == 3
    assert [member["status"] for member in analysis["members"]].count("missing") == 2
    assert data["DATA"]["goal_facts"][0]["goal_complete"] is False
    assert data["DATA"]["goal_facts"][0]["answer"] is None


@pytest.mark.parametrize("index", [0, 1])
def test_actual_requests_keep_all_wire_facts_and_caps_under_explicit_prompt_derivation(actual, index):
    original = actual["original_http"][index]["request"]
    messages = copy.deepcopy(original["messages"])
    wire = json.loads(messages[1]["content"])
    actions = wire["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"]
    # This is a synthetic prompt derivation, not a new response or a real pass.
    messages[0]["content"] = context._terminal_prompt(messages[0]["content"], actions)
    prepared = prepare_request(messages, prompt_version=context.PROMPT_VERSION,
                               max_output_tokens=2000, timeout_seconds=60)
    assert prepared.body()["messages"][1] == original["messages"][1]
    assert decoded(prepared.body()["messages"]) == decoded(original["messages"])
    assert prepared.body()["max_tokens"] == original["max_tokens"] == 2000
    assert prepared.input_token_bound <= 12000
    if index == 0:
        assert prepared.body()["messages"] == original["messages"]
    else:
        assert input_token_upper_bound(original) == 11983
        assert prepared.input_token_bound == 11988
        prompt = prepared.body()["messages"][0]["content"]
        assert "Members qualified!=goals met" in prompt
        assert "permission/budget blocks, including zero" in prompt
        assert "Plan write_analysis" not in prompt
        assert "Reason=Step/params/effects" not in prompt


@pytest.mark.parametrize("mode", ["qualified", "insufficient", "read_complete", "unknown_condition"])
def test_production_terminal_prompt_preserves_positive_negative_and_read_only_facts(mode, monkeypatch):
    request, run = objects(scientific=False)
    request.conditions["explain_results"] = True
    run.permission.allowed_tools = []
    run.budget.orca_starts = run.budget.extra_orca_starts = 0
    run.budget.model_calls = 8
    run.budget.model_tokens = 48000
    complete = mode in {"qualified", "read_complete"}
    if mode == "read_complete":
        request.goals = [Goal(id="read", port="value_observation", minimum_check_version="evidence-read-1")]
        result = Result(run_id=run.id, operation_status="completed",
                        observations={"value_observation": {"value": 3, "units": None,
                                      "scientific_status": "unverified"}})
    elif mode == "qualified":
        check = Check(name="scf_converged", status="passed", rule_version="orca-hf-2")
        result = Result(run_id=run.id, operation_status="completed", checks={"energy": [check]},
                        qualified_outputs={"energy": QualifiedOutput(value=-75, unit="Eh", checks=[check])})
    else:
        result = Result(run_id=run.id, operation_status="completed",
                        observations={"insufficient": {"goal_satisfied": False}})
    if mode == "unknown_condition":
        request.multiplicity = None
        request.conditions_source["multiplicity"] = "unknown"
        request.unresolved = ["field:multiplicity"]
    run.result_ids = [result.id]
    goal = request.goals[0]
    run.goal_status = {goal.id: "satisfied" if complete else "insufficient_evidence"}
    feedback = {"goal_facts": [{"goal_id": goal.id, "port": goal.port, "required": True,
        "goal_complete": complete, "gaps": [] if complete else ["unqualified_or_unknown"],
        "answer": {"value": -75, "unit": "Eh"} if mode == "qualified" else None}]}
    before = request.model_dump_json(), run.model_dump_json(), result.model_dump_json()
    now = utc_now()
    with monkeypatch.context() as patch:
        patch.setattr(context, "_terminal_prompt", lambda prompt, actions: prompt)
        original = context.build_context(request, run, results=[result], feedback=feedback, now=now)
    prepared = context.build_context(request, run, results=[result], feedback=feedback, now=now)
    assert prepared.body()["messages"][1] == original.body()["messages"][1]
    assert payload(prepared) == payload(original)
    assert prepared.input_token_bound <= 12000 and prepared.body()["max_tokens"] == 2000
    prompt = prepared.body()["messages"][0]["content"]
    assert "Members qualified!=goals met" in prompt
    assert "Use goal_status" in prompt and "including zero" in prompt
    actions = payload(prepared)["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"]
    assert set(actions) == ({"stop"} if complete else {"stop", "clarify"})
    if mode == "read_complete":
        assert "Null units=unknown" in prompt and "never inferred" in prompt
        assert payload(prepared)["DATA"]["results"][0]["unqualified_observations"]["value_observation"]["units"] is None
    if mode == "unknown_condition":
        assert payload(prepared)["AUTHORITY"]["request"]["multiplicity"] is None
    assert before == (request.model_dump_json(), run.model_dump_json(), result.model_dump_json())


@pytest.mark.parametrize("tool", ["analysis.energy_compare", "evidence.field"])
def test_nonterminal_analysis_and_immediate_read_keep_their_action_contract(tool):
    request, run = objects(scientific=False)
    run.permission.allowed_tools = [tool]
    run.permission.artifact_writes = True
    prepared = context.build_context(request, run, relevant_tools=[tool])
    data = payload(prepared)
    actions = data["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"]
    prompt = prepared.body()["messages"][0]["content"]
    assert "initial_plan" in actions
    assert "Reason=Step/params/effects; proposed!=settled." in prompt
    assert "Members qualified!=goals met" not in prompt
    if tool == "analysis.energy_compare":
        assert "Plan write_analysis." in prompt
    else:
        assert "call_tool" in actions


def test_last_final_explanation_slot_keeps_facts_and_original_eight_call_caps(monkeypatch):
    request, run = objects(scientific=False)
    request.conditions["explain_results"] = True
    run.goal_status = {request.goals[0].id: "satisfied"}
    run.usage.model_calls = 7
    run.usage.model_tokens_used = 1000
    now = utc_now()
    with monkeypatch.context() as patch:
        patch.setattr(context, "_terminal_prompt", lambda prompt, actions: prompt)
        original = context.build_context(request, run, now=now)
    prepared = context.build_context(request, run, now=now)
    assert prepared.body()["messages"][1] == original.body()["messages"][1]
    assert payload(prepared) == payload(original)
    assert prepared.input_token_bound - original.input_token_bound == 107
    assert prepared.input_token_bound <= 12000
    assert prepared.body()["max_tokens"] == 2000
    assert run.budget.model_calls == 8 and run.budget.model_tokens == 48000
    assert list(payload(prepared)["ACTION_PARAMETERS"]) == ["stop"]
    assert "Use goal_status" in prepared.body()["messages"][0]["content"]
