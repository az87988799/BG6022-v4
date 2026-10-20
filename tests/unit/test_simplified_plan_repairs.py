"""Offline symbolic repairs, retained goal lineage and bounded Python accounting."""

import copy

import pytest

from orca_agent import runner
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    RunConfiguration,
    Step,
    fingerprint,
)
from orca_agent.planning import PlanningError, additional_code_attempts_used, validate_revision
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.store import BudgetExceeded, Store
from orca_agent.tools import managed
from orca_agent.tools.dispatch import execute_call


def case(tmp_path, monkeypatch, *, native=False, policy="simplified-1", retries=1, analysis=4):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(workflow_policy=policy))
    codes = []
    for index in range(4):
        source = tmp_path / f"task-{index}.txt"
        source.write_text(f"# unexecuted task {index}\n", encoding="utf-8")
        codes.append(store.import_artifact(source, "native_input" if native else "code"))
    request = Request(goals=[Goal(id="report", port="artifacts",
        evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
        verification="reported_with_limitations")])
    tool = "orca.run" if native else "python.execute"
    field = "input" if native else "code"
    parameters = {field: CodeRef(artifact_id=codes[0].id, sha256=codes[0].sha256,
                                language="orca" if native else "python")}
    if not native:
        parameters["sdk_orca"] = False
    step = Step(id="original", logical_id="original_work", tool=tool, parameters=parameters)
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"report": OutputBinding(step_id=step.id, port="artifacts")})
    permission = PermissionSnapshot(artifact_writes=True, code_execution=not native,
        scientific_execution=native, allowed_tools=[tool, "workspace.write"],
        artifact_ids=[item.id for item in codes])
    run = store.create_run(request, plan, permission, BudgetLimits(
        code_retries=retries, analysis_executions=analysis, orca_starts=4, extra_orca_starts=3,
        attempts_per_step=4, file_writes=4, plan_revisions=5))
    return store, run, request, plan, codes


def settle(store, run, step, *, state="failed"):
    attempt = store.reserve_attempt(run, step)
    outcome = {"state": state, "reason": "offline synthetic receipt", "not_started": True,
               "handle": None, "exit_code": None, "resource_usage": {}}
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, attempt.frozen_step, attempt, outcome)
    store.save_result(result)
    runner._settle(store, run, attempt, result, outcome)
    return attempt, result


def test_configuration_missing_policy_preserves_old_serialized_hash():
    original = RunConfiguration().model_dump(mode="json")
    assert "workflow_policy" not in original
    loaded = RunConfiguration.model_validate(original)
    assert loaded.workflow_policy is None and fingerprint(loaded) == fingerprint(original)
    simplified = RunConfiguration(workflow_policy="simplified-1")
    assert simplified.model_dump()["workflow_policy"] == "simplified-1"


def test_programmatic_plan_cannot_persist_unpinned_concrete_shorthand(tmp_path, monkeypatch):
    store, run, _, prior, codes = case(tmp_path, monkeypatch)
    raw = prior.model_dump()
    raw["version"] += 1
    raw["steps"][0]["parameters"]["code"] = {"artifact_id": codes[1].id}
    candidate = Plan.model_validate(raw)
    before = store.load_run(run.id).model_dump()
    with pytest.raises(ValueError, match="materialized to a hash"):
        store.commit_revision(run, candidate, basis=current_basis(store, run), decision_id="unpinned")
    assert store.load_run(run.id).model_dump() == before


def test_native_compact_repair_inherits_contract_without_python_permission(tmp_path, monkeypatch):
    store, run, _, prior, codes = case(tmp_path, monkeypatch, native=True)
    old, result = settle(store, run, prior.steps[0])
    old_bytes = store.path(f"{old.directory}/intent.json").read_bytes()
    proposal = {"steps": [{"key": "fixed", "repair_of": old.step_id,
                           "input": {"artifact_id": codes[1].id}}],
                "goal_map": {"report": {"step_key": "fixed", "port": "artifacts"}}}
    original = copy.deepcopy(proposal)
    plan = materialize_plan(store, run, proposal)
    repaired = plan.steps[0]
    assert proposal == original and repaired.logical_id == old.logical_id
    assert repaired.parameters.input.sha256 == codes[1].sha256
    assert repaired.parameters.input.language == "orca"
    updated = store.commit_revision(run, plan, basis=current_basis(store, run),
                                    decision_id="native_repair", related_results=[result.id])
    attempt = store.reserve_attempt(updated, repaired)
    assert not updated.permission.code_execution
    assert attempt.number == 2 and updated.usage.orca_starts_reserved == 2
    assert updated.usage.extra_orca_starts_reserved == 1 and updated.usage.orca_starts_actual == 0
    assert store.path(f"{old.directory}/intent.json").read_bytes() == old_bytes
    assert attempt.directory != old.directory and not attempt.started


def test_native_compact_repair_can_reference_explicit_new_writer(tmp_path, monkeypatch):
    store, run, _, prior, _ = case(tmp_path, monkeypatch, native=True)
    old, _ = settle(store, run, prior.steps[0])
    proposal = {"steps": [
        {"key": "fixed_file", "tool": "workspace.write",
         "parameters": {"name": "fixed.inp", "role": "orca", "content": "# changed input\n"}},
        {"key": "fixed", "repair_of": old.step_id,
         "input": {"producer_key": "fixed_file", "port": "written_artifact"}}],
         "goal_map": {"report": {"step_key": "fixed", "port": "artifacts"}}}
    plan = materialize_plan(store, run, proposal)
    run = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="writer_repair")
    writer, repaired = plan.steps
    result = execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    attempt = store.reserve_attempt(run, repaired)
    assert attempt.logical_id == old.logical_id and attempt.number == 2
    assert attempt.frozen_step.parameters.input.artifact_id == result.observations["written_artifact"]["artifact_id"]
    assert attempt.consumption["_declared_step"] == repaired.model_dump(mode="json")


