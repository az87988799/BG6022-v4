"""Real archived ORCA error replay and offline evidence-boundary negatives."""

import hashlib
import json
from pathlib import Path

import pytest

from orca_agent import context
from orca_agent.models import InternalCall, RunConfiguration
from orca_agent.tools import evidence, managed
from tests.unit.test_managed_attempt_store import make_task
from tests.unit.test_managed_execution import outcome

ERROR_FILE = (Path(__file__).resolve().parents[2] /
    "docs/acceptance/code-as-tools-step7-refreeze-3/evidence/T4/data/artifacts/"
    "artifact_7e3c89697dfa4c3381cd6ea12c9f0182/files/stdout.out")
ERROR_HASH = "277bb9661199fed09419fc8555a64b5ce7d76e01dc75cc1ec996c2dc24058213"


def internal_receipt(store, run, attempt, path, *, state="failed", key="child"):
    artifact = store.import_artifact(path, "raw_evidence", run_id=run.id,
        attempt_id=attempt.id, source={"task_path": "stdout.out"})
    receipt = InternalCall(request_key=key, input_fingerprint="a" * 64,
        parent_attempt_id=attempt.id, state=state, artifact_ids=[artifact.id])
    run.internal_calls.append(receipt)
    return artifact, receipt


def test_real_orca_input_failure_is_visible_even_when_outer_python_only_reports_parse_error(
    tmp_path, monkeypatch,
):
    raw = ERROR_FILE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == ERROR_HASH
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    artifact, _ = internal_receipt(store, run, attempt, ERROR_FILE)
    root = store.path(attempt.directory) / "task"
    root.mkdir(exist_ok=True)
    (root / "stderr.txt").write_text("ValueError: No excitation energies found")
    before = run.model_dump(mode="json")
    result = managed.collect_result(store, run, step, attempt, outcome("failed"))
    failure, = [row for row in result.diagnostics if row["category"] == "internal_orca_failed"]
    evidence, = failure["evidence"]
    assert failure["operation_status"] == "failed" and failure["request_key"] == "child"
    assert "INPUT ERROR" in evidence["text"] and "TDDFT" in evidence["text"]
    assert evidence["artifact_id"] == artifact.id and evidence["sha256"] == ERROR_HASH
    assert evidence["truncated"] and evidence["byte_start"] == len(raw) - 2048
    assert artifact.id in result.artifact_ids and not result.qualified_outputs
    assert result.source["files"]["internal/child/task/stdout.out"]["artifact_id"] == artifact.id
    assert not result.observation_records
    assert run.model_dump(mode="json") == before
    assert ERROR_FILE.read_bytes() == raw


@pytest.mark.parametrize("state", ["completed", "unknown", "running", "reserved"])
def test_only_settled_failed_children_are_described_as_failures(tmp_path, monkeypatch, state):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    internal_receipt(store, run, attempt, ERROR_FILE, state=state)
    diagnostics = []
    managed._collect_internal_failures(store, run, attempt, diagnostics)
    assert diagnostics == []


