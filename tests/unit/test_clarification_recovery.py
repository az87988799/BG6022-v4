"""Synthetic decisions verify durable clarification/control gates, without a model or ORCA."""

import pytest
from test_agent import ScriptedTransport, make_run
from test_natural import bundle_at, store_at

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.natural import initialize_bundle
from orca_agent.semantic import VERSION, FieldEvidence, _field, commit_candidate
from orca_agent.store import ControlChanged, StoreError

CLARIFY = {"action": "clarify", "parameters": {
    "questions": ["请确认电荷。"], "unresolved": ["unconfirmed:charge"]}}


def waiting_run(tmp_path, *, fault=None):
    store, run, artifact = make_run(tmp_path, two_goals=True, initial_plan=True)
    transport = ScriptedTransport(CLARIFY)
    stopped = agent.execute(store, Config(), run.id, transport=transport, fault=fault)
    assert len(transport.sent) == 1 and stopped.usage.model_calls == 1
    assert stopped.usage.evidence_reads == 1 and len(stopped.calls) == 1
    assert store.active_clarification(stopped)["unresolved"] == ["unconfirmed:charge"]
    return store, stopped, artifact


def test_repeated_resume_without_answer_neither_calls_model_nor_executes_ready_step(tmp_path):
    store, stopped, artifact = waiting_run(tmp_path)
    usage = stopped.usage.model_dump()
    for _ in range(3):
        stopped = agent.execute(store, Config(), stopped.id, resume=True, transport=ScriptedTransport())
        assert stopped.state == "waiting_user"
        assert stopped.usage.model_dump() == usage
        assert len(stopped.calls) == 1
    with pytest.raises(ControlChanged, match="unanswered clarification"):
        store.reserve_call(stopped, "evidence.value", {"artifact_id": artifact.id})
    assert store.load_run(stopped.id).usage.model_dump() == usage


@pytest.mark.parametrize("text", ["继续", "status"])
def test_control_only_message_does_not_answer_clarification(tmp_path, text):
    store, stopped, _ = waiting_run(tmp_path)
    usage = stopped.usage.model_dump()
    marker = store.active_clarification(stopped)
    message = store.enqueue_message(stopped.id, text)
    resumed = agent.execute(store, Config(), stopped.id, resume=True, transport=ScriptedTransport())
    assert resumed.state == "waiting_user" and message in resumed.processed_messages
    assert resumed.usage.model_dump() == usage
    assert store.active_clarification(resumed) == marker


def test_crash_after_clarification_decision_is_already_a_durable_blocker(tmp_path):
    def crash(point):
        if point == "after_clarification_saved":
            raise KeyboardInterrupt()

    store, stopped, _ = waiting_run(tmp_path, fault=crash)
    assert stopped.state == "unknown"
    resumed = agent.execute(store, Config(), stopped.id, resume=True, transport=ScriptedTransport())
    assert resumed.state == "waiting_user"
    assert resumed.usage == stopped.usage and len(resumed.calls) == 1


def test_grounded_user_answer_resolves_the_active_question_and_suspends_old_plan(tmp_path):
    store, stopped, _ = waiting_run(tmp_path)
    message = store.enqueue_message(stopped.id, "电荷为1")
    updated = commit_candidate(store, stopped, {
        "schema_version": VERSION, "message_ids": [message], "kind": "amend",
        "text_basis": "电荷为1", "conditions": {
            "charge": {"value": 1, "source": "explicit", "text_basis": "电荷为1"}},
        "resolves": ["unconfirmed:charge"]},
        decision_id="answer_charge", basis=current_basis(store, stopped))
    assert store.active_clarification(updated) is None
    assert updated.plan_id is None and updated.request_version == stopped.request_version + 1
    assert store.load_request(updated).charge == 1
    assert updated.usage == stopped.usage and updated.permission == stopped.permission
    store.check_execution_control(updated)


def test_control_record_cannot_forge_clarification_resolution(tmp_path):
    store, stopped, _ = waiting_run(tmp_path)
    message = store.enqueue_message(stopped.id, "继续")
    marker = store.active_clarification(stopped)
    with pytest.raises(StoreError, match="authenticated new Request"):
        store.commit_revision(stopped, store.load_plan(stopped), decision_id="fake_answer",
            basis=current_basis(store, stopped), user_message_ids=[message],
            semantic_record={"kind": "continue", "resolved_clarification_id": marker["id"]})
    assert store.active_clarification(store.load_run(stopped.id)) == marker


