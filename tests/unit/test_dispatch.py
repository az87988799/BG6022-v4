"""Offline unified Tool tests; real archived output, never a scientific launch."""

import json
import shutil
from pathlib import Path

import pytest

from orca_agent.models import (
    BudgetLimits,
    CalculationParameters,
    EvidenceRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    QualifiedOutput,
    Request,
    Result,
    Step,
)
from orca_agent.orca.adapter import read_outputs
from orca_agent.report import build_report
from orca_agent.store import Store, StoreError, sha256_file
from orca_agent.tools.analysis import SamplingCandidate, SamplingParameters
from orca_agent.tools.dispatch import bind_inputs, execute_call

PROJECT = Path(__file__).resolve().parents[2]
REAL_SP = PROJECT / "tests/fixtures/phase_a/real_water_sp"
MANIFEST = json.loads((PROJECT / "tests/fixtures/phase_b/sampling-candidates.json").read_text())
REVIEW = json.loads((PROJECT / "docs/acceptance/phase-b/reference-review.json").read_text())


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data", environment_root=tmp_path / "environment")


def query_run(store, artifact_ids=(), *, tool="evidence.value", source_ids=(), writes=False,
              plan_step=None, goals=None, result_ids=()):
    request = Request(goals=goals or [Goal(id="query", port="value_observation", minimum_check_version="evidence-read-1")])
    plan = (Plan(request_id=request.id, steps=[plan_step], goal_map={
        request.goals[0].id: OutputBinding(step_id=plan_step.id, port=request.goals[0].port)})
            if plan_step else None)
    return store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=False, allowed_tools=[tool], artifact_ids=list(artifact_ids),
        source_ids=list(source_ids), result_ids=list(result_ids), artifact_writes=writes,
    ), BudgetLimits(orca_starts=0, evidence_reads=8, analysis_executions=4))


