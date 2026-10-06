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
    SystemInput,
)
from orca_agent.orca.adapter import read_outputs
from orca_agent.report import build_report
from orca_agent.store import Store, StoreError, sha256_file
from orca_agent.tools.analysis import SamplingCandidate, SamplingParameters
from orca_agent.tools.dispatch import _members, bind_inputs, execute_call

PROJECT = Path(__file__).resolve().parents[2]
REAL_SP = PROJECT / "tests/fixtures/phase_a/real_water_sp"
MANIFEST = json.loads((PROJECT / "tests/fixtures/phase_b/sampling-candidates.json").read_text())
REVIEW = json.loads((PROJECT / "docs/acceptance/phase-b/reference-review.json").read_text())


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data", environment_root=tmp_path / "environment")


def query_run(store, artifact_ids=(), *, tool="evidence.value", source_ids=(), writes=False,
              plan_step=None, goals=None, result_ids=(), systems=()):
    request = Request(goals=goals or [Goal(id="query", port="value_observation", minimum_check_version="evidence-read-1")],
                      systems=list(systems))
    plan = (Plan(request_id=request.id, steps=[plan_step], goal_map={
        goal.id: OutputBinding(step_id=plan_step.id, port=goal.port) for goal in request.goals})
            if plan_step else None)
    return store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=False, allowed_tools=[tool], artifact_ids=list(artifact_ids),
        source_ids=list(source_ids), result_ids=list(result_ids), artifact_writes=writes,
    ), BudgetLimits(orca_starts=0, evidence_reads=8, analysis_executions=4))


def archived_energy(store, fixture=REAL_SP, *, expect_qualified=True):
    """Recheck archived real files through the one production parser/checker.

Attempt reservation is an offline lifecycle fixture, settled with started=False.
It is never classified as a new real ORCA execution.
"""
    initial = store.import_artifact(fixture / "geometry.xyz", "initial_geometry")
    request = Request(geometry_artifact_id=initial.id, goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    parameters = (CalculationParameters.model_validate(json.loads(
        (fixture / "input-manifest.json").read_text())["parameters"])
        if (fixture / "input-manifest.json").exists() else CalculationParameters(timeout_seconds=120))
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
    assert ("energy" in parsed["qualified_outputs"]) == expect_qualified, parsed["diagnostics"]
    files = {p.name: store.import_artifact(p, "raw_evidence", run_id=run.id, attempt_id=attempt.id)
             for p in workdir.iterdir() if p.is_file()}
    checks = parsed["checks"]
    numeric = parsed["qualified_outputs"].get("energy")
    qualified = ({"energy": QualifiedOutput(value=numeric["value"], unit=numeric["unit"], checks=checks["energy"])}
                 if numeric else {})
    result = Result(run_id=run.id, attempt_id=attempt.id, step_id=step.id,
                    operation_status="completed" if expect_qualified else "failed",
                    checks=checks, qualified_outputs=qualified,
                    artifact_ids=[a.id for a in files.values()], observations=parsed["observations"],
                    source={"input_fingerprint": attempt.input_fingerprint,
                            "geometry_artifact_id": initial.id, "conditions": parameters.model_dump(mode="json"),
                            "files": {name: {"artifact_id": a.id, "sha256": a.sha256} for name, a in files.items()}})
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state=result.operation_status, result_id=result.id, started=False, termination_confirmed=True)
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


def comparison_run(store, source_run, result, *, authorized=True, allow_different=False, members=None,
                   include_member_table_goal=False, systems=()):
    conditions = {"comparison": {"member_a": "A", "member_b": "B", "allow_different_geometries": allow_different}}
    if members is not None:
        conditions["members"] = members
    goal = Goal(id="comparison", port="energy_difference", minimum_check_version="energy-compare-1", conditions=conditions)
    ref = EvidenceRef(run_id=source_run.id, result_id=result.id, attempt_id=result.attempt_id)
    step = Step(id="compare", logical_id="compare", tool="analysis.energy_compare",
                parameters={"goal_id": goal.id}, inputs={"A": ref, "B": ref})
    goals = [goal]
    if include_member_table_goal:
        goals.append(Goal(id="table", port="member_table", minimum_check_version="energy-compare-1",
                          conditions={"analysis_goal_id": goal.id}))
    run = query_run(store, tool=step.tool, writes=True, plan_step=step, goals=goals,
                    result_ids=[result.id] if authorized else [], systems=systems)
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


