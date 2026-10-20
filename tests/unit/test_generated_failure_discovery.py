"""Discover archived task failures without a launch or implicit diagnostic read."""

import copy

import pytest

from orca_agent import context
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import Result
from orca_agent.tools import managed
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_decision_purpose_capacity import decoded
from tests.unit.test_python_repair_budget import case


def failed_case(tmp_path, monkeypatch, *, allow_listing=True):
    store, original, _ = case(tmp_path, monkeypatch)
    request, plan = store.load_request(original), store.load_plan(original)
    allowed = ["python.execute", "evidence.text", "evidence.search"]
    if allow_listing:
        allowed.append("evidence.list")
    run = store.create_run(request, plan, original.permission.model_copy(update={
        "model_execution": True, "allowed_tools": allowed}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 192000,
            "input_tokens": 48000, "output_tokens": 2000, "decision_rounds": 12,
            "evidence_reads": 12}))
    step = plan.steps[0]
    attempt = store.reserve_attempt(run, step)
    root = store.path(attempt.directory)
    (root / "sealed").mkdir(exist_ok=True)
    (root / "task").mkdir(exist_ok=True)
    # Force the actionable stderr behind the generic three-Artifact preview.
    for index in range(4):
        (root / "sealed" / f"metadata-{index}.txt").write_text("offline fixture\n", encoding="utf-8")
    (root / "task" / "stdout.out").write_text("", encoding="utf-8")
    traceback = "NameError: name 'missing_task_value' is not defined\n"
    (root / "task" / "stderr.txt").write_text(traceback, encoding="utf-8")
    outcome = {"state": "failed", "reason": "nonzero_exit_code", "exit_code": 1,
               "handle": None, "resource_usage": {}}
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, step, attempt, outcome)
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="failed", result_id=result.id,
                         started=True, termination_confirmed=True)
    snapshot = collect_delivery_snapshot(store, run, request, plan, [result])
    before = copy.deepcopy((request, run, plan, result))
    prepared = context.build_context(request, run, plan, results=[result],
        relevant_tools=allowed, delivery_snapshot=snapshot,
        feedback={"new_result_ids": [result.id]})
    assert (request, run, plan, result) == before
    return store, run, result, decoded(prepared), traceback


def test_failed_task_has_authorized_discovery_schema_and_exact_diagnostic_reference(tmp_path, monkeypatch):
    store, run, result, wire, traceback = failed_case(tmp_path, monkeypatch)
    feedback = next(item for item in wire["DATA"]["results"] if item["result_id"] == result.id)
    source = result.source["files"]["task/stderr.txt"]
    assert source["artifact_id"] not in feedback.get("artifact_ids", [])
    assert {item["task_path"]: {key: value for key, value in item.items() if key != "task_path"}
            for item in feedback["diagnostic_artifacts"]} == {
                name: result.source["files"][name] for name in ("task/stdout.out", "task/stderr.txt")}
    index = feedback["file_index"]
    assert index["files"][0] == {"file_key": "task/stderr.txt", **source}
    assert index["omitted"] == index["total"] - len(index["files"]) > 0
    assert "byte_limit" in index["omission_reasons"]
    assert traceback not in str(wire)
    assert "evidence.list" in wire["ACTION_SCHEMAS"]
    assert wire["ACTION_SCHEMAS"]["evidence.list"]["required"] == ["run_id"]
    assert wire["ACTION_SCHEMAS"]["evidence.text"]["required"] == ["artifact_id"]
    assert run.usage.evidence_reads == 0
    assert not result.qualified_outputs and not result.observation_records
    read = execute_call(store, run, "evidence.text", {"artifact_id": source["artifact_id"], "lines": 10})
    assert "missing_task_value" in str(read.observations)
    assert run.usage.evidence_reads == 1 and run.usage.orca_starts_actual == 0
    assert len(run.attempts) == 1 and not read.qualified_outputs


def test_discovery_dependency_does_not_grant_an_unpermitted_reader(tmp_path, monkeypatch):
    _, run, _, wire, _ = failed_case(tmp_path, monkeypatch, allow_listing=False)
    assert "evidence.list" not in wire["ACTION_SCHEMAS"]
    assert "evidence.list" not in {tool["name"] for tool in wire["TOOLS"]}
    assert "evidence.list" not in run.permission.allowed_tools


@pytest.mark.parametrize("state", ["completed", "failed", "timed_out", "cancelled"])
def test_diagnostic_index_is_metadata_only_and_keeps_operation_status(state):
    result = Result(run_id="run", operation_status=state, artifact_ids=["error"], source={"files": {
        "task/stderr.txt": {"artifact_id": "error", "sha256": "a" * 64},
        "sealed/task.py": {"artifact_id": "error", "sha256": "a" * 64},
    }})
    before = result.model_copy(deep=True)
    view = context._result(result, 1)
    assert view["diagnostic_artifacts"] == [
        {"task_path": "task/stderr.txt", "artifact_id": "error", "sha256": "a" * 64}]
    assert view["operation_status"] == state
    assert "qualified_outputs" not in view and result == before


@pytest.mark.parametrize("entry", [
    {"artifact_id": "unregistered", "sha256": "a" * 64},
    {"artifact_id": "error", "sha256": "not-a-sha256"},
    {"artifact_id": "error"},
    "not a binding",
])
def test_unaddressable_diagnostic_binding_is_not_projected(entry):
    result = Result(run_id="run", operation_status="failed", artifact_ids=["error"],
        source={"files": {"task/stderr.txt": entry}})
    assert "diagnostic_artifacts" not in context._result(result, 1)
