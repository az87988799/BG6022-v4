"""Failure history survives the writer/revision needed for code repair; offline."""

import copy
import json
from pathlib import Path

import pytest

from orca_agent import agent, context, runner
from orca_agent.decision_purpose import relevant_result_ids, settled_failed_python_attempts
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.model_usage import current_basis
from orca_agent.models import Attempt, InputManifestEntry, OutputBinding, Step
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.proposals import materialize_plan
from orca_agent.tools import managed
from tests.unit.test_decision_purpose_capacity import decoded
from tests.unit.test_generated_delivery import written
from tests.unit.test_python_repair_budget import case


def failed_then_writer(tmp_path, monkeypatch, *, unsafe_field=None):
    store, original, codes = case(tmp_path, monkeypatch)
    request, plan = store.load_request(original), store.load_plan(original)
    writer = Step(id="writer_v1", logical_id="writer_v1", tool="workspace.write",
        parameters={"name": "initial.txt", "content": "input evidence\n", "role": "text"})
    failed = plan.steps[0]
    failed.depends_on = [writer.id]
    failed.parameters.inputs.entries = [InputManifestEntry(artifact_id=codes[1].artifact_id, sha256=codes[1].sha256,
        target_path="source.txt", purpose="fixed source for the requested analysis")]
    failed.parameters.conditions = {"analysis_scope": "original file only"}
    if unsafe_field == "condition_key":
        failed.parameters.conditions = {"filename": "geometry.xyz", "custom": "unchanged"}
    elif unsafe_field == "purpose_path":
        failed.parameters.inputs.entries[0].purpose = "input reference file:///task/geometry.xyz"
    failed.parameters.observation_names = ["artifacts"]
    # Revalidate nested fields before storing the fixture's frozen contract.
    failed = Step.model_validate(failed.model_dump(mode="json"))
    plan.steps = [writer, failed]
    permission = original.permission.model_copy(update={"model_execution": True,
        "allowed_tools": ["workspace.write", "python.execute"]})
    limits = original.budget.model_copy(update={"model_calls": 4, "model_tokens": 240000,
        "input_tokens": 64000, "output_tokens": 2000, "decision_rounds": 12,
        "analysis_executions": 3, "plan_revisions": 4})
    configuration = original.configuration.model_copy(update={"model":
        original.configuration.model.model_copy(update={"input_tokens": 64000})})
    run = store.create_run(request, plan, permission, limits, configuration=configuration)
    written(store, run, writer)
    attempt = store.reserve_attempt(run, failed)
    outcome = {"state": "failed", "reason": "nonzero_exit_code", "exit_code": 1,
        "not_started": False, "resource_usage": {}}
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, failed, attempt, outcome)
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="failed", result_id=result.id,
        started=True, termination_confirmed=True)
    replacement_writer = Step(id="writer_v2", logical_id="writer_v2", tool="workspace.write",
        parameters={"name": "fixed.py", "content": "print('corrected')\n", "role": "python"})
    revised = plan.model_copy(deep=True, update={"version": 2, "steps": [writer, replacement_writer],
        "goal_map": {"report": OutputBinding(port="artifacts", gap="code repair pending")}})
    run = store.commit_revision(run, revised, decision_id="repair_writer", basis=current_basis(store, run))
    writer_result, reference = written(store, run, replacement_writer)
    run.processed_feedback = [identifier for identifier in run.result_ids if identifier != writer_result.id]
    store.save_run(run)
    return store, run, request, revised, run.attempts[0], result, writer_result, reference