def test_member_table_is_a_separately_bound_goal_with_the_same_checked_analysis(store):
    source_run, source_result = archived_energy(store)
    run, step = comparison_run(store, source_run, source_result, include_member_table_goal=True)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    table = result.qualified_outputs["member_table"]
    assert table.value is None and table.unit is None
    assert table.checks == result.qualified_outputs["energy_difference"].checks == result.checks["member_table"]
    assert all(c.status == "passed" and c.rule_version == "energy-compare-1" for c in table.checks)
    assert table.source["analysis_goal_id"] == "comparison"
    artifact = store.load_artifact(table.artifact_id)
    assert artifact.id in result.artifact_ids and artifact.sha256 == table.source["sha256"]
    archived = json.loads(store.artifact_path(artifact.id).read_text(encoding="utf-8"))
    assert archived["members"] == table.source["members"]
    for member in table.source["members"]:
        assert member["required"] and member["status"] == "qualified"
        assert member["source"]["result_id"] == source_result.id
        assert member["source"]["attempt_id"] == source_result.attempt_id
        assert member["source"]["artifact_hashes"]
    run.goal_status.update(comparison="satisfied", table="satisfied")
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


def test_unqualified_bound_members_retain_exact_provenance_without_numeric_fallback(store):
    fixture = PROJECT / "tests/fixtures/phase_a/real_water_scf_limit"
    source_run, failed = archived_energy(store, fixture, expect_qualified=False)
    run, step = comparison_run(store, source_run, failed)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step, decision_id="decision_partial")
    assert result.operation_status == "completed"
    assert not result.qualified_outputs
    summary = result.observations["analysis"]
    assert summary["reason"] == "missing_required_member"
    assert len(summary["members"]) == 2
    for member in summary["members"]:
        assert member["required"] and member["status"] == "missing"
        assert member["energy_eh"] is None
        assert member["missing_reason"] == "source_operation_failed"
        assert member["source"]["result_id"] == failed.id
        assert member["source"]["attempt_id"] == failed.attempt_id
        assert member["source"]["artifact_hashes"]
    assert run.calls[0].consumption["_decision_id"] == "decision_partial"
    assert {m["member_id"] for m in summary["members"]} == {"A", "B"}


def test_unqualified_source_still_requires_permission_and_unchanged_hash(store):
    fixture = PROJECT / "tests/fixtures/phase_a/real_water_scf_limit"
    source_run, failed = archived_energy(store, fixture, expect_qualified=False)
    denied, denied_step = comparison_run(store, source_run, failed, authorized=False)
    with pytest.raises(StoreError, match="not authorized"):
        execute_call(store, denied, denied_step.tool, denied_step.parameters.model_dump(), step=denied_step)
    run, step = comparison_run(store, source_run, failed)
    source_file = store.artifact_path(failed.artifact_ids[0])
    source_file.write_bytes(b"changed")
    with pytest.raises(StoreError, match="hash changed"):
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert not run.calls


@pytest.mark.parametrize("required", [True, False])
def test_request_additional_members_keep_frozen_required_optional_semantics(store, required):
    source_run, energy = archived_energy(store)
    run, step = comparison_run(store, source_run, energy, members=[{"id": "C", "required": required}])
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    summary = result.observations["analysis"]
    assert result.operation_status == "completed"
    assert bool(result.qualified_outputs) is not required
    assert ("member_table" in result.qualified_outputs) is not required
    assert len(summary["members"]) == 3
    extra = next(member for member in summary["members"] if member["member_id"] == "C")
    assert extra["required"] is required and extra["energy_eh"] is None


def test_decision_id_survives_query_result_publication_crash_and_recovery_without_second_read(store):
    artifact = store.import_artifact(REAL_SP / "job.property.json", "property_json")
    run = query_run(store, [artifact.id])

    def crash(point):
        if point == "after_result_saved":
            raise RuntimeError("offline crash injection")

    with pytest.raises(RuntimeError, match="crash injection"):
        execute_call(store, run, "evidence.value", {"artifact_id": artifact.id, "path": [
            {"kind": "key", "key": "Geometries"}, {"kind": "index", "index": 0},
            {"kind": "key", "key": "Dipole_Moment"},
        ]}, decision_id="decision_query", fault=crash)
    current = store.load_run(run.id)
    assert current.calls[0].consumption["_decision_id"] == "decision_query"
    assert store.recover_calls(current)
    for _ in range(2):
        current = store.load_run(run.id)
        assert store.recover_calls(current)
        assert len(current.calls) == len(current.result_ids) == current.usage.evidence_reads == 1
        assert current.calls[0].consumption["_decision_id"] == "decision_query"


