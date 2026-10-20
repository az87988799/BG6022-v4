"""Offline declaration and correction tests; no task code or ORCA executes."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import pytest

from orca_agent import context
from orca_agent.models import fingerprint
from orca_agent.proposals import DecisionIntent, ProposalError, materialize_plan
from orca_agent.store import Store
from tests.unit.test_context import payload
from tests.unit.test_simplified_file_references import _case, _proposal


def test_authoring_field_map_cannot_be_submitted_as_an_executable_plan(tmp_path):
    store, run, request, _ = _case(tmp_path)
    catalog, _ = context._tools(run, run.permission.allowed_tools)
    contract = context._code_authoring_contract(run, catalog)
    assert set(contract) == {"meaning", "writer", "consumer", "goal_routes"}
    assert contract["writer"]["tool"] == "workspace.write"
    assert contract["consumer"]["tool"] == "python.execute"
    assert "complete authorized task code" in contract["writer"]["fields"]["parameters.content"]
    with pytest.raises(ValueError):
        DecisionIntent.model_validate(contract)
    # A separately authored real implementation still uses the unchanged schema.
    plan = materialize_plan(store, run, _proposal())
    assert len(plan.steps) == 2
    writer, consumer = plan.steps
    assert writer.tool == "workspace.write" and writer.parameters.role == "python"
    assert consumer.parameters.code.producer_step_id == writer.id
    assert consumer.parameters.code.port == "written_artifact"
    assert consumer.depends_on == [writer.id]
    assert consumer.parameters.sdk_orca is False and consumer.parameters.observation_names == []
    assert plan.goal_map["report"].step_id == consumer.id
    assert plan.goal_map["report"].port == "artifacts"
    assert not run.calls and not run.attempts and not run.result_ids
    assert store.load_request(run) == request


def test_managed_execution_is_not_a_written_artifact_and_feedback_names_consumer(tmp_path):
    store, run, _, _ = _case(tmp_path)
    proposal = _proposal()
    proposal["steps"].append({"key": "analyze_nmr", "tool": "python.execute", "parameters": {
        "code": {"producer_key": "write", "port": "written_artifact"}, "sdk_orca": False,
        "inputs": {"entries": [{"producer_key": "consume", "port": "written_artifact",
                                "target_path": "raw.out", "purpose": "inspect original evidence"}]}}})
    before = deepcopy(proposal)
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "file_producer_port_mismatch"
    assert detail["path"] == ["parameters", "steps", 2, "parameters", "inputs", "entries", 0]
    assert detail["producer_tool"] == "python.execute"
    assert "written_artifact" not in detail["declared_outputs"]
    assert detail["file_key_source"] == "producer Result.source.files"
    assert proposal == before and not run.attempts and not run.calls


def test_writer_filename_correction_keeps_expected_and_requested_names(tmp_path):
    store, run, _, _ = _case(tmp_path)
    proposal = _proposal(code={"producer_key": "write", "file_key": "wrong.py"})
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "writer_file_key_mismatch"
    assert detail["path"] == ["parameters", "steps", 1, "parameters", "code"]
    assert detail["requested_file_key"] == "wrong.py"
    assert detail["expected_file_key"] == "task.py"
    assert detail["producer_tool"] == "workspace.write"
    assert not run.calls and not run.attempts


def test_unregistered_code_id_keeps_exact_permission_error_path(tmp_path):
    store, run, _, _ = _case(tmp_path)
    before = fingerprint(run)
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, _proposal(code={"artifact_id": "scan_butane_script"}))
    assert caught.value.detail["path"] == ["parameters", "steps", 1, "parameters", "code", "artifact_id"]
    assert "outside the permission snapshot" in str(caught.value)
    assert fingerprint(run) == before


def _a015():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a015/data"
    run_id = "run_8a1b98d41aca459b98cb10392de4ba39"
    if not (root / "runs" / run_id / "run.json").is_file():
        pytest.skip("local a015 archive is not available")
    store = Store(root)
    return store, store.load_run(run_id)


def test_actual_a015_writer_mismatch_exposes_precise_consumer_and_names():
    store, run = _a015()
    path = store.path(f"runs/{run.id}/model/model_ce0aa97665e143debf33007adb92b3f4.response.json")
    original = path.read_bytes()
    model = next(item for item in run.model_records if item["id"] == path.name.split(".")[0])
    assert hashlib.sha256(original).hexdigest() == model["response_record_sha256"]
    parameters = json.loads(original)["proposal"]["parameters"]
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, parameters)
    detail = caught.value.detail
    assert detail["code"] == "writer_file_key_mismatch"
    assert detail["path"][:2] == ["parameters", "steps"]
    assert detail["path"][-3:] == ["inputs", "entries", 0]
    assert detail["expected_file_key"] != detail["requested_file_key"]
    assert detail["producer_tool"] == "workspace.write"
    assert path.read_bytes() == original and store.load_run(run.id) == run


def test_actual_a015_sdk_rejections_survive_complete_context_projection():
    from orca_agent.decision_purpose import relevant_result_ids
    from orca_agent.delivery import collect_delivery_snapshot

    store, archived = _a015()
    result = store.load_result(archived.id, "result_7fb798a466c441be8c1da5e94b6e86b8")
    result_path = store.path(f"runs/{archived.id}/results/{result.id}.json")
    before = result_path.read_bytes()
    assert result.operation_status == "completed" and len(result.diagnostics) == 7
    projected = context._diagnostic_records(result.diagnostics, 256)
    error = next(row for row in projected if row["category"] == "managed_sdk_request_rejected")
    assert "unsupported geometry constraint shape" in error["detail"]
    assert error["count"] == 7 and len(error["responses"]) == 4 and error["omitted_response_count"] == 3
    assert "execution_started" not in error
    for source, original in zip(error["responses"], result.diagnostics):
        assert source["artifact_id"] == original["artifact_id"] and source["sha256"] == original["sha256"]
        assert hashlib.sha256(store.artifact_path(source["artifact_id"]).read_bytes()).hexdigest() == source["sha256"]
    # Reconstruct the real next-send model counters. The complete current
    # Request/Plan and immutable execution evidence are unchanged; no send runs.
    run = archived.model_copy(deep=True)
    next_send = next(item for item in run.model_records if item["id"] == "model_6293b267db8a4fddb3f311dc870045a5")
    preceding = run.model_records[:run.model_records.index(next_send)]
    run.usage.model_calls = len(preceding)
    run.usage.decision_rounds = len(preceding)
    run.usage.model_tokens_used = sum(item["total_tokens"] for item in preceding)
    run.usage.model_tokens_unknown = 0
    run.model_records = preceding
    run.processed_feedback = [identifier for identifier in run.processed_feedback if identifier != result.id]
    request, plan = store.load_request(run), store.load_plan(run)
    assert next_send["basis"]["request_version"] == request.version
    assert next_send["basis"]["plan_version"] == plan.version
    results = {row.step_id: row for row in (store.load_result(run.id, identifier) for identifier in run.result_ids)}
    snapshot = collect_delivery_snapshot(store, run, request, plan, results,
                                        control_generation=run.control_generation)
    selected = [store.load_result(run.id, identifier) for identifier in relevant_result_ids(
        run, plan, snapshot=snapshot, feedback_ids=[result.id])]
    prepared = context.build_context(request, run, plan, results=selected,
        feedback={"new_result_ids": [result.id], "delivery_snapshot": snapshot, "pending_step_ids": []},
        delivery_snapshot=snapshot, relevant_tools=run.permission.allowed_tools,
        now=datetime.fromisoformat(next_send["created_at"]), model_profile="disabled")
    wire = payload(prepared)
    assert "unsupported geometry constraint shape" in json.dumps(wire, ensure_ascii=False)
    assert prepared.input_token_bound <= 64000
    assert result_path.read_bytes() == before and store.load_run(archived.id) == archived
