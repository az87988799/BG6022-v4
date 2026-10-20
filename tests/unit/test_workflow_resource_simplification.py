"""Bounded file writes and future preparation dependencies; entirely offline."""

import copy
import json

import pytest

from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    BudgetUsage,
    EvidenceRef,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    RunConfiguration,
    Step,
    SystemInput,
    ToolCall,
    fingerprint,
)
from orca_agent.planning import PlanningError, validate_revision, validate_tool_call_budget
from orca_agent.store import Store, StoreError
from orca_agent.tools import managed, structure
from orca_agent.tools.dispatch import execute_call
from orca_agent.tools.registry import call_usage_counter, get_tool
from tests.helpers import code_tools_batch as batch
from tests.unit.test_code_tools_batch import policy
from tests.unit.test_general_preparation_admission import execution
from tests.unit.test_refreeze_plan_resources import writer, writing_plan
from tests.unit.test_structure_tools import mock_generator, response


def write_run(tmp_path, *, limit=None, legacy=False):
    count = 4 if legacy else 8 if limit is None else limit
    request, plan, envelope = writing_plan([writer(n) for n in range(max(1, count))])
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    limits = BudgetLimits(analysis_executions=4, artifact_count=8, file_writes=limit,
                          plan_revisions=3)
    run = store.create_run(request, plan if count else None, envelope.permission, limits)
    if legacy:
        # Reconstruct the pre-change wire shape, rather than provide a production
        # switch that could migrate or reset an existing Run's accounting.
        for name in ("budget.json", "run.json"):
            path = store.path(f"runs/{run.id}/{name}")
            wire = json.loads(path.read_bytes())
            (wire["budget"] if name == "run.json" else wire).pop("file_writes")
            path.write_text(json.dumps(wire), encoding="utf-8")
        run = store.load_run(run.id)
    return store, run, request


def test_new_run_writes_have_a_separate_finite_budget_and_keep_artifact_limits(tmp_path):
    store, run, _ = write_run(tmp_path, limit=5)
    assert call_usage_counter(get_tool("workspace.write"), run.budget) == "file_writes"
    assert call_usage_counter(get_tool("analysis.energy_compare"), run.budget) == "analysis_executions"
    for n in range(5):
        step = store.load_plan(run).steps[n]
        result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
        assert result.operation_status == "completed"
    assert run.usage.file_writes == 5 and run.usage.analysis_executions == 0
    assert run.budget.artifact_count == 8 and len(run.calls) == 5
    _, proposed, _ = writing_plan([writer(6)])
    with pytest.raises(PlanningError, match="file_writes"):
        validate_tool_call_budget(proposed, run)
    resumed = store.load_run(run.id)
    assert resumed.usage.file_writes == 5 and resumed.budget.file_writes == 5
    resumed.usage.file_writes = 0
    with pytest.raises(StoreError, match="cumulative usage"):
        store.save_run(resumed)
    assert store.load_run(run.id).usage.file_writes == 5


def test_write_default_and_zero_limit_are_explicitly_frozen(tmp_path):
    store, run, _ = write_run(tmp_path / "default")
    assert run.budget.file_writes == run.budget.artifact_count == 8
    assert store._read_json(f"runs/{run.id}/budget.json")["file_writes"] == 8
    zero_store, zero, request = write_run(tmp_path / "zero", limit=0)
    _, proposed, _ = writing_plan([writer(1)])
    proposed.request_id = request.id
    with pytest.raises(PlanningError, match="file_writes"):
        zero_store.commit_revision(zero, proposed, basis=current_basis(zero_store, zero), decision_id="over")


def test_legacy_snapshot_keeps_original_counter_hash_shape_and_remaining_allowance(tmp_path):
    assert "file_writes" not in BudgetLimits().model_dump()
    assert "file_writes" not in BudgetUsage().model_dump()
    store, run, request = write_run(tmp_path, legacy=True)
    original_budget = store._read_json(f"runs/{run.id}/budget.json")
    assert fingerprint(original_budget) == fingerprint(run.budget)
    assert run.budget.file_writes is None
    for n in range(3):
        step = store.load_plan(run).steps[n]
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    resumed = store.load_run(run.id)
    assert resumed.usage.analysis_executions == 3 and resumed.usage.file_writes == 0
    assert "file_writes" not in resumed.model_dump()["usage"]
    _, plan, _ = writing_plan([writer(9), writer(10)])
    plan.request_id = request.id
    with pytest.raises(PlanningError, match="analysis_executions.*1 remain"):
        validate_tool_call_budget(plan, resumed)
    last = store.load_plan(resumed).steps[3]
    execute_call(store, resumed, last.tool, last.parameters.model_dump(), step=last)
    with pytest.raises(PlanningError, match="analysis_executions"):
        validate_tool_call_budget(plan, resumed)
    assert store._read_json(f"runs/{run.id}/budget.json") == original_budget


