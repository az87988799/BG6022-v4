"""Atomic stop/publication, crash replay and explicit reopen using synthetic HTTP."""

import copy
import hashlib
import json

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.report import build_report
from tests.unit.test_agent import ScriptedTransport, make_run


class Crash(BaseException):
    pass


def fail_once(stage):
    fired = False

    def fault(point):
        nonlocal fired
        if point == stage and not fired:
            fired = True
            raise Crash(stage)

    return fault


def make_partial(tmp_path):
    store, run, artifact = make_run(tmp_path, two_goals=True, initial_plan=True)
    # The existing plan reads a then b. A stop after a must not imply b passed.
    return store, run, artifact


def test_accepted_stop_state_applied_marker_and_receipt_are_one_saved_run(tmp_path):
    store, run, _ = make_partial(tmp_path)
    transport = ScriptedTransport({"action": "stop", "reason": "Synthetic partial delivery."})
    ended = agent.execute(store, Config(), run.id, transport=transport)
    persisted = store.load_run(run.id)
    assert ended == persisted and ended.state == "failed"
    assert len(ended.calls) == 1 and ended.goal_status["b"] != "satisfied"
    assert len(ended.terminal_deliveries) == 1
    receipt = ended.terminal_deliveries[0]
    assert receipt.decision_id in ended.applied_decisions
    assert receipt.contract_status == "passed" and receipt.report_status == "rendered"
    assert receipt.terminal_state == "failed"
    assert hashlib.sha256(store.path(receipt.report_path).read_bytes()).hexdigest() == receipt.report_sha256
    before = ended.model_dump(mode="json")
    replay = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert replay.model_dump(mode="json") == before


@pytest.mark.parametrize("stage", ["after_model_response_saved", "after_terminal_validated",
                                   "after_terminal_saved", "after_terminal_report_written"])
def test_crash_replay_finishes_same_delivery_without_another_http_or_read(tmp_path, stage):
    store, run, _ = make_partial(tmp_path)
    transport = ScriptedTransport({"action": "stop"})
    with pytest.raises(Crash):
        agent.execute(store, Config(), run.id, transport=transport, fault=fail_once(stage))
    interrupted = store.load_run(run.id)
    calls = len(interrupted.calls)
    recovered = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert recovered.state == "failed"
    assert recovered.usage.model_calls == 1 and len(recovered.calls) == calls == 1
    assert len(recovered.terminal_deliveries) == 1
    assert recovered.terminal_deliveries[0].report_status == "rendered"
    again = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert again == recovered


def test_explicit_reopen_redecides_before_remaining_ready_read(tmp_path):
    store, run, _ = make_partial(tmp_path)
    first = agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}))
    old = copy.deepcopy(first.terminal_deliveries[0].model_dump(mode="json"))

    def inspect_before_stop(data):
        current = store.load_run(run.id)
        assert len(current.calls) == 1 and current.goal_status["b"] != "satisfied"
        assert old["decision_id"] in current.reopened_terminal_ids
        return {"action": "stop"}

    ended = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport(inspect_before_stop))
    assert len(ended.calls) == 1 and ended.usage.model_calls == 2
    assert ended.terminal_deliveries[0].model_dump(mode="json") == old
    assert len(ended.terminal_deliveries) == 2


def test_historical_accepted_stop_is_audit_only_and_resume_redecides_before_ready_step(tmp_path):
    from orca_agent.model_usage import current_basis
    from orca_agent.tools.dispatch import execute_call
    store, run, _ = make_partial(tmp_path)
    plan = store.load_plan(run)
    step = plan.steps[0]
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    agent.runner._goals(store, run, plan, {step.id: result})
    run.processed_feedback = list(run.result_ids)
    old_reason = "Original pre-contract stop; retained without alteration."
    run.decisions.append({"id": "legacy_stop", "action": "stop", "basis": current_basis(store, run),
        "parameters": {"reason": "Historical format"}, "reason": old_reason,
        "related_results": list(run.result_ids)})
    run.state = "failed"
    store.save_run(run)
    historical = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert not historical.terminal_deliveries and len(historical.calls) == 1
    assert historical.usage.model_calls == 0

    def redecide(data):
        current = store.load_run(run.id)
        assert len(current.calls) == 1 and current.goal_status["b"] != "satisfied"
        assert "legacy_stop" in current.reopened_terminal_ids
        return {"action": "stop"}

    ended = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport(redecide))
    assert ended.usage.model_calls == 1 and len(ended.calls) == 1
    assert ended.decisions[0]["reason"] == old_reason
    assert len(ended.terminal_deliveries) == 1
    assert ended.terminal_deliveries[0].decision_id != "legacy_stop"


