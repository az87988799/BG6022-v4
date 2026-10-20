"""Structural replay, with explicit current SDK declaration added to old replies.

The archived bytes remain unchanged. The original missing-declaration rejection
is tested separately; these fixtures perform no execution or API calls.
"""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orca_agent.models import Plan, Request, Result, Run
from orca_agent.planning import PlanningError, validate_code_inputs, validate_revision
from orca_agent.proposals import ProposalError, materialize_plan

FIXTURE = Path(__file__).parents[1] / "fixtures/incremental_repair_live006.json"
SOURCE = "step_3e65727f4d5e4ad980da838453fd88e1"


def replay(*, complete_declaration=True):
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
    if complete_declaration:
        data["parameters"]["steps"][0]["sdk_orca"] = True
    return store, run, request, prior, data["parameters"]


def test_archived_last_reply_without_current_sdk_declaration_is_still_rejected():
    store, run, _, _, parameters = replay(complete_declaration=False)
    with pytest.raises(ProposalError) as rejected:
        materialize_plan(store, run, parameters)
    assert rejected.value.detail["code"] == "missing_sdk_orca_declaration"


def test_declaration_completed_last_reply_preserves_attempt_and_budget():
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
    full["parameters"]["sdk_orca"] = True
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
    assert replacement.parameters.sdk_orca is True
    assert replacement.parameters.model_dump(exclude={"code", "sdk_orca"}) == source.parameters.model_dump(
        exclude={"code", "sdk_orca"})
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


def input_repair_case(tmp_path, monkeypatch, *, same_code=False):
    store, run, request, prior, parameters = replay()
    source = next(step for step in prior.steps if step.id == SOURCE)
    geometry = json.loads(FIXTURE.read_bytes())["prepared_geometry"]
    repair = parameters["steps"][0]
    if same_code:
        repair["code"] = source.parameters.code.model_dump(mode="json")
    repair["inputs"] = {"entries": [{
        "artifact_id": geometry["artifact_id"], "sha256": geometry["sha256"],
        "target_path": "coords.xyz", "purpose": "prepared initial geometry for the same search"}]}
    bodies = {hashlib.sha256(step.parameters.content.encode()).hexdigest():
              step.parameters.content.encode() for step in prior.steps if step.tool == "workspace.write"}
    bodies[geometry["sha256"]] = geometry["content"].encode()
    references = [repair["code"], geometry]
    archived = {}
    for index, reference in enumerate(references):
        content = bodies[reference["sha256"]]
        assert hashlib.sha256(content).hexdigest() == reference["sha256"]
        path = tmp_path / f"archived-{index}"
        path.write_bytes(content)
        archived[reference["artifact_id"]] = SimpleNamespace(sha256=reference["sha256"], path=path)
    authorized = set(archived)
    store.verified_own_artifacts = lambda _: authorized
    store.load_artifact = lambda identifier: archived[identifier]
    store.artifact_path = lambda identifier: archived[identifier].path
    # These tests isolate immutable repair references and file admission. Real
    # SDK initial-geometry admission is covered by test_sdk_geometry_preflight.
    monkeypatch.setattr("orca_agent.tools.managed.validate_sdk_orca_preconditions",
                        lambda *args, **kwargs: None)
    return store, run, request, prior, parameters, authorized


@pytest.mark.parametrize("same_code", [False, True])
def test_real006_explicit_coords_manifest_repairs_input_without_changing_old_attempt(tmp_path, monkeypatch, same_code):
    store, run, request, prior, parameters, authorized = input_repair_case(
        tmp_path, monkeypatch, same_code=same_code)
    before = copy.deepcopy((run, request, prior, parameters))
    candidate = materialize_plan(store, run, parameters)
    validate_revision(request, prior, request, candidate, run, authorized_artifacts=authorized)
    repair = next(step for step in candidate.steps if step.tool == "python.execute")
    validate_code_inputs(store, run.permission, repair, run=run)
    assert repair.parameters.inputs.entries[0].target_path == "coords.xyz"
    assert repair.parameters.inputs.entries[0].artifact_id == "artifact_f93b76a761ff4f45b02b5a47f81b8c4d"
    assert not run.attempts[0].frozen_step.parameters.inputs.entries
    assert repair.parameters.conditions == run.attempts[0].frozen_step.parameters.conditions
    assert repair.parameters.observation_names == run.attempts[0].frozen_step.parameters.observation_names
    assert repair.depends_on == run.attempts[0].frozen_step.depends_on
    assert (run, request, prior, parameters) == before


@pytest.mark.parametrize("change", ["unauthorized_input", "wrong_hash", "unchanged_inputs", "conditions"])
def test_explicit_input_repair_preserves_original_admission_gates(tmp_path, monkeypatch, change):
    store, run, request, prior, parameters, authorized = input_repair_case(tmp_path, monkeypatch, same_code=True)
    proposed = parameters["steps"][0]
    if change == "unauthorized_input":
        proposed["inputs"]["entries"][0]["artifact_id"] = "foreign_or_unsettled_artifact"
    elif change == "wrong_hash":
        proposed["inputs"]["entries"][0]["sha256"] = "f" * 64
    elif change == "unchanged_inputs":
        proposed["inputs"] = {"entries": []}
    else:
        source = next(step for step in prior.steps if step.id == SOURCE)
        full = source.model_dump(mode="json")
        full.update(key=proposed["key"], logical_key=full.pop("logical_id"))
        full.pop("id")
        full["parameters"]["inputs"] = proposed["inputs"]
        full["parameters"]["conditions"] = {"analysis_scope": "different task purpose"}
        full["parameters"]["sdk_orca"] = True
        parameters["steps"] = [full]
    before = copy.deepcopy((run, request, prior, parameters))
    with pytest.raises(PlanningError):
        candidate = materialize_plan(store, run, parameters)
        validate_revision(request, prior, request, candidate, run, authorized_artifacts=authorized)
        repair = next(step for step in candidate.steps if step.tool == "python.execute")
        validate_code_inputs(store, run.permission, repair, run=run)
    assert (run, request, prior, parameters) == before