def test_failure_and_frozen_contract_survive_removal_and_later_writer(tmp_path, monkeypatch):
    store, run, request, plan, attempt, result, writer_result, reference = failed_then_writer(tmp_path, monkeypatch)
    before = copy.deepcopy((run, request, plan, result, writer_result))
    ids = relevant_result_ids(run, plan, feedback_ids=[writer_result.id])
    assert result.id in ids and writer_result.id in ids
    results = [store.load_result(run.id, identifier) for identifier in ids]
    assert agent._ready(plan, runner._step_results(store, run)) == []
    snapshot = collect_delivery_snapshot(store, run, request, plan, results)
    prepared = context.build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
        feedback={"new_result_ids": [writer_result.id], "pending_step_ids": []})
    wire = decoded(prepared)
    assert wire["CONTROL"]["pending_step_ids"] == []
    assert "call_tool" not in wire["ACTION_SCHEMAS"]
    assert attempt.step_id not in {step["id"] for step in wire["AUTHORITY"]["plan"]["steps"]}
    history, = wire["AUTHORITY"]["failed_python_attempts"]
    assert history == {"attempt_id": attempt.id, "state": "failed", "started": True,
        "result_id": result.id, "input_fingerprint": attempt.input_fingerprint,
        "frozen_step": attempt.frozen_step.model_dump(mode="json")}
    visible = next(item for item in wire["DATA"]["results"] if item["result_id"] == result.id)
    assert visible["operation_status"] == "failed" and not visible.get("qualified_outputs")
    assert visible["attempt_id"] == attempt.id
    assert wire["AUTHORITY"]["related_results"] == [writer_result.id]
    assert result.id not in wire["AUTHORITY"]["other_result_ids"]
    assert "not ready/permission" in wire["TASK_CODE_RULES"]
    assert wire["AUTHORITY"]["remaining"]["code_retries"] == 1
    assert prepared.input_token_bound <= 64000
    assert (run, request, plan, result, writer_result) == before
    # The model still has to propose the new Step. Existing validation permits
    # only a changed CodeRef for this historical logical task, not a new purpose.
    params = attempt.frozen_step.parameters.model_dump(mode="json")
    params["code"] = reference
    proposal = {"steps": [{"key": step.id} for step in plan.steps] + [{
        "key": "repair", "logical_key": attempt.logical_id, "tool": "python.execute",
        "parameters": params, "depends_on": attempt.frozen_step.depends_on}],
        "goal_map": {"report": {"step_key": "repair", "port": "artifacts"}}}
    candidate = materialize_plan(store, run, proposal)
    validate_revision(request, plan, request, candidate, run, authorized_artifacts=store.verified_own_artifacts(run))
    # The actual bounded code context advertises the short explicit reference,
    # including its closed field set; expansion preserves the same task.
    from tests.unit.test_plan_goal_binding_contract import matches

    reference_proposal = copy.deepcopy(proposal)
    reference_proposal["steps"][-1] = {"key": "repair", "repair_of": attempt.step_id, "code": reference}
    schema = wire["ACTION_SCHEMAS"]["revise_plan"]
    assert matches(schema, reference_proposal, schema)
    mixed_proposal = copy.deepcopy(reference_proposal)
    mixed_proposal["steps"][-1]["depends_on"] = []
    assert not matches(schema, mixed_proposal, schema)
    expanded = materialize_plan(store, run, reference_proposal)
    assert expanded.steps[-1].model_dump(exclude={"id"}) == candidate.steps[-1].model_dump(exclude={"id"})
    validate_revision(request, plan, request, expanded, run, authorized_artifacts=store.verified_own_artifacts(run))
    changed = candidate.model_copy(deep=True)
    changed.steps[-1].parameters.conditions["analysis_scope"] = "different analysis purpose"
    with pytest.raises(PlanningError, match="conditions, observations and dependencies are fixed"):
        validate_revision(request, plan, request, changed, run,
            authorized_artifacts=store.verified_own_artifacts(run))
    assert len(run.attempts) == 1 and not run.model_records


@pytest.mark.parametrize("state", ["intent", "running", "unknown", "cancelled", "completed", "not_started"])
def test_nonfailure_is_not_a_settled_failure_contract(tmp_path, monkeypatch, state):
    _, run, _, _, attempt, result, writer, _ = failed_then_writer(tmp_path, monkeypatch)
    attempt.state = state
    assert settled_failed_python_attempts(run) == []
    assert result.id not in relevant_result_ids(run, feedback_ids=[writer.id])


@pytest.mark.parametrize("change", ["unfinished", "missing_result", "missing_snapshot", "step", "logical", "tool", "orca"])
def test_incomplete_or_mismatched_history_never_fabricates_a_repair_contract(tmp_path, monkeypatch, change):
    _, run, _, _, attempt, _, _, _ = failed_then_writer(tmp_path, monkeypatch)
    if change == "unfinished":
        attempt.finished_at = None
    elif change == "missing_result":
        run.result_ids.remove(attempt.result_id)
    elif change == "missing_snapshot":
        attempt.frozen_step = None
    elif change == "orca":
        attempt.execution_kind = "orca"
    else:
        setattr(attempt.frozen_step, {"step": "id", "logical": "logical_id", "tool": "tool"}[change], "different")
    assert settled_failed_python_attempts(run) == []


def test_multiple_failures_are_not_replaced_by_later_success_or_budget_exhaustion(tmp_path, monkeypatch):
    _, run, _, _, first, _, _, _ = failed_then_writer(tmp_path, monkeypatch)
    second = first.model_copy(deep=True, update={"id": "second", "result_id": "second_result",
        "number": 2, "state": "timed_out"})
    success = first.model_copy(deep=True, update={"id": "third", "result_id": "third_result",
        "number": 3, "state": "completed"})
    run.attempts.extend([second, success])
    run.result_ids.extend([second.result_id, success.result_id])
    run.usage.code_retries = 2
    before = run.model_copy(deep=True)
    assert settled_failed_python_attempts(run) == [first, second]
    assert [row["attempt_id"] for row in context._failed_python_history(run)] == [first.id, second.id]
    assert run == before


