"""Synthetic archived evidence tests; no scientific execution is simulated."""

import json

import pytest

from orca_agent.models import (
    Attempt,
    InputManifest,
    InputManifestEntry,
    NativeInputPolicy,
    Result,
    Step,
    SystemInput,
    utc_now,
)
from orca_agent.task_observations import assess, collect
from tests.unit.test_task_analysis_traceability import declaration, source_item
from tests.unit.test_task_observation_conditions import archived_case


def general_case(tmp_path, **kwargs):
    store, run, request, goal, result = archived_case(tmp_path, **kwargs)
    execution = run.configuration.execution.model_copy(update={
        "native_input_policy": NativeInputPolicy(profile="managed-native-1")})
    run.configuration = run.configuration.model_copy(update={"execution": execution})
    for receipt in run.internal_calls:
        receipt.conditions["admission_version"] = "native-orca-input-3"
    return store, run, request, goal, result


def recollect(store, run, result):
    attempt = run.attempts[0]
    result.observation_records = collect(store, run, attempt.frozen_step, attempt, result.source["files"])


@pytest.mark.parametrize("different", ["environment", "method", "basis", "charge"])
def test_requested_system_conditions_are_checked_per_source_in_comparisons(tmp_path, different):
    store, run, request, goal, result = general_case(tmp_path)
    changed = {"environment": "CPCM(water)", "method": "CCSD(T)", "basis": "def2-QZVP", "charge": 1}[different]
    second = request.systems[0].model_copy(deep=True, update={"id": "second"})
    second.conditions[different] = changed
    request.systems.append(second)
    goal.system_ids.append(second.id)
    if different == "basis":
        goal.conditions.pop("basis")  # A fixed Goal basis would remain mandatory for both sources.
    original = run.internal_calls[0]
    actual = {**original.actual_conditions, "system_id": "second", different: changed}
    run.internal_calls.append(original.model_copy(deep=True, update={
        "request_key": "second", "input_fingerprint": "f" * 64, "actual_conditions": actual}))
    item = source_item(formula="difference = second - first")
    item["sources"].append({"filename": "stdout.out", "internal_request_key": "second"})
    declaration(store, run, result, tmp_path, [item])
    observation, = result.observation_records
    assert observation.validation_status == "unverified"
    assert observation.sources[1].actual_conditions[different] == changed
    assert different not in observation.actual_conditions
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False and not result.qualified_outputs


@pytest.mark.parametrize("different", ["method", "environment"])
def test_undeclared_same_system_condition_changes_do_not_satisfy_goal(tmp_path, different):
    store, run, request, goal, result = general_case(tmp_path)
    actual = {**run.internal_calls[0].actual_conditions,
              different: "PBE0" if different == "method" else "CPCM(water)"}
    run.internal_calls.append(run.internal_calls[0].model_copy(deep=True, update={
        "request_key": "other", "input_fingerprint": "f" * 64, "actual_conditions": actual}))
    item = source_item()
    item["sources"].append({"filename": "stdout.out", "internal_request_key": "other"})
    declaration(store, run, result, tmp_path, [item])
    assert result.observation_records[0].validation_status == "unverified"
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "native_source_condition_missing_or_mismatch:" + different in answer["reasons"]


def test_different_input_spelling_or_execution_stage_is_not_itself_a_scientific_conflict(tmp_path):
    store, run, request, goal, result = general_case(tmp_path)
    original = run.internal_calls[0]
    actual = {**original.actual_conditions, "run_type": "analytic_frequencies",
              "input_keywords": ["HF", "Freq"], "input_blocks": {"scf": {"maxiter": 50}}}
    run.internal_calls.append(original.model_copy(deep=True, update={
        "request_key": "frequency", "input_fingerprint": "f" * 64, "actual_conditions": actual}))
    item = source_item()
    item["sources"].append({"filename": "stdout.out", "internal_request_key": "frequency"})
    declaration(store, run, result, tmp_path, [item])
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert "run_type" not in result.observation_records[0].actual_conditions


def test_all_requested_systems_need_bound_sources(tmp_path):
    store, run, request, goal, result = general_case(tmp_path)
    request.systems.append(SystemInput(id="missing"))
    goal.system_ids.append("missing")
    answer = assess(store, run, request, goal, result)
    assert "generic_observation_requested_system_source_missing" in answer["reasons"]


