"""F-01/F-02/F-03: archived evidence and synthetic lifecycle; zero ORCA starts."""

import json
from pathlib import Path

import pytest

from orca_agent.applicability import source_conditions
from orca_agent.goals import goal_evidence_assessment, validate_goal_evidence
from orca_agent.models import (
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    QualifiedOutput,
    Request,
    Result,
    Step,
    SystemInput,
)
from orca_agent.orca.adapter import prepare_input, read_outputs
from orca_agent.store import Store, StoreError
from orca_agent.tools.dispatch import _current_comparison_members, execute_call
from tests.unit.test_analysis import sampling_case
from tests.unit.test_dispatch import archived_energy, comparison_run
from tests.unit.test_orca import synthetic_output

FIXTURES = Path(__file__).parents[1] / "fixtures" / "phase_a"


@pytest.fixture
def archived(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    run, result = archived_energy(store)
    return store, run, store.load_request(run), result


@pytest.mark.parametrize("name,value", [
    ("environment", "water_solvent"), ("electronic_state", "UHF"),
    ("environment", None), ("electronic_state", "unknown"),
    ("method", "B3LYP"), ("basis", "6-31G"), ("charge", 1), ("multiplicity", 3),
])
def test_full_current_conditions_block_history_and_scientific_plans(archived, name, value):
    store, run, request, result = archived
    request.conditions[name] = value
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], result)
    assert assessment["status"] == "unresolved" and any(name in r for r in assessment["reasons"])
    with pytest.raises(ValueError, match="condition"):
        store.load_plan(run).validate_request(request)
    assert store.load_result(run.id, result.id).model_dump(mode="json")["qualified_outputs"] == result.model_dump(mode="json")["qualified_outputs"]
    assert run.usage.orca_starts_actual == 0


def test_goal_conditions_are_constraints_and_old_profile_restoration_is_traceable(archived):
    store, run, request, result = archived
    goal = request.goals[0]
    actual, evidence = source_conditions(store, result)
    assert actual["electronic_state"] == "RHF" and actual["environment"] == "gas_phase"
    assert evidence["environment"]["input"]["sha256"]
    goal.conditions["basis"] = "6-31G"
    assert not validate_goal_evidence(store, run, request, goal, result)
    with pytest.raises(ValueError, match="conflicting_goal_condition:basis"):
        store.load_plan(run).validate_request(request)
    goal.conditions = {"note": "irrelevant explanatory wording", "temperature": 298.15}
    request.version += 1
    assert validate_goal_evidence(store, run, request, goal, result)


@pytest.mark.parametrize("requirement,passed", [
    ("energy", True), ("orca-hf-2", True), ("converged_scf", True),
    ("converged SCF", True), ("purpose_preserved@1", True),
    ("original purpose and conditions must be preserved", True),
    ("independent_scf_stability_check", False), ("converged_scf@999", False),
    ("some unknown original prose", False),
])
def test_minimum_evidence_actual_predicates_and_versioned_legacy_mapping(archived, requirement, passed):
    store, run, request, result = archived
    goal = request.goals[0]
    goal.minimum_evidence = [requirement]
    assessment = goal_evidence_assessment(store, run, request, goal, result)
    assert (assessment["status"] == "passed") is passed
    item = assessment["minimum_evidence"][0]
    assert item["requested"] == requirement and item["mapping_version"] == "minimum-evidence-1"
    if passed:
        request.conditions["environment"] = "water_solvent"
        assert not validate_goal_evidence(store, run, request, goal, result)


@pytest.mark.parametrize("systems", [["water", "methane"], ["methane", "water"], ["water", "water"], ["missing"]])
def test_multisystem_scalar_never_silently_chooses_first_member(archived, systems):
    store, run, request, result = archived
    request.systems = [SystemInput(id="water", geometry_artifact_id=request.geometry_artifact_id),
                       SystemInput(id="methane", geometry_artifact_id=request.geometry_artifact_id)]
    request.goals[0].system_ids = systems
    assessment = goal_evidence_assessment(store, run, request, request.goals[0], result)
    assert assessment["status"] == "unresolved" and assessment["reasons"]
    assert request.goals[0].system_ids == systems


def test_sampling_consumption_checks_current_request_and_goal_conditions():
    _, members, _, _ = sampling_case("stop")
    goal = Goal(id="sampling", port="sampling", minimum_check_version="finite-sampling-1",
                conditions={"basis": "6-31G"})
    request = Request(goals=[goal], conditions={"environment": "water_solvent"})
    checked = _current_comparison_members(None, request, members, goal)
    assert all(member.evidence is None for member in checked)
    assert all("environment" in member.missing_reason and "basis" in member.missing_reason
               for member in checked if member.required)


