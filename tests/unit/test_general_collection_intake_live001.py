"""Retained failed live intake; offline replay changes no historical response."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_general_intake_correction import environment

from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, Request, SystemInput
from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters, commit_candidate

FIXTURE = Path(__file__).parents[1] / "fixtures/code_intake_step5_live001.json"
DIGEST = "78b1ad4cd1e356d00ea01abf64a8f9f49ffe0eb0c3430175135b03a0aaf6486a"


def replay(tmp_path):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DIGEST
    fixture = json.loads(raw)
    store, _, run = environment(tmp_path, fixture["user_text"])
    message_id = store.read_control(run.id)["messages"][0]["id"]

    def rebind(value):
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value

    return store, run, rebind(fixture["proposal"]["parameters"])


def test_real_reply_reaches_general_evidence_validation_without_legacy_sampling_guard(tmp_path):
    store, run, proposed = replay(tmp_path)
    before = copy.deepcopy(proposed)
    with pytest.raises(ProposalError) as rejected:
        commit_candidate(store, run, proposed, decision_id="offline_replay", basis=current_basis(store, run))
    detail = rejected.value.detail
    correction = next(row for row in detail["corrections"] if
                      row["path"] == ["parameters", "goals", 0, "minimum_evidence", 2])
    assert correction["reason"] == "minimum_evidence_requires_exact_user_wording"
    assert correction["invalid_value"] == "初始结构保留"
    assert "保留初始结构" in correction["user_text"]
    assert "contiguous exact quote" in correction["instruction"]
    assert "delete a requirement" in correction["instruction"]
    assert "affirmative matching user intent" not in str(rejected.value)
    assert proposed == before and not run.decisions and not run.calls and not run.attempts
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST


def test_test_authored_quote_correction_preserves_all_four_general_collection_goals_and_evidence(tmp_path):
    store, run, proposed = replay(tmp_path)
    # This new candidate fixes only the model's non-verbatim quote. Original
    # requested contents and evidence obligations remain intact and unresolved
    # evidence will still require its own evaluator at delivery time.
    proposed["goals"][0]["minimum_evidence"][2] = "保留初始结构"
    before = copy.deepcopy(proposed)
    updated = commit_candidate(store, run, proposed, decision_id="offline_corrected", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert [goal.port for goal in request.goals] == ["sampling", "member_table", "sampling_check", "energy_difference"]
    assert request.normalization_status == "normalized" and not request.unresolved
    assert request.systems[0].identity["contract"] == "general-identity-3"
    for goal, declared in zip(request.goals, proposed["goals"], strict=True):
        assert goal.quantity == declared["quantity"]
        assert goal.minimum_check_version == goal.evidence_contract_version == "generic-observation-1"
        assert goal.minimum_evidence == declared["minimum_evidence"]
        assert goal.verification == "reported_with_limitations"
        assert not goal.unresolved and "sampling" not in goal.conditions
    assert proposed == before and not updated.calls and not updated.attempts and not updated.model_records
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST


def test_named_sampling_goal_retains_explicit_scientific_minimum(tmp_path):
    store, run, proposed = replay(tmp_path)
    proposed["goals"][0]["minimum_evidence"][2] = "保留初始结构"
    proposed["goals"][0]["minimum_evidence"].append("converged_scf@1")
    updated = commit_candidate(store, run, proposed, decision_id="offline_minimum", basis=current_basis(store, run))
    goal = store.load_request(updated).goals[0]
    assert goal.evidence_contract_version == "generic-observation-1"
    assert goal.minimum_evidence[-1] == "converged_scf@1"


def test_general_intake_guidance_scopes_legacy_sp_opt_question():
    request = Request(goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1")],
                      systems=[SystemInput(id="target", geometry_source="prepare")])
    contract = action_parameters(request=request, text_input=True, code_authorized=True)["normalize_request"]
    assert "ask SP vs Opt only when absent" not in contract["input_acquisition"]
    assert "legacy scalar energy goals still require their geometry relation" in contract["input_acquisition"]
    assert "omit geometry_relation" in contract["generated_task_intent"]
    assert "Never change an explicit geometry requirement" in contract["generated_task_intent"]