def test_hash_changed_internal_error_is_not_read_or_treated_as_scientific_evidence(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    artifact, _ = internal_receipt(store, run, attempt, ERROR_FILE)
    store.artifact_path(artifact.id).write_text("forged diagnostic")
    diagnostics = []
    managed._collect_internal_failures(store, run, attempt, diagnostics)
    evidence, = diagnostics[0]["evidence"]
    assert "text" not in evidence and evidence["read_error"] == "StoreError"
    assert evidence["sha256"] == ERROR_HASH


def test_failure_projection_redacts_credentials_and_bounds_large_logs_and_history(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    path = tmp_path / "large.out"
    for index in range(6):
        tail = "sk-" + "x" * 32 if index == 5 else "E" * 5000
        path.write_text(tail, encoding="utf-8")
        internal_receipt(store, run, attempt, path, key=f"child{index}")
    diagnostics = []
    managed._collect_internal_failures(store, run, attempt, diagnostics)
    assert len(diagnostics) == 5
    assert all(len(row["evidence"][0]["text"].encode()) <= 2048 for row in diagnostics[:4])
    assert diagnostics[0]["evidence"][0]["text"].startswith("[redacted")
    assert "sk-" not in json.dumps(diagnostics)
    assert diagnostics[-1] == {
        "category": "internal_orca_failures_bounded", "shown": 4, "total": 6, "omitted": 2}


def test_child_from_another_attempt_is_not_projected(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    _, receipt = internal_receipt(store, run, attempt, ERROR_FILE)
    receipt.parent_attempt_id = "other_attempt"
    diagnostics = []
    managed._collect_internal_failures(store, run, attempt, diagnostics)
    assert diagnostics == []


@pytest.mark.parametrize("parent_state", ["failed", "completed"])
def test_multiple_internal_errors_survive_small_context_even_if_python_catches_failure(
    tmp_path, monkeypatch, parent_state,
):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    for key in ("child_a", "child_b"):
        internal_receipt(store, run, attempt, ERROR_FILE, key=key)
    result = managed.collect_result(store, run, step, attempt, outcome(parent_state))
    before = result.model_dump(mode="json")
    projected = context._result(result, 256)
    failures = [row for row in projected["untrusted_diagnostics"]
                if row.get("category") == "internal_orca_failed"]
    assert {row["request_key"] for row in failures} == {"child_a", "child_b"}
    for row in failures:
        log, = row["evidence"]
        assert log["artifact_id"] in result.artifact_ids and log["sha256"] == ERROR_HASH
        assert "INPUT ERROR" in log["text"] or "TDDFT" in log["text"]
        assert row["operation_status"] == "failed"
    assert not result.qualified_outputs and result.model_dump(mode="json") == before


def test_redaction_and_preview_do_not_change_source_byte_offsets():
    text = "C:/a/x.py\n" * 50 + "INPUT ERROR"
    diagnostic = {"category": "internal_orca_failed", "request_key": "child",
        "operation_status": "failed", "evidence": [{
            "filename": "stdout.out", "artifact_id": "log", "sha256": "a" * 64,
            "text": text, "byte_start": 0, "total_bytes": len(text.encode()), "truncated": False}]}
    view, = context._diagnostic_records([diagnostic], 256)
    log, = view["evidence"]
    assert log["byte_start"] == 0 and log["total_bytes"] == len(text.encode())
    assert log["text_projection"]["source_window_unchanged"]
    assert "C:/a/x.py" not in log["text"] and log["text"].endswith("INPUT ERROR")
    assert diagnostic["evidence"][0]["text"] == text


def test_failed_internal_logs_are_discoverable_first_without_expanding_access(tmp_path, monkeypatch):
    store, original, step, code = make_task(tmp_path, monkeypatch,
                                          budget_changes={"analysis_executions": 1})
    run = store.create_run(store.load_request(original), store.load_plan(original),
        original.permission, original.budget,
        configuration=RunConfiguration(workflow_policy="simplified-1"))
    attempt = store.reserve_attempt(run, step)
    log, receipt = internal_receipt(store, run, attempt, ERROR_FILE)
    receipt.response = {"files": {"stdout.out": {"artifact_id": log.id}}}
    hidden_path = tmp_path / "hidden.txt"
    hidden_path.write_text("Unregistered evidence must remain inaccessible.")
    hidden = store.import_artifact(hidden_path, "raw_evidence", run_id=run.id, attempt_id=attempt.id)
    run.internal_calls.append(InternalCall(request_key="hidden", input_fingerprint="b" * 64,
        parent_attempt_id=attempt.id, state="failed", artifact_ids=[hidden.id],
        response={"files": {"stderr.txt": {"artifact_id": hidden.id}}}))
    result = managed.collect_result(store, run, step, attempt, outcome("failed"))
    # This receipt is intentionally not a published Result or permitted input.
    result.artifact_ids.remove(hidden.id)
    result.source["files"] = {name: ref for name, ref in result.source["files"].items()
                             if ref.get("artifact_id") != hidden.id}
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="failed", result_id=result.id,
                         started=True, termination_confirmed=True)
    before = store.load_run(run.id).model_dump(mode="json")
    first = evidence.list_artifacts(store, run.id, limit=1)
    row, = first["artifacts"]
    assert row["id"] == log.id and row["basename"] == "stdout.out"
    assert row["internal_request_key"] == "child" and row["operation_status"] == "failed"
    assert first["next_offset"] == 1
    rest = evidence.list_artifacts(store, run.id, offset=1)
    assert first["inventory_sha256"] == rest["inventory_sha256"]
    ids = [row["id"], *(item["id"] for item in rest["artifacts"])]
    assert len(ids) == len(set(ids)) == first["total"]
    assert code.id in ids and hidden.id not in ids
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert evidence.list_artifacts(store, run.id, limit=1) == first
