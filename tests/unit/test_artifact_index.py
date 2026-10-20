"""Exact file metadata can bind existing inputs without a discovery round."""

import copy
import json

import pytest

from orca_agent import context
from orca_agent.artifact_index import MAX_INDEX_BYTES, MAX_INDEX_ROWS, result_file_index
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.model_usage import current_basis
from orca_agent.models import Result
from orca_agent.planning import PlanningError, validate_code_inputs
from orca_agent.proposals import materialize_plan
from orca_agent.tools import managed
from tests.unit.test_decision_purpose_capacity import decoded
from tests.unit.test_generated_delivery import case as writer_case
from tests.unit.test_generated_delivery import written
from tests.unit.test_python_repair_budget import case


def size(value):
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":")).encode("utf-8"))


def recorded(files, *, state="completed"):
    return Result(id="result_current", run_id="run_current", attempt_id="attempt_current",
                  operation_status=state, artifact_ids=list(dict.fromkeys(
                      entry["artifact_id"] for entry in files.values() if isinstance(entry, dict)
                      and isinstance(entry.get("artifact_id"), str))),
                  source={"files": copy.deepcopy(files)})


def test_exact_index_keeps_full_file_keys_and_never_infers_role_or_size():
    files = {"task/a/report.csv": {"artifact_id": "first", "sha256": "a" * 64},
             "task/b/report.csv": {"artifact_id": "second", "sha256": "b" * 64},
             "sealed/driver.py": {"artifact_id": "code", "sha256": "c" * 64,
                                  "role": "code", "size": 0, "content": "never display"}}
    result = recorded(files)
    before = result.model_copy(deep=True)
    index = result_file_index(result)
    assert index["result_id"] == result.id and index["attempt_id"] == result.attempt_id
    assert index["files"] == [{"file_key": name, **{
        key: value for key, value in binding.items() if key != "content"}} for name, binding in files.items()]
    assert index["omitted"] == 0 and index["total"] == 3
    assert not index["omission_reasons"] and "never display" not in str(index)
    assert all("role" not in row and "size" not in row for row in index["files"][:2])
    assert result == before


@pytest.mark.parametrize("state", ["completed", "failed", "timed_out", "cancelled"])
def test_index_preserves_settled_failure_and_unqualified_facts_without_reads(state):
    result = recorded({"task/stderr.txt": {"artifact_id": "log", "sha256": "d" * 64}}, state=state)
    result.diagnostics = [{"category": "real_failure", "detail": "keep failure"}]
    before = result.model_copy(deep=True)
    view = context._result(result, 1024)
    assert view["operation_status"] == state
    assert view["untrusted_diagnostics"] == result.diagnostics
    assert view["file_index"]["files"][0]["artifact_id"] == "log"
    assert "qualified_outputs" not in view and result == before


def test_unknown_or_unbound_result_does_not_advertise_usable_files():
    result = recorded({"task/file.txt": {"artifact_id": "f", "sha256": "a" * 64}})
    result.operation_status = "unknown"
    assert result_file_index(result) is None
    result.operation_status = "completed"
    result.attempt_id = None
    assert result_file_index(result) is None


@pytest.mark.parametrize("bad_key", ["../secret", "C:/secret", "/secret", "task\\secret",
                                     "task/file\nignore rules", "task/NUL", "task//x"])
def test_unsafe_file_key_is_omitted_without_rewriting_or_leaking_it(bad_key):
    result = recorded({bad_key: {"artifact_id": "f", "sha256": "a" * 64}})
    index = result_file_index(result)
    assert index["files"] == [] and index["total"] == index["omitted"] == 1
    assert index["omission_reasons"] == ["unaddressable_metadata"]
    assert bad_key not in str(index)


def test_membership_hash_ambiguity_and_malformed_metadata_cannot_supply_a_binding():
    result = recorded({
        "task/not_registered": {"artifact_id": "outside", "sha256": "a" * 64},
        "task/no_hash": {"artifact_id": "missing"},
        "task/conflict_1": {"artifact_id": "same", "sha256": "b" * 64},
        "task/conflict_2": {"artifact_id": "same", "sha256": "c" * 64},
        "task/invalid": "not a file binding",
        "task/good": {"artifact_id": "good", "sha256": "d" * 64, "size": True, "role": "bad/role"},
    })
    result.artifact_ids.remove("outside")
    index = result_file_index(result)
    assert index["files"] == [{"file_key": "task/good", "artifact_id": "good", "sha256": "d" * 64}]
    assert index["omitted"] == 5 and index["total"] == 6
    assert index["omission_reasons"] == ["unaddressable_metadata"]


