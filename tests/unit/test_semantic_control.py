"""Synthetic semantic proposals test boundaries, not real model understanding."""

import json

import pytest
from test_agent import ScriptedTransport, initial_proposal, make_run
from test_natural import bundle_at, query_run, store_at

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.natural import apply_user_update, initialize_bundle
from orca_agent.semantic import VERSION, commit_candidate
from orca_agent.store import ControlChanged, StoreError


def candidate(store, run, **values):
    messages = [m for m in store.read_control(run.id)["messages"] if m["id"] not in run.processed_messages]
    return {"schema_version": VERSION, "message_ids": [m["id"] for m in messages],
            "kind": "clarify", "text_basis": messages[0]["text"], **values}


def test_raw_text_creates_unknown_request_before_first_model_reservation(tmp_path, monkeypatch):
    from orca_agent import doctor
    monkeypatch.setattr(doctor, "diagnose", lambda _: pytest.fail("intake probed ORCA"))
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None,
                           scientific_execution=True, text="优化乙醇，给出优化后的电子能"))
    request = store.load_request(run)
    assert request.normalization_status == "pending"
    assert request.charge is request.method is request.multiplicity is request.basis is None
    assert not run.model_records and run.usage.model_calls == 0 and run.budget.orca_starts == 4
    proposal = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize",
        goals=[{"key": "ethanol", "port": "energy", "text_basis": request.original_text,
                "geometry_relation": "optimized", "unresolved": ["unsupported_system:ethanol"]}],
        unresolved=["ethanol is outside the supported registered water/methane systems"],
        questions=["请提供当前范围支持的体系或保留未支持目标。"])}
    transport = ScriptedTransport(proposal)
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "waiting_user"
    assert stopped.usage.model_calls == 1 and stopped.usage.model_tokens_used == 75
    assert not stopped.attempts and stopped.usage.orca_starts_actual == 0
    normalized = store.load_request(stopped)
    assert normalized.goals[0].conditions["geometry_relation"] == "optimized"
    assert normalized.goals[0].original_text == request.original_text
    assert stopped.processed_messages == proposal["parameters"]["message_ids"]