@pytest.mark.parametrize("stage", ["after_terminal_saved", "after_terminal_report_written"])
def test_new_user_message_supersedes_interrupted_publication_on_explicit_resume(tmp_path, stage):
    store, run, _ = make_partial(tmp_path)
    with pytest.raises(Crash):
        agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}),
                      fault=fail_once(stage))
    accepted = store.load_run(run.id).terminal_deliveries[0].decision_id
    message_id = store.enqueue_message(run.id, "Cancel")
    ended = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert message_id in ended.processed_messages and ended.state == "cancelled"
    assert accepted in ended.reopened_terminal_ids
    assert ended.terminal_deliveries[0].report_status == "failed"
    assert ended.usage.model_calls == 1 and len(ended.calls) == 1
    again = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert again.state == "cancelled" and again.usage.model_calls == 1 and len(again.calls) == 1


def test_old_v06_style_stop_is_rejected_then_one_valid_correction_preserves_raw_reply(tmp_path):
    store, run, _ = make_partial(tmp_path)

    def corrected(data):
        from tests.unit.test_terminal_contract import valid_parameters
        return {"action": "stop", "parameters": valid_parameters(data["DATA"]["delivery"])}

    original = {"action": "stop", "parameters": {}, "reason":
                "goal evidence is sufficient to answer within sampled range"}
    ended = agent.execute(store, Config(), run.id,
        transport=ScriptedTransport(original, corrected, terminal_contract=False))
    assert ended.state == "failed" and ended.usage.model_calls == 2
    assert [item["action"] for item in ended.decisions] == ["rejected", "stop"]
    first = ended.model_records[0]
    raw = json.loads(store.path(f"runs/{run.id}/model/{first['id']}.response.json").read_text())
    assert raw["proposal"]["reason"] == original["reason"]
    assert raw["proposal"]["parameters"] == {}
    assert ended.terminal_deliveries[0].decision_id == ended.model_records[1]["id"]


def test_repeated_contract_failure_preserves_science_facts_and_explanation_failure(tmp_path):
    store, run, _ = make_partial(tmp_path)
    transport = ScriptedTransport({"action": "stop"}, {"action": "stop"}, terminal_contract=False)
    ended = agent.execute(store, Config(), run.id, transport=transport)
    assert ended.usage.model_calls == 2 and len(ended.calls) == 1
    assert ended.goal_status["a"] == "satisfied" and ended.goal_status["b"] != "satisfied"
    assert not ended.terminal_deliveries and ended.state == "failed"
    assert not build_report(store, ended)["user_goal_complete"]
    assert len(ended.fallback_report_receipts) == 1
    report = build_report(store, ended)
    assert report["model_explanation"]["current_status"] == "rejected"
    assert report["report_artifact"]["status"] == "rendered"
    assert not ended.terminal_deliveries


def test_nonstop_report_publication_is_idempotent_and_cannot_accept_a_terminal(tmp_path):
    from orca_agent.terminal import active_terminal, publish_terminal_report
    store, run, _ = make_partial(tmp_path)
    before = run.usage.model_dump(mode="json")
    with pytest.raises(Crash):
        publish_terminal_report(store, run, fault=fail_once("after_terminal_report_written"))
    saved = store.load_run(run.id)
    assert saved.fallback_report_receipts[0].report_status == "pending"
    recovered = publish_terminal_report(store, saved)
    assert active_terminal(recovered) is None and not recovered.applied_decisions
    assert recovered.state == "ready" and recovered.usage.model_dump(mode="json") == before
    receipt = recovered.fallback_report_receipts[0]
    assert receipt.report_status == "rendered"
    assert hashlib.sha256(store.path(receipt.report_path).read_bytes()).hexdigest() == receipt.report_sha256
    assert publish_terminal_report(store, recovered) == recovered
    assert len(recovered.fallback_report_receipts) == 1


def test_pure_terminal_cannot_escape_delivery_by_inventing_a_clarification(tmp_path):
    from orca_agent.models import PermissionSnapshot, Request
    store, original, _ = make_partial(tmp_path)
    request = Request(original_text="Read the requested fields; report any lack of evidence.",
                      goals=store.load_request(original).goals, conditions={"explain_results": True})
    run = store.create_run(request, None, PermissionSnapshot(model_execution=True, allowed_tools=[]), original.budget)
    run.agent_enabled = True
    store.save_run(run)
    transport = ScriptedTransport({"action": "clarify", "parameters": {
        "questions": ["May I have more budget?"], "unresolved": ["budget"]}}, {"action": "stop"})
    ended = agent.execute(store, Config(), run.id, transport=transport)
    assert transport.sent[0]["AUTHORITY"]["decision_purpose"]["allowed_actions"] == ["stop"]
    assert [item["action"] for item in ended.decisions] == ["rejected", "stop"]
    assert ended.usage.model_calls == 2 and ended.state == "failed"
    assert ended.terminal_deliveries[0].contract_status == "passed"
    assert not any(item.get("category") == "clarification" for item in ended.diagnostics)


