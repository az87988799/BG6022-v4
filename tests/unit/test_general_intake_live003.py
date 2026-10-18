"""Replay full live-003 responses without model, HTTP, or calculation calls."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_context import payload
from test_general_intake_correction import environment

from orca_agent.condition_selection import selectable_conditions
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters, commit_candidate

FIXTURE = Path(__file__).parents[1] / "fixtures/code_intake_live003.json"
DIGEST = "c4ee8d7702888b8a139bf5ed4de9507558833e80630661865e62b3394750137a"


def replay(tmp_path, index):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DIGEST
    fixture = json.loads(raw)
    store, _, run = environment(tmp_path, fixture["user_text"])
    response = fixture["replies"][index]["response"]
    assert json.loads(response["raw_content"]) == response["proposal"]
    message_id = store.read_control(run.id)["messages"][0]["id"]
    def rebind(value):
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value
    return store, run, rebind(response["proposal"]["parameters"])


def submit(store, run, parameters):
    return commit_candidate(store, run, parameters, decision_id="offline_normalize", basis=current_basis(store, run))


@pytest.mark.parametrize("index", [0, 1, 2])
def test_full_real_replies_remain_rejected_with_actionable_exact_paths(tmp_path, index):
    store, run, proposed = replay(tmp_path, index)
    unchanged = copy.deepcopy(proposed)
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, proposed)
    corrections = rejected.value.detail["corrections"]
    assert all(item["path"][0] == "parameters" and len(item["instruction"]) > 80 for item in corrections)
    assert all("ethanol" not in item["instruction"].casefold() and "B3LYP" not in item["instruction"]
               for item in corrections)
    if index < 2:
        assert len(corrections) == 1
        item = corrections[0]
        assert item["path"] == ["parameters", "unresolved", 0]
        assert item["reason"] == "available_selection_or_observation_action_is_not_a_missing_user_answer"
        assert "Remove this unresolved entry" in item["instruction"]
        assert "unresolved=[] or omit unresolved" in item["instruction"]
        assert "Keep automatic_selection, all Goals and minimum evidence unchanged" in item["instruction"]
    else:
        paths = {tuple(item["path"]) for item in corrections}
        assert paths >= {("parameters", "goals", 1, "quantity"), ("parameters", "goals", 2, "quantity")}
        # The first Goal is registered input preparation. Its historical false
        # demand for a scientific optimized relation must not be repeated.
        assert not any(path[:3] == ("parameters", "goals", 0) for path in paths)
    feedback = {"category": "ProposalError", "requirement": rejected.value.detail}
    projected = run.model_copy(update={"budget": run.budget.model_copy(update={"input_tokens": 48000,
                                                                              "model_tokens": 1200000})})
    request = store.load_request(run)
    prepared = build_context(request, projected, relevant_tools=[],
        user_messages=store.read_control(run.id)["messages"],
        action_parameters=action_parameters(run.permission.allowed_tools, request=request,
                                            text_input=True, code_authorized=True),
        feedback={"validation_error": feedback})
    assert payload(prepared)["CONTROL"]["validation_error"] == feedback
    assert proposed == unchanged and run.request_version == 1 and not run.processed_messages
    assert not run.calls and not run.attempts and not run.model_records
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST


@pytest.mark.parametrize("mode", ["empty", "omit"])
def test_clearing_only_false_pending_gap_accepts_first_reply_and_preserves_notices(tmp_path, mode):
    store, run, proposed = replay(tmp_path, 0)
    before = copy.deepcopy(proposed)
    if mode == "empty":
        proposed["unresolved"] = []
    else:
        proposed.pop("unresolved")
    assert {key: value for key, value in proposed.items() if key != "unresolved"} == {
        key: value for key, value in before.items() if key != "unresolved"}
    updated = submit(store, run, proposed)
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and not request.unresolved
    assert selectable_conditions(request) == ["method", "basis"]
    assert request.method is request.basis is None
    assert [(goal.quantity, goal.conditions["geometry_relation"], goal.verification) for goal in request.goals] == [
        ("优化结构", "optimized", "reported_with_limitations"),
        ("振动频率", "optimized", "reported_with_limitations")]
    assert updated.decisions[-1]["semantics"]["candidate"]["notices"] == before["notices"]
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST
