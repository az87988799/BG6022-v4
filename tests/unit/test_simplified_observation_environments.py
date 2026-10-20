"""Offline synthetic archives; no real model, native process, or scientific result."""

import copy

import pytest

from orca_agent.models import Goal, InputManifest, SystemInput
from orca_agent.task_observations import assess
from tests.unit.test_general_observation_admission import general_case
from tests.unit.test_simplified_semantic_selection import intake, rebind, retained, submit
from tests.unit.test_task_analysis_traceability import declaration, source_item


def comparison_case(tmp_path, *, port="energy_difference"):
    store, run, request, goal, result = general_case(tmp_path)
    run.configuration = run.configuration.model_copy(update={"workflow_policy": "simplified-1"})
    request.condition_evidence["workflow_policy"] = {
        "version": "simplified-1", "source": "run_configuration"}
    request.conditions_source["environment"] = "default"
    goal.port, goal.quantity = port, "difference and interpretation"
    goal.original_text = "Report the difference between water and gas, with the two original sources."
    for identifier, environment in (("gas", "gas_phase"), ("water", "CPCM(water)")):
        request.goals.append(Goal(id=identifier, port="energy", quantity="electronic energy",
            evidence_contract_version="generic-observation-1",
            verification="reported_with_limitations", system_ids=["molecule"],
            original_text=f"Compute electronic energy in {environment}, using the same initial geometry.",
            conditions={"environment": environment, "geometry_relation": "fixed_initial"},
            text_evidence={"conditions": {"environment": {
                "value": environment, "source": "explicit", "text_basis": environment}}}))
    raw_directory = tmp_path / "second-native"
    raw_directory.mkdir()
    raw = raw_directory / "stdout.out"
    raw.write_text("Second synthetic native archive; never executed.\n", encoding="utf-8")
    original = run.internal_calls[0]
    artifact = store.import_artifact(raw, "raw_output", run_id=run.id,
                                    attempt_id=original.parent_attempt_id)
    run.internal_calls.append(original.model_copy(deep=True, update={
        "request_key": "water", "input_fingerprint": "e" * 64, "artifact_ids": [artifact.id],
        "actual_conditions": {**original.actual_conditions, "environment": "cpcm(water)"}}))
    redeclare(store, run, result, tmp_path, goal)
    return store, run, request, goal, result


def redeclare(store, run, result, tmp_path, goal, *, keys=("native", "water")):
    run.attempts[0].frozen_step.parameters.observation_names = [goal.port]
    declaration(store, run, result, tmp_path, [source_item(
        name=goal.port, value=1.25, unit="kJ/mol", formula="difference = water - gas",
        sources=[{"filename": "stdout.out", "internal_request_key": key} for key in keys])])