def test_published_report_tampering_is_not_adopted_on_replay(tmp_path):
    store, run, _ = make_partial(tmp_path)
    with pytest.raises(Crash):
        agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}),
                      fault=fail_once("after_terminal_report_written"))
    saved = store.load_run(run.id)
    receipt = saved.terminal_deliveries[0]
    original_hash = receipt.report_sha256
    store.path(receipt.report_path).write_text("changed", encoding="utf-8")
    recovered = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert recovered.terminal_deliveries[0].report_status == "failed"
    assert recovered.terminal_deliveries[0].report_sha256 == original_hash
    assert recovered.usage.model_calls == 1 and len(recovered.calls) == 1


def test_readonly_report_detects_changed_publication_without_rewriting_accepted_receipt(tmp_path):
    store, run, _ = make_partial(tmp_path)
    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}))
    before = store.load_run(run.id).model_dump(mode="json")
    receipt = ended.terminal_deliveries[0]
    store.path(receipt.report_path).write_text("tampered report", encoding="utf-8")
    report = build_report(store, ended)
    assert report["model_explanation"]["current"] is True
    assert report["report_artifact"]["status"] == "failed" and not report["report_artifact"]["current"]
    assert report["report_artifact"]["error"] == "report_source_unverified"
    assert store.load_run(run.id).model_dump(mode="json") == before


def test_current_source_change_keeps_old_receipt_but_suppresses_verified_answer(tmp_path):
    store, run, artifact = make_partial(tmp_path)
    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}))
    before = copy.deepcopy(ended.terminal_deliveries[0].model_dump(mode="json"))
    store.artifact_path(artifact.id).write_text('{"a": 999, "b": 2}', encoding="utf-8")
    report = build_report(store, ended)
    assert not report["user_goal_complete"]
    assert report["goal_facts"][0]["answer"] is None
    assert store.load_run(run.id).terminal_deliveries[0].model_dump(mode="json") == before


def test_source_change_between_acceptance_and_publication_cannot_publish_verified_delivery(tmp_path):
    store, run, artifact = make_partial(tmp_path)

    def alter_before_publication(stage):
        if stage == "after_terminal_saved":
            store.artifact_path(artifact.id).write_text('{"a": 999, "b": 2}', encoding="utf-8")

    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}),
                          fault=alter_before_publication)
    receipt = ended.terminal_deliveries[0]
    assert receipt.contract_status == "passed" and receipt.report_status == "failed"
    assert receipt.report_error == "source_or_basis_unverified" and receipt.report_path is None
    report = build_report(store, ended)
    assert not report["model_explanation"]["current"]
    assert report["goal_facts"][0]["answer"] is None
    assert ended.usage.model_calls == 1


def test_source_change_after_validation_prevents_terminal_acceptance(tmp_path):
    store, run, artifact = make_partial(tmp_path)

    def alter_before_acceptance(stage):
        if stage == "after_terminal_validated":
            store.artifact_path(artifact.id).write_text('{"a": 999, "b": 2}', encoding="utf-8")

    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}),
                          fault=alter_before_acceptance)
    assert not ended.terminal_deliveries
    assert not any(item["action"] == "stop" for item in ended.decisions)
    assert ended.usage.model_calls == 1
    report = build_report(store, ended)
    assert not report["user_goal_complete"]
    assert report["goal_facts"][0]["answer"] is None


@pytest.mark.parametrize("change,state", [("pause", "paused"), ("cancel", "cancelled"),
                                           ("message", "waiting_user")])
def test_control_change_after_validation_prevents_atomic_terminal_acceptance(tmp_path, change, state):
    store, run, _ = make_partial(tmp_path)

    def control_change(stage):
        if stage != "after_terminal_validated":
            return
        if change == "message":
            store.enqueue_message(run.id, "The second field is still required.")
        else:
            store.signal(run.id, change)

    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport({"action": "stop"}),
                          fault=control_change)
    assert ended.state == state and not ended.terminal_deliveries
    assert len(ended.calls) == ended.usage.model_calls == 1
    assert not any(item["action"] == "stop" for item in ended.decisions)
