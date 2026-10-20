"""Offline Plan cost and SDK readiness checks; no model, HTTP or science execution."""

import copy
import json
import time

import pytest

from orca_agent.model_usage import current_basis
from orca_agent.models import (
    Attempt,
    BudgetLimits,
    CodeExecutionParameters,
    ExecutionConfiguration,
    Goal,
    NativeInputPolicy,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Run,
    RunConfiguration,
    Step,
    ToolCall,
    fingerprint,
    utc_now,
)
from orca_agent.planning import PlanningError, validate_revision, validate_tool_call_budget
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.store import Store
from orca_agent.tools import managed, structure
from orca_agent.tools.registry import call_usage_counter, get_tool


def writer(number):
    return Step(id=f"write_{number}", logical_id=f"write_{number}", tool="workspace.write",
                parameters={"name": f"part_{number}.py", "role": "python", "content": f"print({number})"})


def writing_plan(steps):
    request = Request(goals=[Goal(id="report", port="artifacts",
        evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
        verification="reported_with_limitations")])
    plan = Plan(request_id=request.id, steps=steps,
                goal_map={"report": OutputBinding(port="artifacts", gap="Execution still required")})
    run = Run(request_id=request.id, request_version=1, permission=PermissionSnapshot(
        allowed_tools=["workspace.write", "analysis.energy_compare"], artifact_writes=True),
        budget=BudgetLimits(analysis_executions=4))
    return request, plan, run


@pytest.mark.parametrize("count,accepted", [(2, True), (3, False)])
def test_plan_checks_remaining_shared_write_budget_before_activation(count, accepted):
    request, plan, run = writing_plan([writer(n) for n in range(count)])
    run.usage.analysis_executions = 2
    before = copy.deepcopy((request, plan, run))
    if accepted:
        validate_revision(request, None, request, plan, run)
    else:
        with pytest.raises(PlanningError, match="analysis_executions.*3.*2 remain"):
            validate_revision(request, None, request, plan, run)
    assert (request, plan, run) == before


@pytest.mark.parametrize("state", ["completed", "failed", "unknown", "reserved"])
def test_reserved_history_is_charged_once_and_new_ids_cannot_refund_it(state):
    old = writer(0)
    request, plan, run = writing_plan([old, writer(1), writer(2)])
    run.calls.append(ToolCall(tool=old.tool, step_id=old.id, state=state,
        parameters=old.parameters.model_dump(), request_version=1, frozen_step=old))
    run.usage.analysis_executions = 2
    validate_revision(request, None, request, plan, run)
    plan.steps.append(writer(3))
    with pytest.raises(PlanningError, match="analysis_executions"):
        validate_revision(request, None, request, plan, run)
    # Dropping history from the Plan does not drop the accumulated debit.
    plan.steps = [writer(4), writer(5), writer(6)]
    with pytest.raises(PlanningError, match="analysis_executions"):
        validate_revision(request, None, request, plan, run)


def test_different_tools_share_the_execution_accounting_rule():
    _, plan, run = writing_plan([writer(0), Step(id="compare", logical_id="compare",
        tool="analysis.energy_compare", parameters={"goal_id": "report"})])
    run.usage.analysis_executions = 3
    assert get_tool("analysis.energy_compare").usage_counter is None  # Existing fallback semantics.
    assert call_usage_counter(get_tool("workspace.write")) == "analysis_executions"
    assert call_usage_counter(get_tool("analysis.energy_compare")) == "analysis_executions"
    with pytest.raises(PlanningError, match="2 unreserved.*1 remain"):
        validate_tool_call_budget(plan, run)
    assert call_usage_counter(get_tool("evidence.list")) == "evidence_reads"
    assert call_usage_counter(get_tool("knowledge.search")) == "knowledge_queries"
    assert call_usage_counter(get_tool("python.execute")) is None
    assert call_usage_counter(get_tool("orca.run")) is None


def test_over_budget_initial_plan_leaves_no_run(tmp_path):
    request, plan, run = writing_plan([writer(n) for n in range(5)])
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    with pytest.raises(PlanningError, match="analysis_executions"):
        store.create_run(request, plan, run.permission, run.budget)
    assert not list(store.root.glob("runs/*/run.json"))


