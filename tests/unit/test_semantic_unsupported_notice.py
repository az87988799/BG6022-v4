"""Actual rejected N05 shapes stay rejected; notices below are test-authored."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import store_at

from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters, commit_candidate
from tests.helpers.phase_b_model_cases import create_request
from tests.helpers.semantic_replay import current_candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-unsupported-v6.json"
REJECTED = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposals"]
GAP = "applicability:unsupported_condition:environment"
NOTICE = "已保留您明确要求的水溶剂环境；当前科学能力仅支持气相，水溶剂目标仍未满足。本轮仅登记，不启动计算。"


@pytest.mark.parametrize("entry", REJECTED, ids=[entry["model_record_id"] for entry in REJECTED])
def test_derived_n05_proposals_require_separate_notice_without_changing_requested_solvent(tmp_path, entry):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-05/raw-unsupported-solvent", 1,
                            category="development", freeze_label="offline-unsupported-notice")
    assert hashlib.sha256(entry["raw_content"].encode("utf-8")).hexdigest() == entry["raw_content_sha256"]
    proposed = copy.deepcopy(json.loads(entry["raw_content"])["parameters"])
    messages = store.read_control(run.id)["messages"]
    # Explicit test derivation updates transport identity and schema only.
    # Original scientific fields, quotations and missing notice stay unchanged.
    proposed["message_ids"] = [messages[0]["id"]]
    proposed = current_candidate(proposed)
    assert proposed["questions"] == [] and proposed["unresolved"] == []
    assert proposed["conditions"]["environment"]["value"] == "water_solvent"
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError, match="parameters.notices") as rejected:
        commit_candidate(store, run, proposed, decision_id="original_shape", basis=current_basis(store, run))
    error = rejected.value.detail
    assert error["path"] == ["parameters", "questions"]
    assert error["new_unresolved"] == [GAP]
    assert "visible question" in error["requirement"] and "unsupported-scope notice" in error["requirement"]
    assert "Preserve explicit user choices" in error["requirement"]
    assert store.load_run(run.id).model_dump_json() == before

    request = store.load_request(run)
    prepared = build_context(request, run, relevant_tools=[], user_messages=messages,
        action_parameters=action_parameters(run.permission.allowed_tools, request=request),
        feedback={"validation_error": {"category": "ProposalError", "requirement": error}})
    data = payload(prepared)
    instruction = data["ACTION_PARAMETERS"]["normalize_request"]["instruction"]
    policy = prepared.body()["messages"][0]["content"]
    assert "science_scope: capability limits, not permission/defaults" in instruction
    assert "New gaps need visible questions text" in policy
    assert "declarative notices in notices" in policy and "neither implies the other" in policy
    assert "no reply, confirmation or resource request" in policy
    assert data["CONTROL"]["validation_error"]["requirement"] == error
    assert prepared.input_token_bound <= run.budget.input_tokens == 12000

    # No punctuation/question-mark requirement is introduced. This is an
    # explicit developer correction, not an accepted live model response.
    original_science = copy.deepcopy(proposed)
    proposed["questions"] = []
    proposed["notices"] = [NOTICE]
    assert "?" not in NOTICE and "？" not in NOTICE
    updated = commit_candidate(store, run, proposed, decision_id="developer_notice", basis=current_basis(store, run))
    normalized = store.load_request(updated)
    assert normalized.normalization_status == "clarification"
    assert normalized.original_text == messages[0]["text"]
    assert normalized.conditions["environment"] == "water_solvent"
    assert normalized.conditions_source["environment"] == "explicit"
    assert normalized.goals[0].conditions["environment"] == "water_solvent"
    assert normalized.goals[0].unresolved == [GAP]
    assert normalized.goals[0].port == "energy"
    assert normalized.goals[0].conditions["geometry_relation"] == "fixed_initial"
    assert normalized.goals[0].minimum_evidence == ["converged_scf@1"]
    saved = updated.decisions[-1]["semantics"]["candidate"]
    for name in original_science["conditions"]:
        assert saved["conditions"][name]["value"] == original_science["conditions"][name]["value"]
        assert saved["conditions"][name]["source"] == original_science["conditions"][name]["source"]
    assert saved["notices"] == [NOTICE]
    assert saved["questions"] == []
    assert not updated.permission.scientific_execution
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert updated.usage.model_calls == updated.usage.orca_starts_actual == 0


@pytest.mark.parametrize("questions", [[], [""], [" \t\n"], ["\u200b"], ["\x00\u200d"]])
def test_unsupported_notice_must_contain_visible_text(tmp_path, questions):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-05/raw-unsupported-solvent", 1,
                            category="development", freeze_label="offline-empty-notice")
    proposed = json.loads(REJECTED[0]["raw_content"])["parameters"]
    proposed["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
    proposed = current_candidate(proposed)
    proposed["questions"] = questions
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError) as rejected:
        commit_candidate(store, run, proposed, decision_id="empty_notice", basis=current_basis(store, run))
    assert rejected.value.detail["path"] == ["parameters", "questions"]
    assert rejected.value.detail["new_unresolved"] == [GAP]
    assert store.load_run(run.id).model_dump_json() == before