def test_live022_fixture_keeps_failure_that_final_model_context_omitted():
    fixture = json.loads((Path(__file__).parents[1] /
        "fixtures/code_contracts/live022-failed-python-history.json").read_text(encoding="utf-8"))
    attempt = Attempt.model_validate(fixture["failed_attempt"])
    assert attempt.started and attempt.state == "failed"
    assert fixture["old_final_context"]["pending_step_ids"] == []
    assert attempt.result_id not in fixture["old_final_context"]["visible_result_ids"]
    assert attempt.result_id in fixture["old_final_context"]["other_result_ids"]
    assert attempt.step_id not in fixture["old_final_context"]["plan_step_ids"]
    assert attempt.step_id not in fixture["old_final_context"]["step_execution"]
    proposal = fixture["final_proposal"]
    repeated = proposal["parameters"]["steps"][-1]
    assert repeated["key"] != attempt.step_id and "logical_key" not in repeated
    assert repeated["parameters"]["code"]["sha256"] == attempt.frozen_step.parameters.code.sha256
    assert "not executed" in proposal["reason"]
    assert fixture["previous_proposal_reason"].startswith("The v1 Python attempt failed")


@pytest.mark.parametrize("schema_columns", [False, True])
def test_compaction_preserves_literal_full_repair_contract(tmp_path, monkeypatch, schema_columns):
    _, run, _, _, _, _, _, _ = failed_then_writer(tmp_path, monkeypatch)
    history = context._failed_python_history(run)
    template = {"AUTHORITY": {"failed_python_attempts": history, "permission": {"code_execution": True}},
        "ACTION_SCHEMAS": {}, "TOOLS": [], "DATA": {"untrusted_copies": [history] * 30}}
    before = copy.deepcopy(template)
    wire = context._compact_native_wire(template, schema_columns=schema_columns)
    assert wire["AUTHORITY"]["failed_python_attempts"] == history
    assert template == before


@pytest.mark.parametrize("limit", ["missing", "count", "tokens", "wrong_binding"])
def test_failure_context_refuses_omission_or_overflow(tmp_path, monkeypatch, limit):
    store, run, request, plan, first, result, writer, _ = failed_then_writer(tmp_path, monkeypatch)
    results = [store.load_result(run.id, identifier) for identifier in relevant_result_ids(
        run, plan, feedback_ids=[writer.id])]
    snapshot = collect_delivery_snapshot(store, run, request, plan, results)
    if limit == "missing":
        results = [item for item in results if item.id != result.id]
        match = "failure Result must remain"
    elif limit == "count":
        for index in range(33):
            attempt = first.model_copy(deep=True, update={"id": f"failed_{index}", "result_id": f"result_{index}"})
            run.attempts.append(attempt)
            run.result_ids.append(attempt.result_id)
            results.append(result.model_copy(deep=True, update={"id": attempt.result_id, "attempt_id": attempt.id}))
        match = "result selection exceeds bounds"
    elif limit == "tokens":
        run.budget.input_tokens = 512
        run.configuration = run.configuration.model_copy(update={"model":
            run.configuration.model.model_copy(update={"input_tokens": 512})})
        match = "bound"
    else:
        next(item for item in results if item.id == result.id).attempt_id = "wrong_attempt"
        match = "does not match its bound Attempt"
    before = copy.deepcopy((run, request, plan, results))
    with pytest.raises(ValueError, match=match):
        context.build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
            feedback={"new_result_ids": [writer.id], "pending_step_ids": []})
    assert (run, request, plan, results) == before
    assert not run.model_records


@pytest.mark.parametrize("field", ["condition_key", "purpose_path"])
def test_safe_projection_cannot_silently_change_a_valid_frozen_contract(tmp_path, monkeypatch, field):
    store, run, request, plan, attempt, _, writer, _ = failed_then_writer(tmp_path, monkeypatch, unsafe_field=field)
    # The actual Step/Attempt reservation accepts these values. The existing
    # context privacy projection cannot copy them exactly, so stop before HTTP.
    assert Step.model_validate(attempt.frozen_step.model_dump()) == attempt.frozen_step
    results = [store.load_result(run.id, identifier) for identifier in relevant_result_ids(
        run, plan, feedback_ids=[writer.id])]
    snapshot = collect_delivery_snapshot(store, run, request, plan, results)
    history = context._failed_python_history(run)
    assert context._safe(history) != history
    before = copy.deepcopy((run, request, plan, results))
    with pytest.raises(context.ContextLimitError, match="cannot be safely displayed without changes"):
        context.build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
            feedback={"new_result_ids": [writer.id], "pending_step_ids": []})
    assert (run, request, plan, results) == before and not run.model_records