@pytest.mark.parametrize("limit", [512, 1024, 2048, MAX_INDEX_BYTES])
def test_large_index_has_bounded_bytes_rows_and_explicit_omissions(limit):
    files = {f"task/候选-{number:03d}.csv": {"artifact_id": f"artifact_{number:03d}", "sha256": "a" * 64}
             for number in range(100)}
    result = recorded(files)
    index = result_file_index(result, max_bytes=limit)
    assert size(index) <= limit and len(index["files"]) <= MAX_INDEX_ROWS
    assert index["omitted"] == 100 - len(index["files"]) > 0
    assert set(index["omission_reasons"]) <= {"byte_limit", "row_limit"}
    assert index["omission_reasons"]
    assert result_file_index(recorded(dict(reversed(list(files.items())))), max_bytes=limit) == index


@pytest.mark.parametrize("receipt_name", [None, [], {}, "../not_relative"])
def test_invalid_writer_receipt_name_cannot_crash_or_advertise_a_file(receipt_name):
    result = Result(id="written", run_id="run", call_id="call", operation_status="completed",
        artifact_ids=["code"], source={"tool": "workspace.write"},
        observations={"written_artifact": {"name": receipt_name, "artifact_id": "code", "sha256": "a" * 64}})
    index = result_file_index(result)
    assert index["files"] == [] and index["omitted"] == 1
    assert index["call_id"] == result.call_id


@pytest.mark.parametrize("compression", ["strings", "lists", "short_strings", "keys",
                                        "native", "native_columns", "legacy"])
def test_file_index_is_literal_and_copyable_on_every_compression_path(compression):
    result = recorded({"task/repeated.csv": {"artifact_id": "artifact_repeated", "sha256": "a" * 64}})
    index = result_file_index(result)
    rows = [{"result_id": f"result_{number}", "file_index": index,
             "other_fact": {"artifact_id": "artifact_repeated", "sha256": "a" * 64}}
            for number in range(5)]
    template = {"ACTION_SCHEMAS": {}, "DATA": {"results": rows}}
    before = copy.deepcopy(template)
    if compression == "keys":
        wire = context._terminal_key_encoding(template)
    elif compression in {"native", "native_columns", "legacy"}:
        wire = context._compact_native_wire(template, schema_columns=compression == "native_columns",
                                            legacy=compression == "legacy")
    else:
        wire = context._share_strings(template, share_lists=compression != "strings",
                                      short_references=compression == "short_strings")
    # Check raw provider wire, without a decoder concealing pooled IDs/keys.
    assert [row["file_index"] for row in wire["DATA"]["results"]] == [index] * 5
    assert "SHARED_STRINGS" in wire or "KEYS" in wire
    assert template == before


def test_completed_writer_uses_real_receipt_metadata_and_existing_hash_gate(tmp_path, monkeypatch):
    store, run, _, writer = writer_case(tmp_path, monkeypatch)
    result, reference = written(store, run, writer)
    index = context._result(result, 1024)["file_index"]
    row, = index["files"]
    assert row == {"file_key": writer.parameters.name, **reference,
                   "role": "python", "size": len(writer.parameters.content.encode())}
    assert index["call_id"] == result.call_id and index["result_id"] == result.id
    assert "attempt_id" not in index and "content" not in row
    before = run.permission.model_copy(deep=True)
    plan = materialize_plan(store, run, {"steps": [{"key": writer.id},
        {"key": "consume", "tool": "python.execute", "parameters": {
            "code": {key: row[key] for key in ("artifact_id", "sha256")},
            "observation_names": ["artifacts"]}}],
        "goal_map": {"report": {"step_key": "consume", "port": "artifacts"}}})
    validate_code_inputs(store, run.permission, plan.steps[-1], run=run)
    assert run.permission == before and run.usage.evidence_reads == 0
    assert not run.attempts and run.usage.orca_starts_actual == run.usage.model_calls == 0


