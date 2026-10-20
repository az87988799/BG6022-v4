"""Offline file provenance and conservative delivery; no generated process is launched."""

import json

import pytest

from orca_agent.delivery import collect_goal_facts
from orca_agent.goals import goal_evidence_assessment
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store, StoreError
from orca_agent.tools import managed
from orca_agent.tools.dispatch import execute_call
from orca_agent.tools.workspace import WriteParameters


def case(tmp_path, monkeypatch, *, scientific_context=None):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda name: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    request = Request(conditions_source={name: "default" for name in
        ("method", "basis", "charge", "multiplicity", "geometry")},
        goals=[Goal(id="report", port="artifacts", minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations",
        minimum_evidence=["purpose_preserved@1"])])
    inputs = []
    if scientific_context:
        goal = request.goals[0]
        goal.port, goal.quantity, goal.original_text = "frequencies", "振动频率", "输入体系的振动频率"
        if scientific_context == "root_geometry":
            geometry_path = tmp_path / "initial.xyz"
            geometry_path.write_text("2\nsynthetic\nH 0 0 0\nH 0 0 0.74\n", encoding="ascii")
            geometry = store.import_artifact(geometry_path, "geometry")
            request.geometry_artifact_id = geometry.id
            inputs.append(geometry.id)
        elif scientific_context.startswith("goal_"):
            field = scientific_context.removeprefix("goal_")
            goal.conditions[field] = getattr(request, field)
        else:
            request.conditions_source["method"] = scientific_context.removeprefix("request_")
            if scientific_context == "request_auto_selected":
                request.selection_reasons["method"] = "synthetic frozen selection; no live source retrieval"
    writer = Step(id="write", logical_id="write", tool="workspace.write",
        parameters={"name": "task.py", "content": "print('hello')\n", "role": "python"})
    plan = Plan(request_id=request.id, steps=[writer],
                goal_map={"report": OutputBinding(port=request.goals[0].port, gap="code not yet executed")})
    run = store.create_run(request, plan, PermissionSnapshot(code_execution=True, artifact_writes=True,
        allowed_tools=["workspace.write", "python.execute"], artifact_ids=inputs),
        BudgetLimits(analysis_executions=2, plan_revisions=2, code_retries=1))
    return store, run, request, writer


def written(store, run, writer):
    result = execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    assert result.operation_status == "completed"
    ref = result.observations["written_artifact"]
    return result, {"artifact_id": ref["artifact_id"], "sha256": ref["sha256"]}


def execute_fixture(store, run, reference, *, manifest=None):
    plan = store.load_plan(run)
    port = store.load_request(run).goals[0].port
    step = Step(id="execute", logical_id="execute", tool="python.execute",
                parameters={"code": reference, "observation_names": [port]})
    candidate = plan.model_copy(deep=True, update={"version": 2, "steps": [*plan.steps, step],
        "goal_map": {"report": OutputBinding(port=port, step_id=step.id)}})
    run = store.commit_revision(run, candidate, decision_id="execute_decision", basis=current_basis(store, run))
    attempt = store.reserve_attempt(run, step)
    directory = store.path(attempt.directory) / "task"
    directory.mkdir(exist_ok=True)
    (directory / "report.txt").write_text("observed text", encoding="utf-8")
    manifest = manifest or {"version": 1, "observations": [{"name": port, "value": "observed text",
        "sources": [{"filename": "report.txt", "location": "line 1"}]}]}
    (directory / "observations.json").write_text(json.dumps(manifest), encoding="utf-8")
    outcome = {"state": "completed", "exit_code": 0, "resource_usage": {}}
    store._write_json(f"{attempt.directory}/execution.json", outcome, immutable=True)
    result = managed.collect_result(store, run, step, attempt, outcome)
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         started=True, termination_confirmed=True)
    return run, attempt, result


def test_immutable_write_can_be_consumed_without_expanding_permission(tmp_path, monkeypatch):
    store, run, request, writer = case(tmp_path, monkeypatch)
    before = run.permission.model_dump()
    written_result, reference = written(store, run, writer)
    assert store.verified_own_artifacts(run) == {reference["artifact_id"]}
    assert run.usage.file_writes == 1 and run.usage.analysis_executions == 0
    assert not written_result.qualified_outputs
    run, attempt, result = execute_fixture(store, run, reference)
    assert run.permission.model_dump() == before and not run.permission.artifact_ids
    assert not result.qualified_outputs
    observed = result.observation_records[0]
    assert observed.validation_status == "unverified" and observed.code_sha256 == reference["sha256"]
    assert observed.sources[0].attempt_id == attempt.id
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], result)
    assert assessment["status"] == "passed" and assessment["scientific_qualification"] is False
    row = collect_goal_facts(store, run, request, store.load_plan(run))[0]
    assert row["answer"]["scientific_qualification"] is False
    assert row["answer"]["observation"]["value"] == "observed text"