@pytest.mark.parametrize("state", ["completed", "failed", "unknown", "reserved"])
def test_new_counter_retains_all_reservations_and_plan_ids_cannot_refund(tmp_path, state):
    store, run, request = write_run(tmp_path, limit=2)
    prior = writer(0)
    call = store.reserve_call(run, prior.tool, prior.parameters.model_dump(), step=prior)
    call.state = state
    store.save_run(run)
    _, plan, _ = writing_plan([writer(1), writer(2)])
    plan.request_id = request.id
    with pytest.raises(PlanningError, match="file_writes.*1 remain"):
        validate_tool_call_budget(plan, run)
    assert run.usage.file_writes == 1 and run.usage.analysis_executions == 0


def test_old_batch_budget_is_readable_but_new_batch_requires_explicit_write_limit(tmp_path):
    value, _, _ = policy(count=1)
    old = copy.deepcopy(value)
    old["slots"][0]["run_budget"].pop("file_writes")
    assert batch._policy(old) == old
    with pytest.raises(ValueError, match="complete frozen BudgetLimits"):
        batch.create_batch(tmp_path / "batch", freeze_sha256="a" * 64, policy=old,
                           environment_root=tmp_path / "environment")
    batch.create_batch(tmp_path / "batch", freeze_sha256="a" * 64, policy=value,
                       environment_root=tmp_path / "environment")


def test_final_resource_snapshot_exposes_new_counter_without_changing_legacy_shape():
    from orca_agent.delivery import _science_resources

    _, _, legacy = writing_plan([writer(0)])
    before = _science_resources(legacy)
    current = legacy.model_copy(deep=True)
    current.budget.file_writes = 5
    current.usage.file_writes = 3
    projected = _science_resources(current)
    assert [projected[name]["file_writes"] for name in ("limits", "usage", "remaining")] == [5, 3, 2]
    assert fingerprint(projected) != fingerprint(before)
    for name in ("limits", "usage", "remaining"):
        projected[name].pop("file_writes")
        assert "file_writes" not in before[name]
    assert projected == before


@pytest.mark.parametrize("writes,analysis,expected", [(0, 4, False), (1, 0, True)])
def test_existing_source_repair_uses_the_frozen_write_counter(writes, analysis, expected):
    from orca_agent.delivery import delivery_snapshot
    from tests.unit.test_existing_evidence_repair import case

    request, run, _, selections = case()
    run.budget.file_writes = writes
    run.budget.analysis_executions = analysis
    snapshot = delivery_snapshot(request, run, selections)
    repairs = [fact["value"] for fact in snapshot["facts"] if fact["kind"] == "existing_evidence_repair"]
    assert bool(repairs) is expected
    if repairs:
        assert repairs[0]["file_writes_remaining"] == 1
        assert "analysis_executions_remaining" not in repairs[0]
    assert not snapshot["goals"][0]["goal_complete"]


def future_sdk_case(tmp_path, *, mode=True, dependency=True, producer_system="water",
                    planned=True, policy_enabled=True):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(execution=execution()) if policy_enabled else RunConfiguration())
    code_path = tmp_path / "task.py"
    code_path.write_text("print('offline fixture; never executed')\n", encoding="utf-8")
    code = store.import_artifact(code_path, "code")
    text = "Prepare water and methane; report the water task artifacts."
    proof = {"message_id": "input", "text_basis": text}
    request = Request(original_text=text, messages=[{"id": "input", "text": text}],
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        systems=[SystemInput(id=name, identity={"canonical_names": [name], "text_evidence": proof},
                             geometry_source="prepare")
                 for name in ("water", "methane")],
        goals=[Goal(id="report", port="artifacts", system_ids=["water"],
                    evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
                    verification="reported_with_limitations")])
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve",
                   system_id=producer_system, parameters={"system_id": producer_system})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id=producer_system,
        parameters={"system_id": producer_system}, depends_on=[resolve.id],
        inputs={"identity": EvidenceRef(producer_step_id=resolve.id, port="resolved_identity")})
    script = Step(id="script", logical_id="script", tool="python.execute", system_id="water",
        parameters={"code": {"artifact_id": code.id, "sha256": code.sha256}, "sdk_orca": mode},
        depends_on=[prepare.id] if dependency else [])
    plan = Plan(request_id=request.id, steps=[resolve, prepare, script],
                goal_map={"report": OutputBinding(port="artifacts", step_id=script.id)})
    permission = PermissionSnapshot(allowed_tools=["structure.resolve", "structure.prepare", "python.execute", "orca.run"],
        artifact_ids=[code.id], artifact_writes=True, scientific_execution=True, code_execution=True,
        geometry_preparation=True, external_identity_queries=True)
    run = store.create_run(request, plan if planned else None, permission,
        BudgetLimits(identity_queries=2, structure_preparations=2, plan_revisions=3))
    return store, run, request, plan


