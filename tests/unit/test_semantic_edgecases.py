"""Adversarial semantic candidates and recovery remain offline and bounded."""

from datetime import timedelta

import pytest
from test_agent import ScriptedTransport
from test_natural import bundle_at, query_run, store_at
from test_semantic_control import candidate

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, Request, SystemInput
from orca_agent.natural import apply_user_update, initialize_bundle
from orca_agent.semantic import FieldEvidence, _field, commit_candidate
from orca_agent.store import StoreError


def request_for_fields(**changes):
    return Request(goals=[Goal(id="q", port="value_observation", minimum_check_version="evidence-read-1")],
                   **changes)


@pytest.mark.parametrize("name,value,text", [("method", "HF", "UHF"),
    ("charge", 0, "电荷10"), ("multiplicity", 1, "multiplicity 10")])
def test_explicit_scientific_tokens_do_not_match_substrings(name, value, text):
    with pytest.raises(StoreError, match="lexical|basis|explicit"):
        _field(name, FieldEvidence(value=value, source="explicit", text_basis=text),
               request_for_fields(), [{"text": text}])


@pytest.mark.parametrize("name,value,text", [("method", "HF", "使用HF方法"),
    ("charge", 0, "中性"), ("multiplicity", 1, "单重态")])
def test_explicit_scientific_tokens_keep_supported_positive_forms(name, value, text):
    observed, source, proof = _field(name, FieldEvidence(value=value, source="explicit", text_basis=text),
                                    request_for_fields(), [{"text": text}])
    assert observed == value and source == "explicit" and proof["text_basis"] == text


def test_inferred_value_cannot_be_laundered_by_inheritance():
    request = request_for_fields(method="HF", conditions_source={"method": "inferred"})
    with pytest.raises(StoreError, match="inherited|confirmed|source"):
        _field("method", FieldEvidence(value="HF", source="inherited", request_version=request.version),
               request, [{"text": "沿用之前的条件"}])


def test_system_inferred_value_cannot_be_laundered_by_inheritance():
    request = request_for_fields(systems=[SystemInput(id="water", conditions={"charge": 0},
                                                       conditions_source={"charge": "inferred"})])
    with pytest.raises(StoreError, match="inherited|confirmed|source"):
        _field("charge", FieldEvidence(value=0, source="inherited", request_version=request.version,
                                        system_ref="water"), request, [{"text": "沿用"}])


def test_unique_confirmed_inheritance_preserves_source_version():
    request = request_for_fields(method="HF", conditions_source={"method": "explicit"})
    value, source, proof = _field("method", FieldEvidence(value="HF", source="inherited",
        request_version=request.version), request, [{"text": "沿用之前的条件"}])
    assert value == "HF" and source == "inherited" and proof["request_version"] == request.version
    with pytest.raises(StoreError, match="current Request version"):
        _field("method", FieldEvidence(value="HF", source="inherited", request_version=0),
               request, [{"text": "沿用之前的条件"}])


def test_authorized_default_only_fills_absent_condition():
    request = request_for_fields(method=None, conditions_source={}, semantic_defaults={"method": "HF"})
    default = FieldEvidence(value="HF", source="default", default_rule="local-hf-1")
    assert _field("method", default, request, [{"text": "计算能量"}])[:2] == ("HF", "default")
    explicit = request_for_fields(method="UHF", conditions_source={"method": "explicit"},
                                  semantic_defaults={"method": "HF"})
    with pytest.raises(StoreError, match="default|explicit|condition"):
        _field("method", default, explicit, [{"text": "继续"}])
    unknown = request_for_fields(method=None, conditions_source={"method": "unknown"},
                                 semantic_defaults={"method": "HF"})
    with pytest.raises(StoreError, match="unknown|unconfirmed"):
        _field("method", default, unknown, [{"text": "暂时不知道方法"}])


