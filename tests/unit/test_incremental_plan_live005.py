"""Offline structural replay of live005; no generated code or API is executed."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.models import Plan, Request, Result, Run
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.proposals import ProposalError, materialize_plan

FIXTURE = Path(__file__).parents[1] / "fixtures/incremental_plan_live005.json"


def replay(*, current_candidate=True):
    data = json.loads(FIXTURE.read_bytes())
    request, prior, run = (model.model_validate(data[key]) for model, key in (
        (Request, "request"), (Plan, "prior_plan"), (Run, "run")))
    results = {item["id"]: Result.model_validate(item) for item in data["results"]}
    store = SimpleNamespace(load_plan=lambda _: prior, load_request=lambda _: request,
                            load_result=lambda _, identifier: results[identifier])
    authorized = {reference["artifact_id"] for step in data["parameters"]["steps"]
                  for reference in ([step.get("parameters", {}).get("code", {})]
                      + step.get("parameters", {}).get("inputs", {}).get("entries", []))
                  if "artifact_id" in reference}
    parameters = copy.deepcopy(data["parameters"])
    if current_candidate:
        # Derive a current proposal; the frozen response and historical Steps
        # stay unchanged. This archived script calls the managed run_orca SDK.
        script = parameters["steps"][-1]
        assert script["key"] == "execute_conformer_search" and script["tool"] == "python.execute"
        script["parameters"]["sdk_orca"] = True
    return store, run, request, prior, parameters, authorized


def validate(store, run, request, prior, parameters, authorized):
    plan = materialize_plan(store, run, parameters)
    validate_revision(request, prior, request, plan, run, authorized_artifacts=authorized)
    return plan


def test_derived_last_reply_reuses_settled_history_and_equivalent_goal_route():
    store, run, request, prior, parameters, authorized = replay()
    before = run.model_dump(mode="json"), request.model_dump(mode="json"), copy.deepcopy(parameters)
    plan = validate(store, run, request, prior, parameters, authorized)
    initial = plan.goal_map["goal_initial_geometry"]
    assert initial.step_id == "step_531906ff5f5d42dab59c8e08db2527df" and initial.evidence is None
    for call in run.calls:
        assert next(step for step in plan.steps if step.id == call.step_id) == call.frozen_step
    assert plan.id == prior.id and plan.version == prior.version + 1
    assert (run.model_dump(mode="json"), request.model_dump(mode="json"), parameters) == before


def test_raw_historical_reply_requires_explicit_sdk_mode_without_rewriting_history():
    fixture_before = FIXTURE.read_bytes()
    store, run, request, prior, parameters, authorized = replay(current_candidate=False)
    before = (run.model_dump(mode="json"), request.model_dump(mode="json"),
              prior.model_dump(mode="json"), copy.deepcopy(parameters))
    assert "sdk_orca" not in parameters["steps"][-1]["parameters"]
    with pytest.raises(ProposalError) as caught:
        validate(store, run, request, prior, parameters, authorized)
    assert caught.value.detail["code"] == "missing_sdk_orca_declaration"
    assert caught.value.detail["path"] == [
        "parameters", "steps", len(parameters["steps"]) - 1, "parameters", "sdk_orca"]
    assert (run.model_dump(mode="json"), request.model_dump(mode="json"),
            prior.model_dump(mode="json"), parameters) == before
    assert FIXTURE.read_bytes() == fixture_before


def test_incremental_reply_can_omit_settled_step_closure_and_untouched_goal_binding():
    store, run, request, prior, parameters, authorized = replay()
    parameters["steps"] = [parameters["steps"][-1]]
    parameters["goal_map"].pop("goal_initial_geometry")
    plan = validate(store, run, request, prior, parameters, authorized)
    assert plan.goal_map["goal_initial_geometry"] == prior.goal_map["goal_initial_geometry"]
    assert all(call.frozen_step in plan.steps for call in run.calls)
    assert len(plan.steps) == len(run.calls) + 1


@pytest.mark.parametrize("change", ["unknown", "unsettled"])
def test_incremental_carry_cannot_insert_unknown_or_unsettled_steps(change):
    store, run, request, prior, parameters, authorized = replay()
    parameters["steps"] = [parameters["steps"][-1]]
    if change == "unknown":
        parameters["steps"][0]["depends_on"].append("unknown_step")
    else:
        next(call for call in run.calls if call.tool == "structure.resolve").state = "unknown"
    with pytest.raises(ProposalError):
        validate(store, run, request, prior, parameters, authorized)


def test_explicit_frozen_change_remains_rejected():
    store, run, request, prior, parameters, authorized = replay()
    call = next(call for call in run.calls if call.tool == "workspace.write")
    changed = call.frozen_step.parameters.model_dump(mode="json")
    changed["content"] = "# an unauthorized change to the frozen Step\n"
    parameters["steps"][2] = {"key": call.step_id, "tool": call.tool, "parameters": changed}
    with pytest.raises(PlanningError, match="immutable"):
        validate(store, run, request, prior, parameters, authorized)


def test_equivalent_route_does_not_erase_explicit_wrong_rule():
    store, run, request, prior, parameters, authorized = replay()
    parameters["goal_map"]["goal_initial_geometry"]["evidence"]["rule_version"] = "invented-1"
    with pytest.raises(PlanningError, match="minimum scientific rule"):
        validate(store, run, request, prior, parameters, authorized)
