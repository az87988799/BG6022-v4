"""Exact live024 rejected plans and offline graph controls; no script execution."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.models import Plan, Request, Run
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.proposals import ProposalError, materialize_plan
from tests.unit.test_proposals import single, source

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live024-plan-reference-rejections.json"


def case():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "70d71b908aa2fd1d14a09c6910d04d997352ef6662d49f82bbaabc18faa57db5")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prior = Plan.model_validate(data["plan"])
    request = Request.model_validate(data["request"])
    run = Run.model_validate(data["run_context"])
    store = SimpleNamespace(load_plan=lambda _: prior, load_request=lambda _: request)
    return data, prior, request, run, store


def test_actual_live024_missing_dependencies_identify_all_omitted_historical_steps():
    data, prior, request, run, store = case()
    proposal = data["rejections"][0]["proposal"]["parameters"]
    before = copy.deepcopy((prior, request, run, proposal))
    dependencies = proposal["steps"][0]["depends_on"]
    assert set(dependencies) == {step.id for step in prior.steps}
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "missing_step_reference"
    assert detail["path"] == ["parameters", "steps", 0, "depends_on", 0]
    assert detail["missing_step_keys"] == dependencies
    assert detail["known_history_step_keys"] == dependencies
    assert detail["retained_step_shapes"] == [{"key": key} for key in dependencies]
    assert detail["missing_references"] == [
        {"key": key, "path": ["parameters", "steps", 0, "depends_on", index]}
        for index, key in enumerate(dependencies)]
    assert "does not insert" in str(caught.value)
    assert (prior, request, run, proposal) == before
    assert not run.attempts and not run.usage.orca_starts_reserved


@pytest.mark.parametrize("rejection_index", [1, 2])
def test_actual_live024_retained_prepare_extras_are_located_without_changing_candidate(rejection_index):
    data, prior, request, run, store = case()
    proposal = data["rejections"][rejection_index]["proposal"]["parameters"]
    before = copy.deepcopy((prior, request, run, proposal))
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "retained_step_has_fields"
    assert detail["path"] == ["parameters", "steps", 1]
    assert detail["step_key"] == prior.steps[1].id
    assert detail["unexpected_fields"] == ["depends_on"]
    assert detail["retained_step_shape"] == {"key": prior.steps[1].id}
    assert "every frozen Step" in str(caught.value)
    assert (prior, request, run, proposal) == before
    assert not run.attempts and not run.usage.orca_starts_reserved


def test_synthetic_all_three_retained_shapes_validate_without_execution_or_source_changes():
    data, prior, request, run, store = case()
    proposal = copy.deepcopy(data["rejections"][-1]["proposal"]["parameters"])
    proposal["steps"][:3] = [{"key": step.id} for step in prior.steps]
    before = copy.deepcopy((prior, request, run, proposal))
    candidate = materialize_plan(store, run, proposal)
    for old, retained in zip(prior.steps, candidate.steps[:3], strict=True):
        assert retained == old and retained is not old
    parameters = proposal["steps"][-1]["parameters"]
    own_artifacts = [parameters["code"]["artifact_id"],
                     *(entry["artifact_id"] for entry in parameters["inputs"]["entries"])]
    validate_revision(request, prior, request, candidate, run, authorized_artifacts=own_artifacts)
    assert (prior, request, run, proposal) == before
    assert not run.attempts and not run.usage.orca_starts_reserved
    # These are explicit fixture references from completed writer/prepare Calls,
    # not runtime authorization or a real model correction. No Plan is activated.


def test_stripping_only_reported_extras_does_not_bypass_other_frozen_step_checks():
    data, prior, request, run, store = case()
    proposal = copy.deepcopy(data["rejections"][-1]["proposal"]["parameters"])
    proposal["steps"][1:3] = [{"key": step.id} for step in prior.steps[1:3]]
    before = copy.deepcopy((prior, request, run, proposal))
    candidate = materialize_plan(store, run, proposal)
    assert candidate.steps[0].system_id != prior.steps[0].system_id
    with pytest.raises(PlanningError, match="reserved Step inputs and parameters are immutable"):
        validate_revision(request, prior, request, candidate, run)
    assert (prior, request, run, proposal) == before


def test_unknown_retained_key_is_distinct_from_known_key_with_extra_fields(tmp_path):
    store, run, _, _, _ = source(tmp_path, initial=True)
    proposal = {"steps": [{"key": "missing"}],
                "goal_map": {"energy": {"step_key": "missing", "port": "energy"}}}
    before = copy.deepcopy((run, proposal))
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["code"] == "unknown_retained_step"
    assert caught.value.detail["path"] == ["parameters", "steps", 0, "key"]
    assert caught.value.detail["step_key"] == "missing"
    assert (run, proposal) == before
    assert store.load_plan(run).steps[0].id == "old_sp"


def test_retained_explicit_null_and_empty_fields_are_reported_not_silently_removed(tmp_path):
    store, run, _, _, _ = source(tmp_path, initial=True)
    proposal = {"steps": [{"key": "old_sp", "tool": None, "depends_on": [], "parameters": {}}],
                "goal_map": {"energy": {"step_key": "old_sp", "port": "energy"}}}
    before = copy.deepcopy(proposal)
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["code"] == "retained_step_has_fields"
    assert caught.value.detail["unexpected_fields"] == ["depends_on", "parameters", "tool"]
    assert caught.value.detail["retained_step_shape"] == {"key": "old_sp"}
    assert proposal == before


@pytest.mark.parametrize("field_path", [
    ["geometry"], ["inputs", "geometry"], ["inputs", "raw"],
])
@pytest.mark.parametrize("producer_key", ["missing", {"private": "untrusted-value"}])
def test_producer_reference_feedback_keeps_original_field_path_and_input_values(tmp_path, field_path, producer_key):
    store, run, _, _, _ = source(tmp_path)
    proposal = single()
    field = proposal["steps"][0]
    for part in field_path[:-1]:
        field = field.setdefault(part, {})
    field[field_path[-1]] = {"producer_key": producer_key, "port": "optimized_geometry"}
    before = copy.deepcopy((run, proposal))
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["path"] == ["parameters", "steps", 0, *field_path, "producer_key"]
    if isinstance(producer_key, str):
        assert detail["code"] == "missing_step_reference"
        assert detail["missing_step_keys"] == ["missing"]
        assert detail["known_history_step_keys"] == [] and detail["retained_step_shapes"] == []
    else:
        assert detail["code"] == "invalid_step_reference"
        assert "untrusted-value" not in str(detail)
    assert (run, proposal) == before


def test_missing_dependencies_distinguish_historical_and_unknown_keys(tmp_path):
    store, run, _, _, _ = source(tmp_path, initial=True)
    proposal = single()
    proposal["steps"][0]["depends_on"] = ["old_sp", "missing", "old_sp"]
    before = copy.deepcopy(proposal)
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["missing_step_keys"] == ["old_sp", "missing"]
    assert detail["known_history_step_keys"] == ["old_sp"]
    assert detail["retained_step_shapes"] == [{"key": "old_sp"}]
    assert [entry["path"][-1] for entry in detail["missing_references"]] == [0, 1, 2]
    assert proposal == before


def test_unexecuted_step_still_accepts_explicit_authorized_parameter_change(tmp_path):
    store, run, request, _, prior = source(tmp_path, initial=True)
    proposal = {"steps": [{"key": "old_sp", "tool": "orca.sp", "parameters": {"scf_maxiter": 100}}],
                "goal_map": {"energy": {"step_key": "old_sp", "port": "energy"}}}
    before = copy.deepcopy((run, prior, proposal))
    candidate = materialize_plan(store, run, proposal)
    assert candidate.steps[0].parameters.scf_maxiter == 100
    assert candidate.steps[0].logical_id == prior.steps[0].logical_id
    validate_revision(request, prior, request, candidate, run)
    assert (run, prior, proposal) == before