def test_store_revision_uses_actual_charged_calls_without_recharging_retained_writers(tmp_path):
    from orca_agent.tools.dispatch import execute_call

    request, plan, envelope = writing_plan([writer(0), writer(1)])
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    limits = envelope.budget.model_copy(update={"plan_revisions": 2})
    run = store.create_run(request, plan, envelope.permission, limits)
    for step in plan.steps:
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert run.usage.analysis_executions == 2 and len(run.calls) == 2
    candidate = plan.model_copy(deep=True, update={
        "version": 2, "steps": [*plan.steps, writer(2), writer(3), writer(4)]})
    before = store.load_run(run.id).model_dump()
    with pytest.raises(PlanningError, match="3 unreserved.*2 remain"):
        store.commit_revision(run, candidate, basis=current_basis(store, run), decision_id="over_budget")
    assert store.load_run(run.id).model_dump() == before and store.load_plan(run) == plan
    candidate.steps.pop()
    updated = store.commit_revision(run, candidate, basis=current_basis(store, run), decision_id="within_budget")
    for step in candidate.steps[-2:]:
        execute_call(store, updated, step.tool, step.parameters.model_dump(), step=step)
    assert updated.usage.analysis_executions == 4 and len(updated.calls) == 4
    assert updated.budget.analysis_executions == 4


def sdk_case(tmp_path, *, mode=None, geometry=True, planned=True, policy=True):
    execution = (ExecutionConfiguration(backend="linux-container", image_id="sha256:" + "a" * 64,
        manifest_sha256="b" * 64, docker_context="offline", docker_path="E:/offline-docker.exe",
        native_input_policy=NativeInputPolicy(profile="managed-native-1", allow_task_geometry=True))
        if policy else ExecutionConfiguration())
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(execution=execution))
    source = tmp_path / "task.py"
    source.write_text("print('offline: never executed')\n", encoding="utf-8")
    code = store.import_artifact(source, "code")
    geometry_artifact = None
    if geometry:
        xyz = tmp_path / "initial.xyz"
        xyz.write_text("2\nsynthetic initial geometry\nH 0 0 0\nH 0 0 .74\n", encoding="ascii")
        geometry_artifact = store.import_artifact(xyz, "geometry")
    request = Request(geometry_artifact_id=geometry_artifact.id if geometry_artifact else None,
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        goals=[Goal(id="report", port="artifacts", evidence_contract_version="generic-observation-1",
                    minimum_check_version="generic-observation-1", verification="reported_with_limitations")])
    step = Step(id="python", logical_id="analysis", tool="python.execute", parameters={
        "code": {"artifact_id": code.id, "sha256": code.sha256}, "sdk_orca": mode})
    plan = Plan(request_id=request.id, steps=[step],
        goal_map={"report": OutputBinding(port="artifacts", step_id=step.id)})
    permission = PermissionSnapshot(code_execution=True, scientific_execution=True, artifact_writes=True,
        allowed_tools=["python.execute", "orca.run"],
        artifact_ids=[code.id] + ([geometry_artifact.id] if geometry_artifact else []))
    run = store.create_run(request, plan if planned else None, permission, BudgetLimits(plan_revisions=2))
    return store, run, request, plan, geometry_artifact


@pytest.mark.parametrize("policy", [True, False])
def test_declared_sdk_missing_anchor_fails_before_run_or_attempt(tmp_path, policy):
    with pytest.raises(ValueError, match="prepared or registered geometry.*task-generated XYZ"):
        sdk_case(tmp_path, mode=True, geometry=False, policy=policy)
    assert not list((tmp_path / "data").glob("runs/*/run.json"))
    assert not list((tmp_path / "data").glob("runs/*/attempts/*"))


def test_new_plan_preflight_uses_candidate_not_old_plan_and_preserves_failure(tmp_path):
    store, run, request, plan, _ = sdk_case(tmp_path, mode=True, geometry=False, planned=False)
    before = store.load_run(run.id).model_dump()
    with pytest.raises(ValueError, match="prepared or registered geometry"):
        store.commit_revision(run, plan=plan, basis=current_basis(store, run), decision_id="candidate")
    assert store.load_run(run.id).model_dump() == before
    assert store.load_plan(run) is None and not run.attempts and not run.calls