@pytest.mark.parametrize("source,value", [("inferred", "HF"), ("unknown", None)])
def test_uncertain_conditions_persist_with_provenance_and_blocking_fact(tmp_path, source, value):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, "可能用HF，尚待确认")
    parameters = candidate(store, run, kind="amend", conditions={"method": {
        "value": value, "source": source, "text_basis": "可能用HF，尚待确认"}}, questions=["是否确认采用HF？"])
    updated = commit_candidate(store, run, parameters, decision_id="uncertain", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.method == value and request.conditions_source["method"] == source
    assert "unconfirmed:method" in request.unresolved
    assert request.normalization_status == "clarification"
    assert updated.plan_id is None and updated.usage == run.usage


def test_initial_clarification_accepts_normal_answer_without_goal_replacement_phrase(tmp_path):
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text="帮我读取一下"))
    first = commit_candidate(store, run, candidate(store, run, kind="clarify",
        unresolved=["missing_query_field"], questions=["要读取哪个字段？"]),
        decision_id="clarification", basis=current_basis(store, run))
    assert store.load_request(first).normalization_status in {"pending", "clarification"}
    store.enqueue_message(run.id, "读取a字段")
    second = commit_candidate(store, first, candidate(store, first, kind="normalize", goals=[{
        "key": "a", "port": "value_observation", "text_basis": "读取a字段"}]),
        decision_id="answer", basis=current_basis(store, first))
    assert store.load_request(second).goals[0].id == "goal_a"
    assert second.usage == run.usage and len(second.processed_messages) == 2


def test_raw_multi_system_energy_is_split_without_pretyped_goals_or_conditions(tmp_path):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nsynthetic geometry\nO 0 0 0\nH 0 0.8 0.6\nH 0 -0.8 0.6\n")
    (tmp_path / "methane.xyz").write_text("5\nsynthetic geometry\nC 0 0 0\nH 1 1 1\nH -1 -1 1\nH -1 1 -1\nH 1 -1 -1\n")
    text = "计算水和甲烷的电子能"
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text, geometries=[
        {"id": "water", "file": "water.xyz"}, {"id": "methane", "file": "methane.xyz"}]))
    updated = commit_candidate(store, run, candidate(store, run, kind="normalize", goals=[{
        "key": "both", "port": "energy", "text_basis": text, "system_refs": ["water", "methane"],
        "geometry_relation": "fixed_initial"}], questions=["请确认各体系的方法、基组、电荷与多重度。"]),
        decision_id="split", basis=current_basis(store, run))
    goals = store.load_request(updated).goals
    assert [(g.id, g.system_ids, g.required) for g in goals] == [
        ("goal_both_water", ["water"], True), ("goal_both_methane", ["methane"], True)]
    assert all(g.original_text == text for g in goals)
    assert not updated.attempts and not updated.calls


def test_all_optional_normalization_rejected(tmp_path):
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text="读取a"))
    with pytest.raises(ValueError, match="mandatory"):
        commit_candidate(store, run, candidate(store, run, kind="normalize", goals=[{
            "key": "a", "port": "value_observation", "text_basis": "读取a", "required": False}]),
            decision_id="optional", basis=current_basis(store, run))
    assert store.load_run(run.id).processed_messages == []


def test_answer_to_superseded_question_does_not_consume_or_activate(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    change = store.enqueue_message(run.id, "先明确新的请求")
    old_answer = store.enqueue_message(run.id, "继续沿用之前的条件")
    updated = apply_user_update(store, run.id, change, {"unresolved": ["new_question"]})
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(StoreError, match="older Request"):
        commit_candidate(store, updated, candidate(store, updated, kind="amend"),
                         decision_id="old_answer", basis=current_basis(store, updated))
    assert store.load_run(run.id).model_dump_json() == before
    assert old_answer not in updated.processed_messages


def test_deadline_after_wait_allows_consumption_but_no_new_tool_or_model(tmp_path, monkeypatch):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, "继续")
    monkeypatch.setattr(agent, "utc_now", lambda: run.deadline + timedelta(seconds=1))
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert stopped.state == "budget_exhausted" and not stopped.calls and not stopped.model_records
    assert stopped.deadline == run.deadline
    restarted = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert restarted.usage == stopped.usage and restarted.deadline == stopped.deadline
    assert restarted.processed_messages == stopped.processed_messages
