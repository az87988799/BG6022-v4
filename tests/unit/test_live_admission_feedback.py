"""Offline mechanical admission repair and real malformed-response diagnostics.

The writer is real; the managed producer is a labelled synthetic archived
receipt. No task Python, ORCA, HTTP or live model is started.
"""

import copy
import json
from pathlib import Path

import pytest
import test_simplified_file_references as files
from test_agent import ScriptedTransport

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.decision_purpose import decision_identity, pending_admission_error
from orca_agent.file_references import FileReferenceError
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    Goal,
    ModelLimits,
    PermissionSnapshot,
    Request,
    fingerprint,
)
from orca_agent.store import Store


def admission_case(tmp_path, monkeypatch):
    config = Config(data_root=tmp_path / "data", workflow_policy="simplified-1",
                    mechanical_progression=True, model=ModelLimits(input_tokens=64000, output_tokens=3000))

    def model_case(path):
        store = Store(config.data_root, environment_root=path / "environment",
                      configuration=config.run_configuration())
        request = Request(goals=[Goal(id="report", port="artifacts",
            evidence_contract_version="generic-observation-1",
            minimum_check_version="generic-observation-1", verification="reported_with_limitations")])
        run = store.create_run(request, None, PermissionSnapshot(
            model_execution=True, code_execution=True, artifact_writes=True,
            allowed_tools=["workspace.write", "python.execute", "evidence.text"]),
            BudgetLimits(analysis_executions=2, plan_revisions=2, model_calls=4,
                evidence_reads=2, input_tokens=64000, output_tokens=3000, model_tokens=300000, decision_rounds=12))
        run.agent_enabled = True
        store.save_run(run)
        return store, run, request, []

    monkeypatch.setattr(files, "_case", model_case)
    store, run, plan, produced, _ = files._managed_result_case(tmp_path, file_key="missing.json")
    # The prior producer is a synthetic settled receipt, already reviewed.
    # Leave only the actual immutable writer receipt as mechanical feedback.
    run.processed_feedback = [produced.id]
    runner._goals(store, run, plan, runner._step_results(store, run))
    store.save_run(run)
    return store, config, run, plan


@pytest.mark.parametrize("repair", [False, True])
def test_real_writer_prelaunch_reference_failure_returns_to_model(tmp_path, monkeypatch, repair):
    store, config, run, plan = admission_case(tmp_path, monkeypatch)
    before_attempts = copy.deepcopy(run.attempts)
    writer_result = store.load_result(run.id, run.calls[0].result_id)
    original = store.path(f"runs/{run.id}/results/{writer_result.id}.json").read_bytes()

    def respond(data):
        error = data["CONTROL"]["validation_error"]
        assert error["category"] == "step_admission_rejected"
        assert error["step_id"] == plan.steps[-1].id
        detail = error["requirement"]
        assert detail["code"] == "producer_file_missing"
        assert detail["requested_file_key"] == "missing.json"
        assert detail["available_file_keys"] == ["report.json"]
        assert detail["path"] == ["parameters", "inputs", "entries", 0]
        assert detail["producer_step_id"] == plan.steps[1].id
        if not repair:
            return {"action": "stop", "parameters": {"reason": "Explicit missing-file gap."}}
        return {"action": "revise_plan", "parameters": {
            "steps": [{"key": step.id} for step in plan.steps[:2]] + [{
                "key": "write_gap", "tool": "workspace.write", "parameters": {
                    "name": "gap.txt", "role": "text", "content": "Missing producer output; no execution retried."}}],
            "goal_map": {"report": {"port": "artifacts", "gap": "Missing original producer output."}}}}

    def stop_after_revision(data):
        assert not data["CONTROL"].get("validation_error")  # Old-basis feedback is not a new rejection.
        return {"action": "stop", "parameters": {"reason": "Recorded missing-output limitation."}}

    transport = ScriptedTransport(respond, *([stop_after_revision] if repair else []))
    ended = agent.execute(store, config, run.id, transport=transport)
    assert not transport.scripts, ended.diagnostics
    rejected = [row for row in ended.diagnostics if row["category"] == "step_admission_rejected"]
    assert len(rejected) == 1 and rejected[0]["execution_started"] is False
    assert any(row.get("selection") == "mechanical-feedback-1" and
               row["parameters"]["step_id"] == plan.steps[-1].id for row in ended.decisions)
    assert rejected[0]["decision_id"] in ended.applied_decisions
    assert ended.attempts == before_attempts and all(not item.started for item in ended.attempts)
    assert store.environment_lease() is None and ended.usage.orca_starts_actual == 0
    assert ended.usage.model_calls == len(transport.sent) == (2 if repair else 1)
    assert ended.delivery_status == "partial" and ended.goal_status["report"] == "insufficient_evidence"
    assert store.path(f"runs/{run.id}/results/{writer_result.id}.json").read_bytes() == original
    assert len(ended.calls) == (2 if repair else 1)