def test_raw_text_to_request_to_plan_to_query_one_loop(tmp_path):
    store = store_at(tmp_path)
    raw = tmp_path / "raw.json"
    raw.write_text('{"a": 3}')
    artifact = store.import_artifact(raw, "synthetic_test_evidence")
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None,
        text="读取已有文件的a字段", artifact_ids=[artifact.id]))
    normalize = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize",
        goals=[{"key": "read", "port": "value_observation", "text_basis": "读取已有文件的a字段",
                "query": {"artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}}])}
    transport = ScriptedTransport(normalize, initial_proposal)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed", completed.diagnostics
    assert completed.usage.model_calls == 2 and completed.usage.evidence_reads == 1
    assert completed.usage.plan_revisions == 0 and not completed.attempts


def test_continue_consumes_message_without_a_model_and_keeps_budget(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "继续，沿用之前的全部条件")
    completed = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert completed.state == "completed", completed.diagnostics
    assert completed.processed_messages == [message]
    assert completed.usage.model_calls == completed.usage.plan_revisions == 0
    assert completed.usage.evidence_reads == 1 and completed.deadline == run.deadline


@pytest.mark.parametrize("text", ["现在是什么状态", "status"])
def test_status_message_does_not_advance_ready_plan_or_use_model_budget(tmp_path, text):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, text)
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert stopped.state == run.state
    assert not stopped.calls and stopped.usage.model_calls == 0


def test_negated_continue_pauses_and_explicit_resume_does_not_deadlock(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "不要继续")
    paused = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert paused.state == "paused" and paused.processed_messages == [message]
    assert not paused.calls
    completed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert completed.state == "completed", completed.diagnostics
    assert completed.usage.evidence_reads == 1 and completed.usage.model_calls == 0


def test_request_revision_and_message_consumption_activate_together_after_crash(tmp_path):
    store, run, _, plan = query_run(tmp_path)
    store.enqueue_message(run.id, "那个结果改一下，但我还没决定条件")
    parameters = candidate(store, run, unresolved=["ambiguous_condition"], questions=["需要什么条件？"])
    basis = current_basis(store, run)

    def crash(point):
        if point == "after_revision_saved":
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        commit_candidate(store, run, parameters, decision_id="semantic_crash", basis=basis, fault=crash)
    unchanged = store.load_run(run.id)
    assert unchanged.plan_id == plan.id and unchanged.processed_messages == []
    with pytest.raises(ControlChanged):
        store.reserve_call(unchanged, plan.steps[0].tool, plan.steps[0].parameters.model_dump(), plan.steps[0])
    updated = commit_candidate(store, unchanged, parameters, decision_id="semantic_crash", basis=basis)
    assert updated.plan_id is None and store.load_request(updated).unresolved == ["ambiguous_condition"]
    assert updated.processed_messages == parameters["message_ids"]
    assert updated.usage == run.usage
    replayed = store.commit_revision(updated, None, decision_id="semantic_crash", basis=basis)
    assert replayed == updated


def test_user_may_replace_goal_but_ordinary_model_revision_cannot(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "把能量改为优化结构")
    old = store.load_request(run)
    new_goal = {"id": "geometry", "port": "optimized_geometry", "minimum_check_version": "orca-hf-2",
                "original_text": "把能量改为优化结构"}
    updated = apply_user_update(store, run.id, message, {"goals": [new_goal]})
    assert store.load_request(updated).goals[0].id == "geometry"
    assert store.load_request_revision(updated, 1) == old
    assert updated.usage == run.usage and updated.permission == run.permission
    assert updated.plan_id is None
    changed = store.load_request(updated).model_copy(deep=True)
    changed.version += 1
    changed.goals = old.goals
    with pytest.raises(ValueError, match="model cannot"):
        store.commit_revision(updated, None, request=changed, decision_id="illegal_model_change",
                              basis=current_basis(store, updated))


@pytest.mark.parametrize("changes", [{"permission": {}}, {"budget": {}}, {"goals": []}])
def test_semantic_schema_cannot_import_authority(tmp_path, changes):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, "请继续")
    with pytest.raises(ValueError):
        commit_candidate(store, run, candidate(store, run, **changes), decision_id="invalid",
                         basis=current_basis(store, run))


def test_explicit_field_needs_value_quote_and_default_needs_configuration(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, "电荷为1")
    for field in ({"value": 0, "source": "explicit", "text_basis": "电荷为1"},
                  {"value": 0, "source": "default", "default_rule": "local-hf-1"}):
        with pytest.raises(StoreError):
            commit_candidate(store, run, candidate(store, run, kind="amend", conditions={"charge": field}),
                             decision_id="invalid", basis=current_basis(store, run))


@pytest.mark.parametrize("text,basis_text", [
    ("不要改变目标，仍算单点能", "改变目标"),
    ("不要改为优化结构", "改为优化结构"),
    ("Do not replace energy with geometry", "replace energy with geometry"),
])
def test_model_cannot_turn_a_negated_goal_change_into_user_authority(tmp_path, text, basis_text):
    store, run, _, _ = query_run(tmp_path)
    store.enqueue_message(run.id, text)
    with pytest.raises(StoreError, match="explicit user replacement"):
        commit_candidate(store, run, candidate(store, run, kind="replace_goals", text_basis=basis_text,
            replaces=["read"], goals=[{"key": "geometry", "port": "optimized_geometry",
                                       "text_basis": text}]),
                         decision_id="invalid_goal_change", basis=current_basis(store, run))
    assert store.load_run(run.id) == run


def test_unrelated_explicit_field_cannot_clear_unsupported_system(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "乙醇需求先保持未决")
    run = apply_user_update(store, run.id, message, {"unresolved": ["unsupported_system:ethanol"]})
    store.enqueue_message(run.id, "方法用HF")
    with pytest.raises(StoreError, match="resolved ambiguity"):
        commit_candidate(store, run, candidate(store, run, kind="amend",
            conditions={"method": {"value": "HF", "source": "explicit", "text_basis": "方法用HF"}},
            resolves=["unsupported_system:ethanol"]),
                         decision_id="unrelated_answer", basis=current_basis(store, run))
    assert store.load_request(run).unresolved == ["unsupported_system:ethanol"]


def test_new_message_after_plan_activation_prevents_ready_read(tmp_path):
    store, run, _ = make_run(tmp_path)

    def message(point):
        if point == "after_revision_activated":
            store.enqueue_message(run.id, "不要继续")

    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport(initial_proposal), fault=message)
    assert stopped.state == "paused"
    assert stopped.usage.evidence_reads == 0 and not stopped.calls


def test_new_message_blocks_reservation_without_acknowledging_generation(tmp_path):
    store, run, _, plan = query_run(tmp_path)
    store.enqueue_message(run.id, "不要读取a，条件需要修改")
    with pytest.raises(ControlChanged):
        store.reserve_call(run, plan.steps[0].tool, plan.steps[0].parameters.model_dump(), plan.steps[0])
    assert store.load_run(run.id).control_generation == 0
    assert store.load_run(run.id).calls == []


def test_message_after_tool_reservation_blocks_implementation_and_retains_cost(tmp_path, monkeypatch):
    from orca_agent.tools import evidence
    store, run, _, _ = query_run(tmp_path)
    monkeypatch.setattr(evidence, "read_value", lambda *a, **k: pytest.fail("read started"))

    def message(point):
        if point == "after_call_reserved":
            store.enqueue_message(run.id, "条件已经变化")

    stopped = agent.execute(store, Config(), run.id, fault=message, transport=ScriptedTransport())
    assert stopped.state == "waiting_user"
    assert stopped.usage.evidence_reads == 1 and len(stopped.calls) == 1
    result = store.load_result(run.id, stopped.calls[0].result_id)
    assert result.operation_status == "cancelled" and result.source["not_started"]
    assert not result.observations


def test_update_file_is_only_queued_by_cli_and_status_does_not_enqueue(tmp_path, monkeypatch, capsys):
    from orca_agent.cli import main
    store, run, _, _ = query_run(tmp_path)
    monkeypatch.setattr("orca_agent.cli.load_config", lambda _: Config(data_root=store.root))
    path = tmp_path / "update.json"
    path.write_text(json.dumps({"unresolved": ["need confirmation"]}))
    assert main(["message", run.id, "补充条件", "--update-file", str(path)]) == 0
    assert store.load_run(run.id).request_version == 1
    before = store.read_control(run.id)
    assert main(["message", run.id, "现在是什么状态"]) == 0
    assert store.read_control(run.id) == before
    capsys.readouterr()