def test_registered_anchor_passes_candidate_plan_and_is_rechecked_before_attempt(tmp_path):
    store, run, _, plan, geometry = sdk_case(tmp_path, mode=True, planned=False)
    updated = store.commit_revision(run, plan=plan, basis=current_basis(store, run), decision_id="candidate")
    assert store.load_plan(updated) == plan and not updated.attempts
    bound = managed.validate_sdk_orca_preconditions(store, updated, plan.steps[0])
    assert bound["geometry_sha256"] == geometry.sha256
    assert bound["initial_atom_symbols"] == ["H", "H"]
    store.artifact_path(geometry.id).write_text("2\nchanged\nH 0 0 0\nH 0 0 .75\n")
    with pytest.raises(ValueError, match="hash|changed|pinned"):
        store.reserve_attempt(updated, plan.steps[0])
    assert not store.load_run(run.id).attempts
    assert store.environment_lease() is None


def test_explicit_analysis_needs_no_anchor_and_cannot_call_orca(tmp_path):
    store, run, _, plan, _ = sdk_case(tmp_path, mode=False, geometry=False)
    attempt = store.reserve_attempt(run, plan.steps[0])
    assert attempt.execution_kind == "python" and run.usage.orca_starts_reserved == 0
    broker = managed._Broker(store, run, plan.steps[0], attempt, None, None,
        tmp_path, tmp_path, time.monotonic() + 10)
    with pytest.raises(ValueError, match="analysis only"):
        broker.execute({"version": 2, "kind": "orca"}, None)
    assert not run.internal_calls and run.usage.orca_starts_actual == 0
    broker._checkpoint = lambda value: {"checkpoint": value["kind"]}
    assert broker.execute({"version": 2, "kind": "checkpoint"}, None) == {"checkpoint": "checkpoint"}


def test_legacy_code_serialization_and_broker_behavior_are_not_rewritten(tmp_path):
    store, run, _, plan, _ = sdk_case(tmp_path, geometry=False)
    parameters = plan.steps[0].parameters.model_dump(mode="json")
    assert "sdk_orca" not in parameters
    assert fingerprint(parameters) == fingerprint(CodeExecutionParameters.model_validate(parameters))
    assert store.load_plan(run).steps[0].parameters.sdk_orca is None
    broker = managed._Broker(store, run, plan.steps[0], None, None, None,
        tmp_path, tmp_path, time.monotonic() + 10)
    with pytest.raises(ValueError, match="SDK boundary was not authorized"):
        broker.execute({"version": 1, "request_key": "call", "input": "", "dependencies": []}, None)


@pytest.mark.parametrize("declaration", ["missing", None, "true", 1])
def test_new_model_code_proposal_requires_boolean_declaration(tmp_path, declaration):
    store, run, _, plan, _ = sdk_case(tmp_path, planned=False, geometry=False)
    params = plan.steps[0].parameters.model_dump(mode="json")
    if declaration != "missing":
        params["sdk_orca"] = declaration
    with pytest.raises(ProposalError, match="Declare parameters.sdk_orca"):
        materialize_plan(store, run, {"steps": [{"key": "new", "tool": "python.execute", "parameters": params}],
            "goal_map": {"report": {"step_key": "new", "port": "artifacts"}}})
    assert not run.attempts and store.load_plan(run) is None


def test_retained_legacy_step_does_not_need_new_declaration(tmp_path):
    store, run, _, plan, _ = sdk_case(tmp_path, geometry=False)
    candidate = materialize_plan(store, run, {"steps": [{"key": plan.steps[0].id}],
        "goal_map": {"report": {"step_key": plan.steps[0].id, "port": "artifacts"}}})
    assert candidate.steps == plan.steps
    assert "sdk_orca" not in candidate.steps[0].parameters.model_dump()