@pytest.mark.parametrize("timing", ["already_pending", "new_pending"])
def test_admission_feedback_never_recovers_unsettled_execution(tmp_path, monkeypatch, timing):
    store, _, run, plan = admission_case(tmp_path, monkeypatch)
    pending = run.attempts[0].model_copy(update={"id": "attempt_unsettled", "state": "unknown",
                                               "finished_at": None, "result_id": None})
    if timing == "already_pending":
        run.attempts.append(pending)
    before = ([item.id for item in run.attempts], [item.id for item in run.calls],
              [item.request_key for item in run.internal_calls])
    if timing == "new_pending":
        run.attempts.append(pending)
    unchanged = run.model_dump(mode="json")
    original_error = FileReferenceError("Fixture missing file", code="producer_file_missing")
    with pytest.raises(FileReferenceError) as raised:
        agent._record_admission_error(store, run, plan.steps[-1], None, original_error, before)
    assert raised.value is original_error and run.model_dump(mode="json") == unchanged
    assert not any(row["category"] == "step_admission_rejected" for row in run.diagnostics)
    assert run.usage.model_calls == 0


@pytest.mark.parametrize("model_id", ["model_3636e802c8f748159d8a5d51a78397b7",
                                     "model_9abaebddc04e48b9a6a06fa770d5f3e9"])
def test_actual_t2_json_failures_preserve_parser_position_and_bounded_window(model_id):
    path = (Path(__file__).parents[2] / "docs/acceptance/code-as-tools-step7-refreeze-4/evidence/T2/data/runs"
            / "run_42acda94c6034af1b73af6b91a501ecd/model" / f"{model_id}.response.json")
    raw_record = path.read_bytes()
    reply = json.loads(raw_record)
    assert reply["error_category"] == "invalid_proposal_json"
    raw = reply["raw_content"]
    with pytest.raises(json.JSONDecodeError) as malformed:
        json.loads(raw)
    error = malformed.value
    detail = agent._proposal_json_error(reply)
    assert (detail["line"], detail["column"], detail["character"]) == (error.lineno, error.colno, error.pos)
    window = detail["rejected_data_window"]
    assert window["text"] == raw[window["start"]:window["end"]]
    assert len(window["text"]) <= 320 and window["total_characters"] == len(raw)
    assert window["start"] <= error.pos <= window["end"] and window["truncated"]
    assert path.read_bytes() == raw_record


def test_pending_admission_error_requires_exact_current_basis(tmp_path, monkeypatch):
    store, _, run, plan = admission_case(tmp_path, monkeypatch)
    before = ([item.id for item in run.attempts], [item.id for item in run.calls], [])
    agent._record_admission_error(store, run, plan.steps[-1], None,
        FileReferenceError("Fixture missing file", code="producer_file_missing"), before)
    basis = current_basis(store, run)
    assert pending_admission_error(run, basis) is not None
    assert pending_admission_error(run, {**basis, "plan_version": basis["plan_version"] + 1}) is None


