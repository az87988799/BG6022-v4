"""Mixed pending code and existing inputs admit a Plan, never premature execution."""

import pytest

from orca_agent.file_references import resolve_step_files
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    ExecutionConfiguration,
    Goal,
    NativeInputPolicy,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    RunConfiguration,
    Step,
)
from orca_agent.store import Store
from orca_agent.tools import managed
from orca_agent.tools.dispatch import execute_call


def case(tmp_path, *, science=True):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
        configuration=RunConfiguration(workflow_policy="simplified-1",
            execution=ExecutionConfiguration(backend="linux-container",
                image_id="sha256:" + "a" * 64, manifest_sha256="b" * 64,
                docker_context="offline", docker_path="E:/offline-docker.exe",
                native_input_policy=NativeInputPolicy(profile="managed-native-1"))))
    xyz = tmp_path / "initial.xyz"
    xyz.write_bytes(b"2\nregistered initial geometry\nH 0 0 0\nH 0 0 .74\n")
    geometry = store.import_artifact(xyz, "geometry")
    request = Request(geometry_artifact_id=geometry.id,
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        goals=[Goal(id="report", port="artifacts", evidence_contract_version="generic-observation-1",
                    minimum_check_version="generic-observation-1", verification="reported_with_limitations")])
    input_writer = Step(id="existing_input", logical_id="existing_input", tool="workspace.write",
        parameters={"name": "initial.xyz", "role": "text", "content": xyz.read_text("ascii")})
    initial_plan = Plan(request_id=request.id, steps=[input_writer],
        goal_map={"report": OutputBinding(port="artifacts", gap="Analysis is not planned yet")})
    run = store.create_run(request, initial_plan, PermissionSnapshot(
        scientific_execution=science, code_execution=True, artifact_writes=True,
        allowed_tools=["workspace.write", "python.execute", "orca.run"], artifact_ids=[geometry.id]),
        BudgetLimits(plan_revisions=2, analysis_executions=3))
    result = execute_call(store, run, input_writer.tool,
                          input_writer.parameters.model_dump(mode="json"), step=input_writer)
    assert result.operation_status == "completed"
    writer = Step(id="writer", logical_id="writer", tool="workspace.write", parameters={
        "name": "task.py", "role": "python", "content": "print('not executed in this test')\n"})
    consumer = Step(id="consumer", logical_id="consumer", tool="python.execute",
        depends_on=[writer.id, input_writer.id], parameters={"sdk_orca": True,
            "code": {"producer_step_id": writer.id, "port": "written_artifact"},
            "inputs": {"entries": [{"producer_step_id": input_writer.id, "port": "written_artifact",
                "target_path": "initial.xyz", "purpose": "authorized initial geometry"}]}})
    plan = Plan(id=initial_plan.id, version=2, request_id=request.id, steps=[input_writer, writer, consumer],
                goal_map={"report": OutputBinding(port="artifacts", step_id=consumer.id)})
    return store, run, request, plan, geometry


def test_partial_input_resolution_preserves_pending_code_only_during_plan_preflight(tmp_path):
    store, run, request, plan, geometry = case(tmp_path)
    consumer = plan.steps[-1]
    resolved = resolve_step_files(store, run, consumer, request=request, plan=plan, allow_pending=True)
    assert resolved != consumer  # This is the second comparison that previously lost allow_pending.
    assert resolved.parameters.code == consumer.parameters.code
    assert resolved.parameters.inputs.entries[0].sha256 == geometry.sha256
    updated = store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="mixed_plan")
    assert store.load_plan(updated) == plan
    before = store.load_run(updated.id).model_dump(mode="json")
    with pytest.raises(ValueError, match="file producer has no settled Result"):
        managed.native_input_binding(store, updated, resolved)
    with pytest.raises(ValueError, match="file producer has no settled Result"):
        store.reserve_attempt(updated, consumer)
    assert store.load_run(updated.id).model_dump(mode="json") == before
    assert not updated.attempts and not updated.internal_calls
    assert updated.usage.orca_starts_actual == updated.usage.orca_starts_reserved == 0
    assert store.environment_lease() is None


@pytest.mark.parametrize("problem,message", [("permission", "authorized managed ORCA"),
                                             ("geometry_hash", "hash|changed|pinned")])
def test_pending_plan_keeps_permission_and_existing_geometry_validation(tmp_path, problem, message):
    store, run, _, plan, geometry = case(tmp_path, science=problem != "permission")
    before = store.load_run(run.id).model_dump(mode="json")
    if problem == "geometry_hash":
        store.artifact_path(geometry.id).write_bytes(b"2\nchanged\nH 0 0 0\nH 0 0 .75\n")
    with pytest.raises(ValueError, match=message):
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="invalid_mixed_plan")
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert store.load_plan(run).version == 1 and not run.attempts
    assert store.environment_lease() is None