def test_known_native_source_cannot_supply_conditions_for_an_unverified_import(tmp_path):
    store, run, request, goal, result = general_case(tmp_path)
    path = tmp_path / "other.out"
    path.write_text("An existing calculation with unknown conditions.\n", encoding="utf-8")
    artifact = store.import_artifact(path, "raw_output")
    run.permission.artifact_ids.append(artifact.id)
    run.attempts[0].frozen_step.parameters.inputs = InputManifest(entries=[InputManifestEntry(
        artifact_id=artifact.id, sha256=artifact.sha256, target_path="other.out", purpose="comparison source")])
    item = source_item(formula="first - second")
    item["sources"].append({"filename": "other.out"})
    declaration(store, run, result, tmp_path, [item])
    answer = assess(store, run, request, goal, result)
    assert "mixed_input_source_conditions_unverified" in answer["reasons"]
    assert answer["status"] == "unresolved"


@pytest.mark.parametrize("run_type,relation,final", [
    ("optimization", "optimized", True),
    ("analytic_frequencies", "fixed_initial", False),
    ("numerical_frequencies", "fixed_initial", False),
])
def test_independent_native_jobs_keep_explicit_geometry_contract(tmp_path, run_type, relation, final):
    store, run, request, goal, result = general_case(tmp_path, run_type=run_type, final_geometry=final)
    goal.conditions["geometry_relation"] = relation
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False


@pytest.mark.parametrize("name,value", [("temperature_K", 298.15), ("standard_state", "1 bar")])
def test_additional_conditions_require_matching_trusted_receipt_values(tmp_path, name, value):
    store, run, request, goal, result = general_case(tmp_path)
    goal.conditions[name] = value
    assert "native_condition_not_verified:" + name in assess(store, run, request, goal, result)["reasons"]
    run.internal_calls[0].actual_conditions[name] = value
    recollect(store, run, result)
    assert assess(store, run, request, goal, result)["status"] == "passed"
    run.internal_calls[0].actual_conditions[name] = 500.0 if name == "temperature_K" else "1 mol/L"
    recollect(store, run, result)
    assert "native_source_condition_missing_or_mismatch:" + name in assess(
        store, run, request, goal, result)["reasons"]


def imported_case(tmp_path):
    store, run, request, goal, result = general_case(tmp_path)
    run.internal_calls.clear()
    request.method = request.basis = request.charge = request.multiplicity = None
    request.conditions = request.conditions_source = {}
    goal.conditions = {}
    source = tmp_path / "existing.out"
    source.write_text("Existing user evidence, values and conditions not independently verified.\n", encoding="utf-8")
    artifact = store.import_artifact(source, "raw_output")
    run.permission.artifact_ids.append(artifact.id)
    run.permission.scientific_execution = False
    step = run.attempts[0].frozen_step
    step.parameters.inputs = InputManifest(entries=[InputManifestEntry(artifact_id=artifact.id,
        sha256=artifact.sha256, target_path="existing.out", purpose="existing evidence analysis")])
    declaration(store, run, result, tmp_path, [source_item(
        sources=[{"filename": "existing.out", "location": "whole supplied file"}])])
    return store, run, request, goal, result, artifact


def test_registered_input_analysis_needs_no_new_scientific_execution(tmp_path):
    store, run, request, goal, result, artifact = imported_case(tmp_path)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert not run.internal_calls and run.usage.orca_starts_actual == 0
    assert not run.permission.scientific_execution
    source, = result.observation_records[0].sources
    assert (source.artifact_id, source.sha256) == (artifact.id, artifact.sha256)
    assert source.actual_conditions["origin"] == "registered_task_input"
    assert answer["current_use"]["source_conditions"] == {}
    assert answer["scientific_qualification"] is False


@pytest.mark.parametrize("change", ["physical_condition", "minimum_evidence", "unauthorized", "hash", "no_input"])
def test_input_analysis_cannot_manufacture_scientific_evidence_or_input_provenance(tmp_path, change):
    store, run, request, goal, result, artifact = imported_case(tmp_path)
    if change == "physical_condition":
        goal.conditions["method"] = "CCSD(T)"
    elif change == "minimum_evidence":
        goal.minimum_evidence.append("converged_scf@1")
    elif change == "unauthorized":
        run.permission.artifact_ids.remove(artifact.id)
    elif change == "hash":
        store.artifact_path(artifact.id).write_text("changed", encoding="utf-8")
    else:
        run.attempts[0].frozen_step.parameters.inputs = InputManifest()
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved", answer
    assert answer["scientific_qualification"] is False


