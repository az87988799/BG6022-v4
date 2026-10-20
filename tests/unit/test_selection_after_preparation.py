"""a008 selection closure regression; inert records, no model or execution."""

import copy
import json

import pytest
from test_simplified_semantic_selection import choices, selected_fixture

from orca_agent.condition_selection import commit_selections, selection_available
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    Attempt,
    Check,
    InternalCall,
    QualifiedOutput,
    Result,
    Step,
    ToolCall,
    utc_now,
)
from orca_agent.store import StoreError


def preparation_history(tmp_path, *, native=False):
    store, run = selected_fixture(tmp_path)
    raw = tmp_path / ("input.inp" if native else "metadata.py")
    raw.write_text("! PBE0 def2-SVP\n" if native else "seed = 1701\n", encoding="utf-8")
    code = store.import_artifact(raw, "native_input" if native else "task_code", run_id=run.id)
    reference = {"artifact_id": code.id, "sha256": code.sha256, "language": "orca" if native else "python"}
    step = Step(id="preparation_step", logical_id="preparation_logical",
        tool="orca.run" if native else "python.execute", parameters=(
            {"input": reference} if native else {"code": reference, "sdk_orca": False}))
    attempt = Attempt(id="preparation_attempt", step_id=step.id, logical_id=step.logical_id,
        number=1, tool=step.tool, execution_kind="orca" if native else "python",
        state="failed" if native else "completed", started=not native, finished_at=utc_now(),
        input_fingerprint="inert_fixture", directory=f"runs/{run.id}/steps/{step.id}/attempt-001",
        frozen_step=step, request_version=run.request_version,
        consumption={"code": (step.parameters.input if native else step.parameters.code).model_dump(mode="json")})
    execution = {"state": attempt.state, "not_started": native,
        "reason": "native conditions unresolved" if native else "process_tree_exited",
        "handle": None, "actual_start_status": "not_started" if native else "not_applicable"}
    receipt_path = tmp_path / "execution.json"
    receipt_path.write_text(json.dumps(execution), encoding="utf-8")
    receipt = store.import_artifact(receipt_path, "execution_record", run_id=run.id, attempt_id=attempt.id)
    result = Result(run_id=run.id, attempt_id=attempt.id, step_id=step.id,
        operation_status=attempt.state, observations={"evidence_collection_complete": True},
        artifact_ids=[receipt.id], source={"execution": execution, "code_sha256": code.sha256,
            "files": {"execution.json": {"artifact_id": receipt.id, "sha256": receipt.sha256}}})
    attempt.result_id = result.id
    run.attempts.append(attempt)
    run.result_ids.append(result.id)
    run.usage.orca_starts_reserved = int(native)
    run.usage.analysis_executions = int(not native)
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result, immutable=True)
    store.save_run(run)
    return store, run, attempt, result, receipt


@pytest.mark.parametrize("native", [False, True])
def test_selection_after_settled_metadata_or_known_not_started_preserves_history(tmp_path, native):
    store, run, attempt, result, _ = preparation_history(tmp_path, native=native)
    before = copy.deepcopy((run.attempts, run.usage, run.budget, run.permission))
    result_path = store.path(f"runs/{run.id}/results/{result.id}.json")
    original = result_path.read_bytes()
    assert selection_available(None, run) and selection_available(store, run)
    updated = commit_selections(store, run, choices(run), decision_id="choose_after_preparation",
                                basis=current_basis(store, run))
    request = store.load_request(updated)
    assert (request.method, request.basis) == ("PBE0", "def2-SVP")
    assert (updated.attempts, updated.usage, updated.budget, updated.permission) == before
    assert result_path.read_bytes() == original
    assert not store.load_result(run.id, result.id).qualified_outputs
    assert updated.attempts[0].started is (not native)
    assert updated.attempts[0].state == ("failed" if native else "completed")
    assert store.load_request_revision(updated, attempt.request_version).method is None


