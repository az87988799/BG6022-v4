"""Meaning/provenance examples and new-question gates, without live execution."""

import copy
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import query_run, store_at
from test_semantic_control import candidate

from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.semantic import (
    LEXICAL_ALIASES,
    FieldEvidence,
    _field,
    action_parameters,
    commit_candidate,
)
from orca_agent.store import StoreError
from tests.helpers.phase_b_model_cases import create_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-proposals-v2.json"
V2 = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposals"]


@pytest.mark.parametrize("field,value,quote", [
    (field, value, alias) for (field, value), aliases in LEXICAL_ALIASES.items() for alias in aliases])
def test_protocol_lexicon_uses_the_same_explicit_aliases_as_field_validation(tmp_path, field, value, quote):
    store, run, _, _ = query_run(tmp_path)
    request = store.load_request(run)
    actual, origin, _ = _field(field, FieldEvidence(value=value, source="explicit", text_basis=quote),
                                request, [{"text": quote}])
    assert actual == value and origin == "explicit"
    aliases = action_parameters()["normalize_request"]["condition_lexicon"][field]
    assert any(mapped == value and quote in words for mapped, words in aliases)


def test_v2_real_shapes_reject_translated_quotes_and_unasked_inference_then_correct_offline(tmp_path):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-v2-replay")
    messages = store.read_control(run.id)["messages"]
    before = store.load_run(run.id).model_dump_json()
    first, second = [copy.deepcopy(item["parameters"]) for item in V2]
    for parameters in (first, second):
        parameters["message_ids"] = [messages[0]["id"]]
    with pytest.raises(StoreError, match="quote a supplied user message"):
        commit_candidate(store, run, first, decision_id="translated_quote", basis=current_basis(store, run))
    with pytest.raises(ProposalError, match="visible question") as caught:
        commit_candidate(store, run, second, decision_id="unasked_ground", basis=current_basis(store, run))
    assert caught.value.detail["path"] == ["parameters", "questions"]
    assert "unconfirmed:electronic_state" in caught.value.detail["new_unresolved"]
    assert "applicability:conflicting_condition:method/electronic_state" in caught.value.detail["new_unresolved"]
    assert store.load_run(run.id).model_dump_json() == before
    prepared = build_context(store.load_request(run), run, relevant_tools=[], user_messages=messages,
        action_parameters=action_parameters(),
        feedback={"validation_error": {"category": "ProposalError", "requirement": caught.value.detail}})
    contract = payload(prepared)["ACTION_PARAMETERS"]["normalize_request"]
    assert prepared.input_token_bound <= 12000
    assert "electronic_state means RHF/UHF reference" in contract["instruction"]
    assert "no translation, paraphrase or added parentheses" in contract["instruction"]
    assert "Energy needs no temperature_K/standard_state gap unless the user requires them" in contract["instruction"]
    assert "Absent unit:unknown" in contract["instruction"]
    assert [0, ["中性", "neutral"]] in contract["condition_lexicon"]["charge"]
    # This correction is authored by the test, not another model trajectory.
    second["conditions"]["electronic_state"] = {"value": "RHF", "source": "explicit", "text_basis": "RHF"}
    updated = commit_candidate(store, run, second, decision_id="offline_correct_reference", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and not request.unresolved
    assert request.conditions["electronic_state"] == "RHF"
    assert request.conditions_source["electronic_state"] == "explicit"
    assert request.conditions_source["charge"] == request.conditions_source["multiplicity"] == "explicit"
    assert not request.goals[0].unresolved
    assert not {"temperature_K", "standard_state"} & request.conditions.keys()
    assert not updated.model_records and not updated.calls and not updated.attempts


@pytest.mark.parametrize("questions", [[], [""], [" \t"], ["\u200b"]])
def test_new_uncertainty_requires_a_visible_question_before_activation(tmp_path, questions):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, "方法还没有确定。")
    before = store.load_run(run.id).model_dump_json()
    proposed = candidate(store, run, kind="amend",
        conditions={"method": {"value": None, "source": "unknown"}}, questions=questions)
    with pytest.raises(ProposalError) as caught:
        commit_candidate(store, run, proposed, decision_id="question_missing", basis=current_basis(store, run))
    assert caught.value.detail["path"] == ["parameters", "questions"]
    assert caught.value.detail["new_unresolved"] == ["unconfirmed:method"]
    assert store.load_run(run.id).model_dump_json() == before


def test_existing_gap_can_be_preserved_without_repeating_a_question(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, "方法尚未确定。")
    initial = commit_candidate(store, run, candidate(store, run, kind="amend",
        conditions={"method": {"value": None, "source": "unknown"}}, questions=["请确认方法。"]),
        decision_id="ask_method", basis=current_basis(store, run))
    store.enqueue_message(run.id, "继续保留这个未决问题。")
    updated = commit_candidate(store, initial, candidate(store, initial, kind="amend"),
        decision_id="retain_gap", basis=current_basis(store, initial))
    assert store.load_request(updated).unresolved == ["unconfirmed:method"]
    assert updated.usage == initial.usage