@pytest.mark.parametrize("policy_enabled", [True, False])
@pytest.mark.parametrize("planned", [True, False])
def test_plan_accepts_future_prepare_but_attempt_requires_completed_qualified_anchor(
        tmp_path, monkeypatch, planned, policy_enabled):
    store, run, request, plan = future_sdk_case(tmp_path, planned=planned, policy_enabled=policy_enabled)
    if not planned:
        run = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="plan")
    before = store.load_run(run.id).model_dump()
    script = plan.steps[-1]
    with pytest.raises(ValueError, match="prepared or registered geometry"):
        store.reserve_attempt(run, script)
    assert store.load_run(run.id).model_dump() == before and store.environment_lease() is None
    monkeypatch.setattr(structure, "_paced_query", lambda *args, **kwargs: (200, response(), True))
    mock_generator(monkeypatch)
    resolve, prepare = plan.steps[:2]
    identity = execute_call(store, run, resolve.tool, resolve.parameters.model_dump(), step=resolve)
    geometry = execute_call(store, run, prepare.tool, prepare.parameters.model_dump(), step=prepare,
                            results={resolve.id: identity})
    assert geometry.operation_status == "completed"
    assert run.input_bindings["water"]["prepared_geometry"]["artifact_id"] in geometry.artifact_ids
    attempt = store.reserve_attempt(run, script)
    assert attempt.execution_kind == "python" and not attempt.started
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == run.usage.model_calls == 0
    # Consumption rechecks the prepared evidence even after reservation.
    artifact_id = run.input_bindings["water"]["prepared_geometry"]["artifact_id"]
    store.artifact_path(artifact_id).write_text("corrupt geometry", encoding="utf-8")
    with pytest.raises(ValueError, match="hash|changed|mismatch"):
        managed._prepare_python_inputs(store, run, script, tmp_path / "root", tmp_path / "task")


@pytest.mark.parametrize("options", [{"dependency": False}, {"producer_system": "methane"}])
def test_missing_or_wrong_system_prepare_dependency_does_not_enable_sdk_plan(tmp_path, options):
    with pytest.raises(ValueError, match="prepared or registered geometry"):
        future_sdk_case(tmp_path, **options)
    assert not list((tmp_path / "data/runs").glob("*/run.json"))


@pytest.mark.parametrize("state", ["failed", "unknown", "completed", "reserved"])
def test_already_reserved_unbound_producer_cannot_be_reused_as_future_anchor(tmp_path, state):
    store, run, request, plan = future_sdk_case(tmp_path, planned=False)
    prepare = plan.steps[1]
    run.calls.append(ToolCall(tool=prepare.tool, step_id=prepare.id, state=state,
        parameters=prepare.parameters.model_dump(), request_version=request.version, frozen_step=prepare))
    store.save_run(run)
    with pytest.raises(ValueError, match="prepared or registered geometry"):
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="plan")
    assert store.load_plan(run) is None and not run.attempts


def test_analysis_only_python_still_needs_no_initial_anchor_or_preparation_dependency(tmp_path):
    store, run, _, plan = future_sdk_case(tmp_path, mode=False, dependency=False)
    assert store.reserve_attempt(run, plan.steps[-1]).execution_kind == "python"


def test_evidence_gap_can_keep_preparation_plan_but_not_claim_qualified_goal(tmp_path):
    store, run, request, plan = future_sdk_case(tmp_path, mode=False, planned=False)
    goal = request.goals[0].model_copy(update={"port": "prepared_geometry",
        "minimum_check_version": "structure-prepare-1", "evidence_contract_version": "legacy-1",
        "verification": "qualified", "unresolved": ["unsupported:minimum_evidence"]})
    request.goals = [goal]
    # This exercises the Plan contract without mutating the persisted Request.
    run.request_id, run.request_version = request.id, request.version
    plan.goal_map = {"report": OutputBinding(port="prepared_geometry", gap="Minimum evidence remains unavailable.")}
    validate_revision(request, None, request, plan, run)
    plan.goal_map["report"] = OutputBinding(port="prepared_geometry", step_id="prepare")
    with pytest.raises(PlanningError, match="unresolved scientific goal"):
        validate_revision(request, None, request, plan, run)
