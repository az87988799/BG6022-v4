"""Explicit read receipts survive code preparation; no source files are read."""

import copy
import json
from pathlib import Path

import pytest

from orca_agent.context import ContextLimitError, _result, _reuse_read_windows, build_context
from orca_agent.decision_purpose import relevant_result_ids, retained_read_result_ids
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import Result, ToolCall
from orca_agent.proposals import valid_call_tool_parameters
from tests.unit.test_context import payload
from tests.unit.test_generated_delivery import case, written


def recorded_reads():
    fixture = Path(__file__).parents[1] / "fixtures/code_contracts/live014-explicit-read-windows.json"
    return json.loads(fixture.read_text(encoding="utf-8"))["reads"]


def read_context(tmp_path, monkeypatch, *, input_tokens=48000, indices=None):
    store, original, request, writer = case(tmp_path, monkeypatch)
    plan = store.load_plan(original)
    permission = original.permission.model_copy(update={"model_execution": True,
        "allowed_tools": [*original.permission.allowed_tools, "evidence.text", "evidence.list"]})
    budget = original.budget.model_copy(update={"model_calls": 8, "model_tokens": 1000000,
        "input_tokens": input_tokens, "output_tokens": 2000, "evidence_reads": 10})
    run = store.create_run(request, plan, permission, budget)
    author, _ = written(store, run, writer)
    run.configuration = run.configuration.model_copy(update={
        "model": run.configuration.model.model_copy(update={"input_tokens": input_tokens})})
    snapshot = collect_delivery_snapshot(store, run, request, plan, [author])
    reads = recorded_reads()
    reads = reads if indices is None else [reads[index] for index in indices]
    results = [author]
    for row in reads:
        result = Result.model_validate(row["result"]).model_copy(update={"run_id": run.id})
        call = ToolCall.model_validate(row["call"]).model_copy(update={"request_version": run.request_version})
        run.calls.append(call)
        run.result_ids.append(result.id)
        run.processed_feedback.append(result.id)
        results.append(result)
    run.usage.evidence_reads = len(reads)
    run.processed_feedback.append(author.id)
    before = copy.deepcopy((run, request, plan, results, snapshot))
    prepared = build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
                             feedback={"pending_step_ids": []})
    assert (run, request, plan, results, snapshot) == before
    return run, results, prepared, payload(prepared)


def test_real_read_windows_remain_together_and_duplicate_content_has_visible_source(tmp_path, monkeypatch):
    run, results, prepared, wire = read_context(tmp_path, monkeypatch)
    assert prepared.input_token_bound <= 48000
    views = {item["result_id"]: item for item in wire["DATA"]["results"]}
    assert set(views) == {result.id for result in results}
    # The recorded whole script and its separately requested tail both survive.
    first = views[results[1].id]["unqualified_observations"]["text_window"]
    tail = views[results[4].id]["unqualified_observations"]["text_window"]
    assert [row["line"] for row in first["lines"]] == list(range(1, 68))
    assert [row["line"] for row in tail["lines"]] == list(range(23, 68))
    assert "context_page" not in first and "context_page" not in tail
    for index, count in ((6, 19), (8, 12)):
        assert len(views[results[index].id]["unqualified_observations"]["text_window"]["lines"]) == count
    duplicate = views[results[7].id]["unqualified_observations"]["text_window"]
    assert duplicate["lines_ref"] == {"result_id": results[1].id, "observation": "text_window", "field": "lines"}
    assert duplicate["coverage"]["requested"]["stop"] == 121
    assert first["coverage"]["requested"]["stop"] == 201
    inventory = views[results[2].id]["unqualified_observations"]["artifact_metadata"]
    assert len(inventory["artifacts"]) == 11 and inventory["inventory_sha256"]
    assert run.usage.evidence_reads == 9 and not run.attempts
    assert all(not view.get("qualified_outputs") for view in views.values())


def test_small_envelope_retains_distinct_pages_with_explicit_preview_coverage(tmp_path, monkeypatch):
    # This new combination exceeds 12k even with short previews. Fail closed;
    # existing smaller 12k planning scenarios are tested in test_code_context.
    with pytest.raises(ContextLimitError, match="bound"):
        read_context(tmp_path, monkeypatch, input_tokens=12000, indices=[0, 3])
    rows = recorded_reads()
    pages = [_result(Result.model_validate(rows[index]["result"]), 256)["unqualified_observations"]["text_window"]
             for index in (0, 3)]
    assert [page["lines"][0]["line"] for page in pages] == [1, 23]
    assert all(page["context_page"]["omitted_from_result"] > 0 for page in pages)
    assert all(page["coverage"]["complete"] is True for page in pages)


@pytest.mark.parametrize("change", ["hash", "artifact", "window", "content", "failed", "unmatched_call"])
def test_window_reuse_requires_same_immutable_source_and_successful_receipt(tmp_path, monkeypatch, change):
    run, results, _, _ = read_context(tmp_path, monkeypatch, indices=[0, 2])
    first, other = results[1:]
    observation = other.observations["text_window"]
    call = next(item for item in run.calls if item.id == other.call_id)
    if change == "hash":
        observation["sha256"] = "b" * 64
    elif change == "artifact":
        observation["artifact_id"] = "different_artifact"
    elif change == "window":
        observation["coverage"]["returned"]["start"] = 23
    elif change == "content":
        observation["lines"][0]["text"] = "different text"
    elif change == "failed":
        other.operation_status = call.state = "failed"
    else:
        call.result_id = "not_this_result"
    projected = [_result(result, 16384) for result in (first, other)]
    assert not _reuse_read_windows(run, [first, other], projected)
    assert all("lines" in item["unqualified_observations"]["text_window"] for item in projected)


def test_read_selection_requires_code_permission_and_keeps_existing_bounds(tmp_path, monkeypatch):
    run, results, _, _ = read_context(tmp_path, monkeypatch, indices=[0, 3])
    assert retained_read_result_ids(run) == {result.id for result in results[1:]}
    run.permission.code_execution = False
    assert retained_read_result_ids(run) == set()
    assert set(relevant_result_ids(run)) == {results[0].id}
    run.permission.code_execution = True
    run.calls[-1].state = "failed"
    assert retained_read_result_ids(run) == {results[1].id}


def test_native_advertises_only_named_immediate_action_but_legacy_form_stays_valid(tmp_path, monkeypatch):
    _, _, prepared, wire = read_context(tmp_path, monkeypatch, indices=[0])
    prompt = prepared.body()["messages"][0]["content"]
    assert "evidence.text" in wire["ACTION_SCHEMAS"]
    assert "call_tool" not in wire["ACTION_SCHEMAS"]
    assert "call_tool" not in wire["RESPONSE_ENVELOPE"]["action"].split(" | ")
    assert "Immediate Tool: action=exact Tool name" in prompt
    assert "no top-level tool" in prompt
    assert "call_tool {tool,parameters}" not in prompt
    assert valid_call_tool_parameters({"tool": "evidence.text", "parameters": {"artifact_id": "registered"}})