@pytest.mark.parametrize("change", ["legacy", "successful", "unknown", "unchanged", "conflicting_language"])
def test_native_compact_repair_does_not_remove_existing_admission_gates(tmp_path, monkeypatch, change):
    store, run, request, prior, codes = case(tmp_path, monkeypatch, native=True,
                                           policy=None if change == "legacy" else "simplified-1")
    old, _ = settle(store, run, prior.steps[0], state="completed" if change == "successful" else "failed")
    value = {"artifact_id": codes[0 if change == "unchanged" else 1].id}
    if change == "conflicting_language":
        value["language"] = "python"
    proposal = {"steps": [{"key": "fixed", "repair_of": "missing" if change == "unknown" else old.step_id,
                           "input": value}]}
    before = run.model_dump(mode="json")
    with pytest.raises((ProposalError, PlanningError, ValueError)):
        plan = materialize_plan(store, run, proposal)
        validate_revision(request, prior, request, plan, run)
    assert run.model_dump(mode="json") == before


def test_missing_goal_becomes_explicit_gap_and_not_goal_completion(tmp_path, monkeypatch):
    store, run, request, _, _ = case(tmp_path, monkeypatch)
    plan = materialize_plan(store, run, {"steps": [
        {"key": "only_write", "tool": "workspace.write",
         "parameters": {"name": "part.py", "role": "python", "content": "# not executed"}}]})
    assert set(plan.goal_map) == {goal.id for goal in request.goals}
    assert plan.goal_map["report"].gap and plan.goal_map["report"].step_id is None
    assert run.goal_status["report"] == "insufficient_evidence"


def test_gap_relaxation_keeps_bound_goal_geometry_contract(tmp_path):
    from tests.unit.test_proposals import source

    store, run, request, geometry, _ = source(tmp_path)
    run.configuration = RunConfiguration(workflow_policy="simplified-1")
    run.science_baseline_policy = "first_science_plan"
    run.initial_science_steps = None
    request.goals[0].conditions["geometry_relation"] = "fixed_initial"
    step = Step(id="opt", logical_id="opt", tool="orca.opt",
                geometry={"artifact_id": geometry.id})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    with pytest.raises(PlanningError, match="requested geometry relation"):
        validate_revision(request, None, request, plan, run)
    plan.goal_map["energy"] = OutputBinding(port="energy", gap="Fixed geometry energy remains unavailable")
    validate_revision(request, None, request, plan, run)


def test_two_independent_python_steps_use_analysis_allowance_without_retry_debit(tmp_path, monkeypatch):
    store, run, _, prior, codes = case(tmp_path, monkeypatch, retries=0, analysis=2)
    settle(store, run, prior.steps[0], state="completed")
    plan = materialize_plan(store, run, {"steps": [{
        "key": "independent", "tool": "python.execute", "parameters": {
            "code": {"artifact_id": codes[1].id}, "sdk_orca": False}}]})
    run = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="independent")
    step = next(item for item in plan.steps if item.id != prior.steps[0].id)
    attempt = store.reserve_attempt(run, step)
    assert attempt.number == 1 and run.usage.analysis_executions == 2
    assert run.usage.code_retries == additional_code_attempts_used(run) == 0


@pytest.mark.parametrize("drop_prior_route", [False, True])
def test_failed_goal_cannot_escape_retry_with_new_logical_id(tmp_path, monkeypatch, drop_prior_route):
    store, run, request, prior, codes = case(tmp_path, monkeypatch, retries=0)
    attempt, _ = settle(store, run, prior.steps[0])
    assert attempt.consumption["_goal_ids"] == ["report"]
    if drop_prior_route:
        gap_plan = materialize_plan(store, run, {"steps": [{
            "key": "note", "tool": "workspace.write",
            "parameters": {"name": "note.txt", "role": "text", "content": "pending"}}],
            "goal_map": {"report": {"gap": "Still missing", "port": "artifacts"}}})
        run = store.commit_revision(run, gap_plan, basis=current_basis(store, run), decision_id="gap")
        prior = gap_plan
    plan = materialize_plan(store, run, {"steps": [{
        "key": "renamed", "tool": "python.execute", "parameters": {
            "code": {"artifact_id": codes[1].id}, "sdk_orca": False}}],
        "goal_map": {"report": {"step_key": "renamed", "port": "artifacts"}}})
    with pytest.raises(PlanningError, match="same logical Step"):
        validate_revision(request, prior, request, plan, run)
    assert run.usage.analysis_executions == 1 and run.usage.code_retries == 0


def test_same_logical_retry_consumes_both_retry_and_analysis_limits(tmp_path, monkeypatch):
    store, run, _, prior, codes = case(tmp_path, monkeypatch, retries=1, analysis=2)
    old, _ = settle(store, run, prior.steps[0])
    plan = materialize_plan(store, run, {"steps": [{
        "key": "repair", "repair_of": old.step_id, "code": {"artifact_id": codes[1].id}}],
        "goal_map": {"report": {"step_key": "repair", "port": "artifacts"}}})
    run = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="retry")
    attempt, _ = settle(store, run, plan.steps[0])
    assert attempt.number == 2
    assert run.usage.analysis_executions == 2 and run.usage.code_retries == 1
    assert additional_code_attempts_used(run) == 1
    with pytest.raises(BudgetExceeded, match="analysis|retry"):
        store.reserve_attempt(run, plan.steps[0])
