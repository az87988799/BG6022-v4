"""Historical N05 shapes receive program notices without changing science."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_natural import store_at

from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.semantic import commit_candidate
from tests.helpers.phase_b_model_cases import create_request
from tests.helpers.semantic_replay import current_candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-unsupported-v6.json"
REJECTED = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposals"]
GAP = "applicability:unsupported_condition:environment"
NOTICE = "已保留您明确要求的水溶剂环境；当前科学能力仅支持气相，水溶剂目标仍未满足。本轮仅登记，不启动计算。"


@pytest.mark.parametrize("entry", REJECTED, ids=[entry["model_record_id"] for entry in REJECTED])
def test_derived_n05_proposals_receive_notice_without_changing_requested_solvent(tmp_path, entry):
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
    original_science = copy.deepcopy(proposed)
    updated = commit_candidate(store, run, proposed, decision_id="program_notice", basis=current_basis(store, run))
    normalized = store.load_request(updated)
    assert normalized.normalization_status == "normalized"
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
    assert saved["notices"] == []
    assert saved["questions"] == []
    generated = updated.decisions[-1]["semantics"]["notice_contract"]["generated"]
    assert generated[0]["gap"] == GAP and generated[0]["source"] == "program"
    assert not updated.decisions[-1]["semantics"]["awaiting_reply"]
    assert proposed == original_science
    assert not updated.permission.scientific_execution
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert updated.usage.model_calls == updated.usage.orca_starts_actual == 0


@pytest.mark.parametrize("questions", [[""], [" \t\n"], ["\u200b"], ["\x00\u200d"]])
def test_program_notice_does_not_make_an_invisible_model_question_valid(tmp_path, questions):
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
    assert "questions" in rejected.value.detail["path"]
    assert store.load_run(run.id).model_dump_json() == before