def test_sdk_capability_without_any_native_call_does_not_freeze_selection(tmp_path):
    store, run, attempt, _, _ = preparation_history(tmp_path)
    attempt.frozen_step.parameters.sdk_orca = True
    store._write_json(f"runs/{run.id}/run.json", run)
    before = copy.deepcopy((run.attempts, run.usage))
    assert selection_available(None, run) and selection_available(store, run)
    updated = commit_selections(store, run, choices(run), decision_id="choose_after_unused_sdk",
                                basis=current_basis(store, run))
    assert store.load_request(updated).method == "PBE0"
    assert (updated.attempts, updated.usage) == before
    assert updated.internal_calls == [] and updated.usage.orca_starts_actual == 0


@pytest.mark.parametrize("mutation", ["actual_count", "started", "running", "unknown", "unfinished",
    "missing_result", "sdk_internal", "unsettled_call"])
def test_science_or_unsettled_history_keeps_selection_closed(tmp_path, mutation):
    store, run, attempt, _, _ = preparation_history(tmp_path, native=True)
    if mutation == "actual_count":
        run.usage.orca_starts_actual = 1
    elif mutation == "started":
        attempt.started = True
    elif mutation in {"running", "unknown"}:
        attempt.state = mutation
    elif mutation == "unfinished":
        attempt.finished_at = None
    elif mutation == "missing_result":
        run.result_ids.clear()
    elif mutation == "sdk_internal":
        run.internal_calls.append(InternalCall(request_key="native", input_fingerprint="a" * 64,
            state="completed", started=True, parent_attempt_id=attempt.id, finished_at=utc_now()))
    else:
        run.calls.append(ToolCall(tool="workspace.write", parameters={}, request_version=run.request_version))
    assert not selection_available(None, run)
    assert not selection_available(store, run)


@pytest.mark.parametrize("mutation", ["not_started_false", "unconfirmed", "foreign_result",
    "receipt_hash", "receipt_bytes", "foreign_attempt", "missing_receipt", "code_bytes", "qualified_source"])
def test_summary_never_grants_selection_without_original_execution_proof(tmp_path, mutation):
    store, run, attempt, result, receipt = preparation_history(tmp_path, native=True)
    if mutation == "not_started_false":
        result.source["execution"]["not_started"] = False
    elif mutation == "unconfirmed":
        result.source["execution"]["actual_start_status"] = "unconfirmed"
    elif mutation == "foreign_result":
        result.attempt_id = "foreign"
    elif mutation == "receipt_hash":
        result.source["files"]["execution.json"]["sha256"] = "0" * 64
    elif mutation == "receipt_bytes":
        store.artifact_path(receipt.id).write_text("{}", encoding="utf-8")
    elif mutation == "foreign_attempt":
        receipt.attempt_id = "foreign"
        store._write_json(f"artifacts/{receipt.id}/artifact.json", receipt)
    elif mutation == "missing_receipt":
        result.source["files"] = {}
    elif mutation == "code_bytes":
        store.artifact_path(attempt.frozen_step.parameters.input.artifact_id).write_text("changed", encoding="utf-8")
    else:
        result.qualified_outputs = {"energy": QualifiedOutput(value=-1, unit="Eh",
            checks=[Check(name="fixture", status="passed", rule_version="inert-fixture")])}
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result)
    assert selection_available(None, run)  # Availability is a proposal, never a grant.
    assert not selection_available(store, run)
    with pytest.raises(StoreError, match="actual or uncertain scientific"):
        commit_selections(store, run, choices(run), decision_id="bad", basis=current_basis(store, run))
    assert store.load_request(run).method is None


def test_legacy_workflow_keeps_original_attempt_gate(tmp_path):
    store, run, _, _, _ = preparation_history(tmp_path)
    run.configuration = run.configuration.model_copy(update={"workflow_policy": None})
    assert not selection_available(None, run)
    assert not selection_available(store, run)
