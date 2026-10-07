"""G02: production collection and consumption; synthetic faults, no backend starts."""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from orca_agent.applicability import qualified_geometry
from orca_agent.goals import validate_goal_evidence
from orca_agent.models import (
    CalculationParameters,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
    SystemInput,
)
from orca_agent.orca.adapter import prepare_input, read_outputs
from orca_agent.orca.checks import OPTIMIZATION_STAGE_RULE
from orca_agent.store import Store, StoreError
from orca_agent.tools.calculation import collect_result
from orca_agent.versions import CURRENT_CHECK_VERSION
from tests.unit.test_orca import snapshot, synthetic_output

ROOT = Path(__file__).parents[2]
FIXTURES = ROOT / "tests/fixtures"
REAL_OPT = FIXTURES / "optimization_final_stage/real_methane_opt"
COORDINATES = "CARTESIAN COORDINATES (ANGSTROEM)"
SUCCESS = "THE OPTIMIZATION HAS CONVERGED"
NORMAL = "ORCA TERMINATED NORMALLY\n"
FINAL_EVALUATION = "FINAL ENERGY EVALUATION AT THE STATIONARY POINT\n"


def _run(tmp_path, *, initial_path=None, params=None):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    initial = store.import_artifact(
        initial_path or FIXTURES / "phase_a/water_opt/geometry.xyz", "initial_geometry")
    goals = [Goal(id="energy", port="energy", system_ids=["target"],
                  conditions={"geometry_relation": "optimized"},
                  minimum_check_version=CURRENT_CHECK_VERSION),
             Goal(id="geometry", port="optimized_geometry", system_ids=["target"],
                  minimum_check_version=CURRENT_CHECK_VERSION)]
    request = Request(goals=goals, systems=[SystemInput(id="target", geometry_artifact_id=initial.id)])
    opt = Step(id="opt", logical_id="opt", tool="orca.opt", system_id="target",
               geometry=InputRef(artifact_id=initial.id), parameters=params or CalculationParameters())
    sp = Step(id="sp", logical_id="sp", tool="orca.sp", system_id="target", depends_on=["opt"],
              geometry=InputRef(producer_step_id="opt", port="optimized_geometry"))
    plan = Plan(request_id=request.id, steps=[opt, sp], goal_map={
        "energy": OutputBinding(step_id="opt", port="energy"),
        "geometry": OutputBinding(step_id="opt", port="optimized_geometry")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[initial.id]))
    attempt = store.reserve_attempt(run, opt, initial.id)
    return store, run, request, opt, sp, initial, attempt


def _case_text(geometry, case):
    base = synthetic_output(geometry, opt=True, normal=False)
    preamble, body = base.split(COORDINATES, 1)
    cycle = COORDINATES + body
    text = preamble + "GEOMETRY OPTIMIZATION CYCLE 1\n" + cycle
    second = "GEOMETRY OPTIMIZATION CYCLE 2\n" + cycle
    if case == "success_then_failure":
        text += second.replace(SUCCESS, "The optimization did not converge but reached the maximum number of\noptimization cycles.")
    elif case == "success_then_failed_threshold":
        text += second.replace("0.000003 0.000100 YES", "0.000300 0.000100 NO")
    elif case == "success_then_missing_table":
        text += second.split("Geometry convergence", 1)[0] + SUCCESS + "\n"
    elif case == "success_then_incomplete_cycle":
        text += "GEOMETRY OPTIMIZATION CYCLE 2\n"
    elif case == "success_then_missing_coordinates":
        text += "GEOMETRY OPTIMIZATION CYCLE 2\nSCF CONVERGED AFTER 9 CYCLES\nFINAL SINGLE POINT ENERGY -75.1\n" + cycle.split("Geometry convergence", 1)[1]
    elif case == "contradictory_terminal":
        text += "OPTIMIZATION HAS NOT CONVERGED\n"
    elif case == "table_after_success":
        text += "Geometry convergence\nMAX gradient 0.1 0.000100 NO\n"
    elif case == "duplicate_success":
        text += SUCCESS + "\n"
    elif case == "duplicate_threshold_row":
        text = text.replace(SUCCESS, "MAX gradient 0.000003 0.000100 YES\n" + SUCCESS)
    elif case == "truncated_cycle_header":
        text += "GEOMETRY OPTIMIZATION CYCLE\n"
    elif case == "truncated_evaluation_header":
        text += "FINAL ENERGY EVALUATION\n"
    elif case == "empty_trailing_coordinates":
        text += COORDINATES + "\n------------------------\n"
    elif case == "trailing_changed_coordinates":
        coords = geometry.read_text().splitlines()[2:]
        atom = coords[1].split()
        atom[1] = str(float(atom[1]) + 0.3)
        coords[1] = " ".join(atom)
        text += COORDINATES + "\n" + "\n".join(coords) + "\n"
    elif case in {"valid_final_evaluation", "duplicate_final_evaluation", "changed_final_evaluation"}:
        tail = FINAL_EVALUATION + cycle.split("Geometry convergence", 1)[0]
        if case == "changed_final_evaluation":
            atom = geometry.read_text().splitlines()[3]
            changed = atom.split()
            changed[1] = str(float(changed[1]) + 0.3)
            tail = tail.replace(atom, " ".join(changed))
        text += tail * (2 if case == "duplicate_final_evaluation" else 1)
    elif case == "unannounced_later_energy":
        text += cycle.split("Geometry convergence", 1)[0]
    elif case not in {"valid", "missing_normal"}:
        raise AssertionError(case)
    return text + ("" if case == "missing_normal" else NORMAL)


