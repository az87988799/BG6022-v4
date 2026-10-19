"""Execution receipt facts survive selected-Result projection; no live calls."""

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_general_input_goals_live004 import completed_input_chain
from test_plan_goal_binding_contract import matches

from orca_agent.context import _recorded_step_execution, _result, _schema, build_context
from orca_agent.decision_purpose import relevant_result_ids
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.models import EvidenceRef, Result
from orca_agent.proposals import ProposedPlan
from orca_agent.readable_context import native_schema
from orca_agent.store import Store


def input_context(tmp_path, monkeypatch, *, ready=(), evidence_reads=24, code=True):
    store, run, request, _, prepared_result, identity = completed_input_chain(tmp_path, monkeypatch)
    plan = store.load_plan(run)
    run.permission = run.permission.model_copy(update={"code_execution": code})
    run.budget = run.budget.model_copy(update={"input_tokens": 48000, "model_tokens": 1200000,
                                              "knowledge_queries": run.usage.knowledge_queries + 2,
                                              "evidence_reads": evidence_reads})
    run.configuration = run.configuration.model_copy(update={
        "model": run.configuration.model.model_copy(update={"input_tokens": 48000})})
    snapshot = collect_delivery_snapshot(store, run, request, plan, [prepared_result, identity])
    feedback = {"new_result_ids": [prepared_result.id]}
    if ready is not None:
        feedback["pending_step_ids"] = list(ready)
    before = copy.deepcopy((run, request, plan, identity, prepared_result))
    context = build_context(request, run, plan, results=[identity, prepared_result],
                            delivery_snapshot=snapshot, feedback=feedback)
    wire = json.loads(context.body()["messages"][1]["content"])
    assert (run, request, plan, identity, prepared_result) == before
    return run, plan, identity, prepared_result, wire


def test_omitted_identity_result_still_has_completed_receipt_and_empty_ready_fact(tmp_path, monkeypatch):
    run, plan, identity, result, wire = input_context(tmp_path, monkeypatch)
    assert identity.id not in relevant_result_ids(run, plan, feedback_ids=[result.id])
    assert identity.id not in [item["result_id"] for item in wire["DATA"]["results"]]
    assert wire["CONTROL"]["pending_step_ids"] == []
    for item in (identity, result):
        assert wire["AUTHORITY"]["step_execution"][item.step_id] == [{
            "record_id": item.call_id, "state": "completed", "result_id": item.id}]
    assert "call_tool" not in wire["ACTION_SCHEMAS"]
    assert "structure.resolve" not in wire["ACTION_SCHEMAS"]
    assert "knowledge.search" in wire["ACTION_SCHEMAS"]
    assert all(tool["name"] in run.permission.allowed_tools for tool in wire["TOOLS"] if tool.get("immediate"))
    assert "revise_plan" in wire["ACTION_SCHEMAS"]


def test_completed_input_context_retains_reference_contract_for_plan_revision(tmp_path, monkeypatch):
    _, plan, _, result, wire = input_context(tmp_path, monkeypatch)
    schema = wire["ACTION_SCHEMAS"]["revise_plan"]
    reference = schema["$defs"]["EvidenceRef"]
    assert reference == native_schema(EvidenceRef.model_json_schema())
    reference = _schema(reference)  # Annotation defaults do not affect validity.
    fixture = Path(__file__).parents[1] / "fixtures/code_contracts/live012-file-only-evidence.json"
    rejected = json.loads(fixture.read_text(encoding="utf-8"))["proposal"]["parameters"]
    file_only = rejected["goal_map"]["goal_prepared_geometry"]["evidence"]
    assert not matches(reference, file_only, schema)
    assert matches(reference, {"producer_step_id": result.step_id, "port": "prepared_geometry"}, schema)
    assert matches(reference, {"run_id": result.run_id, "result_id": result.id,
                               "port": "prepared_geometry"}, schema)
    # Revision guidance does not replace the accepted binding or authorize a
    # rerun of the completed input Step.
    assert wire["AUTHORITY"]["plan"]["goal_map"] == plan.model_dump(mode="json", exclude_none=True)["goal_map"]
    assert wire["CONTROL"]["pending_step_ids"] == []


def test_native_revision_exposes_complete_plan_schema_and_rejects_live013_runtime_fields(tmp_path, monkeypatch):
    from orca_agent import context

    prompts = []
    prepare = context.prepare_request

    def capture(messages, **kwargs):
        prompts.append(messages[0]["content"])
        return prepare(messages, **kwargs)

    monkeypatch.setattr(context, "prepare_request", capture)
    _, plan, _, _, wire = input_context(tmp_path, monkeypatch)
    schema = wire["ACTION_SCHEMAS"]["revise_plan"]
    assert schema == native_schema(ProposedPlan.model_json_schema())
    schema = _schema(schema)
    fixture = Path(__file__).parents[1] / "fixtures/code_contracts/live013-runtime-plan-fields.json"
    replies = json.loads(fixture.read_text(encoding="utf-8"))["replies"]
    assert len(replies) == 3
    for reply in replies:
        assert not matches(schema, reply["proposal"]["parameters"], schema)
    step_rule = schema["$defs"]["ProposedStep"]
    assert step_rule["required"] == ["key"]
    assert step_rule["additionalProperties"] is False
    assert not {"id", "logical_id", "immutable_frozen"} & step_rule["properties"].keys()
    # A test-authored retention proposal proves the advertised shape. It is
    # never applied or presented as a model-generated correction.
    retained = {"steps": [{"key": step.id} for step in plan.steps], "goal_map": {}}
    for goal_id, binding in plan.goal_map.items():
        value = binding.model_dump(exclude_none=True)
        if "step_id" in value:
            value["step_key"] = value.pop("step_id")
        retained["goal_map"][goal_id] = value
    assert matches(schema, retained, schema)
    assert "Retain an existing Step as {key:existing Step.id}" in prompts[-1]
    assert "Goal bindings use step_key=that key, not runtime step_id" in prompts[-1]