def test_rejected_repair_uses_persisted_admission_identity(tmp_path, monkeypatch):
    from test_rejected_proposal_context import correction_feedback

    from orca_agent.context import _rejected_proposal_view

    store, _, run, plan = admission_case(tmp_path, monkeypatch)
    before = ([item.id for item in run.attempts], [item.id for item in run.calls], [])
    agent._record_admission_error(store, run, plan.steps[-1], None,
        FileReferenceError("Fixture missing file", code="producer_file_missing"), before)
    proposal = {"action": "revise_plan", "parameters": {"steps": []}}
    feedback, basis = correction_feedback(run, proposal)
    identity = decision_identity(run, basis, [])
    assert identity["admission_id"] == run.diagnostics[-1]["admission_id"]
    logical = "decision_" + fingerprint(identity)[:24]
    run.model_records[-1]["logical_id"] = logical + "_1"
    feedback["rejected_proposal"]["logical_request_id"] = logical
    feedback["admission_id"] = "untrusted_feedback_value"
    assert _rejected_proposal_view(feedback, run, basis, limit=16384)["proposal"] == proposal
    run.diagnostics[-1]["admission_id"] = "another_persisted_admission"
    assert _rejected_proposal_view(feedback, run, basis, limit=16384) is None


def test_saved_admission_restarts_with_feedback_and_successful_read_consumes_it(tmp_path, monkeypatch):
    store, config, run, _ = admission_case(tmp_path, monkeypatch)
    before_attempts = copy.deepcopy(run.attempts)
    artifact_id = store.load_result(run.id, run.calls[0].result_id).artifact_ids[0]
    original = agent._record_admission_error

    def interrupt_after_save(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(agent, "_record_admission_error", interrupt_after_save)
    interrupted = agent.execute(store, config, run.id, transport=ScriptedTransport())
    assert interrupted.state == "unknown" and interrupted.usage.model_calls == 0
    assert pending_admission_error(interrupted, current_basis(store, interrupted)) is not None
    monkeypatch.setattr(agent, "_record_admission_error", original)

    def inspect_error(data):
        assert data["CONTROL"]["validation_error"]["requirement"]["code"] == "producer_file_missing"
        return {"action": "call_tool", "parameters": {"tool": "evidence.text", "parameters": {
            "artifact_id": artifact_id, "start_line": 1, "lines": 2}}}

    def stop_after_read(data):
        assert not data["CONTROL"].get("validation_error")
        loaded = store.load_run(run.id)
        assert loaded.usage.evidence_reads == 1
        assert pending_admission_error(loaded, current_basis(store, loaded)) is None
        return {"action": "stop", "parameters": {"reason": "Missing-file limit remains explicit."}}

    transport = ScriptedTransport(inspect_error, stop_after_read)
    ended = agent.execute(store, config, run.id, transport=transport, resume=True)
    assert not transport.scripts, ended.diagnostics
    assert ended.attempts == before_attempts and ended.usage.orca_starts_actual == 0
    assert ended.usage.model_calls == 2 and ended.usage.evidence_reads == 1
    assert len([row for row in ended.diagnostics if row["category"] == "step_admission_rejected"]) == 1


def test_admission_decision_count_is_checked_without_upgrading_history(tmp_path, monkeypatch):
    store, _, run, plan = admission_case(tmp_path, monkeypatch)
    before = ([item.id for item in run.attempts], [item.id for item in run.calls], [])
    agent._record_admission_error(store, run, plan.steps[-1], None,
        FileReferenceError("Fixture missing file", code="producer_file_missing"), before)
    basis = current_basis(store, run)
    for count in (True, -1, len(run.decisions) + 1):
        run.diagnostics[-1]["decision_count"] = count
        with pytest.raises(ValueError, match="admission decision count"):
            pending_admission_error(run, basis)
    run.diagnostics[-1].pop("decision_count")
    assert pending_admission_error(run, basis) is run.diagnostics[-1]