def _collect_synthetic(tmp_path, case):
    store, run, request, opt, sp, initial, attempt = _run(tmp_path)
    work = store.path(attempt.directory)
    prepare_input(work, store.artifact_path(initial.id), opt.parameters, opt.tool)
    (work / "stdout.out").write_text(_case_text(work / "geometry.xyz", case), encoding="utf-8")
    shutil.copyfile(work / "geometry.xyz", work / "job.xyz")
    before = snapshot(work)
    result = collect_result(store, run, opt, attempt, {
        "state": "completed", "reason": "synthetic final-stage regression; no process started"})
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         started=False, termination_confirmed=True)
    assert snapshot(work) == before
    for name, entry in result.source["files"].items():
        assert entry["sha256"] == before[name]
        assert store.artifact_path(entry["artifact_id"]).read_bytes() == (work / name).read_bytes()
    assert run.usage.orca_starts_actual == run.usage.postprocess_starts == 0
    return store, run, request, sp, result


@pytest.mark.parametrize("case,energy", [
    ("success_then_failure", True),
    ("success_then_failed_threshold", True),
    ("success_then_missing_table", True),
    ("success_then_incomplete_cycle", False),
    ("success_then_missing_coordinates", False),
    ("contradictory_terminal", True),
    ("table_after_success", True),
    ("duplicate_success", True),
    ("duplicate_threshold_row", True),
    ("truncated_cycle_header", False),
    ("truncated_evaluation_header", False),
    ("empty_trailing_coordinates", True),
    ("trailing_changed_coordinates", True),
    ("duplicate_final_evaluation", True),
    ("changed_final_evaluation", True),
    ("unannounced_later_energy", True),
    ("missing_normal", False),
])
def test_final_stage_faults_cannot_publish_or_consume_optimized_structure(tmp_path, case, energy):
    store, run, request, sp, result = _collect_synthetic(tmp_path, case)
    assert ("energy" in result.qualified_outputs) is energy
    assert "optimized_geometry" not in result.qualified_outputs
    check = next(c for c in result.checks["optimized_geometry"] if c.name == "optimization_stage_binding")
    assert check.status == "failed" and check.source["rule_version"] == OPTIMIZATION_STAGE_RULE
    assert result.observations["optimization_stage"]["reasons"]
    assert any(d["category"] == "optimization_stage_unverified" for d in result.diagnostics)
    assert all(not validate_goal_evidence(store, run, request, goal, result) for goal in request.goals)
    budget = run.usage.model_dump()
    geometry = result.source["files"]["job.xyz"]["artifact_id"]
    with pytest.raises(StoreError):
        store.reserve_attempt(run, sp, geometry)
    assert run.usage.model_dump() == budget and len(run.attempts) == 1
    assert store.environment_lease() is None


@pytest.mark.parametrize("case", ["valid", "valid_final_evaluation"])
def test_complete_final_cycle_and_stationary_point_evaluation_are_consumable(tmp_path, case):
    store, run, request, sp, result = _collect_synthetic(tmp_path, case)
    assert all(validate_goal_evidence(store, run, request, goal, result) for goal in request.goals)
    geometry = qualified_geometry(store, result)
    proof = result.observations["optimization_stage"]
    assert proof["reasons"] == [] and proof["cycle_number"] == 1
    assert proof["stage_start_line"] < proof["converged_geometry_line"] < proof["converged_energy_line"]
    assert proof["converged_energy_line"] < proof["threshold_header_line"] < proof["success_lines"][0]
    assert (proof["final_geometry_line"] > proof["success_lines"][0]) is (case == "valid_final_evaluation")
    attempt = store.reserve_attempt(run, sp, geometry)
    assert attempt.geometry_artifact_id == geometry
    assert run.usage.orca_starts_actual == 0
    store.finish_attempt(run, attempt.id, state="cancelled", started=False, termination_confirmed=True)


