"""Explicit model-chosen code replacement retains every trusted task field; offline."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import Field, ValidationError

from orca_agent import agent
from orca_agent.model_usage import current_basis
from orca_agent.models import Attempt, CodeRef, OutputBinding, Plan, Result, Step, utc_now
from orca_agent.planning import (
    PlanningError,
    _python_repair,
    validate_code_inputs,
    validate_revision,
)
from orca_agent.proposals import (
    ProposalError,
    ProposedPlan,
    ProposedStep,
    _schema_error,
    materialize_plan,
    plan_structure_schema,
    proposed_plan_schema,
    validate_action_parameters,
)
from tests.unit.test_context import objects
from tests.unit.test_failed_python_history import failed_then_writer
from tests.unit.test_python_repair_budget import case, settle


def test_python_omitted_inputs_cannot_inherit_legacy_calculation_parameters():
    request, run = objects()
    legacy = Step(id="legacy", logical_id="legacy_logical", tool="orca.sp",
        parameters={}, geometry={"artifact_id": request.geometry_artifact_id})
    prior = Plan(request_id=request.id, steps=[legacy],
        goal_map={"energy": OutputBinding(step_id=legacy.id, port="energy")})
    run.plan_id, run.plan_version = prior.id, prior.version
    run.permission.code_execution = True
    attempt = Attempt(step_id=legacy.id, logical_id=legacy.logical_id, number=1,
        tool=legacy.tool, state="failed", input_fingerprint="legacy_input", directory="attempts/legacy",
        finished_at=utc_now(), result_id="legacy_result", frozen_step=legacy)
    result = Result(id="legacy_result", run_id=run.id, step_id=legacy.id,
        attempt_id=attempt.id, operation_status="failed")
    run.attempts, run.result_ids = [attempt], [result.id]
    store = SimpleNamespace(load_plan=lambda _: prior, load_request=lambda _: request,
        load_result=lambda *_: result)
    parameters = {"steps": [{"key": "repair", "logical_key": legacy.logical_id,
        "tool": "python.execute", "parameters": {"code": {"artifact_id": "script", "sha256": "a" * 64},
            "observation_names": ["energy"]}}],
        "goal_map": {"energy": {"step_key": "repair", "port": "energy"}}}
    before = copy.deepcopy((request, run, prior, parameters))
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, parameters)
    assert caught.value.detail["code"] == "invalid_code_repair_source_tool"
    assert caught.value.detail["path"] == ["parameters", "steps", 0, "logical_key"]
    assert (request, run, prior, parameters) == before


def proposal(attempt, code, *, retained=()):
    return {"steps": [{"key": step.id} for step in retained] + [{
        "key": "repair", "repair_of": attempt.step_id,
        "code": code.model_dump(mode="json") if hasattr(code, "model_dump") else code}],
        "goal_map": {"report": {"step_key": "repair", "port": "artifacts"}}}


def noncode(step):
    value = step.model_dump(mode="json")
    value.pop("id")
    value["parameters"].pop("code")
    return value


def test_historical_failure_reference_copies_full_contract_without_mutation(tmp_path, monkeypatch):
    store, run, request, prior, attempt, _, _, code = failed_then_writer(tmp_path, monkeypatch)
    original = copy.deepcopy((run, request, prior))
    values = proposal(attempt, code, retained=prior.steps)
    before = copy.deepcopy(values)
    candidate = materialize_plan(store, run, values)
    repaired = candidate.steps[-1]
    assert repaired.id != attempt.step_id and repaired.logical_id == attempt.logical_id
    assert noncode(repaired) == noncode(attempt.frozen_step)
    assert repaired.parameters.inputs.entries[0].purpose == "fixed source for the requested analysis"
    assert repaired.parameters.observation_names == ["artifacts"]
    assert repaired.parameters.code == CodeRef.model_validate(code)
    assert attempt.step_id not in {step.id for step in prior.steps}
    validate_revision(request, prior, request, candidate, run,
        authorized_artifacts=store.verified_own_artifacts(run))
    agent._repair_evidence(store, run, candidate)
    assert values == before and (run, request, prior) == original


@pytest.mark.parametrize("settled", [True, False])
def test_reference_carries_only_verified_settled_dependencies(tmp_path, monkeypatch, settled):
    store, run, _, _, attempt, _, _, code = failed_then_writer(tmp_path, monkeypatch)
    if not settled:
        next(call for call in run.calls if call.step_id == attempt.frozen_step.depends_on[0]).state = "unknown"
    values = proposal(attempt, code)
    before = copy.deepcopy((values, run))
    if settled:
        candidate = materialize_plan(store, run, values)
        assert set(attempt.frozen_step.depends_on) <= {step.id for step in candidate.steps}
        dependency = next(call.frozen_step for call in run.calls
                          if call.step_id == attempt.frozen_step.depends_on[0])
        assert dependency in candidate.steps
    else:
        with pytest.raises(ProposalError) as caught:
            materialize_plan(store, run, values)
        assert caught.value.detail["code"] == "missing_step_reference"
        assert caught.value.detail["missing_step_keys"] == attempt.frozen_step.depends_on
        assert caught.value.detail["path"] == ["parameters", "steps", 0, "repair_of"]
    assert (values, run) == before


def test_full_repair_omitting_manifest_preserves_the_original_input_copies(tmp_path, monkeypatch):
    store, run, request, prior, attempt, _, _, code = failed_then_writer(tmp_path, monkeypatch)
    values = proposal(attempt, code, retained=prior.steps)
    source = attempt.frozen_step
    parameters = source.parameters.model_dump(mode="json", exclude={"inputs"})
    parameters["code"] = code
    values["steps"][-1] = {"key": "repair", "logical_key": source.logical_id,
        "tool": source.tool, "parameters": parameters, "system_id": source.system_id,
        "depends_on": source.depends_on,
        "inputs": {name: ref.model_dump(mode="json") for name, ref in source.inputs.items()}}
    candidate = materialize_plan(store, run, values)
    repaired = candidate.steps[-1]
    assert repaired.parameters.inputs == source.parameters.inputs
    validate_revision(request, prior, request, candidate, run,
                      authorized_artifacts=store.verified_own_artifacts(run))


@pytest.mark.parametrize("change", ["unauthorized", "unknown", "current_key", "completed", "running", "unknown_state",
    "not_started", "unfinished", "no_result", "foreign_result", "no_snapshot", "mismatched_snapshot", "orca", "superseded"])
def test_invalid_reference_never_materializes_or_changes_history(tmp_path, monkeypatch, change):
    store, run, codes = case(tmp_path, monkeypatch)
    attempt, _ = settle(store, run)
    values = proposal(attempt, codes[1])
    if change == "unauthorized":
        run.permission.code_execution = False
    elif change == "unknown":
        values["steps"][0]["repair_of"] = "other_run_step"
    elif change == "current_key":
        values["steps"][0]["key"] = attempt.step_id
    elif change in {"completed", "running", "unknown_state", "not_started"}:
        attempt.state = "unknown" if change == "unknown_state" else change
    elif change == "unfinished":
        attempt.finished_at = None
    elif change == "no_result":
        attempt.result_id = None
    elif change == "foreign_result":
        run.result_ids.remove(attempt.result_id)
    elif change == "no_snapshot":
        attempt.frozen_step = None
    elif change == "mismatched_snapshot":
        attempt.frozen_step.logical_id = "other_task"
    elif change == "orca":
        attempt.execution_kind = "orca"
    else:
        run.attempts.append(attempt.model_copy(deep=True, update={"id": "later", "step_id": "later_step"}))
    before = copy.deepcopy((run, values))
    with pytest.raises(ProposalError):
        materialize_plan(store, run, values)
    assert (run, values) == before


@pytest.mark.parametrize("field,value", [("tool", "python.execute"), ("logical_key", "new"),
    ("parameters", {}), ("depends_on", []), ("conditions", {}), ("system_id", "system")])
def test_closed_reference_cannot_override_frozen_fields(field, value):
    values = {"steps": [{"key": "repair", "repair_of": "failed", "code": {
        "artifact_id": "new", "sha256": "a" * 64}, field: value}], "goal_map": {}}
    before = copy.deepcopy(values)
    with pytest.raises(ProposalError):
        validate_action_parameters("revise_plan", values)
    assert values == before


@pytest.mark.parametrize("field,value", [("run_id", "other_run"), ("step_id", "other_step"),
    ("attempt_id", "other_attempt"), ("operation_status", "completed"), ("id", "other_result")])
def test_reference_requires_exact_result_identity(tmp_path, monkeypatch, field, value):
    store, run, codes = case(tmp_path, monkeypatch)
    attempt, result = settle(store, run)
    altered = result.model_copy(deep=True, update={field: value})
    monkeypatch.setattr(store, "load_result", lambda *_: altered)
    before = run.model_copy(deep=True)
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal(attempt, codes[1]))
    assert caught.value.detail["code"] == "code_repair_result_mismatch"
    assert run == before


@pytest.mark.parametrize("change", ["same_code", "unregistered", "wrong_hash", "wrong_language", "no_budget", "qualified"])
def test_original_admission_budget_and_scientific_gates_remain(tmp_path, monkeypatch, change):
    store, run, codes = case(tmp_path, monkeypatch)
    attempt, _ = settle(store, run)
    code = codes[0 if change == "same_code" else 1].model_dump(mode="json")
    if change == "unregistered":
        code["artifact_id"] = "unregistered"
    elif change == "wrong_hash":
        code["sha256"] = "f" * 64
    elif change == "wrong_language":
        code["language"] = "orca"
    elif change == "no_budget":
        run.usage.code_retries = run.budget.code_retries
    values = proposal(attempt, code)
    original = copy.deepcopy((run, values))
    request, prior = store.load_request(run), store.load_plan(run)
    if change == "qualified":
        request.goals[0].verification = "qualified"
    with pytest.raises((PlanningError, ProposalError)):
        candidate = materialize_plan(store, run, values)
        validate_revision(request, prior, request, candidate, run)
        validate_code_inputs(store, run.permission, candidate.steps[0], run=run)
    assert (run, values) == original


def test_valid_reference_still_charges_real_reservation_budget(tmp_path, monkeypatch):
    store, run, codes = case(tmp_path, monkeypatch)
    attempt, result = settle(store, run)
    candidate = materialize_plan(store, run, proposal(attempt, codes[1]))
    agent._repair_evidence(store, run, candidate)
    updated = store.commit_revision(run, candidate, decision_id="explicit_repair",
        basis=current_basis(store, run), related_results=[result.id])
    reserved = store.reserve_attempt(updated, candidate.steps[0])
    assert reserved.logical_id == attempt.logical_id and reserved.number == 2
    assert updated.usage.code_retries == 1 and updated.usage.orca_starts_reserved == 0
    assert reserved.started is False  # Reservation only; no process, SDK or ORCA.


def test_actual_live037_remains_rejected_and_explicit_reference_preserves_manifest(tmp_path, monkeypatch):
    fixture = json.loads((Path(__file__).parents[1] /
        "fixtures/code_contracts/live037-frozen-repair-contract.json").read_text(encoding="utf-8"))
    before = copy.deepcopy(fixture)
    attempt = Attempt.model_validate(fixture["frozen_attempt"])
    frozen = attempt.frozen_step
    submitted = next(item for item in fixture["actual_rejected_proposal"]["parameters"]["steps"]
        if item.get("tool") == "python.execute")
    bad = Step(id="bad", logical_id=frozen.logical_id, tool=submitted["tool"],
        parameters=submitted["parameters"], depends_on=submitted["depends_on"],
        system_id=submitted["system_id"])
    # The original live037 rejection stays historical. Under the explicitly
    # adopted input-repair contract this shape can be proposed, but its files
    # must still pass the same independent authorization/hash boundary.
    _python_repair(frozen, bad, SimpleNamespace(attempts=[attempt]), {})
    store, run, _ = case(tmp_path, monkeypatch)
    with pytest.raises(PlanningError, match="outside the permission snapshot"):
        validate_code_inputs(store, run.permission, bad, run=run)
    request = store.load_request(run)
    dependencies = [Step(id=identifier, logical_id="dependency_" + str(index), tool="workspace.write",
        parameters={"name": f"input{index}.txt", "content": "offline fixture", "role": "text"})
        for index, identifier in enumerate(frozen.depends_on)]
    prior = Plan(request_id=request.id, steps=dependencies,
        goal_map={"report": OutputBinding(port="artifacts", gap="repair pending")})
    # This synthetic facade tests proposal expansion only. It does not supply
    # a real successful repair receipt or alter the actual live037 store.
    facade = SimpleNamespace(load_request=lambda _: request, load_plan=lambda _: prior,
        load_result=lambda *_: SimpleNamespace(id=attempt.result_id, run_id=run.id,
            step_id=attempt.step_id, attempt_id=attempt.id, operation_status=attempt.state))
    run.attempts = [attempt]
    run.result_ids = [attempt.result_id]
    values = proposal(attempt, fixture["revised_code"], retained=dependencies)
    with pytest.raises(ProposalError, match="when repairing a legacy Python Step"):
        materialize_plan(facade, run, values)
    values["steps"][-1]["sdk_orca"] = True  # New declaration; the archived fixture stays unchanged.
    candidate = materialize_plan(facade, run, values)
    corrected = candidate.steps[-1]
    expected = noncode(frozen)
    expected["parameters"]["sdk_orca"] = True
    assert noncode(corrected) == expected
    assert corrected.parameters.inputs.entries[0].target_path == "ethanol_opt_freq.py"
    assert corrected.parameters.code.artifact_id != corrected.parameters.inputs.entries[0].artifact_id
    _python_repair(frozen, corrected, run, {})
    assert fixture == before  # The actual model never submitted this synthetic corrected shape.


def test_unauthorized_schema_projection_keeps_old_step_contract_and_structural_projection():
    full = proposed_plan_schema()
    legacy = proposed_plan_schema(code_repair=False)
    assert len(full["properties"]["steps"]["items"]["anyOf"]) == 2
    assert legacy["properties"]["steps"]["items"] == {"$ref": "#/$defs/ProposedStep"}
    assert legacy["$defs"]["ProposedStep"] == full["$defs"]["ProposedStep"]
    assert "ProposedCodeRepair" not in legacy["$defs"] and "CodeRef" not in legacy["$defs"]
    assert plan_structure_schema()["properties"]["steps"]["items"] == {"type": "object"}


def test_old_malformed_step_feedback_matches_original_type_exactly():
    class OriginalPlan(ProposedPlan):
        steps: list[ProposedStep] = Field(min_length=1)

    values = {"steps": [{"id": "stored", "tool": 42}], "goal_map": {}}
    with pytest.raises(ValidationError) as original:
        OriginalPlan.model_validate(values)
    expected = _schema_error(original.value, path=["parameters"]).detail
    expected["step_fields"] = list(ProposedStep.model_fields)
    expected["step_instruction"] = (
        "Proposed Steps require key, not the stored Step.id field. "
        "Retain an existing Step with {key:existing_step_id}; the program reuses its frozen contents. "
        "Do not copy stored id/logical_id/immutable_frozen metadata into a proposal. "
        "A new Step uses key/tool/parameters and its declared dependencies; "
        "a repair uses logical_key for the prior logical_id, subject to the existing repair rules.")
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", values)
    assert caught.value.detail == expected
    assert "repair_of" not in json.dumps(caught.value.detail)


def test_mixed_shape_errors_use_each_actual_step_and_do_not_consume_other_branch_slots():
    values = {"steps": [{"id": "old"}, {"key": "repair", "repair_of": "failed", "code": {
        "artifact_id": "new", "sha256": "a" * 64}, "depends_on": []}], "goal_map": {}}
    before = copy.deepcopy(values)
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", values)
    errors = caught.value.detail["errors"]
    assert errors == [{"type": "missing", "loc": ["parameters", "steps", 0, "key"]},
        {"type": "extra_forbidden", "loc": ["parameters", "steps", 0, "id"]},
        {"type": "extra_forbidden", "loc": ["parameters", "steps", 1, "depends_on"]}]
    assert caught.value.detail["code_repair_fields"] == ["key", "repair_of", "code", "input", "inputs", "sdk_orca"]
    assert values == before


def test_invalid_explicit_reference_reports_actual_code_path_only():
    values = {"steps": [{"key": "repair", "repair_of": "failed", "code": {
        "artifact_id": "new", "sha256": "invalid"}}], "goal_map": {}}
    with pytest.raises(ProposalError) as caught:
        validate_action_parameters("revise_plan", values)
    assert caught.value.detail["errors"] == [{"type": "string_pattern_mismatch",
        "loc": ["parameters", "steps", 0, "code", "sha256"]}]
    assert "code permission" in caught.value.detail["code_repair_instruction"]