def test_unrelated_grounded_amendment_preserves_the_active_question(tmp_path):
    store, stopped, _ = waiting_run(tmp_path)
    message = store.enqueue_message(stopped.id, "基组为STO-3G")
    marker = store.active_clarification(stopped)
    updated = commit_candidate(store, stopped, {
        "schema_version": VERSION, "message_ids": [message], "kind": "amend",
        "text_basis": "基组为STO-3G", "conditions": {
            "basis": {"value": "STO-3G", "source": "explicit", "text_basis": "基组为STO-3G"}}},
        decision_id="unrelated_basis", basis=current_basis(store, stopped))
    assert store.active_clarification(updated) == marker
    assert "unconfirmed:charge" in store.load_request(updated).unresolved
    with pytest.raises(ControlChanged, match="unanswered clarification"):
        store.check_execution_control(updated)


def test_answered_question_can_be_replaced_by_a_new_persisted_clarification(tmp_path):
    store, stopped, _ = waiting_run(tmp_path)
    message = store.enqueue_message(stopped.id, "电荷为1")
    updated = commit_candidate(store, stopped, {
        "schema_version": VERSION, "message_ids": [message], "kind": "clarify",
        "text_basis": "电荷为1", "conditions": {
            "charge": {"value": 1, "source": "explicit", "text_basis": "电荷为1"}},
        "resolves": ["unconfirmed:charge"], "unresolved": ["field:environment"],
        "questions": ["请确认计算环境。"]},
        decision_id="question_environment", basis=current_basis(store, stopped))
    assert store.active_clarification(updated) is None
    assert store.load_request(updated).unresolved == ["field:environment"]
    with pytest.raises(ControlChanged, match="unresolved user conditions"):
        store.check_execution_control(updated)
    for _ in range(2):
        updated = agent.execute(store, Config(), stopped.id, resume=True, transport=ScriptedTransport())
        assert updated.state == "waiting_user"
        assert updated.usage == stopped.usage and len(updated.calls) == 1
    answer = store.enqueue_message(stopped.id, "气相")
    resolved = commit_candidate(store, updated, {
        "schema_version": VERSION, "message_ids": [answer], "kind": "amend",
        "text_basis": "气相", "conditions": {
            "environment": {"value": "gas_phase", "source": "explicit", "text_basis": "气相"}},
        "resolves": ["field:environment"]},
        decision_id="answer_environment", basis=current_basis(store, updated))
    assert not store.load_request(resolved).unresolved
    assert store.active_clarification(resolved) is None
    store.check_execution_control(resolved)
    assert resolved.usage == stopped.usage and resolved.request_version == stopped.request_version + 2


def test_pending_user_clarify_does_not_restart_model_on_resume(tmp_path):
    store, run, _ = make_run(tmp_path, initial_plan=True)
    message = store.enqueue_message(run.id, "还没确定电荷")
    # Pending messages use the advertised normalization contract, including
    # its clarify kind; a top-level legacy clarify is not an intake action.
    candidate = {"action": "normalize_request", "parameters": {
        "schema_version": VERSION, "message_ids": [message], "kind": "clarify",
        "text_basis": "还没确定电荷", **CLARIFY["parameters"]}}
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport(candidate))
    assert stopped.state == "waiting_user" and stopped.processed_messages == [message]
    assert store.active_clarification(stopped) is None
    assert store.load_request(stopped).normalization_status == "clarification"
    assert not stopped.calls and stopped.usage.model_calls == 1
    for _ in range(2):
        stopped = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert stopped.state == "waiting_user" and stopped.usage.model_calls == 1
        assert not stopped.calls


def test_initial_structured_missing_conditions_still_get_the_first_model_question(tmp_path):
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=[{
        "id": "energy", "port": "energy", "minimum_check_version": "orca-hf-2"}]))
    assert store.load_request(run).normalization_status == "clarification"
    assert not run.decisions
    transport = ScriptedTransport(CLARIFY)
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "waiting_user" and len(transport.sent) == 1
    assert stopped.usage.model_calls == 1 and not stopped.calls and not stopped.attempts
    resumed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert resumed.state == "waiting_user" and resumed.usage.model_calls == 1


@pytest.mark.parametrize("command, expected", [("暂停", "paused"), ("取消", "cancelled")])
@pytest.mark.parametrize("window", ["after_revision_saved", "after_revision_activated"])
def test_control_message_replays_each_crash_window_once(tmp_path, command, expected, window):
    store, run, artifact = make_run(tmp_path, initial_plan=True, agent_enabled=False)
    message = store.enqueue_message(run.id, command)
    generation = store.read_control(run.id)["generation"]

    def crash(point):
        if point == window:
            raise KeyboardInterrupt()

    stopped = agent.execute(store, Config(), run.id, fault=crash, transport=ScriptedTransport())
    assert not stopped.calls and stopped.usage.model_calls == 0
    assert store.read_control(run.id)["generation"] == generation
    # Plain re-entry must finish the same control decision, not resume the job.
    replayed = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert replayed.state == expected and replayed.processed_messages == [message]
    assert len(replayed.decisions) == 1 and not replayed.calls
    assert replayed.usage.model_dump(exclude={"resource_usage_complete"}) == run.usage.model_dump(
        exclude={"resource_usage_complete"})
    with pytest.raises(ControlChanged, match="paused or cancelled"):
        store.reserve_call(replayed, "evidence.value", {"artifact_id": artifact.id})
    for _ in range(2):
        replayed = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
        assert replayed.state == expected and len(replayed.decisions) == 1
        assert not replayed.calls
    resumed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert resumed.state == ("completed" if expected == "paused" else "cancelled")
    assert resumed.usage.evidence_reads == (1 if expected == "paused" else 0)
    assert resumed.usage.model_calls == 0 and len(resumed.decisions) == 1


