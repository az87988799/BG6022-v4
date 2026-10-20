"""Actual rejected plans remain rejected; only program correction guidance changes."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_context import payload

from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    ModelLimits,
    PermissionSnapshot,
    Request,
    RunConfiguration,
)
from orca_agent.proposals import (
    ProposalError,
    _schema_error,
    action_parameter_schema,
    materialize_plan,
)
from orca_agent.store import Store
from orca_agent.tools.registry import get_tool, validate_parameters

FIXTURE = Path(__file__).parents[1] / "fixtures/code_plan_live006.json"
DIGEST = "4cfe62dd668037c1e117affae15958e0e52d054419e2c71575b83744086b0009"


def replay(tmp_path, index):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DIGEST
    retained = json.loads(raw)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    request = Request.model_validate(retained["request"])
    run = store.create_run(request, None,
        PermissionSnapshot.model_validate(retained["permission"]),
        BudgetLimits.model_validate(retained["budget"]),
        science_baseline_policy=retained["science_baseline_policy"],
        configuration=RunConfiguration(model=ModelLimits.model_validate(retained["model_limits"])))
    reply = retained["replies"][index]
    return store, run, json.loads(reply["raw_content"])["parameters"], reply


@pytest.mark.parametrize("index", range(3))
def test_real_plan_rejections_keep_original_schema_errors_and_add_actionable_feedback(tmp_path, index):
    store, run, proposed, reply = replay(tmp_path, index)
    original = copy.deepcopy(proposed)
    before = store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()
    with pytest.raises(ProposalError) as rejected:
        materialize_plan(store, run, proposed)
    detail = rejected.value.detail
    baseline = reply["historical_correction_in_next_send"]
    if baseline is None:
        # The last proposal has the same malformed ID/hash fields but no next
        # live send; this comparison is explicitly an offline schema replay.
        baseline = json.loads(FIXTURE.read_bytes())["replies"][1]["historical_correction_in_next_send"]
    assert {key: detail[key] for key in baseline if key != "errors"} == {
        key: value for key, value in baseline.items() if key != "errors"}
    # New reference alternatives add possible-selector diagnostics. Every
    # historical concrete ID/hash failure must still survive at its real path.
    assert all(error in detail["errors"] for error in baseline["errors"])
    assert len(detail["errors"]) <= 8
    assert not any(part in {"CodeRef", "ArtifactCodeSelection", "InputManifestEntry",
                           "ArtifactInputSelection", "ProducerFileRef", "ProducerInputEntry", "[field]"}
                   for error in detail["errors"] for part in error["loc"])
    fields = ["input"] if index == 0 else ["input", "inputs"]
    assert [item["path"] for item in detail["corrections"]] == [
        ["parameters", "steps", 3, "parameters", field] for field in fields]
    for item in detail["corrections"]:
        assert item["reason"] == "explicit_file_reference_required"
        instruction = item["instruction"]
        assert "registered Artifact reference" in instruction
        assert "producer_step_id/producer_key" in instruction and "exact file_key" in instruction
        assert "simplified-1" in instruction and "Historical policies still require concrete ID/hash" in instruction
        assert "union alternatives are not cumulative" in instruction
        assert "Never guess hashes or weaken Goals" in instruction
        assert "B3LYP" not in instruction and "ethanol" not in instruction
    feedback = {"category": "ProposalError", "requirement": detail}
    prepared = build_context(store.load_request(run), run, relevant_tools=[], results=[],
        action_parameters={"initial_plan": action_parameter_schema("initial_plan")},
        feedback={"validation_error": feedback})
    assert payload(prepared)["CONTROL"]["validation_error"] == feedback
    assert proposed == original
    assert (store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()) == before
    assert not run.calls and not run.attempts and not run.model_records


@pytest.mark.parametrize("index", range(3))
@pytest.mark.parametrize("include_writer", [False, True])
def test_explicit_offline_partial_plan_preserves_goals_and_materializes(tmp_path, index, include_writer):
    store, run, proposed, _ = replay(tmp_path, index)
    request = store.load_request(run)
    permission = run.permission.model_dump_json()
    # Test-authored correction only. No production code rewrites the proposal,
    # fills IDs/hashes, validates the written chemistry, or runs these Steps.
    partial = {"steps": copy.deepcopy(proposed["steps"][:3 if include_writer else 2]),
        "goal_map": {goal.id: {"port": goal.port, "gap": "Await registered task files and execution evidence."}
                     for goal in request.goals}}
    candidate = materialize_plan(store, run, partial)
    assert len(candidate.steps) == (3 if include_writer else 2)
    updated = store.commit_revision(run, candidate, decision_id="offline_partial_plan",
                                    basis=current_basis(store, run))
    assert store.load_plan(updated) == candidate
    assert store.load_request(updated) == request
    assert updated.permission.model_dump_json() == permission
    assert set(candidate.goal_map) == {goal.id for goal in request.goals}
    assert all(candidate.goal_map[goal.id].port == goal.port and candidate.goal_map[goal.id].gap
               for goal in request.goals)
    assert all(value == "insufficient_evidence" for value in updated.goal_status.values())
    assert updated.usage.plan_revisions == 0  # Initial Plan, not a revision.
    assert len(updated.usage.logical_steps) == len(candidate.steps)
    assert updated.usage.orca_starts_actual == updated.usage.orca_starts_reserved == 0
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST


@pytest.mark.parametrize("role,field", [("code", "code"), ("native_input", "input"),
                                         ("input_manifest", "inputs")])
def test_correction_is_selected_by_registry_role_and_never_echoes_bad_values(monkeypatch, role, field):
    tool = "orca.run" if role == "native_input" else "python.execute"
    values = {"input" if tool == "orca.run" else "code": {
        "artifact_id": "existing_id", "sha256": "0" * 64,
        "language": "orca" if tool == "orca.run" else "python"}}
    marker = "untrusted-never-copy-this-value"
    values[field] = marker
    with pytest.raises(ValidationError) as caught:
        validate_parameters(tool, values)
    definition = get_tool(tool).model_copy(update={"name": "metadata.test", "input_roles": [role]})
    monkeypatch.setattr("orca_agent.proposals.get_tool", lambda name: definition)
    detail = _schema_error(caught.value, path=["parameters"], tool="metadata.test").detail
    assert detail["corrections"][0]["path"] == ["parameters", field]
    assert marker not in json.dumps(detail)
    definition.input_roles = []
    assert "corrections" not in _schema_error(caught.value, path=["parameters"], tool="metadata.test").detail


def test_unrelated_schema_failure_does_not_receive_artifact_guidance():
    with pytest.raises(ValidationError) as caught:
        validate_parameters("orca.sp", {"scf_maxiter": "not-an-integer"})
    detail = _schema_error(caught.value, path=["parameters"], tool="orca.sp").detail
    assert "corrections" not in detail
    assert "not-an-integer" not in json.dumps(detail)


def test_many_reference_errors_keep_each_affected_field_within_eight_error_bound():
    values = {
        "input": {"artifact_id": "invalid id", "sha256": "wrong", "language": "orca"},
        "inputs": {"entries": [{"artifact_id": "invalid id", "sha256": "wrong",
                               "target_path": f"file{index}.xyz", "purpose": "source"}
                              for index in range(12)]},
        "observation_names": ["invalid-name"],
    }
    with pytest.raises(ValidationError) as caught:
        validate_parameters("orca.run", values)
    detail = _schema_error(caught.value, path=["parameters"], tool="orca.run").detail
    assert len(detail["errors"]) == 8
    assert len({json.dumps(error, sort_keys=True) for error in detail["errors"]}) == 8
    assert {error["loc"][1] for error in detail["errors"]} == {
        "input", "inputs", "observation_names"}
    assert {tuple(item["path"]) for item in detail["corrections"]} == {
        ("parameters", "input"), ("parameters", "inputs")}
    for field in ("artifact_id", "sha256"):
        assert {"type": "string_pattern_mismatch", "loc": ["parameters", "input", field]} in detail["errors"]
        assert {"type": "string_pattern_mismatch",
                "loc": ["parameters", "inputs", "entries", 0, field]} in detail["errors"]
    assert "invalid id" not in json.dumps(detail) and "wrong" not in json.dumps(detail)
