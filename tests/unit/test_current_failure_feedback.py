"""Actual a022 read-window replay and archival negatives; no execution/model."""

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

from orca_agent import context
from orca_agent.models import Result, Run
from orca_agent.tools import managed
from tests.unit.test_managed_attempt_store import make_task
from tests.unit.test_managed_execution import outcome

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live022-failure-read-context.json"


def actual():
    source = json.loads(FIXTURE.read_text("utf-8"))
    return (Run.model_validate(source["run"]),
            [Result.model_validate(value) for value in source["results"]], source["expected_read_id"])


def test_actual_stdout_error_survives_three_knowledge_queries_and_tight_history_projection():
    run, results, read_id = actual()
    before = copy.deepcopy((run, results))
    retained = context._failure_read_diagnostics(run, results)
    failure = results[0]
    diagnostic = retained[failure.id]
    stdout = next(row for row in diagnostic["evidence"] if row["filename"] == "task/stdout.out")
    assert stdout["read_result_id"] == read_id
    assert "INPUT ERROR" in stdout["text"] and "GIAO" in stdout["text"]
    assert len(stdout["text"].encode()) <= 2048
    projected = [context._result(result, 256) for result in results]
    context._consumed_read_previews(run, results, projected)
    read = next(row for row in projected if row["result_id"] == read_id)
    assert "consumed_read" in read and "unqualified_observations" not in read
    context._settled_history_references(run, results, projected, {})
    assert "unqualified_observations" not in read  # ordinary read history stays bounded
    diagnostic_view = context._diagnostic_records([*failure.diagnostics, diagnostic], 256)
    shown = next(row for row in diagnostic_view if row["category"] == "execution_failed")
    text = json.dumps(shown)
    assert "UNRECOGNIZED OR DUPLICATED" in text and "GIAO" in text
    assert shown["evidence_source"] == "checked_read_window"
    assert all(len(row.get("text", "").encode()) <= 1024 for row in shown["evidence"])
    assert (run, results) == before


@pytest.mark.parametrize("corruption", ["hash", "foreign_read", "unchecked", "wrong_path"])
def test_failure_does_not_pin_wrong_or_unchecked_query_sources(corruption):
    run, results, read_id = actual()
    query = next(result for result in results if result.id == read_id)
    if corruption == "hash":
        results[0].source["files"]["task/stdout.out"]["sha256"] = "0" * 64
    elif corruption == "foreign_read":
        query.run_id = "foreign_run"
    elif corruption == "unchecked":
        query.checks["text_window"][0].status = "failed"
    else:
        query.source["task_path"] = "task/nested/stdout.out"
    projected = context._failure_read_diagnostics(run, results)
    evidence = projected.get(results[0].id, {}).get("evidence", [])
    assert not any(row.get("read_result_id") == read_id for row in evidence)
    assert "GIAO" not in json.dumps(projected)


def test_latest_window_is_bound_to_log_and_removed_only_after_same_logical_success():
    run, results, read_id = actual()
    failed = run.attempts[0]
    replacement = failed.model_copy(deep=True, update={"id": "settled_replacement",
        "number": failed.number + 1, "state": "completed", "result_id": "replacement_result",
        "finished_at": failed.finished_at + timedelta(seconds=1)})
    success = Result(id=replacement.result_id, run_id=run.id, attempt_id=replacement.id,
                     step_id=replacement.step_id, operation_status="completed")
    run.attempts.append(replacement)
    results.append(success)
    # Another logical task's success cannot clear this failure.
    replacement.logical_id = "another_logical_task"
    assert results[0].id in context._failure_read_diagnostics(run, results)
    replacement.logical_id = failed.logical_id
    assert context._failure_read_diagnostics(run, results) == {}


def test_new_failed_result_carries_both_verified_log_tails_without_execution(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch, tool="orca.run")
    attempt = store.reserve_attempt(run, step)
    task = store.path(attempt.directory) / "task"
    task.mkdir(exist_ok=True)
    (task / "stdout.out").write_text("banner\n" * 1000 + "INPUT ERROR\nUNRECOGNIZED KEYWORD\nGIAO\n")
    (task / "stderr.txt").write_text("source location only\n")
    (task / "nested").mkdir()
    (task / "nested/stdout.out").write_text("must not be selected by basename\n")
    before = run.model_dump(mode="json")
    result = managed.collect_result(store, run, step, attempt, outcome("failed"))
    diagnostic, = [row for row in result.diagnostics if row["category"] == "execution_failed"]
    assert [row["filename"] for row in diagnostic["evidence"]] == ["task/stdout.out", "task/stderr.txt"]
    assert "GIAO" in diagnostic["evidence"][0]["text"]
    assert all(len(row["text"].encode()) <= 2048 for row in diagnostic["evidence"])
    assert result.qualified_outputs == {} and run.model_dump(mode="json") == before
    assert not attempt.started and run.usage.orca_starts_actual == 0
    views = context._result(result, 256)["untrusted_diagnostics"]
    assert "INPUT ERROR" in json.dumps(views) and "GIAO" in json.dumps(views)


@pytest.mark.parametrize("corruption", ["changed_bytes", "foreign_owner", "wrong_hash"])
def test_new_failure_tail_rejects_changed_or_foreign_archive(tmp_path, monkeypatch, corruption):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    file = tmp_path / "stdout.out"
    file.write_text("genuine error\n")
    artifact = store.import_artifact(file, "raw_evidence", run_id=run.id, attempt_id=attempt.id,
                                     source={"task_path": "task/stdout.out"})
    expected = artifact.sha256
    if corruption == "changed_bytes":
        store.artifact_path(artifact.id).write_text("forged error\n")
    elif corruption == "foreign_owner":
        artifact.attempt_id = "foreign_attempt"
    else:
        expected = "0" * 64
    projected = managed._failure_log_tail(store, run, attempt, artifact, "task/stdout.out", expected)
    assert "text" not in projected and projected["read_error"] in {"StoreError", "ValueError"}
