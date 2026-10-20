"""Read-only replay of the settled H1 execution that disappeared after a read."""

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest

from orca_agent.context import _decision_result_references, _frozen_completed, _recorded_step_execution, _result, build_context
from orca_agent.decision_purpose import current_execution_result_ids, relevant_result_ids
from orca_agent.file_references import declared_attempt_step
from orca_agent.models import Plan, Result, Run
from orca_agent.proposals import proposed_plan_schema
from orca_agent.store import Store
from tests.unit.test_context import payload


ROOT = Path(__file__).parents[2] / "data/live-repair/attempts/a003"
FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live-h1-a003-execution-receipt.json"
EXECUTION_RESULT = "result_f379943cdb164b2aa94cadd212a34a0f"
READ_RESULT = "result_0b03d2d057be400db03efd35d0f7ce91"
MODEL = "model_4770bcceb8c34b17aeed4aa589b1c8a8"
STDOUT = "artifact_ecd099752a6e4cb3aee83c1fb0819219"


def retained(*, live=False):
    if not live:
        assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
            "5cab8fb8e2e47070e695600e794a9d587583d3cc24a54ab60f625418c3c150b2")
        data = json.loads(FIXTURE.read_bytes())
        run, plan, result = Run.model_validate(data["run"]), Plan.model_validate(data["plan"]), Result.model_validate(data["result"])
        return None, run, None, plan, run.attempts[0], result
    if not (ROOT / "start.json").exists():
        pytest.skip("retained H1 a003 unavailable; no real execution evidence fabricated")
    start = json.loads((ROOT / "start.json").read_bytes())
    store = object.__new__(Store)
    store.root = (ROOT / "data").resolve()
    run = store.load_run(start["run_id"])
    request, plan = store.load_request(run), store.load_plan(run)
    attempt = next(item for item in run.attempts if item.result_id == EXECUTION_RESULT)
    result = store.load_result(run.id, EXECUTION_RESULT)
    assert attempt.state == result.operation_status == "completed"
    assert not result.qualified_outputs
    return store, run, request, plan, attempt, result


def test_retained_h1_receipt_uses_approved_declaration_and_survives_later_read():
    _, run, _, plan, attempt, result = retained()
    step = next(item for item in plan.steps if item.id == attempt.step_id)
    assert attempt.frozen_step != step  # Concrete pinned files versus symbolic producer refs.
    assert declared_attempt_step(attempt) == step
    before = copy.deepcopy((run, plan, result))
    assert _recorded_step_execution(plan, run)[step.id] == [{
        "record_id": attempt.id, "state": "completed", "result_id": result.id}]
    assert _frozen_completed(plan, run, [result])[step.id] == step.model_dump(mode="json")
    assert result.id in relevant_result_ids(run, plan, feedback_ids=[READ_RESULT])
    assert all(binding.gap for binding in plan.goal_map.values())
    assert (run, plan, result) == before
    assert not result.qualified_outputs


@pytest.mark.parametrize("mutation", ["changed_contract", "wrong_identity", "unfinished", "unregistered_result"])
def test_completed_execution_selection_still_requires_the_current_settled_receipt(mutation):
    _, run, _, plan, attempt, result = retained()
    if mutation == "changed_contract":
        step = next(item for item in plan.steps if item.id == attempt.step_id)
        step.parameters.conditions = {"environment": "different"}
    elif mutation == "wrong_identity":
        attempt.logical_id = "other_logical_id"
    elif mutation == "unfinished":
        attempt.finished_at = None
    else:
        run.result_ids.remove(result.id)
    assert result.id not in current_execution_result_ids(run, plan)


def test_compact_success_reference_retains_source_index_without_scientific_upgrade():
    _, run, _, plan, _, result = retained()
    projected = _result(result, 2048)
    shown, references = _decision_result_references(run, plan, [result], [projected],
                                                    {"new_result_ids": [READ_RESULT]})
    assert not shown
    reference, = references
    assert reference["file_index"] == projected["file_index"]
    assert STDOUT in json.dumps(reference["file_index"])
    assert reference["attempt_id"] == result.attempt_id
    assert not reference.get("qualified_outputs")
    assert "unqualified_observations" not in reference


def test_actual_h1_later_read_wire_keeps_execution_and_raw_output_discoverable():
    store, run, request, plan, attempt, result = retained(live=True)
    directory = store.root / "runs" / run.id
    original_path = directory / "model" / f"{MODEL}.request.json"
    original_hash = hashlib.sha256(original_path.read_bytes()).hexdigest()
    snapshot = json.loads((directory / "model" / f"{MODEL}.delivery.json").read_bytes())
    selected = relevant_result_ids(run, plan, snapshot=snapshot, feedback_ids=[READ_RESULT])
    results = [store.load_result(run.id, identifier) for identifier in selected]
    prepared = build_context(request, run, plan, results=results, delivery_snapshot=snapshot,
        feedback={"pending_step_ids": [], "new_result_ids": [READ_RESULT]},
        relevant_tools=run.permission.allowed_tools,
        action_parameters={"revise_plan": proposed_plan_schema()},
        now=run.created_at + timedelta(seconds=180))
    wire = payload(prepared)
    assert wire["AUTHORITY"]["step_execution"][attempt.step_id] == [{
        "record_id": attempt.id, "state": "completed", "result_id": result.id}]
    assert STDOUT in json.dumps(wire["DATA"])
    assert result.id not in wire["AUTHORITY"]["other_result_ids"]
    assert wire["AUTHORITY"]["goal_status"]["goal_giao_shielding_tensor"] == "insufficient_evidence"
    assert hashlib.sha256(original_path.read_bytes()).hexdigest() == original_hash
