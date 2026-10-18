"""Code intent preserves goals and requires the managed execution boundary."""

import json
import shutil
from pathlib import Path

import pytest

from orca_agent.applicability import effective_conditions
from orca_agent.config import Config
from orca_agent.goals import goal_evidence_assessment
from orca_agent.minimum_evidence import describe_evidence_contract
from orca_agent.models import (
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Step,
)
from orca_agent.orca.adapter import prepare_input, read_outputs
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.runner import execute
from orca_agent.store import Store
from orca_agent.tools.dispatch import execute_call


def code_task(tmp_path, tool="python.execute"):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    path = tmp_path / "task.txt"
    path.write_text("print('a task artifact')", encoding="utf-8")
    artifact = store.import_artifact(path, "task_code")
    goal = Goal(id="excited", port="excitation_energy", quantity="电子激发能",
                original_text="计算丙酮电子激发能", minimum_check_version="generic-observation-1",
                evidence_contract_version="generic-observation-1",
                verification="reported_with_limitations",
                minimum_evidence=["source_file_and_units"],
                minimum_evidence_source={"source_file_and_units": "explicit"})
    request = Request(goals=[goal], method="PBE0", basis="def2-SVP",
                      conditions_source={"method": "auto_selected", "basis": "auto_selected"},
                      selection_reasons={"method": "task choice", "basis": "task choice"})
    code_key, language = ("code", "python") if tool == "python.execute" else ("input", "orca")
    step = Step(id="code1", logical_id="analysis", tool=tool, parameters={
        code_key: {"artifact_id": artifact.id, "sha256": artifact.sha256, "language": language},
        "observation_names": [goal.port], "conditions": {"method": "PBE0", "basis": "def2-SVP"}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={goal.id: OutputBinding(port=goal.port, step_id=step.id)})
    permission = PermissionSnapshot(allowed_tools=[tool], artifact_ids=[artifact.id],
                                    code_execution=True, scientific_execution=True, artifact_writes=True)
    return store, request, plan, permission


@pytest.mark.parametrize("tool", ["python.execute", "orca.run"])
def test_generic_goal_and_code_step_cannot_bypass_managed_attempt(tmp_path, tool):
    store, request, plan, permission = code_task(tmp_path, tool)
    run = store.create_run(request, plan, permission)
    assert store.load_plan(run) == plan
    assert store.load_request(run).goals[0].port == "excitation_energy"
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ValueError, match="require a Plan/Step/Attempt"):
        execute_call(store, run, tool, plan.steps[0].parameters.model_dump(), step=plan.steps[0])
    assert store.load_run(run.id).model_dump_json() == before
    coverage = describe_evidence_contract(request.goals[0])
    assert coverage["quantity"] == "电子激发能"
    assert coverage["scientific_checker"] == "unavailable"
    assert coverage["requirements"][0]["source"] == "explicit"


@pytest.mark.parametrize("tool", ["python.execute", "orca.run"])
def test_agent_checks_execution_permission_before_managed_launch(tmp_path, tool, monkeypatch):
    store, request, plan, permission = code_task(tmp_path, tool)
    permission.code_execution = permission.scientific_execution = False
    run = store.create_run(request, plan, permission)
    # Admission-only test; no mocked successful process/isolation evidence.
    monkeypatch.setattr("orca_agent.natural.ensure_scientific_environment", lambda *args: None)
    execute(store, Config(data_root=store.root), run.id)
    finished = store.load_run(run.id)
    assert finished.state == "failed"
    assert "not authorized" in json.dumps(finished.diagnostics)
    assert not finished.calls and not finished.attempts
    assert finished.usage.orca_starts_reserved == finished.usage.model_calls == 0


@pytest.mark.parametrize("change", ["evidence", "verification", "quantity"])
def test_model_or_revision_flag_cannot_lower_user_evidence(tmp_path, change):
    store, request, plan, permission = code_task(tmp_path)
    run = store.create_run(request, plan, permission)
    proposed = request.model_copy(deep=True)
    proposed.version += 1
    proposed.messages.append({"id": "user1", "source": "user", "text": "继续",
                              "created_at": "2026-10-18T00:00:00Z"})
    if change == "evidence":
        proposed.goals[0].minimum_evidence = []
    elif change == "verification":
        request.goals[0].verification = "qualified"
    else:
        proposed.goals[0].quantity = "轨道能隙"
    with pytest.raises(PlanningError):
        validate_revision(request, plan, proposed, None, run, user_update=True)