@pytest.mark.parametrize("scientific_context", ["root_geometry", "goal_method", "goal_basis",
                                               "request_explicit", "request_auto_selected"])
def test_file_observation_cannot_discharge_declared_physical_purpose(tmp_path, monkeypatch, scientific_context):
    store, run, request, writer = case(tmp_path, monkeypatch, scientific_context=scientific_context)
    _, reference = written(store, run, writer)
    run, _, result = execute_fixture(store, run, reference)
    assert not request.systems and not run.internal_calls
    assert not run.permission.scientific_execution and run.usage.orca_starts_actual == 0
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], result)
    assert assessment["status"] == "unresolved", assessment
    assert assessment["scientific_qualification"] is False and not result.qualified_outputs


def test_pure_file_observation_ignores_only_legacy_unrequested_physical_defaults(tmp_path, monkeypatch):
    store, run, request, writer = case(tmp_path, monkeypatch)
    assert request.method == "HF" and request.basis == "STO-3G"
    assert not request.systems and request.geometry_artifact_id is None and not request.goals[0].conditions
    assert request.conditions_source["method"] == request.conditions_source["basis"] == "default"
    _, reference = written(store, run, writer)
    run, _, result = execute_fixture(store, run, reference)
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], result)
    assert assessment["status"] == "passed" and assessment["scientific_qualification"] is False
    assert not run.internal_calls and not result.qualified_outputs


@pytest.mark.parametrize("name", ["../x.py", "/x.py", "C:x.py", "NUL", "x/y.py", "x\\y.py", "x. "])
def test_write_path_is_task_basename(name):
    with pytest.raises(ValueError):
        WriteParameters(name=name, content="x", role="python")


def test_writer_bounds_utf8_bytes():
    with pytest.raises(ValueError):
        WriteParameters(name="x.py", content="汉" * 22000, role="python")


def test_pure_file_delivery_does_not_require_electronic_state(tmp_path, monkeypatch):
    from orca_agent.decision_purpose import terminal_actions
    _, run, request, _ = case(tmp_path, monkeypatch)
    assert terminal_actions(request, run) == ("stop",)


def test_unknown_writer_does_not_grant_input_access(tmp_path, monkeypatch):
    store, run, _, writer = case(tmp_path, monkeypatch)
    _, reference = written(store, run, writer)
    run.calls[0].state = "unknown"
    with pytest.raises(StoreError, match="settled Tool call cannot be rewritten"):
        store.save_run(run)
    assert reference["artifact_id"] not in store.verified_own_artifacts(run)


@pytest.mark.parametrize("change", ["missing_source", "host_path", "undeclared", "script_passed", "duplicate"])
def test_manifest_rejection_retains_raw_evidence_and_operation(tmp_path, monkeypatch, change):
    store, run, request, writer = case(tmp_path, monkeypatch)
    _, ref = written(store, run, writer)
    item = {"name": "artifacts", "value": 1, "unit": "test", "sources": [{"filename": "report.txt"}]}
    if change == "missing_source":
        item["sources"][0]["filename"] = "absent.txt"
    elif change == "host_path":
        item["sources"][0]["filename"] = "C:/secrets.txt"
    elif change == "undeclared":
        item["name"] = "different_goal"
    elif change == "script_passed":
        item["validation_status"] = "passed"
    manifest = {"version": 1, "observations": [item, item] if change == "duplicate" else [item]}
    run, _, result = execute_fixture(store, run, ref, manifest=manifest)
    assert result.operation_status == "completed" and not result.observation_records
    assert result.source["files"]["task/observations.json"]
    assert any(d["category"] == "observation_manifest_rejected" for d in result.diagnostics)
    assert goal_evidence_assessment(store, run, request, request.goals[0], result)["status"] == "unresolved"


@pytest.mark.parametrize("change", ["qualified", "minimum_scf", "purpose", "value", "hash"])
def test_observation_cannot_upgrade_science_or_change_purpose(tmp_path, monkeypatch, change):
    store, run, request, writer = case(tmp_path, monkeypatch)
    _, ref = written(store, run, writer)
    run, _, result = execute_fixture(store, run, ref)
    goal = request.goals[0]
    if change == "qualified":
        goal.verification = "qualified"
    elif change == "minimum_scf":
        goal.minimum_evidence.append("converged_scf@1")
    elif change == "purpose":
        request.version += 1
    elif change == "value":
        result.observation_records[0].value = "forged"
    elif change == "hash":
        artifact = result.observation_records[0].sources[0].artifact_id
        store.artifact_path(artifact).write_text("changed", encoding="utf-8")
    assert goal_evidence_assessment(store, run, request, goal, result)["status"] == "unresolved"


def test_duplicate_writer_is_not_reexecuted(tmp_path, monkeypatch):
    store, run, _, writer = case(tmp_path, monkeypatch)
    written(store, run, writer)
    with pytest.raises(StoreError, match="durable Call"):
        execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    assert run.usage.file_writes == 1 and run.usage.analysis_executions == 0