def test_derived_result_reuse_rechecks_outer_conditions_but_not_unrelated_text(archived):
    store, source_run, _, source = archived
    run, step = comparison_run(store, source_run, source)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    request = store.load_request(run)
    goal = request.goals[0]
    assert validate_goal_evidence(store, run, request, goal, result)
    request.original_text += " (same scientific purpose)"
    request.version += 1
    assert validate_goal_evidence(store, run, request, goal, result)
    request.conditions["environment"] = "water_solvent"
    assert not validate_goal_evidence(store, run, request, goal, result)
    assert result.qualified_outputs["energy_difference"].value == 0


def _synthetic_result(store, run, step, geometry, *, converged=True):
    """One production parser/checker over marked text; never a backend launch."""
    attempt = store.reserve_attempt(run, step, geometry)
    work = store.path(attempt.directory)
    prepare_input(work, store.artifact_path(geometry), step.parameters, step.tool)
    text = synthetic_output(work / "geometry.xyz", opt=step.tool == "orca.opt")
    if step.tool == "orca.opt":
        final = (work / "geometry.xyz").read_text().splitlines()
        final[1] = "Synthetic final structure; no scientific execution"
        atom = final[3].split()
        atom[1] = str(float(atom[1]) + 0.025)
        final[3] = " ".join(atom)
        (work / "job.xyz").write_text("\n".join(final) + "\n")
        final_block = "CARTESIAN COORDINATES (ANGSTROEM)\n------------------------\n" + "\n".join(final[2:]) + "\n\n"
        text = text.replace("SCF CONVERGED AFTER", final_block + "SCF CONVERGED AFTER")
        if not converged:
            text = text.replace("THE OPTIMIZATION HAS CONVERGED", "OPTIMIZATION DID NOT CONVERGE")
    (work / "stdout.out").write_text(text)
    parsed = read_outputs(work, step.parameters, step.tool)
    files = {path.name: store.import_artifact(path, "raw_evidence", run_id=run.id, attempt_id=attempt.id)
             for path in work.iterdir() if path.is_file()}
    outputs = {}
    for port, output in parsed["qualified_outputs"].items():
        args = ({"artifact_id": files["job.xyz"].id} if port == "optimized_geometry"
                else {"value": output["value"], "unit": output["unit"]})
        outputs[port] = QualifiedOutput(**args, checks=parsed["checks"][port])
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
        operation_status="completed", checks=parsed["checks"], qualified_outputs=outputs,
        artifact_ids=[artifact.id for artifact in files.values()], observations=parsed["observations"],
        source={"input_fingerprint": attempt.input_fingerprint, "geometry_artifact_id": geometry,
                "conditions": step.parameters.model_dump(mode="json"),
                "files": {name: {"artifact_id": artifact.id, "sha256": artifact.sha256}
                          for name, artifact in files.items()}})
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         started=False, termination_confirmed=True)
    return result


def _optimization_run(tmp_path, *, other_consumer=False):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    initial = store.import_artifact(FIXTURES / "water_opt" / "geometry.xyz", "initial_geometry")
    methane = store.import_artifact(FIXTURES / "methane_sp" / "geometry.xyz", "initial_geometry")
    target = "methane" if other_consumer else "water"
    goal = Goal(id="energy", port="energy", system_ids=[target], minimum_check_version="orca-hf-2",
                conditions={"geometry_relation": "optimized"})
    request = Request(goals=[goal], systems=[SystemInput(id="water", geometry_artifact_id=initial.id),
                                            SystemInput(id="methane", geometry_artifact_id=methane.id)])
    opt = Step(id="opt", logical_id="opt", tool="orca.opt", system_id="water",
               geometry=InputRef(artifact_id=initial.id))
    sp = Step(id="sp", logical_id="sp", tool="orca.sp", system_id=target, depends_on=["opt"],
              geometry=InputRef(producer_step_id="opt", port="optimized_geometry"))
    plan = Plan(request_id=request.id, steps=[opt, sp], goal_map={"energy": OutputBinding(step_id="sp", port="energy")})
    permission = PermissionSnapshot(scientific_execution=True, artifact_ids=[initial.id, methane.id])
    return store, request, plan, permission, initial