def test_generic_observation_cannot_bind_a_strict_scientific_goal(tmp_path):
    store, request, plan, permission = code_task(tmp_path)
    request.goals[0].verification = "qualified"
    run = store.create_run(request, None, permission)
    with pytest.raises(PlanningError, match="cannot replace required scientific"):
        validate_revision(request, None, request, plan, run)
    with pytest.raises(PlanningError, match="cannot replace required scientific"):
        store.create_run(request, plan, permission)


def test_declared_conditions_cannot_change_explicit_request(tmp_path):
    _, request, plan, _ = code_task(tmp_path)
    plan.steps[0].parameters.conditions["basis"] = "STO-3G"
    with pytest.raises(ValueError, match="changes request condition:basis"):
        plan.validate_request(request)


def test_general_conditions_are_expressible_without_claiming_legacy_execution(tmp_path):
    _, request, _, _ = code_task(tmp_path)
    assessment = effective_conditions(request)
    assert assessment["conditions"]["electronic_state"] is None
    assert assessment["conditions"]["environment"] is None
    assert not any(reason.startswith("unsupported_condition") for reason in assessment["reasons"])
    request.conditions.update(electronic_state="restricted_KS", environment="gas_phase")
    assert effective_conditions(request)["status"] == "passed"
    with pytest.raises(ValueError, match="legacy_adapter_profile_unsupported"):
        prepare_input(tmp_path / "attempt", tmp_path / "absent.xyz",
                      {"method": "PBE0", "basis": "def2-SVP"}, "orca.sp")
    assert not (tmp_path / "attempt").exists()
    with pytest.raises(ValueError, match="legacy_adapter_profile_unsupported"):
        read_outputs(tmp_path, {"method": "PBE0", "basis": "def2-SVP"}, "orca.sp")


def test_script_claims_never_satisfy_generic_or_strict_goal(tmp_path):
    store, request, plan, permission = code_task(tmp_path)
    run = store.create_run(request, plan, permission)
    result = Result(run_id=run.id, operation_status="completed",
                    observations={"excitation_energy": {"value": 4.2, "unit": "eV", "passed": True}})
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], result)
    assert assessment["status"] == "unresolved"
    assert "generic_observation_requires_current_run_provenance" in assessment["reasons"]


@pytest.mark.parametrize("mutation", ["permission", "digest"])
def test_task_inputs_are_checked_before_persistence(tmp_path, mutation):
    store, request, plan, permission = code_task(tmp_path)
    if mutation == "permission":
        permission.artifact_ids = []
    else:
        plan.steps[0].parameters.code.sha256 = "0" * 64
    with pytest.raises(ValueError, match="permission snapshot|hash differs"):
        store.create_run(request, plan, permission)


@pytest.mark.parametrize("field,value", [("method", "PBE0"), ("basis", "def2-SVP")])
def test_legacy_parser_cannot_relabel_real_hf_output_as_another_method(tmp_path, field, value):
    fixture = Path(__file__).parents[1] / "fixtures" / "phase_a" / "real_water_sp"
    shutil.copytree(fixture, tmp_path / "evidence")
    path = tmp_path / "evidence" / "input-manifest.json"
    manifest = json.loads(path.read_text())
    manifest["parameters"][field] = value
    path.write_text(json.dumps(manifest))
    original = path.read_bytes()
    with pytest.raises(ValueError, match="legacy_adapter_profile_unsupported"):
        read_outputs(path.parent, manifest["parameters"], "orca.sp")
    assert path.read_bytes() == original


@pytest.mark.parametrize("multiplicity", [0, -1])
def test_general_request_requires_positive_explicit_multiplicity(multiplicity):
    with pytest.raises(ValueError):
        Request(goals=[Goal(id="g", port="observation")], multiplicity=multiplicity)
