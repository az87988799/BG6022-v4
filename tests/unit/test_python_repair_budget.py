"""Offline reservation/repair contracts; these fixtures do not launch processes."""

import json

import pytest

from orca_agent import agent
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    InputManifestEntry,
    InternalCall,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.planning import PlanningError, additional_code_attempts_used, validate_revision
from orca_agent.store import BudgetExceeded, EnvironmentBusy, Store, StoreError
from orca_agent.tools import managed


def case(tmp_path, monkeypatch, *, allowance=1, initial_steps=1):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda name: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    codes = []
    for number in range(3):
        source = tmp_path / f"code-{number}.py"
        source.write_text(f"print({number})\n")
        artifact = store.import_artifact(source, "code")
        codes.append(CodeRef(artifact_id=artifact.id, sha256=artifact.sha256))
    request = Request(goals=[Goal(id="report", port="artifacts",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])
    steps = [Step(id=f"script_{n}", logical_id=f"analysis_{n}", tool="python.execute",
                  parameters={"code": codes[n]}) for n in range(initial_steps)]
    plan = Plan(request_id=request.id, steps=steps,
                goal_map={"report": OutputBinding(step_id=steps[-1].id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(code_execution=True,
        artifact_writes=True, artifact_ids=[code.artifact_id for code in codes],
        allowed_tools=["python.execute"]),
        BudgetLimits(code_retries=allowance, plan_revisions=3))
    return store, run, codes


def settle(store, run, *, state="failed", execution_changes=None, collect_complete=True):
    step = store.load_plan(run).steps[0]
    attempt = store.reserve_attempt(run, step)
    execution = {"state": state, "reason": "nonzero_exit_code", "exit_code": 1,
                 "handle": None, "resource_usage": {}, **(execution_changes or {})}
    store._write_json(f"{attempt.directory}/execution.json", execution, immutable=True)
    result = managed.collect_result(store, run, step, attempt, execution)
    result.observations["evidence_collection_complete"] = collect_complete
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state=state, result_id=result.id,
                         started=True, termination_confirmed=state != "unknown")
    return attempt, result


def revision(store, run, code, *, logical=None):
    plan = store.load_plan(run)
    step = plan.steps[0].model_copy(deep=True)
    step.id = "replacement"
    step.logical_id = logical or step.logical_id
    step.parameters.code = code
    revised = plan.model_copy(deep=True, update={"version": plan.version + 1, "steps": [step]})
    revised.goal_map["report"].step_id = step.id
    return revised


@pytest.mark.parametrize("first_state", ["completed", "failed", "timed_out", "not_started"])
def test_new_logical_and_new_hash_cannot_reset_zero_additional_allowance(tmp_path, monkeypatch, first_state):
    store, run, _ = case(tmp_path, monkeypatch, allowance=0, initial_steps=2)
    plan = store.load_plan(run)
    first = store.reserve_attempt(run, plan.steps[0])
    store.finish_attempt(run, first.id, state=first_state,
                         started=first_state != "not_started", termination_confirmed=True)
    assert plan.steps[0].logical_id != plan.steps[1].logical_id
    assert plan.steps[0].parameters.code.sha256 != plan.steps[1].parameters.code.sha256
    with pytest.raises(BudgetExceeded, match="code retry"):
        store.reserve_attempt(run, plan.steps[1])
    assert run.usage.code_retries == 0 and len(run.attempts) == 1
    assert store.environment_lease() is None


def test_independent_analysis_consumes_additional_allowance_across_reopen(tmp_path, monkeypatch):
    store, run, _ = case(tmp_path, monkeypatch, initial_steps=3)
    plan = store.load_plan(run)
    for step in plan.steps[:2]:
        attempt = store.reserve_attempt(run, step)
        store.finish_attempt(run, attempt.id, state="completed", started=True, termination_confirmed=True)
    assert [attempt.number for attempt in run.attempts] == [1, 1]
    assert run.usage.code_retries == 1
    reopened = Store(store.root, environment_root=store.environment_root)
    loaded = reopened.load_run(run.id)
    with pytest.raises(BudgetExceeded, match="code retry"):
        reopened.reserve_attempt(loaded, plan.steps[2])
    assert loaded.usage.code_retries == 1 and loaded.usage.orca_starts_reserved == 0


def test_unknown_python_keeps_lease_and_blocks_new_logical_code(tmp_path, monkeypatch):
    store, run, _ = case(tmp_path, monkeypatch, initial_steps=2)
    plan = store.load_plan(run)
    first = store.reserve_attempt(run, plan.steps[0])
    store.finish_attempt(run, first.id, state="failed", started=True, termination_confirmed=False)
    with pytest.raises(EnvironmentBusy, match="reconciliation"):
        store.reserve_attempt(run, plan.steps[1])
    assert store.environment_lease()["attempt_id"] == first.id
    assert run.usage.code_retries == 0


def test_historical_undercount_cannot_grant_additional_execution_or_rewrite_history(tmp_path, monkeypatch):
    store, run, codes = case(tmp_path, monkeypatch, allowance=2, initial_steps=3)
    plan = store.load_plan(run)
    for step in plan.steps:
        attempt = store.reserve_attempt(run, step)
        store.finish_attempt(run, attempt.id, state="completed", started=True, termination_confirmed=True)
    # Model the persisted historical bug: three distinct logical/code attempts
    # used to leave this counter zero. No production method lowers a counter.
    historical = run.model_dump(mode="json")
    historical["usage"]["code_retries"] = 0
    path = store.path(f"runs/{run.id}/run.json")
    path.write_text(json.dumps(historical), encoding="utf-8")
    before = path.read_bytes()
    reopened = Store(store.root, environment_root=store.environment_root)
    loaded = reopened.load_run(run.id)
    assert additional_code_attempts_used(loaded) == 2 and loaded.usage.code_retries == 0
    with pytest.raises(BudgetExceeded, match="code retry"):
        reopened.reserve_attempt(loaded, plan.steps[-1])
    candidate = revision(reopened, loaded, codes[1], logical="renamed_again")
    with pytest.raises(PlanningError, match="additional code attempt"):
        reopened.commit_revision(loaded, candidate, decision_id="legacy_retry",
                                 basis=current_basis(reopened, loaded))
    assert path.read_bytes() == before


def test_changed_code_repair_retains_logical_identity_and_frozen_failure(tmp_path, monkeypatch):
    store, run, codes = case(tmp_path, monkeypatch)
    attempt, result = settle(store, run)
    frozen = attempt.model_dump()
    candidate = revision(store, run, codes[1])
    agent._repair_evidence(store, run, candidate)
    updated = store.commit_revision(run, candidate, decision_id="repair_decision",
                                    basis=current_basis(store, run), related_results=[result.id])
    retry = store.reserve_attempt(updated, candidate.steps[0])
    assert retry.logical_id == attempt.logical_id and retry.number == 2
    assert updated.usage.code_retries == 1
    assert updated.attempts[0].model_dump() == frozen
    assert not result.qualified_outputs


@pytest.mark.parametrize("change", ["conditions", "observations", "same_hash", "same_step"])
def test_python_repair_cannot_change_purpose_or_rename_unchanged_code(tmp_path, monkeypatch, change):
    store, run, codes = case(tmp_path, monkeypatch)
    settle(store, run)
    candidate = revision(store, run, codes[1])
    step = candidate.steps[0]
    if change == "conditions":
        step.parameters.conditions["analysis_mode"] = "different"
    elif change == "observations":
        step.parameters.observation_names.append("different_quantity")
    elif change == "same_hash":
        step.parameters.code = codes[0]
    else:
        step.id = run.attempts[0].step_id
        candidate.goal_map["report"].step_id = step.id
    with pytest.raises(PlanningError):
        validate_revision(store.load_request(run), store.load_plan(run), store.load_request(run), candidate, run)


def test_input_only_repair_freezes_new_copy_and_consumes_existing_retry_budget(tmp_path, monkeypatch):
    store, run, codes = case(tmp_path, monkeypatch)
    attempt, result = settle(store, run)
    original = attempt.model_dump(mode="json")
    candidate = revision(store, run, codes[0])
    candidate.steps[0].parameters.inputs.entries.append(InputManifestEntry(
        artifact_id=codes[2].artifact_id, sha256=codes[2].sha256,
        target_path="coords.xyz", purpose="corrected task input copy"))
    agent._repair_evidence(store, run, candidate)
    updated = store.commit_revision(run, candidate, decision_id="explicit_input_repair",
        basis=current_basis(store, run), related_results=[result.id])
    repaired = store.reserve_attempt(updated, candidate.steps[0])
    assert repaired.number == 2 and repaired.logical_id == attempt.logical_id
    assert repaired.frozen_step.parameters.code == attempt.frozen_step.parameters.code
    assert repaired.frozen_step.parameters.inputs.entries[0].target_path == "coords.xyz"
    assert repaired.input_fingerprint != attempt.input_fingerprint
    assert updated.usage.code_retries == 1 and updated.usage.orca_starts_reserved == 0
    assert updated.attempts[0].model_dump(mode="json") == original
    assert not repaired.started


def test_plan_preflight_counts_new_logical_code_against_run_wide_allowance(tmp_path, monkeypatch):
    store, run, codes = case(tmp_path, monkeypatch, allowance=0)
    settle(store, run)
    candidate = revision(store, run, codes[1], logical="independent_analysis")
    with pytest.raises(PlanningError, match="additional code attempt"):
        store.commit_revision(run, candidate, decision_id="new_analysis", basis=current_basis(store, run))
    assert store.load_run(run.id).usage.plan_revisions == 0


def test_completed_python_cannot_be_relabelled_as_same_logical_failure_repair(tmp_path, monkeypatch):
    store, run, codes = case(tmp_path, monkeypatch)
    settle(store, run, state="completed")
    candidate = revision(store, run, codes[1])
    with pytest.raises(PlanningError, match="settled failed attempt"):
        store.commit_revision(run, candidate, decision_id="false_repair", basis=current_basis(store, run))
    independent = revision(store, run, codes[1], logical="independent_analysis")
    updated = store.commit_revision(run, independent, decision_id="independent_analysis",
                                    basis=current_basis(store, run))
    additional = store.reserve_attempt(updated, independent.steps[0])
    assert additional.number == 1 and updated.usage.code_retries == 1


@pytest.mark.parametrize("failure", ["completed", "unknown", "lost_coordinator", "collection",
                                     "internal_unknown", "wrong_code_hash", "missing_receipt"])
def test_python_repair_requires_settled_failure_and_intact_evidence(tmp_path, monkeypatch, failure):
    store, run, codes = case(tmp_path, monkeypatch)
    state = failure if failure in {"completed", "unknown"} else "failed"
    execution_changes = {"reconciliation": {"state": "terminated"}} if failure == "lost_coordinator" else None
    attempt, result = settle(store, run, state=state, execution_changes=execution_changes,
                             collect_complete=failure != "collection")
    if failure == "internal_unknown":
        run.internal_calls.append(InternalCall(request_key="internal", input_fingerprint="a" * 64,
            state="unknown", parent_attempt_id=attempt.id))
    if failure in {"wrong_code_hash", "missing_receipt"}:
        # Direct validator input represents a corrupted/unbound receipt, not a
        # replacement of the immutable historical Result stored on disk.
        result = result.model_copy(deep=True)
        if failure == "wrong_code_hash":
            result.source["code_sha256"] = codes[1].sha256
        else:
            result.source["files"].pop("execution.json")
        with pytest.raises(StoreError, match="Python repair"):
            agent._python_repair_evidence(store, run, attempt, result)
    else:
        with pytest.raises(StoreError, match="repair"):
            agent._repair_evidence(store, run, revision(store, run, codes[1]))


def test_archived_execution_content_must_match_result(tmp_path, monkeypatch):
    store, run, _ = case(tmp_path, monkeypatch)
    attempt, result = settle(store, run)
    changed = result.model_copy(deep=True)
    changed.source["execution"]["exit_code"] = 9
    with pytest.raises(StoreError, match="execution receipt differs"):
        agent._python_repair_evidence(store, run, attempt, changed)
    receipt = result.source["files"]["execution.json"]
    assert json.loads(store.artifact_path(receipt["artifact_id"]).read_text())["exit_code"] == 1