def archived_energy(store, fixture=REAL_SP):
    """Recheck archived real files through the one production parser/checker.

Attempt reservation is an offline lifecycle fixture, settled with started=False.
It is never classified as a new real ORCA execution.
"""
    initial = store.import_artifact(fixture / "geometry.xyz", "initial_geometry")
    request = Request(geometry_artifact_id=initial.id, goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    parameters = CalculationParameters(timeout_seconds=120)
    step = Step(id="producer", logical_id="producer", tool="orca.sp", parameters=parameters,
                geometry=InputRef(artifact_id=initial.id))
    plan = Plan(request_id=request.id, steps=[step], goal_map={
        "energy": OutputBinding(step_id=step.id, port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(scientific_execution=True, artifact_ids=[initial.id]))
    attempt = store.reserve_attempt(run, step, initial.id)
    workdir = store.path(attempt.directory)
    for path in fixture.iterdir():
        if path.is_file():
            shutil.copyfile(path, workdir / path.name)
    if not (workdir / "input-manifest.json").exists():
        (workdir / "input-manifest.json").write_text(json.dumps({
            "tool": "orca.sp", "parameters": parameters.model_dump(mode="json"),
            "geometry_sha256": sha256_file(workdir / "geometry.xyz"),
            "input_sha256": sha256_file(workdir / "job.inp"),
            "input_file": "job.inp", "geometry_file": "geometry.xyz",
        }), encoding="utf-8")
    parsed = read_outputs(workdir, parameters, "orca.sp", expected_orca_version="6.1.1")
    assert "energy" in parsed["qualified_outputs"], parsed["diagnostics"]
    files = {p.name: store.import_artifact(p, "raw_evidence", run_id=run.id, attempt_id=attempt.id)
             for p in workdir.iterdir() if p.is_file()}
    checks = parsed["checks"]
    numeric = parsed["qualified_outputs"]["energy"]
    result = Result(run_id=run.id, attempt_id=attempt.id, step_id=step.id, operation_status="completed",
                    checks=checks, qualified_outputs={"energy": QualifiedOutput(
                        value=numeric["value"], unit=numeric["unit"], checks=checks["energy"])},
                    artifact_ids=[a.id for a in files.values()], observations=parsed["observations"],
                    source={"input_fingerprint": attempt.input_fingerprint,
                            "geometry_artifact_id": initial.id, "conditions": parameters.model_dump(mode="json"),
                            "files": {name: {"artifact_id": a.id, "sha256": a.sha256} for name, a in files.items()}})
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id, started=False, termination_confirmed=True)
    assert run.usage.orca_starts_actual == 0
    return run, result


def test_query_without_plan_or_scientific_permission_records_verified_read_not_science(store):
    artifact = store.import_artifact(REAL_SP / "job.property.json", "property_json")
    before = store.artifact_path(artifact.id).read_bytes()
    run = query_run(store, [artifact.id])
    result = execute_call(store, run, "evidence.value", {"artifact_id": artifact.id, "path": [
        {"kind": "key", "key": "Geometries"}, {"kind": "index", "index": 0},
        {"kind": "key", "key": "Dipole_Moment"}, {"kind": "index", "index": 0},
        {"kind": "key", "key": "dipoleMagnitude"},
    ]})
    assert result.operation_status == "completed"
    assert not result.qualified_outputs
    assert result.checks["value_observation"][0].status == "passed"
    assert result.observations["value_observation"]["scientific_status"] == "unverified"
    assert store.artifact_path(artifact.id).read_bytes() == before
    assert run.usage.evidence_reads == 1 and run.usage.orca_starts_reserved == 0
    assert run.usage.orca_starts_actual == run.usage.postprocess_starts == 0
    assert store.environment_lease() is None


def test_missing_field_remains_missing_and_never_scientifically_qualified(store):
    artifact = store.import_artifact(REAL_SP / "job.property.json", "property_json")
    run = query_run(store, [artifact.id])
    result = execute_call(store, run, "evidence.value", {
        "artifact_id": artifact.id, "path": [{"kind": "key", "key": "not_present"}],
    })
    assert result.operation_status == "completed"
    assert result.observations["value_observation"]["status"] == "missing"
    assert result.checks["value_observation"][0].status == "unverified"
    assert result.qualified_outputs == {}


def test_query_cannot_read_unauthorized_artifact_and_does_not_reserve(store):
    artifact = store.import_artifact(REAL_SP / "job.property.json", "property_json")
    run = query_run(store)
    with pytest.raises(StoreError, match="outside the permission"):
        execute_call(store, run, "evidence.value", {"artifact_id": artifact.id})
    assert not run.calls and run.usage.evidence_reads == 0


@pytest.mark.parametrize("tool,parameters", [
    ("evidence.import", {"source_id": "source_water"}),
    ("analysis.energy_compare", {"goal_id": "comparison"}),
])
@pytest.mark.parametrize("writes", [False, True])
def test_writes_never_bypass_an_explicit_plan_step(store, tool, parameters, writes):
    run = query_run(store, tool=tool, source_ids=["source_water"], writes=writes)
    with pytest.raises(StoreError, match="explicit Step"):
        execute_call(store, run, tool, parameters)
    assert not run.calls and run.usage.analysis_executions == 0


def test_import_rejects_missing_write_permission_and_unauthorized_source(store):
    step = Step(id="import", logical_id="import", tool="evidence.import", parameters={"source_id": "source_water"})
    goals = [Goal(id="imported", port="imported_evidence", minimum_check_version="evidence-read-1")]
    run = query_run(store, tool=step.tool, source_ids=["source_water"], plan_step=step, goals=goals)
    with pytest.raises(StoreError, match="authorized explicit Step"):
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    unauthorized = query_run(store, tool=step.tool, writes=True, plan_step=step, goals=goals)
    with pytest.raises(StoreError, match="source is outside"):
        execute_call(store, unauthorized, step.tool, step.parameters.model_dump(), step=step)


def test_partial_import_preserves_original_unknowns_and_no_calculation(store, tmp_path):
    step = Step(id="import", logical_id="import", tool="evidence.import", parameters={"source_id": "source_water"})
    run = query_run(store, tool=step.tool, source_ids=["source_water"], writes=True, plan_step=step,
                    goals=[Goal(id="imported", port="imported_evidence", minimum_check_version="evidence-read-1")])
    store._write_json(f"runs/{run.id}/sources.json", {"source_water": {"files": [
        {"path": str(REAL_SP / "stdout.out"), "role": "stdout", "sha256": sha256_file(REAL_SP / "stdout.out")},
        {"path": str(tmp_path / "missing.json"), "role": "property_json"},
    ]}}, immutable=True)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert result.operation_status == "completed"
    data = result.observations["imported_evidence"]
    assert data["status"] == "partial" and data["missing"]
    artifact = store.load_artifact(result.artifact_ids[0])
    assert artifact.source["original_attempt"] == artifact.source["original_cost"] == "unknown"
    assert result.attempt_id is None and result.qualified_outputs == {}
    assert run.usage.orca_starts_actual == run.usage.postprocess_starts == 0


def comparison_run(store, source_run, result, *, authorized=True, allow_different=False):
    conditions = {"comparison": {"member_a": "A", "member_b": "B", "allow_different_geometries": allow_different}}
    goal = Goal(id="comparison", port="energy_difference", minimum_check_version="energy-compare-1", conditions=conditions)
    ref = EvidenceRef(run_id=source_run.id, result_id=result.id, attempt_id=result.attempt_id)
    step = Step(id="compare", logical_id="compare", tool="analysis.energy_compare",
                parameters={"goal_id": goal.id}, inputs={"A": ref, "B": ref})
    run = query_run(store, tool=step.tool, writes=True, plan_step=step, goals=[goal],
                    result_ids=[result.id] if authorized else [])
    return run, step


def test_derived_energy_uses_original_goal_and_concrete_actual_scientific_sources(store):
    source_run, source_result = archived_energy(store)
    run, step = comparison_run(store, source_run, source_result)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert result.operation_status == "completed", result.diagnostics
    output = result.qualified_outputs["energy_difference"]
    assert output.value == 0 and output.unit == "Eh"
    assert output.source["A"]["attempt_id"] == source_result.attempt_id
    assert output.source["B"]["result_id"] == source_result.id
    assert run.calls[0].consumption["A"]["rule_version"] == "orca-hf-2"
    assert run.calls[0].consumption["A"]["artifact_hashes"]
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0
    assert len(result.artifact_ids) == 1
    run.goal_status["comparison"] = "satisfied"
    store.save_run(run)
    assert build_report(store, run)["user_goal_complete"]


def test_unapproved_external_result_rejected_before_reservation(store):
    source_run, source_result = archived_energy(store)
    run, step = comparison_run(store, source_run, source_result, authorized=False)
    with pytest.raises(StoreError, match="not authorized"):
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert run.calls == [] and run.usage.analysis_executions == 0


def test_model_cannot_override_request_geometry_relation_or_tolerance_in_tool_parameters(store):
    source_run, source_result = archived_energy(store)
    run, step = comparison_run(store, source_run, source_result)
    with pytest.raises(ValueError):
        execute_call(store, run, step.tool, {"goal_id": "comparison", "allow_different_geometries": True}, step=step)
    assert not run.calls


def test_future_reference_requires_persisted_exact_step_result_and_explicit_rule(store):
    source_run, result = archived_energy(store)
    step = Step(id="analysis", logical_id="analysis", tool="analysis.energy_compare", parameters={"goal_id": "unused"},
                inputs={"A": EvidenceRef(producer_step_id="producer", port="energy")})
    good = bind_inputs(store, source_run, step, {"producer": result})
    assert good["A"]["result_id"] == result.id
    forged = result.model_copy(deep=True)
    forged.qualified_outputs["energy"].value -= 1
    with pytest.raises(StoreError, match="persisted Step"):
        bind_inputs(store, source_run, step, {"producer": forged})
    step.inputs["A"].producer_step_id = "other_step"
    with pytest.raises(StoreError, match="persisted Step"):
        bind_inputs(store, source_run, step, {"other_step": result})
    step.inputs["A"].producer_step_id = "producer"
    step.inputs["A"].rule_version = "orca-hf-1"
    with pytest.raises(StoreError, match="consumer rule"):
        bind_inputs(store, source_run, step, {"producer": result})


def test_sampling_wrapper_retains_all_five_members_and_hashes_actual_geometries(store):
    window = next(w for w in MANIFEST["windows"] if w["id"] == "stop")
    candidates, inputs, authorized_results = [], {}, []
    for candidate in window["candidates"]:
        geometry = store.import_artifact(PROJECT / candidate["path"], "sampling_candidate")
        candidates.append(SamplingCandidate(id=candidate["id"], artifact_id=geometry.id, sha256=geometry.sha256,
                          declared_r_angstrom=candidate["declared_r_angstrom"], required_initial=candidate["required_initial"]))
        if candidate["required_initial"]:
            fixture = PROJECT / "tests/fixtures/phase_b/independent" / candidate["reference_id"]
            source_run, result = archived_energy(store, fixture)
            inputs[candidate["id"]] = EvidenceRef(run_id=source_run.id, result_id=result.id, attempt_id=result.attempt_id)
            authorized_results.append(result.id)
    parameters = SamplingParameters(target_width_angstrom=window["target_width_angstrom"],
                                    energy_threshold_eh=REVIEW["sampling"]["threshold_eh"],
                                    fixed_bond_angstrom=MANIFEST["source"]["r02_angstrom"],
                                    fixed_angle_degrees=MANIFEST["source"]["angle_degrees"])
    goal = Goal(id="sampling", port="sampling", minimum_check_version="finite-sampling-1", conditions={
        "sampling": parameters.model_dump(mode="json"), "candidates": [c.model_dump(mode="json") for c in candidates],
    })
    step = Step(id="analyze", logical_id="analyze", tool="analysis.finite_sampling", parameters={"goal_id": goal.id}, inputs=inputs)
    run = query_run(store, [c.artifact_id for c in candidates], tool=step.tool, writes=True, plan_step=step,
                    goals=[goal], result_ids=authorized_results)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert result.operation_status == "completed", result.diagnostics
    data = result.observations["analysis"]
    assert len(data["members"]) == 5
    assert len(data["sampled_candidate_ids"]) == 3 and len(data["unsampled_candidate_ids"]) == 2
    assert data["goal_satisfied"] and "sampling" in result.qualified_outputs
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0
    assert "selected_action" not in data


def test_missing_frozen_analysis_conditions_leave_a_structured_failure_after_reservation(store):
    goal = Goal(id="comparison", port="energy_difference", minimum_check_version="energy-compare-1")
    step = Step(id="compare", logical_id="compare", tool="analysis.energy_compare", parameters={"goal_id": goal.id})
    run = query_run(store, tool=step.tool, writes=True, plan_step=step, goals=[goal])
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert result.operation_status == "failed"
    assert result.call_id == run.calls[0].id
    assert run.calls[0].state == "failed"
    assert run.usage.analysis_executions == 1
    assert result.id in store.load_run(run.id).result_ids
    assert not result.qualified_outputs