def test_cross_system_future_geometry_rejected_before_plan_activation(tmp_path):
    store, request, plan, permission, _ = _optimization_run(tmp_path, other_consumer=True)
    with pytest.raises(ValueError, match="cross-system"):
        store.create_run(request, plan, permission)
    assert store.environment_lease() is None


def test_initial_sp_cannot_satisfy_optimized_energy(archived):
    store, run, request, result = archived
    request.goals[0].conditions["geometry_relation"] = "optimized"
    assert not validate_goal_evidence(store, run, request, request.goals[0], result)
    assert result.qualified_outputs["energy"]


@pytest.mark.parametrize("converged", [False, True])
def test_optimized_energy_requires_actual_converged_geometry_and_accepts_same_run_opt_sp(tmp_path, converged):
    store, request, plan, permission, initial = _optimization_run(tmp_path)
    run = store.create_run(request, plan, permission)
    result = _synthetic_result(store, run, plan.steps[0], initial.id, converged=converged)
    goal = request.goals[0]
    assert "energy" in result.qualified_outputs
    assert validate_goal_evidence(store, run, request, goal, result) is converged
    if converged:
        geometry = result.qualified_outputs["optimized_geometry"].artifact_id
        assert store.load_artifact(geometry).sha256 != initial.sha256
        downstream = _synthetic_result(store, run, plan.steps[1], geometry)
        assert validate_goal_evidence(store, run, request, goal, downstream)
        other = store.create_run(request, None, PermissionSnapshot(
            artifact_ids=permission.artifact_ids, result_ids=[result.id, downstream.id]))
        assert validate_goal_evidence(store, other, request, goal, result)
        assert validate_goal_evidence(store, other, request, goal, downstream)
        goal.conditions["geometry_relation"] = "fixed_initial"
        assert not validate_goal_evidence(store, run, request, goal, downstream)
    assert run.usage.orca_starts_actual == 0


def test_frozen_producer_system_cannot_be_relabelled_before_consumption(tmp_path):
    store, request, plan, permission, initial = _optimization_run(tmp_path)
    run = store.create_run(request, plan, permission)
    result = _synthetic_result(store, run, plan.steps[0], initial.id)
    geometry = result.qualified_outputs["optimized_geometry"].artifact_id
    # Corruption probe against persisted recovery input, not a permitted update.
    path = store.path(f"runs/{run.id}/run.json")
    payload = json.loads(path.read_text())
    payload["attempts"][0]["frozen_step"]["system_id"] = "methane"
    path.write_text(json.dumps(payload), encoding="utf-8")
    run = store.load_run(run.id)
    with pytest.raises(StoreError, match="cross_system"):
        store.reserve_attempt(run, plan.steps[1], geometry)
    assert len(run.attempts) == 1 and run.usage.orca_starts_actual == 0


def test_each_explicit_required_system_must_have_its_own_energy(tmp_path):
    from orca_agent import runner

    store, _, _, permission, water = _optimization_run(tmp_path)
    methane_id = next(identifier for identifier in permission.artifact_ids if identifier != water.id)
    systems = [SystemInput(id="water", geometry_artifact_id=water.id),
               SystemInput(id="methane", geometry_artifact_id=methane_id)]
    goals = [Goal(id=system.id, port="energy", system_ids=[system.id], minimum_check_version="orca-hf-2")
             for system in systems]
    request = Request(systems=systems, goals=goals)
    steps = [Step(id=system.id, logical_id=system.id, tool="orca.sp", system_id=system.id,
                  geometry=InputRef(artifact_id=system.geometry_artifact_id)) for system in systems]
    plan = Plan(request_id=request.id, steps=steps,
                goal_map={goal.id: OutputBinding(step_id=goal.id, port="energy") for goal in goals})
    run = store.create_run(request, plan, permission)
    water_result = _synthetic_result(store, run, steps[0], water.id)
    assert not runner._goals(store, run, plan, {"water": water_result})
    assert run.goal_status == {"water": "satisfied", "methane": "insufficient_evidence"}
    store.save_run(run)
    methane_result = _synthetic_result(store, run, steps[1], methane_id)
    assert runner._goals(store, run, plan, {"water": water_result, "methane": methane_result})
    assert run.usage.orca_starts_actual == 0


