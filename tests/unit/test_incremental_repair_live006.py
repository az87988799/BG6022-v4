"""Replay the final live006 rejection structurally, without execution or API calls."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orca_agent.models import Plan, Request, Result, Run
from orca_agent.planning import validate_revision
from orca_agent.proposals import ProposalError, materialize_plan

FIXTURE = Path(__file__).parents[1] / "fixtures/incremental_repair_live006.json"
SOURCE = "step_3e65727f4d5e4ad980da838453fd88e1"


def replay():
    data = json.loads(FIXTURE.read_bytes())
    steps = {step["id"]: step for step in data["prior_plan"]["steps"]}
    for call in data["run"]["calls"]:
        call["frozen_step"] = copy.deepcopy(steps[call.pop("frozen_step_id")])
        call["parameters"] = copy.deepcopy(call["frozen_step"]["parameters"])
    for attempt in data["run"]["attempts"]:
        attempt["frozen_step"] = copy.deepcopy(steps[attempt.pop("frozen_step_id")])
    request, prior, run = (model.model_validate(data[key]) for model, key in (
        (Request, "request"), (Plan, "prior_plan"), (Run, "run")))
    results = {item["id"]: Result.model_validate(item) for item in data["results"]}
    store = SimpleNamespace(load_plan=lambda _: prior, load_request=lambda _: request,
                            load_result=lambda _, identifier: results[identifier])
    return store, run, request, prior, data["parameters"]


def test_real_last_reply_replaces_failed_step_without_rewriting_attempt_or_budget():
    store, run, request, prior, parameters = replay()
    before = copy.deepcopy((run, request, prior, parameters))
    candidate = materialize_plan(store, run, parameters)
    authorized = {parameters["steps"][0]["code"]["artifact_id"]}
    validate_revision(request, prior, request, candidate, run, authorized_artifacts=authorized)
    failed = next(step for step in prior.steps if step.id == SOURCE)
    repair = next(step for step in candidate.steps if step.tool == "python.execute")
    assert failed not in candidate.steps and repair.id != failed.id
    assert repair.logical_id == failed.logical_id
    assert repair.parameters.inputs == failed.parameters.inputs
    assert repair.parameters.conditions == failed.parameters.conditions
    assert repair.depends_on == failed.depends_on
    assert all(step in candidate.steps for step in prior.steps if step.id != SOURCE)
    assert candidate.goal_map["goal_initial_structure"] == prior.goal_map["goal_initial_structure"]
    assert candidate.goal_map["goal_conformer_search"].step_id == repair.id
    assert run.attempts[0].frozen_step == failed and run.attempts[0].state == "failed"
    assert (run, request, prior, parameters) == before


def test_explicit_old_step_is_not_silently_removed_to_make_repair_valid():
    store, run, _, _, parameters = replay()
    parameters["steps"].append({"key": SOURCE})
    with pytest.raises(ValidationError, match="logical step identities must be unique"):
        materialize_plan(store, run, parameters)


def test_real_reply_full_repair_form_retains_the_same_frozen_noncode_contract():
    store, run, request, prior, parameters = replay()
    source = next(step for step in prior.steps if step.id == SOURCE)
    repair = parameters["steps"][0]
    full = source.model_dump(mode="json")
    full["key"] = repair["key"]
    full["logical_key"] = full.pop("logical_id")
    full.pop("id")
    full["parameters"]["code"] = repair["code"]
    parameters["steps"] = [full]
    before = copy.deepcopy((run, request, prior, parameters))
    candidate = materialize_plan(store, run, parameters)
    validate_revision(request, prior, request, candidate, run,
                      authorized_artifacts={repair["code"]["artifact_id"]})
    replacement = next(step for step in candidate.steps if step.tool == "python.execute")
    assert replacement.logical_id == source.logical_id and replacement.id != source.id
    assert source.id not in {step.id for step in candidate.steps}
    assert replacement.model_dump(exclude={"id", "parameters"}) == source.model_dump(
        exclude={"id", "parameters"})
    assert replacement.parameters.model_dump(exclude={"code"}) == source.parameters.model_dump(
        exclude={"code"})
    assert (run, request, prior, parameters) == before


def test_omitted_binding_to_replaced_step_requires_explicit_new_route():
    store, run, _, _, parameters = replay()
    parameters["goal_map"].pop("goal_conformer_search")
    with pytest.raises(ProposalError) as rejected:
        materialize_plan(store, run, parameters)
    assert rejected.value.detail["path"] == ["parameters", "goal_map"]
    assert rejected.value.detail["missing_goals"] == [
        {"goal_id": "goal_conformer_search", "port": "conformer_search"}]


@pytest.mark.parametrize("change", ["unknown_reference", "unknown_state", "unfinished"])
def test_repair_still_requires_the_exact_settled_failed_attempt(change):
    store, run, _, _, parameters = replay()
    if change == "unknown_reference":
        parameters["steps"][0]["repair_of"] = "unknown_step"
    elif change == "unknown_state":
        run.attempts[0].state = "unknown"
    else:
        run.attempts[0].finished_at = None
    with pytest.raises(ProposalError) as rejected:
        materialize_plan(store, run, parameters)
    assert rejected.value.detail["code"] == "invalid_code_repair_reference"