@pytest.mark.parametrize("kind", ["absent", "missing_version", "old_version"])
def test_old_optimization_checks_do_not_acquire_new_local_rule(tmp_path, monkeypatch, kind):
    store, run, request, sp, current = _collect_synthetic(tmp_path, "valid")
    original = store.load_result(run.id, current.id).model_dump_json()
    historical = current.model_copy(deep=True)
    checks = historical.checks["optimized_geometry"]
    if kind == "absent":
        checks[:] = [check for check in checks if check.name != "optimization_stage_binding"]
    else:
        stage = next(check for check in checks if check.name == "optimization_stage_binding")
        stage.source = {} if kind == "missing_version" else {"rule_version": "optimization-final-stage-0"}
    historical.qualified_outputs["optimized_geometry"].checks = checks
    geometry = historical.qualified_outputs["optimized_geometry"].artifact_id
    with pytest.raises(ValueError, match="optimized_geometry"):
        qualified_geometry(store, historical)
    assert all(not validate_goal_evidence(store, run, request, goal, historical) for goal in request.goals)
    load_result = store.load_result
    monkeypatch.setattr(store, "load_result", lambda run_id, result_id: historical
                        if (run_id, result_id) == (run.id, historical.id) else load_result(run_id, result_id))
    before = run.usage.model_dump()
    with pytest.raises(StoreError):
        store.reserve_attempt(run, sp, geometry)
    assert run.usage.model_dump() == before and len(run.attempts) == 1
    assert load_result(run.id, current.id).model_dump_json() == original


def test_real_historical_opt_readonly_replay_has_final_cycle_proof_and_new_result(tmp_path):
    provenance = json.loads((REAL_OPT / "provenance.json").read_text())
    sources = {entry["name"]: ROOT / entry["repository_path"] for entry in provenance["files"]}
    for entry in provenance["files"]:
        assert hashlib.sha256(sources[entry["name"]].read_bytes()).hexdigest() == entry["sha256"]
    raw_before = {name: path.read_bytes() for name, path in sources.items()}
    params = CalculationParameters.model_validate(json.loads(sources["input-manifest.json"].read_text())["parameters"])
    store, run, request, opt, sp, _, attempt = _run(
        tmp_path, initial_path=sources["geometry.xyz"], params=params)
    work = store.path(attempt.directory)
    for name, source in sources.items():
        shutil.copyfile(source, work / name)
    shutil.copyfile(REAL_OPT / "provenance.json", work / "replay-provenance.json")
    before = snapshot(work)
    parsed = read_outputs(work, params, "orca.opt")
    assert set(parsed["qualified_outputs"]) == {"energy", "optimized_geometry"}
    result = collect_result(store, run, opt, attempt, {
        "state": "completed", "reason": "read_only_historical_replay; no new ORCA process",
        "replay_provenance": "replay-provenance.json"})
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         started=False, termination_confirmed=True)
    proof = result.observations["optimization_stage"]
    assert proof["cycle_number"] == 4 and proof["reasons"] == []
    assert proof["final_evaluation_lines"] and len(proof["success_lines"]) == 1
    assert all(validate_goal_evidence(store, run, request, goal, result) for goal in request.goals)
    assert "replay-provenance.json" in result.source["files"]
    assert snapshot(work) == before
    assert {name: path.read_bytes() for name, path in sources.items()} == raw_before
    assert not (work / "job.property.json").exists()
    geometry = qualified_geometry(store, result)
    following = store.reserve_attempt(run, sp, geometry)
    assert run.usage.orca_starts_actual == run.usage.postprocess_starts == 0
    store.finish_attempt(run, following.id, state="cancelled", started=False, termination_confirmed=True)


def test_real_sp_preserves_existing_rule_and_readonly_energy_qualification():
    directory = FIXTURES / "phase_a/real_water_sp"
    before = snapshot(directory)
    params = json.loads((directory / "input-manifest.json").read_text())["parameters"]
    parsed = read_outputs(directory, params, "orca.sp")
    assert set(parsed["qualified_outputs"]) == {"energy"}
    assert all(check.rule_version == CURRENT_CHECK_VERSION for check in parsed["checks"]["energy"])
    assert not any(check.name == "optimization_stage_binding" for check in parsed["checks"]["energy"])
    assert snapshot(directory) == before