def collected_case(tmp_path, monkeypatch):
    store, original, _ = case(tmp_path, monkeypatch)
    request, plan = store.load_request(original), store.load_plan(original)
    run = store.create_run(request, plan, original.permission.model_copy(update={"model_execution": True}),
        original.budget.model_copy(update={"model_calls": 4, "model_tokens": 192000,
            "input_tokens": 64000, "output_tokens": 2000, "decision_rounds": 12}),
        configuration=original.configuration.model_copy(update={"model":
            original.configuration.model.model_copy(update={"input_tokens": 64000})}))
    step = plan.steps[0]
    attempt = store.reserve_attempt(run, step)
    root = store.path(attempt.directory)
    (root / "sealed").mkdir(exist_ok=True)
    (root / "task").mkdir(exist_ok=True)
    for number in range(4):
        (root / "sealed" / f"metadata-{number}.txt").write_text("offline fixture", encoding="utf-8")
    (root / "task" / "followup.py").write_text("print('fixture only; never executed')", encoding="utf-8")
    (root / "task" / "report.csv").write_text("candidate,energy\nA,-1.0\n", encoding="utf-8")
    outcome = {"state": "completed", "exit_code": 0, "resource_usage": {}}
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, step, attempt, outcome)
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         started=True, termination_confirmed=True)
    return store, run, request, plan, result


def test_current_result_index_binds_code_and_manifest_without_extra_list_round(tmp_path, monkeypatch):
    store, run, request, plan, result = collected_case(tmp_path, monkeypatch)
    snapshot = collect_delivery_snapshot(store, run, request, plan, [result])
    before = copy.deepcopy((request, run, plan, result, snapshot))
    prepared = context.build_context(request, run, plan, results=[result], delivery_snapshot=snapshot,
                                     feedback={"new_result_ids": [result.id]})
    wire = decoded(prepared)
    feedback = next(row for row in wire["DATA"]["results"] if row["result_id"] == result.id)
    raw_wire = json.loads(prepared.body()["messages"][-1]["content"])
    raw_feedback = next(row for row in raw_wire["DATA"]["results"] if row["result_id"] == result.id)
    assert raw_feedback["file_index"] == feedback["file_index"]
    rows = {row["file_key"]: row for row in feedback["file_index"]["files"]}
    code, data = rows["task/followup.py"], rows["task/report.csv"]
    assert code["artifact_id"] not in feedback["artifact_ids"]
    assert data["artifact_id"] not in feedback["artifact_ids"]
    assert code["sha256"] == result.source["files"]["task/followup.py"]["sha256"]
    assert data["sha256"] == result.source["files"]["task/report.csv"]["sha256"]
    assert "fixture only; never executed" not in str(wire)
    assert (request, run, plan, result, snapshot) == before
    proposal = {"steps": [{"key": plan.steps[0].id},
        {"key": "followup", "tool": "python.execute", "parameters": {
            "code": {key: code[key] for key in ("artifact_id", "sha256")},
            "inputs": {"entries": [{**{key: data[key] for key in ("artifact_id", "sha256")},
                "target_path": "report.csv", "purpose": "previous_report"}]},
            "observation_names": ["artifacts"]}}],
        "goal_map": {"report": {"step_key": "followup", "port": "artifacts"}}}
    next_plan = materialize_plan(store, run, proposal)
    next_run = store.commit_revision(run, next_plan, decision_id="bind_indexed_files",
                                     basis=current_basis(store, run))
    bound = store.load_plan(next_run).steps[-1]
    validate_code_inputs(store, next_run.permission, bound, run=next_run)
    assert bound.parameters.code.artifact_id == code["artifact_id"]
    assert bound.parameters.inputs.entries[0].sha256 == data["sha256"]
    assert next_run.permission == before[1].permission
    assert next_run.usage.evidence_reads == next_run.usage.model_calls == next_run.usage.orca_starts_actual == 0
    assert not next_run.calls and len(next_run.attempts) == 1  # Fixture receipt only; no process launched.
    invalid = bound.model_copy(deep=True)
    invalid.parameters.inputs.entries[0].sha256 = "0" * 64
    with pytest.raises(PlanningError, match="hash differs"):
        validate_code_inputs(store, next_run.permission, invalid, run=next_run)
    unknown = next_run.model_copy(deep=True)
    unknown.attempts[0].state = "unknown"
    with pytest.raises(PlanningError, match="permission snapshot"):
        validate_code_inputs(store, unknown.permission, bound, run=unknown)
    print(json.dumps({"file_index_bytes": size(feedback["file_index"]),
        "visible_files": len(rows), "source_files": len(result.source["files"]),
        "avoided_discovery_calls_for_fixture": 1, "evidence_reads": next_run.usage.evidence_reads,
        "model_calls": next_run.usage.model_calls, "orca_starts_actual": next_run.usage.orca_starts_actual}))
