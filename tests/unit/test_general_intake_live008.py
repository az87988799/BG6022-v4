"""Offline replay of immutable live008; corrections below are test-authored."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_general_intake_correction import environment

from orca_agent.condition_selection import selectable_conditions
from orca_agent.context import build_context
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters, commit_candidate

EVIDENCE = Path(__file__).parents[2] / "docs/acceptance/code-as-tools-step3/validation/live008-intake-failure.json"
DIGEST = "0d56df8c1638c9fa9d39cb88c46b8f485ca9083e7efc5c94946747918d76e24d"


def replay(tmp_path, index):
    raw = EVIDENCE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DIGEST
    receipt = json.loads(raw)["calls_in_persisted_order"][index]["complete_saved_response"]
    assert json.loads(receipt["raw_content"]) == receipt["proposal"]
    parameters = receipt["proposal"]["parameters"]
    store, _, run = environment(tmp_path, parameters["text_basis"])
    message_id = store.read_control(run.id)["messages"][0]["id"]

    def rebind(value):
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value

    return store, run, rebind(parameters)


def submit(store, run, parameters):
    return commit_candidate(store, run, parameters, decision_id="offline_live008_replay",
                            basis=current_basis(store, run))


@pytest.mark.parametrize("index,expected", [
    (0, {("parameters", "goals", 0, "geometry_relation"),
         ("parameters", "goals", 1, "quantity"), ("parameters", "goals", 2, "quantity"),
         ("parameters", "unresolved", 0)}),
    (1, {("parameters", "goals", 0, "geometry_relation")}),
    (2, {("parameters", "goals", 1, "quantity"), ("parameters", "goals", 2, "quantity"),
         ("parameters", "unresolved", 0)}),
])
def test_all_currently_detectable_intake_errors_are_returned_together(tmp_path, index, expected):
    store, run, proposed = replay(tmp_path, index)
    before = copy.deepcopy(proposed)
    request_before = store.load_request(run).model_dump_json()
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    corrections = rejected.value.detail["corrections"]
    assert {tuple(item["path"]) for item in corrections} == expected
    assert all(item["instruction"] for item in corrections)
    assert "Apply all listed corrections together" in rejected.value.detail["requirement"]
    assert proposed == before and store.load_request(run).model_dump_json() == request_before
    assert run.request_version == 1 and not run.calls and not run.attempts and not run.model_records
    assert not run.processed_messages and not run.decisions
    assert hashlib.sha256(EVIDENCE.read_bytes()).hexdigest() == DIGEST


def test_test_authored_incremental_correction_cannot_reintroduce_old_errors(tmp_path):
    store, run, proposed = replay(tmp_path, 0)
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    assert len(rejected.value.detail["corrections"]) == 4
    proposed["goals"][0].pop("geometry_relation")
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    assert len(rejected.value.detail["corrections"]) == 3
    proposed["goals"][1]["quantity"] = "优化结构"
    proposed["goals"][2]["quantity"] = "振动频率"
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    assert len(rejected.value.detail["corrections"]) == 1
    proposed["unresolved"] = []
    run = submit(store, run, proposed)
    request = store.load_request(run)
    assert len(request.goals) == 4  # No requested input, science or explanation Goal dropped.
    assert request.goals[0].conditions == {}
    assert request.goals[0].minimum_check_version == "structure-prepare-3"
    assert [goal.quantity for goal in request.goals[1:3]] == ["优化结构", "振动频率"]
    assert all(goal.conditions["geometry_relation"] == "optimized" for goal in request.goals[1:3])
    assert all(goal.verification == "reported_with_limitations" for goal in request.goals[1:3])
    assert request.method is request.basis is None
    assert selectable_conditions(request) == ["method", "basis"]
    assert not run.calls and not run.attempts and not run.model_records


def test_native_intake_exposes_all_feedback_and_distinguishes_input_and_science(tmp_path):
    store, run, proposed = replay(tmp_path, 0)
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    feedback = {"category": "ProposalError", "requirement": rejected.value.detail}
    run = run.model_copy(update={"budget": run.budget.model_copy(update={
        "input_tokens": 48000, "model_tokens": 1200000})})
    request = store.load_request(run)
    prepared = build_context(request, run, delivery_snapshot=collect_delivery_snapshot(store, run, request),
        action_parameters=action_parameters(run.permission.allowed_tools, request=request,
                                           text_input=True, code_authorized=True),
        user_messages=store.read_control(run.id)["messages"], feedback={"validation_error": feedback})
    wire = json.loads(prepared.body()["messages"][1]["content"])
    assert "ACTION_SCHEMAS" in wire and "@rows" not in wire
    assert wire["CONTROL"]["validation_error"] == feedback
    rules = wire["INTENT_RULES"]["generated_task_intent"]
    assert "Input Goals omit geometry_relation" in rules
    assert "Every general scientific Goal must supply quantity" in rules
    assert "Request.conditions.explain_results=true" in rules
    assert "knowledge_answer is for independent knowledge questions" in rules
    assert not wire["AUTHORITY"].get("execution_resources")  # Intake grants no execution.
    assert prepared.input_token_bound <= 48000
