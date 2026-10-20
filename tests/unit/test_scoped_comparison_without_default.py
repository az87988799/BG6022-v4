"""Same-system comparisons need explicit source scopes, not a fabricated default."""

import copy

import pytest

from orca_agent.task_observations import _comparison_environment_goals, assess
from tests.unit.test_simplified_observation_environments import comparison_case, redeclare


def no_global_environment(tmp_path):
    store, run, request, goal, result = comparison_case(tmp_path)
    request.conditions.pop("environment", None)
    request.conditions_source.pop("environment", None)
    request.condition_evidence.pop("request.environment", None)
    return store, run, request, goal, result


def test_absent_global_environment_keeps_each_explicit_source_scope(tmp_path):
    store, run, request, goal, result = no_global_environment(tmp_path)
    before = copy.deepcopy([item.model_dump(mode="json") for item in (run, request, result)])
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False
    assert not result.qualified_outputs
    assert {source.actual_conditions["environment"] for source in result.observation_records[0].sources} == {
        "gas_phase", "cpcm(water)"}
    assert "environment" not in result.observation_records[0].actual_conditions
    assert [item.model_dump(mode="json") for item in (run, request, result)] == before


@pytest.mark.parametrize("change", [
    "value", "null", "explicit", "unknown", "inherited", "evidence", "unscoped_evidence",
    "selection_evidence", "system_source", "system_evidence",
])
def test_an_incomplete_or_explicit_global_declaration_is_not_absence(tmp_path, change):
    _, run, request, goal, _ = no_global_environment(tmp_path)
    if change == "value":
        request.conditions["environment"] = "gas_phase"
    elif change == "null":
        request.conditions["environment"] = None
    elif change in {"explicit", "unknown", "inherited"}:
        request.conditions_source["environment"] = change
    elif change == "evidence":
        request.condition_evidence["request.environment"] = {"source": "unknown"}
    elif change == "unscoped_evidence":
        request.condition_evidence["environment"] = None
    elif change == "selection_evidence":
        request.condition_evidence["automatic_selection.environment"] = {"source": "unknown"}
    elif change == "system_source":
        request.systems[0].conditions_source["environment"] = "unknown"
    else:
        request.condition_evidence["molecule.environment"] = {"source": "unknown"}
    assert _comparison_environment_goals(run, request, goal, ["molecule"]) == {}


@pytest.mark.parametrize("change,reason", [
    ("missing_water", "comparison_requires_multiple_native_sources"),
    ("method", "native_source_condition_missing_or_mismatch:method"),
    ("geometry", "geometry_relation_initial_geometry_hash_changed"),
    ("old_request", "native_input_purpose_binding_missing_or_changed"),
])
def test_absent_default_never_waives_source_proofs(tmp_path, change, reason):
    store, run, request, goal, result = no_global_environment(tmp_path)
    second = run.internal_calls[1]
    keys = ("native", "water")
    if change == "missing_water":
        keys = ("native",)
    elif change == "method":
        second.actual_conditions["method"] = "PBE0"
    elif change == "geometry":
        second.actual_conditions["input_geometry_sha256"] = "0" * 64
    else:
        second.actual_conditions["request_version"] += 1
    redeclare(store, run, result, tmp_path, goal, keys=keys)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved", answer
    assert reason in answer["reasons"], answer
    assert answer["scientific_qualification"] is False