@pytest.mark.parametrize("command, expected", [("暂停", "paused"), ("取消", "cancelled")])
def test_explicit_resume_finishes_unactivated_control_message_before_any_work(tmp_path, command, expected):
    store, run, _ = make_run(tmp_path, initial_plan=True, agent_enabled=False)
    message = store.enqueue_message(run.id, command)

    def crash(point):
        if point == "after_revision_saved":
            raise KeyboardInterrupt()

    agent.execute(store, Config(), run.id, fault=crash, transport=ScriptedTransport())
    recovered = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert recovered.state == expected and recovered.processed_messages == [message]
    assert not recovered.calls and recovered.usage.model_calls == 0
    assert len(recovered.decisions) == 1


def test_external_pause_signal_retains_explicit_resume_support(tmp_path):
    store, run, _ = make_run(tmp_path, initial_plan=True, agent_enabled=False)
    store.signal(run.id, "pause")
    paused = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert paused.state == "paused" and not paused.calls
    completed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert completed.state == "completed" and completed.usage.evidence_reads == 1
    assert store.read_signal(run.id) is None


@pytest.mark.parametrize("text, expected", [("取消", "cancelled"), ("cancel", "cancelled"),
                                           ("电荷改为1", "paused")])
def test_paused_run_only_consumes_exact_queued_cancel_without_resume(tmp_path, text, expected):
    store, run, _ = make_run(tmp_path, initial_plan=True, agent_enabled=False)
    pause = store.enqueue_message(run.id, "暂停")
    paused = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    message = store.enqueue_message(run.id, text)
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert stopped.state == expected and stopped.usage == paused.usage
    assert stopped.request_version == paused.request_version and stopped.plan_id == paused.plan_id
    assert stopped.processed_messages == ([pause, message] if expected == "cancelled" else [pause])
    assert not stopped.calls and not stopped.attempts


@pytest.mark.parametrize("name,value,text,quote", [
    ("charge", 1, "电荷不是1", "电荷不是1"),
    ("charge", 1, "电荷不是1", "1"),
    ("charge", 1, "多重度为1", "多重度为1"),
    ("multiplicity", 1, "电荷为1", "电荷为1"),
    ("charge", 0, "非中性", "中性"),
    ("charge", 0, "可提出中性单重态的推断建议", "中性"),
    ("multiplicity", 1, "不是单重态", "单重态"),
    ("method", "HF", "不要用HF", "HF"),
    ("basis", "STO-3G", "STO-3G不是我要的基组", "STO-3G"),
    ("environment", "gas_phase", "不要气相", "气相"),
    ("charge", 0, "charge is not 0", "0"),
    ("multiplicity", 1, "Do not use multiplicity 1", "multiplicity 1"),
    ("method", "HF", "Maybe HF", "HF"),
    ("charge", 1, "重试1次", "重试1次"),
    ("multiplicity", 1, "multiplicity 1.5", "multiplicity 1.5"),
])
def test_explicit_fields_reject_wrong_numeric_association_and_clipped_negation(
        tmp_path, name, value, text, quote):
    store, run, _ = make_run(tmp_path)
    with pytest.raises(StoreError, match="explicit"):
        _field(name, FieldEvidence(value=value, source="explicit", text_basis=quote),
               store.load_request(run), [{"text": text}])


@pytest.mark.parametrize("name,value,text", [
    ("charge", 0, "电荷0、多重度1"), ("multiplicity", 1, "电荷0、多重度1"),
    ("charge", 0, "中性单重态"), ("multiplicity", 1, "中性单重态"),
    ("method", "HF", "RHF/STO-3G"), ("basis", "STO-3G", "RHF/STO-3G"),
    ("electronic_state", "RHF", "RHF/STO-3G"),
    ("charge", 1, "charge is +1, multiplicity is 1"),
    ("multiplicity", 1, "charge is +1, multiplicity is 1"),
    ("charge", -1, "电荷为-1，多重度为1"),
    ("method", "HF", "不要优化，使用RHF/STO-3G算单点能"),
])
def test_explicit_field_standard_positive_forms_remain_supported(tmp_path, name, value, text):
    store, run, _ = make_run(tmp_path)
    observed, source, _ = _field(name, FieldEvidence(value=value, source="explicit", text_basis=text),
                                  store.load_request(run), [{"text": text}])
    assert observed == value and source == "explicit"