def test_consumption_result_cannot_change_between_reservation_and_partial_analysis(store):
    fixture = PROJECT / "tests/fixtures/phase_a/real_water_scf_limit"
    source_run, failed = archived_energy(store, fixture, expect_qualified=False)
    run, step = comparison_run(store, source_run, failed)
    consumption = bind_inputs(store, run, step, {})
    call = store.reserve_call(run, step.tool, step.parameters.model_dump(), step, consumption=consumption)
    path = store.path(f"runs/{source_run.id}/results/{failed.id}.json")
    original = json.loads(path.read_text())
    original["operation_status"] = "completed"
    path.write_text(json.dumps(original), encoding="utf-8")
    with pytest.raises(StoreError, match="Result changed"):
        _members(store, call, ["A", "B"])


def test_input_member_cannot_spoof_decision_metadata(store):
    source_run, energy = archived_energy(store)
    run, step = comparison_run(store, source_run, energy)
    step.inputs["_decision_id"] = step.inputs["A"]
    with pytest.raises(StoreError, match="reserved metadata"):
        bind_inputs(store, run, step, {})


@pytest.mark.parametrize("change", ["step", "attempt", "input", "geometry", "file_hash", "artifact_owner"])
def test_unqualified_source_rechecks_historical_identity_and_manifest_before_reservation(store, change):
    fixture = PROJECT / "tests/fixtures/phase_a/real_water_scf_limit"
    source_run, failed = archived_energy(store, fixture, expect_qualified=False)
    run, step = comparison_run(store, source_run, failed)
    path = store.path(f"runs/{source_run.id}/results/{failed.id}.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    if change == "step":
        data["step_id"] = "other_step"
    elif change == "attempt":
        data["attempt_id"] = "missing_attempt"
    elif change == "input":
        data["source"]["input_fingerprint"] = "b" * 64
    elif change == "geometry":
        data["source"]["geometry_artifact_id"] = "other_geometry"
    elif change == "file_hash":
        next(iter(data["source"]["files"].values()))["sha256"] = "b" * 64
    else:
        artifact_path = store.path(f"artifacts/{failed.artifact_ids[0]}/artifact.json")
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        artifact["attempt_id"] = "another_attempt"
        artifact_path.write_text(json.dumps(artifact), encoding="utf-8")
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(StoreError, match="provenance"):
        execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert not run.calls and run.usage.analysis_executions == 0


def test_unqualified_source_attempt_provenance_rechecked_after_call_reservation(store):
    fixture = PROJECT / "tests/fixtures/phase_a/real_water_scf_limit"
    source_run, failed = archived_energy(store, fixture, expect_qualified=False)
    run, step = comparison_run(store, source_run, failed)
    consumption = bind_inputs(store, run, step, {})
    call = store.reserve_call(run, step.tool, step.parameters.model_dump(), step, consumption=consumption)
    source_run.attempts[0].input_fingerprint = "b" * 64
    store._write_json(f"runs/{source_run.id}/run.json", source_run)
    with pytest.raises(StoreError, match="provenance"):
        _members(store, call, ["A", "B"])


@pytest.mark.parametrize("conditions,field", [({"basis": "6-31G"}, "basis"),
                                             ({"multiplicity": None}, "multiplicity"),
                                             ({"system": "CH4"}, "system")])
def test_compatible_actual_energies_cannot_satisfy_different_or_missing_requested_operand(store, conditions, field):
    source_run, energy = archived_energy(store)
    run, step = comparison_run(store, source_run, energy, systems=[
        SystemInput(id="A"), SystemInput(id="B", conditions=conditions)])
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert result.operation_status == "completed" and not result.qualified_outputs
    rows = result.observations["analysis"]["members"]
    assert rows[0]["status"] == "qualified" and rows[1]["status"] == "missing"
    assert rows[1]["energy_eh"] is None
    assert field in rows[1]["missing_reason"]
    assert rows[1]["source"]["result_id"] == energy.id
    assert rows[1]["source"]["artifact_hashes"]
    assert "energy_eh" not in rows[1]["source"]


def test_only_explicit_rhf_method_alias_matches_qualified_hf_operand(store):
    source_run, energy = archived_energy(store)
    run, step = comparison_run(store, source_run, energy, systems=[
        SystemInput(id="A", conditions={"method": "RHF"}),
        SystemInput(id="B", conditions={"method": "HF"})])
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert "energy_difference" in result.qualified_outputs and "member_table" in result.qualified_outputs