def test_unconfirmed_identity_stays_failed_and_cannot_create_geometry_binding(tmp_path, monkeypatch):
    from orca_agent.semantic import commit_candidate
    from tests.unit.test_general_identity_selection import intake, pubchem, wikidata
    from tests.unit.test_structure_tools import tool_call

    monkeypatch.setattr("orca_agent.semantic.CURRENT_IDENTITY_CONTRACT", "general-identity-2")
    store, run, candidate = intake(tmp_path, name="乙醇", automatic=False)
    candidate["identities"][0]["query_name"] = "ethanol"
    run = commit_candidate(store, run, candidate, decision_id="normalize", basis=current_basis(store, run))
    search, entities = wikidata(cid="124")  # Deliberate same-name/different-CID negative evidence.
    replies = [pubchem("CCO", "C2H6O"), json.dumps({"InformationList": {"Information": [
        {"CID": 123, "Synonym": ["ethanol"]}]}}).encode(),
        json.dumps(search).encode(), json.dumps(entities).encode()]
    consumed = []

    def query(*args, **kwargs):
        response = replies[len(consumed)]
        consumed.append(response)
        return 200, response, True

    monkeypatch.setattr(structure, "_paced_query", query)
    call = tool_call(store, run, "structure.resolve", {"system_id": "system_target"})
    outcome = structure.resolve(store, run, call)
    assert outcome["operation_status"] == "failed" and not outcome["qualified_outputs"]
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id,
        operation_status="failed", diagnostics=outcome["diagnostics"],
        artifact_ids=outcome["artifact_ids"],
        source={"tool": call.tool, **outcome["source"]})
    store.finish_call(run, call, result)
    assert not run.input_bindings and not result.qualified_outputs and len(consumed) == 4
    assert [store.artifact_path(a).read_bytes() for a in outcome["artifact_ids"]] == replies
    with pytest.raises(ValueError, match="prepared or registered geometry"):
        managed._initial_geometry(store, run, store.load_request(run), "system_target")


def native_repair_case():
    request = Request(goals=[Goal(id="raw", port="artifacts",
        evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
        verification="reported_with_limitations")])
    old = Step(id="native_old", logical_id="native_intent", tool="orca.run",
        parameters={"input": {"artifact_id": "old", "sha256": "a" * 64, "language": "orca"}})
    prior = Plan(request_id=request.id, steps=[old],
        goal_map={"raw": OutputBinding(step_id=old.id, port="artifacts")})
    attempt = Attempt(step_id=old.id, logical_id=old.logical_id, tool=old.tool,
        execution_kind="orca", state="failed", result_id="failure", finished_at=utc_now(),
        number=1, input_fingerprint="original", directory="attempts/original", frozen_step=old)
    run = Run(request_id=request.id, request_version=1, plan_id=prior.id, plan_version=1, attempts=[attempt],
        permission=PermissionSnapshot(allowed_tools=["orca.run"], scientific_execution=True,
            artifact_writes=True, artifact_ids=["old", "new"]),
        budget=BudgetLimits(orca_starts=2, extra_orca_starts=1), initial_science_steps=[old.logical_id])
    run.usage.orca_starts_reserved = 1
    run.usage.logical_attempts = {old.logical_id: 1}
    repaired = old.model_copy(deep=True, update={"id": "native_repaired"})
    repaired.parameters.input.artifact_id = "new"
    repaired.parameters.input.sha256 = "b" * 64
    candidate = prior.model_copy(deep=True, update={"version": 2, "steps": [repaired]})
    candidate.goal_map["raw"].step_id = repaired.id
    return request, prior, candidate, run


def test_native_input_repair_preserves_logical_budget_and_does_not_relax_scf_tools():
    request, prior, candidate, run = native_repair_case()
    before = copy.deepcopy((request, prior, run))
    validate_revision(request, prior, request, candidate, run)
    assert (request, prior, run) == before
    run.usage.logical_attempts[prior.steps[0].logical_id] = run.budget.attempts_per_step
    with pytest.raises(PlanningError, match="logical Step attempt budget"):
        validate_revision(request, prior, request, candidate, run)


@pytest.mark.parametrize("mutation", ["condition", "observation", "dependency", "unchanged", "unknown",
                                    "completed", "unfinished", "no_result"])
def test_native_repair_cannot_change_intent_or_escape_unsettled_history(mutation):
    request, prior, candidate, run = native_repair_case()
    step = candidate.steps[0]
    if mutation == "condition":
        step.parameters.conditions["analysis_scope"] = "different purpose"
    elif mutation == "observation":
        step.parameters.observation_names = ["different_observation"]
    elif mutation == "dependency":
        step.depends_on = [prior.steps[0].id]
        candidate.steps.insert(0, prior.steps[0])
    elif mutation == "unchanged":
        step.parameters.input = prior.steps[0].parameters.input
    elif mutation == "unfinished":
        run.attempts[-1].finished_at = None
    elif mutation == "no_result":
        run.attempts[-1].result_id = None
    else:
        run.attempts[-1].state = mutation
    with pytest.raises(ValueError):
        validate_revision(request, prior, request, candidate, run)