def test_source_unknown_and_missing_or_failed_checks_cannot_use_legacy_defaults(archived):
    store, run, request, source = archived
    goal = request.goals[0]
    for change in ("unknown", "missing_check", "failed_check", "wrong_rule"):
        result = source.model_copy(deep=True)
        if change == "unknown":
            result.source["conditions"]["environment"] = None
        else:
            checks = result.qualified_outputs["energy"].checks
            if change == "missing_check":
                checks[:] = [check for check in checks if check.name != "scf_converged"]
            elif change == "failed_check":
                checks[0].status = "failed"
            else:
                checks[0].rule_version = "orca-hf-1"
            result.checks["energy"] = checks
        assert not validate_goal_evidence(store, run, request, goal, result)


def test_goal_system_environment_conflict_blocks_plan_without_changing_requirements(tmp_path):
    store, request, plan, permission, _ = _optimization_run(tmp_path)
    request.systems[0].conditions["environment"] = "gas_phase"
    request.goals[0].conditions["environment"] = "water_solvent"
    with pytest.raises(ValueError, match="conflicting_goal_condition:environment"):
        store.create_run(request, plan, permission)
    assert request.goals[0].conditions["environment"] == "water_solvent"


def test_wrong_final_structure_and_wrong_target_never_satisfy_optimized_energy(tmp_path):
    store, request, plan, permission, initial = _optimization_run(tmp_path)
    run = store.create_run(request, plan, permission)
    result = _synthetic_result(store, run, plan.steps[0], initial.id)
    goal = request.goals[0]
    assert validate_goal_evidence(store, run, request, goal, result)
    other_target = request.model_copy(deep=True)
    other_target.goals[0].system_ids = ["methane"]
    assert not validate_goal_evidence(store, run, other_target, other_target.goals[0], result)
    wrong = result.model_copy(deep=True)
    wrong.qualified_outputs["optimized_geometry"].artifact_id = initial.id
    assert not validate_goal_evidence(store, run, request, goal, wrong)


def test_derived_minimum_evidence_addition_is_reassessed_against_actual_output(archived):
    store, source_run, _, source = archived
    run, step = comparison_run(store, source_run, source)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    request = store.load_request(run)
    goal = request.goals[0]
    goal.minimum_evidence = ["original purpose and conditions must be preserved"]
    assert validate_goal_evidence(store, run, request, goal, result)
    goal.minimum_evidence.append("independent_scf_stability_check")
    assert not validate_goal_evidence(store, run, request, goal, result)


def test_new_run_can_consume_historical_optimized_artifact_with_original_root(tmp_path):
    store, request, plan, permission, initial = _optimization_run(tmp_path)
    source_run = store.create_run(request, plan, permission)
    optimized = _synthetic_result(store, source_run, plan.steps[0], initial.id)
    artifact = optimized.qualified_outputs["optimized_geometry"].artifact_id
    step = Step(id="new_sp", logical_id="new_sp", tool="orca.sp", system_id="water",
                geometry=InputRef(artifact_id=artifact))
    current_plan = Plan(request_id=request.id, steps=[step],
                        goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    permission.artifact_ids.append(artifact)
    permission.result_ids.append(optimized.id)
    run = store.create_run(request, current_plan, permission)
    result = _synthetic_result(store, run, step, artifact)
    assert validate_goal_evidence(store, run, request, request.goals[0], result)
    assert run.usage.orca_starts_actual == source_run.usage.orca_starts_actual == 0


def test_arbitrary_registered_geometry_cannot_replace_initial_geometry(tmp_path):
    store, request, plan, permission, initial = _optimization_run(tmp_path)
    other = tmp_path / "other.xyz"
    text = store.artifact_path(initial.id).read_text()
    other.write_text(text + "\n")
    artifact = store.import_artifact(other, "initial_geometry")
    # Same coordinates but a different unqualified external identity is not an
    # optimized structure with a checked provenance chain.
    plan.steps[0].geometry = InputRef(artifact_id=artifact.id)
    permission.artifact_ids.append(artifact.id)
    with pytest.raises((StoreError, ValueError), match="direct_geometry"):
        store.create_run(request, plan, permission)


def test_independently_qualified_structure_does_not_require_energy_output_port(tmp_path):
    store, request, plan, permission, initial = _optimization_run(tmp_path)
    run = store.create_run(request, plan, permission)
    result = _synthetic_result(store, run, plan.steps[0], initial.id)
    result.qualified_outputs.pop("energy")
    goal = Goal(id="geometry", port="optimized_geometry", system_ids=["water"], minimum_check_version="orca-hf-2")
    assert validate_goal_evidence(store, run, request, goal, result)