def test_rewritten_task_copy_cannot_masquerade_as_registered_original(tmp_path):
    store, run, request, goal, result, _ = imported_case(tmp_path)
    path = tmp_path / "edited.out"
    path.write_text("rewritten values", encoding="utf-8")
    artifact = store.import_artifact(path, "task_output", run_id=run.id, attempt_id=run.attempts[0].id)
    result.source["files"]["task/existing.out"] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"


def direct_source_case(tmp_path):
    store, run, request, goal, result = general_case(tmp_path)
    original = run.internal_calls.pop()
    path = tmp_path / "direct.inp"
    path.write_text("# synthetic archived native input; never executed\n", encoding="utf-8")
    code = store.import_artifact(path, "orca_input")
    direct = Step(id="direct", logical_id="direct", tool="orca.run", system_id="molecule",
        parameters={"input": {"artifact_id": code.id, "sha256": code.sha256, "language": "orca"},
                    "inputs": original.inputs.model_dump()})
    binding = {**original.conditions["input_binding"], "step_id": direct.id}
    workload = {"admission_version": "native-orca-input-3", "input_binding": binding,
                "input_sha256": code.sha256, "native_input_policy": {"profile": "managed-native-1"},
                "execution_impact": {"internal_work_units": 1}}
    parent = Attempt(step_id=direct.id, logical_id=direct.logical_id, number=1, tool="orca.run",
        state="completed", execution_kind="orca", input_fingerprint="e" * 64,
        directory="synthetic-direct", request_version=request.version, frozen_step=direct,
        finished_at=utc_now(), native_workload=workload)
    run.attempts.append(parent)
    path = tmp_path / "direct.out"
    path.write_text("Synthetic archived direct output.\n", encoding="utf-8")
    raw = store.import_artifact(path, "raw_output", run_id=run.id, attempt_id=parent.id)
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps({**workload, "actual_conditions": original.actual_conditions}), encoding="utf-8")
    prepared = store.import_artifact(path, "execution_record", run_id=run.id, attempt_id=parent.id)
    source = Result(run_id=run.id, step_id=direct.id, attempt_id=parent.id,
        operation_status="completed", observations={"evidence_collection_complete": True},
        artifact_ids=[raw.id, prepared.id], source={"files": {
            "prepared.json": {"artifact_id": prepared.id, "sha256": prepared.sha256},
            "task/stdout.out": {"artifact_id": raw.id, "sha256": raw.sha256}}})
    parent.result_id = source.id
    run.result_ids.append(source.id)
    # This is an explicitly synthetic archive fixture, not a launch or successful Run.
    store._write_json(f"runs/{run.id}/results/{source.id}.json", source.model_dump(mode="json"), immutable=True)
    run.permission.artifact_ids.append(raw.id)
    run.attempts[0].frozen_step.parameters.inputs = InputManifest(entries=[InputManifestEntry(
        artifact_id=raw.id, sha256=raw.sha256, target_path="direct.out", purpose="previous direct ORCA evidence")])
    declaration(store, run, result, tmp_path, [source_item(sources=[{"filename": "direct.out"}])])
    return store, run, request, goal, result, parent, prepared


def test_completed_direct_orca_evidence_can_be_analyzed_without_an_sdk_call(tmp_path):
    store, run, request, goal, result, parent, _ = direct_source_case(tmp_path)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert not run.internal_calls and run.usage.orca_starts_actual == 0
    source, = result.observation_records[0].sources
    assert source.attempt_id == parent.id
    assert source.actual_conditions["native_attempt_id"] == parent.id
    assert source.actual_conditions["method"] == "HF"
    assert answer["scientific_qualification"] is False


@pytest.mark.parametrize("change", ["unfinished", "workload", "archive_hash", "request_version"])
def test_direct_source_requires_exact_completed_admission_and_original_conditions(tmp_path, change):
    store, run, request, goal, result, parent, prepared = direct_source_case(tmp_path)
    if change == "unfinished":
        parent.state = "unknown"
    elif change == "workload":
        parent.native_workload["input_sha256"] = "0" * 64
    elif change == "archive_hash":
        store.artifact_path(prepared.id).write_text("{}", encoding="utf-8")
    else:
        parent.request_version += 1
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved", answer