def test_unscheduled_context_does_not_invent_empty_ready_constraint(tmp_path, monkeypatch):
    _, _, identity, _, wire = input_context(tmp_path, monkeypatch, ready=None)
    assert "pending_step_ids" not in wire["CONTROL"]
    assert "step_execution" not in wire["AUTHORITY"]
    schema = wire["ACTION_SCHEMAS"]["call_tool"]
    assert matches(schema, {"step_id": identity.step_id}, schema)  # Structural only; runtime still gates.


def test_noncode_context_keeps_pending_and_results_without_new_execution_display(tmp_path, monkeypatch):
    from orca_agent import context

    prompts = []
    prepare = context.prepare_request

    def capture(messages, **kwargs):
        prompts.append(messages[0]["content"])
        return prepare(messages, **kwargs)

    monkeypatch.setattr(context, "prepare_request", capture)
    run, plan, identity, result, wire = input_context(tmp_path, monkeypatch, code=False)
    assert wire["CONTROL"]["pending_step_ids"] == []
    assert "step_execution" not in wire["AUTHORITY"]
    assert "AUTHORITY.step_execution" not in prompts[-1]
    assert "call_tool" not in wire["ACTION_SCHEMAS"]
    assert "structure.resolve" not in wire["ACTION_SCHEMAS"]
    assert any(item["result_id"] == result.id for item in wire["DATA"]["results"])
    assert _recorded_step_execution(plan, run)[identity.step_id] == [{
        "record_id": identity.call_id, "state": "completed", "result_id": identity.id}]


def test_zero_read_budget_does_not_hide_available_knowledge_query(tmp_path, monkeypatch):
    run, _, _, _, wire = input_context(tmp_path, monkeypatch, evidence_reads=0)
    assert run.budget.evidence_reads == 0
    assert run.usage.knowledge_queries < run.budget.knowledge_queries
    assert "call_tool" not in wire["ACTION_SCHEMAS"]
    assert "knowledge.search" in wire["ACTION_SCHEMAS"]
    assert "evidence.text" not in wire["ACTION_SCHEMAS"]


@pytest.mark.parametrize("state", ["failed", "unknown", "cancelled", "completed"])
def test_receipts_preserve_state_without_claiming_scientific_success(tmp_path, monkeypatch, state):
    run, plan, identity, _, _ = input_context(tmp_path, monkeypatch)
    call = next(item for item in run.calls if item.id == identity.call_id)
    call.state = state
    view = _recorded_step_execution(plan, run)
    assert view[identity.step_id] == [{"record_id": call.id, "state": state, "result_id": identity.id}]
    assert "qualified" not in str(view)
    call.parameters = {"system_id": "different"}
    assert identity.step_id not in _recorded_step_execution(plan, run)


def test_input_artifact_keeps_original_hash_beyond_three_artifact_preview(tmp_path, monkeypatch):
    _, _, _, result, wire = input_context(tmp_path, monkeypatch)
    visible = next(item for item in wire["DATA"]["results"] if item["result_id"] == result.id)
    output = result.qualified_outputs["prepared_geometry"]
    assert visible["qualified_outputs"]["prepared_geometry"]["artifact_id"] == output.artifact_id
    assert visible["qualified_outputs"]["prepared_geometry"]["sha256"] == output.source["sha256"]
    for source in ({"tool": "python.execute"}, {"tool": "unregistered_tool"}):
        altered = result.model_copy(update={"source": source})
        assert "sha256" not in _result(altered, 256)["qualified_outputs"]["prepared_geometry"]
    altered = result.model_copy(update={"artifact_ids": []})
    assert "sha256" not in _result(altered, 256)["qualified_outputs"]["prepared_geometry"]


def test_retained_live009_projection_records_both_completed_steps_without_rewriting_evidence():
    root = Path("data/code-tools-step3/live-store")
    run_id = "run_acb8d2a0e9ed400896f23533af95d9f3"
    directory = root / "runs" / run_id
    if not directory.is_dir():
        pytest.skip("retained live009 unavailable; no live evidence fabricated")
    store = object.__new__(Store)
    store.root = root.absolute()
    run = store.load_run(run_id)
    request, plan = store.load_request(run), store.load_plan(run)
    paths = list((directory / "model").glob("*.json"))
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    model_id = run.model_records[-1]["id"]
    snapshot = json.loads((directory / "model" / f"{model_id}.delivery.json").read_text(encoding="utf-8"))
    results = [Result.model_validate_json(path.read_text(encoding="utf-8"))
               for path in (directory / "results").glob("*.json")]
    prepared = build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
        feedback={"pending_step_ids": [], "new_result_ids": [run.calls[-1].result_id]},
        now=run.created_at + timedelta(seconds=60))
    wire = json.loads(prepared.body()["messages"][1]["content"])
    assert wire["CONTROL"]["pending_step_ids"] == []
    assert set(wire["AUTHORITY"]["step_execution"]) == {step.id for step in plan.steps}
    assert all(entry["state"] == "completed" for entries in wire["AUTHORITY"]["step_execution"].values()
               for entry in entries)
    assert "call_tool" not in wire["ACTION_SCHEMAS"]
    assert "structure.resolve" not in wire["ACTION_SCHEMAS"]
    assert "evidence.text" in wire["ACTION_SCHEMAS"]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths} == hashes
