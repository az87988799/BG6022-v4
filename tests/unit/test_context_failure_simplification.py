"""Historical raw evidence and synthetic transport replay; no model/ORCA/network."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_agent import ScriptedTransport, initial_proposal, make_run
from test_context import objects, payload

from orca_agent import agent, context, runner
from orca_agent.llm import ModelReply, ModelUsage, proposal_recovery_kind
from orca_agent.models import Result, fingerprint
from orca_agent.store import StoreError

ROOT = Path(__file__).resolve().parents[2]
H2_ARCHIVE = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-2/review/H2-feedback-audit.json"


@pytest.mark.parametrize("profile", ["disabled", "thinking_low"])
def test_recorded_h2_short_traceback_is_complete_at_12k(profile):
    raw = H2_ARCHIVE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "16bcaf3364906d736052260ed906a2f7508497ce08cf4ae53266dfa368c23b86"
    window = json.loads(raw)["full_tool_results"][0]["text_window"]
    assert len(window["lines"]) == 10 and window["coverage"]["complete"]
    request, run = objects()
    result = Result(run_id=run.id, operation_status="completed",
                    artifact_ids=[window["artifact_id"]], observations={"text_window": window})
    run.result_ids = [result.id]
    before = result.model_dump(mode="json")
    prepared = context.build_context(request, run, results=[result], model_profile=profile)
    shown = payload(prepared)["DATA"]["results"][0]["unqualified_observations"]["text_window"]
    assert prepared.input_token_bound <= run.budget.input_tokens == 12000
    assert [row["line"] for row in shown["lines"]] == list(range(1, 11))
    assert shown["lines"][-1]["text"] == window["lines"][-1]["text"]
    assert "int(lines[0].split()[0])" in json.dumps(shown)
    assert shown["coverage"] == window["coverage"]
    assert "context_page" not in shown
    assert result.model_dump(mode="json") == before


def test_long_traceback_keeps_task_frame_and_tail_with_explicit_omissions():
    texts = ["Traceback (most recent call last):",
             '  File "/work/sealed/task.py", line 29, in <module>', "    parse_input()"]
    texts += [f'  File "/library/module{i}.py", line {i}, in helper' for i in range(180)]
    texts += ["ValueError: invalid literal"]
    lines = [{"line": i + 1, "text": text} for i, text in enumerate(texts)]
    observation = {"artifact_id": "stderr", "sha256": "a" * 64,
                   "view": {"type": "raw_text", "version": "1"}, "lines": lines,
                   "coverage": {"complete": True}}
    original = copy.deepcopy(observation)
    shown = context._observations({"text_window": observation}, 256)["text_window"]
    selected = {row["line"]: row for row in shown["lines"]}
    assert 2 in selected and 3 in selected and len(lines) in selected
    assert selected[len(lines)]["text"] == "ValueError: invalid literal"
    assert len(json.dumps(shown["lines"], separators=(",", ":")).encode()) <= 4096
    assert shown["context_page"]["selection"] == "traceback frames and exception tail"
    assert shown["context_page"]["omitted_from_result"] == len(lines) - len(selected)
    assert shown["context_page"]["next_line"] not in selected
    assert observation == original


class EmptyLengthTransport(ScriptedTransport):
    def __init__(self, empty_count=1, *, category="truncated", **kwargs):
        super().__init__(initial_proposal, **kwargs)
        self.empty_count = empty_count
        self.category = category
        self.requests = []

    def send(self, prepared, *, reserve, settle):
        self.requests.append(prepared)
        if self.empty_count:
            self.empty_count -= 1
            ticket = reserve(prepared)
            self.sent.append(payload(prepared))
            self.tickets.append(ticket)
            reply = ModelReply(request_hash=prepared.request_hash, raw_content="", proposal=None,
                error_category=self.category, finish_reason="length", http_status=200,
                usage=ModelUsage(prompt_tokens=50, completion_tokens=25, total_tokens=75),
                response_hash=fingerprint({"offline_failure": self.category}),
                response_model="offline-fake", retryable=False)
            settle(ticket, reply)
            return reply
        return super().send(prepared, reserve=reserve, settle=settle)


def workflow_run(tmp_path, *, enabled=True, corrections=1):
    store, original, artifact = make_run(tmp_path)
    configuration = original.configuration.model_copy(update={
        "workflow_policy": "simplified-1" if enabled else None})
    budget = original.budget.model_copy(update={"corrections_per_proposal": corrections})
    run = store.create_run(store.load_request(original), None, original.permission, budget,
                           configuration=configuration)
    run.agent_enabled = True
    runner._goals(store, run, None, {})
    store.save_run(run)
    return store, run, artifact


def test_empty_length_recovers_one_smaller_action_with_existing_budget(tmp_path):
    store, run, _ = workflow_run(tmp_path)
    budget = run.budget.model_dump()
    transport = EmptyLengthTransport()
    completed, action, _ = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "plan"
    assert completed.usage.model_calls == 2 and completed.usage.model_tokens_used == 150
    assert completed.budget.model_dump() == budget
    rejected = [d for d in completed.decisions if d.get("action") == "rejected"]
    assert len(rejected) == 1 and rejected[0]["parameters"]["recovery_kind"] == "smaller_action"
    assert rejected[0]["parameters"]["error_category"] == "truncated"
    assert transport.sent[1]["CONTROL"]["validation_error"]["smaller_action"]
    assert "at most one Step" in transport.requests[1].body()["messages"][0]["content"]
    assert transport.requests[1].output_token_bound <= run.budget.output_tokens
    assert store.load_request(completed).goals[0].id == "a"
    assert not completed.attempts and completed.usage.orca_starts_reserved == 0


@pytest.mark.parametrize("enabled,corrections,empty_count,expected", [
    (False, 3, 1, 1), (True, 0, 1, 1), (True, 3, 2, 2),
])
def test_empty_length_is_not_unbounded_or_reenabled_by_resume(
        tmp_path, enabled, corrections, empty_count, expected):
    store, run, _ = workflow_run(tmp_path, enabled=enabled, corrections=corrections)
    transport = EmptyLengthTransport(empty_count)
    with pytest.raises(StoreError, match="bounded proposal"):
        agent._decision(store, run, None, {}, transport, None, None)
    restored = store.load_run(run.id)
    assert restored.usage.model_calls == expected
    replay = EmptyLengthTransport()
    with pytest.raises(StoreError, match="bounded proposal"):
        agent._decision(store, restored, None, {}, replay, None, None)
    assert not replay.sent and store.load_run(run.id).usage.model_calls == expected


@pytest.mark.parametrize("category", [
    "authentication", "permission", "token_bound_exceeded", "credential_in_response",
    "response_missing", "redirect_rejected", "response_encoding_rejected",
])
def test_true_fatal_failures_stay_fatal(category):
    reply = {"error_category": category, "finish_reason": "length", "raw_content": ""}
    assert proposal_recovery_kind(reply, workflow_simplified=True) == "fatal"


def test_recovery_cannot_repeat_a_large_multistep_plan(tmp_path):
    store, run, _ = workflow_run(tmp_path)
    transport = EmptyLengthTransport()

    def oversized(data):
        proposed = initial_proposal(data)
        step = copy.deepcopy(proposed["parameters"]["steps"][0])
        step["key"] = "unnecessary_second_step"
        proposed["parameters"]["steps"].append(step)
        return proposed

    transport.scripts = [oversized]
    with pytest.raises(StoreError, match="bounded proposal"):
        agent._decision(store, run, None, {}, transport, None, None)
    assert store.load_run(run.id).usage.model_calls == 2
    assert store.load_run(run.id).plan_id is None
    assert store.load_run(run.id).decisions[-1]["parameters"]["requirement"]["path"] == ["parameters", "steps"]


@pytest.mark.parametrize("native_only", [False, True])
def test_new_workflow_prompt_exposes_program_bound_files_and_optional_method_sources(tmp_path, native_only):
    from tests.unit.test_code_context import selection_context

    _, run, prepared, wire = selection_context(
        tmp_path, general=True, simplified=True, native_only=native_only)
    assert run.configuration.workflow_policy == "simplified-1"
    assert prepared.prompt_version == "agent-json-v78"
    assert "producer_key" in wire["TASK_CODE_RULES"]
    assert "missing controls are rejected, never filled in" not in wire["TASK_CODE_RULES"]
    assert "program-materialized" in wire["TASK_CODE_RULES"] if native_only else (
        "program materializes omitted approved electronic state" in wire["TASK_CODE_RULES"])
    selection = wire["CONDITION_SELECTION_RULES"]
    assert "Documentation retrieval is optional" in selection
    assert "Honor any user-required documentation" in selection
    assert "geometry preparation may precede selection" in selection
    assert "Explicit values, unknowns and user minimum evidence stay fixed" in selection
    assert "model_reason" in selection
    if not native_only:
        assert "SDK waits still count toward the frozen script deadline" in wire["TASK_CODE_RULES"]


def test_window_reuse_never_replaces_a_larger_current_preview_with_a_short_old_one(tmp_path, monkeypatch):
    from tests.unit.test_retained_read_windows import read_context

    run, results, _, _ = read_context(tmp_path, monkeypatch, indices=[0, 2])
    first, other = results[1:]
    previews = [context._result(first, 256), context._result(other, 16384)]
    assert not context._reuse_read_windows(run, [first, other], previews)
    assert len(previews[1]["unqualified_observations"]["text_window"]["lines"]) == 67
    assert previews[0]["unqualified_observations"]["text_window"]["context_page"]["omitted_from_result"] > 0


def test_new_native_failure_history_survives_plan_removal_but_does_not_upgrade_old_run():
    from orca_agent.config import Config
    from orca_agent.decision_purpose import relevant_result_ids
    from orca_agent.models import Attempt, Step, utc_now

    _, run = objects()
    run.configuration = Config(workflow_policy="simplified-1").run_configuration()
    step = Step(id="native_failed", logical_id="native", tool="orca.run", parameters={
        "input": {"artifact_id": "original_input", "sha256": "a" * 64, "language": "orca"}})
    attempt = Attempt(step_id=step.id, logical_id=step.logical_id, number=1, tool=step.tool,
        input_fingerprint="prior_input", directory="attempts/failed", frozen_step=step,
        execution_kind="orca", state="failed", finished_at=utc_now(), result_id="native_result")
    run.attempts.append(attempt)
    run.result_ids = [attempt.result_id]
    run.processed_feedback = [attempt.result_id]
    before = run.model_dump(mode="json")
    assert relevant_result_ids(run) == ["native_result"]
    history = context._failed_attempt_history(run, native=True)
    assert history[0]["frozen_step"]["parameters"]["input"]["sha256"] == "a" * 64
    assert run.model_dump(mode="json") == before
    run.configuration = run.configuration.model_copy(update={"workflow_policy": None})
    assert context._failed_attempt_history(run, native=True) == []
    assert relevant_result_ids(run) == []


def test_intake_uses_program_fragments_and_one_authoritative_schema(tmp_path):
    from orca_agent import semantic
    from orca_agent.delivery import collect_delivery_snapshot
    from orca_agent.store import Store
    from tests.unit.test_code_context import selection_context

    request, run, _, _ = selection_context(tmp_path, general=True, simplified=True)
    request.normalization_status = "pending"
    messages = [{"id": "current_user", "text": "保持原始几何；报告全部失败。不要优化。"}]
    contract = semantic.action_parameters(
        request=request, text_input=True, code_authorized=True,
        allowed_tools=run.permission.allowed_tools, messages=messages, workflow_simplified=True)
    snapshot = collect_delivery_snapshot(Store(tmp_path / "data"), run, request)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot,
        action_parameters=contract, user_messages=messages)
    wire = payload(prepared)
    assert wire["INTENT_RULES"]["instruction"] == contract["normalize_request"]["instruction"]
    assert wire["INTENT_RULES"]["source_fragments"] == semantic.source_fragments(messages)
    assert "schema" not in wire["INTENT_RULES"]
    schema = wire["ACTION_SCHEMAS"]["normalize_request"]
    assert "source_refs" in str(schema)
    assert "Before submission explicitly adopt" not in json.dumps(wire)
    assert any(row["message"].get("text") == messages[0]["text"] for row in wire["AUTHORITY"]["user_messages"])