@pytest.mark.parametrize("port", ["energy_difference", "report", "comparison_data", "energy"])
def test_explicit_same_system_variants_remain_sourced_unqualified_observations(tmp_path, port):
    store, run, request, goal, result = comparison_case(tmp_path, port=port)
    before = [copy.deepcopy(item.model_dump(mode="json")) for item in (run, request, goal, result)]
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False and not result.qualified_outputs
    observation, = result.observation_records
    assert observation.validation_status == "unverified"
    assert observation.value == 1.25 and observation.unit == "kJ/mol"
    assert observation.formula == "difference = water - gas"
    assert "environment" not in observation.actual_conditions
    assert {source.actual_conditions["environment"] for source in observation.sources} == {
        "gas_phase", "cpcm(water)"}
    assert any("environment" in limitation for limitation in observation.limitations)
    assert [item.model_dump(mode="json") for item in (run, request, goal, result)] == before
    assert run.usage.model_calls == run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("change,reason", [
    ("third_environment", "native_source_environment_outside_requested_comparison"),
    ("two_gas", "comparison_requested_environment_source_missing"),
    ("one_source", "comparison_requires_multiple_native_sources"),
    ("same_archive", "comparison_requires_multiple_native_sources"),
    ("missing_explicit", "native_source_condition_missing_or_mismatch:environment"),
    ("source_goal_wrong_system", "native_source_condition_missing_or_mismatch:environment"),
    ("old_run", "native_source_condition_missing_or_mismatch:environment"),
    ("old_request", "native_source_condition_missing_or_mismatch:environment"),
    ("explicit_request", "native_source_condition_missing_or_mismatch:environment"),
    ("system_environment", "native_source_condition_missing_or_mismatch:environment"),
    ("own_environment", "native_source_condition_missing_or_mismatch:environment"),
    ("missing_dependency", "geometry_relation_initial_dependency_missing"),
    ("changed_geometry", "geometry_relation_initial_geometry_hash_changed"),
    ("optimized_source", "fixed_initial_requires_bound_single_point_output"),
    ("qualified", "generic_observation_cannot_satisfy_qualified_evidence"),
    ("minimum", "minimum_evidence_not_applicable_to_output:converged_scf@1"),
    ("unit_requirement", "unsupported_minimum_evidence:report independently checked kJ/mol"),
    ("manifest_changed_unit", "generic_observation_differs_from_archived_manifest"),
    ("manifest_changed_quantity", "generic_observation_differs_from_archived_manifest"),
    ("source_unknown", "requested_observation_missing"),
    ("request_version", "native_input_purpose_binding_missing_or_changed"),
])
def test_environment_projection_does_not_relax_other_contracts(tmp_path, change, reason):
    store, run, request, goal, result = comparison_case(tmp_path)
    second = run.internal_calls[1]
    keys = ("native", "water")
    if change == "third_environment":
        second.actual_conditions["environment"] = "CPCM(methanol)"
    elif change == "two_gas":
        second.actual_conditions["environment"] = "gas_phase"
    elif change == "one_source":
        keys = ("native",)
    elif change == "same_archive":
        second.artifact_ids = list(run.internal_calls[0].artifact_ids)
    elif change == "missing_explicit":
        request.goals[2].text_evidence["conditions"]["environment"]["source"] = "default"
    elif change == "source_goal_wrong_system":
        request.systems.append(SystemInput(id="other"))
        request.goals[2].system_ids = ["other"]
    elif change == "old_run":
        run.configuration = run.configuration.model_copy(update={"workflow_policy": None})
    elif change == "old_request":
        request.condition_evidence.pop("workflow_policy")
    elif change == "explicit_request":
        request.conditions_source["environment"] = "explicit"
    elif change == "system_environment":
        request.systems[0].conditions["environment"] = "gas_phase"
    elif change == "own_environment":
        goal.port, goal.quantity = "energy", "absolute gas energy"
        goal.conditions["environment"] = "gas_phase"
    elif change == "missing_dependency":
        second.inputs = InputManifest()
    elif change == "changed_geometry":
        second.actual_conditions["input_geometry_sha256"] = "0" * 64
    elif change == "optimized_source":
        second.actual_conditions["run_type"] = "optimization"
    elif change == "qualified":
        goal.verification = "qualified"
    elif change == "minimum":
        goal.minimum_evidence.append("converged_scf@1")
    elif change == "unit_requirement":
        goal.minimum_evidence.append("report independently checked kJ/mol")
    elif change == "source_unknown":
        second.state = "unknown"
    elif change == "request_version":
        second.actual_conditions["request_version"] += 1
    redeclare(store, run, result, tmp_path, goal, keys=keys)
    if change == "manifest_changed_unit":
        result.observation_records[0].unit = "Eh"
    elif change == "manifest_changed_quantity":
        result.observation_records[0].name = "hydration_free_energy"
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved", answer
    assert reason in answer["reasons"], answer
    assert answer["scientific_qualification"] is False and not result.qualified_outputs


@pytest.mark.parametrize("name,value", [
    ("method", "PBE0"), ("basis", "def2-SVP"), ("charge", 1), ("multiplicity", 3),
    ("electronic_state", "UHF"),
])
def test_only_environment_can_vary_not_other_scientific_conditions(tmp_path, name, value):
    store, run, request, goal, result = comparison_case(tmp_path)
    run.internal_calls[1].actual_conditions[name] = value
    redeclare(store, run, result, tmp_path, goal)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "native_source_condition_missing_or_mismatch:" + name in answer["reasons"]


def test_original_source_goal_additional_condition_is_not_lost_in_derived_report(tmp_path):
    store, run, request, goal, result = comparison_case(tmp_path)
    request.goals[2].conditions["temperature_K"] = 310.0
    answer = assess(store, run, request, goal, result)
    assert "native_condition_not_verified:temperature_K" in answer["reasons"]
    assert answer["status"] == "unresolved"


@pytest.mark.parametrize("port", ["energy_difference", "report"])
def test_retained_normalized_goal_structure_uses_original_explicit_source_conditions(tmp_path, port):
    text, proposal, path, raw = retained("T5")
    normalized_store, _, intake_run = intake(tmp_path / "intake", text)
    parameters = rebind(proposal, normalized_store.read_control(intake_run.id)["messages"][0]["id"])
    normalized = normalized_store.load_request(submit(normalized_store, intake_run, parameters))
    store, run, request, _, result = comparison_case(tmp_path / "archive", port=port)
    # Use the real normalized Goal shapes, with only the synthetic archive's
    # system identity rebound. This fixture is not an executed scientific run.
    request.goals = [item.model_copy(deep=True, update={"system_ids": ["molecule"]})
                     for item in normalized.goals if item.port != "prepared_geometry"]
    goal = next(item for item in request.goals if item.port == port)
    before = goal.model_dump(mode="json")
    redeclare(store, run, result, tmp_path / "archive", goal)
    answer = assess(store, run, request, goal, result)
    assert "native_source_condition_missing_or_mismatch:environment" not in answer["reasons"], answer
    assert not any("comparison_" in item for item in answer["reasons"]), answer
    # Original unit/geometry/user evidence obligations keep their evaluator
    # status; this change supplies no new numerical or scientific checker.
    assert goal.model_dump(mode="json") == before and path.read_bytes() == raw
    assert answer["scientific_qualification"] is False and not result.qualified_outputs
    assert [item["requested"] for item in answer["minimum_evidence"]] == goal.minimum_evidence
