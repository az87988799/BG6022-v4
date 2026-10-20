"""Native input patches preserve exact files and frozen Run policy; no execution."""

import hashlib
import json

import pytest
from test_plan_goal_binding_contract import matches

from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    RunConfiguration,
    Step,
    fingerprint,
)
from orca_agent.store import Store, StoreError
from orca_agent.tools.dispatch import execute_call
from orca_agent.tools.registry import get_tool
from orca_agent.tools.workspace import WriteParameters

ORIGINAL = b"! RHF def2-SVP\r\n%scf\r\n  MaxIter 64\r\nend\r\n* xyzfile 0 1 initial.xyz\r\n"


def _parameters(base, edits=None):
    return {
        "name": "repaired.inp",
        "role": "orca",
        "base": {
            "artifact_id": base.id, "sha256": base.sha256, "language": "orca", "version": 1,
        },
        "edits": edits if edits is not None else [{"old": "MaxIter 64", "new": "MaxIter 128"}],
    }


def _request():
    return Request(goals=[Goal(
        id="report", port="artifacts", minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations",
    )])


def _plan(request, steps):
    return Plan(request_id=request.id, steps=steps, goal_map={
        "report": OutputBinding(port="artifacts", gap="file editing only"),
    })


def _case(tmp_path, *, policy="simplified-1", original=ORIGINAL, edits=None,
          authorized=True, wrong_hash=False):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(workflow_policy=policy))
    source = tmp_path / "original.inp"
    source.write_bytes(original)
    base = store.import_artifact(source, "native_input")
    parameters = _parameters(base, edits)
    if wrong_hash:
        parameters["base"]["sha256"] = "0" * 64
    step = Step(id="patch", logical_id="patch", tool="workspace.write", parameters=parameters)
    request = _request()
    run = store.create_run(request, _plan(request, [step]), PermissionSnapshot(
        artifact_writes=True, allowed_tools=["workspace.write"],
        artifact_ids=[base.id] if authorized else [],
    ), BudgetLimits(plan_revisions=2))
    return store, run, step, base


def _assert_rejected_without_reservation(store, run, step, message):
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError), match=message):
        store.reserve_call(run, step.tool, step.parameters.model_dump(mode="json"), step=step)
    assert run.model_dump(mode="json") == before
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert not run.calls and not run.attempts
    assert run.usage.file_writes == run.usage.analysis_executions == 0
    assert store.environment_lease() is None


def test_native_patch_preserves_source_plan_hash_permission_and_line_endings(tmp_path):
    store, run, step, base = _case(tmp_path)
    old_plan = store.path(f"runs/{run.id}/plan-revisions/1.json")
    old_plan_bytes = old_plan.read_bytes()
    old_parameters = step.parameters.model_dump(mode="json")
    old_fingerprint = fingerprint(old_parameters)
    old_permission = run.permission.model_dump(mode="json")
    result = execute_call(store, run, step.tool, old_parameters, step=step)
    created = result.observations["written_artifact"]
    expected = ORIGINAL.replace(b"MaxIter 64", b"MaxIter 128")
    assert result.operation_status == "completed"
    assert created["artifact_id"] != base.id and created["role"] == "orca"
    assert created["sha256"] == hashlib.sha256(expected).hexdigest() != base.sha256
    assert store.artifact_path(created["artifact_id"]).read_bytes() == expected
    assert store.load_artifact(created["artifact_id"]).role == "native_input"
    assert store.artifact_path(base.id).read_bytes() == ORIGINAL
    assert store.load_artifact(base.id).sha256 == hashlib.sha256(ORIGINAL).hexdigest()
    assert fingerprint(step.parameters) == old_fingerprint
    assert run.calls[0].parameters == old_parameters
    assert run.permission.model_dump(mode="json") == old_permission
    assert old_plan.read_bytes() == old_plan_bytes
    assert not result.qualified_outputs and not run.attempts
    assert run.usage.file_writes == 1
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0


def test_settled_native_writer_is_patchable_without_permission_expansion(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(workflow_policy="simplified-1"))
    request = _request()
    writer = Step(id="writer", logical_id="writer", tool="workspace.write", parameters={
        "name": "original.inp", "role": "orca", "content": ORIGINAL.decode(),
    })
    initial = _plan(request, [writer])
    run = store.create_run(request, initial, PermissionSnapshot(
        artifact_writes=True, allowed_tools=["workspace.write"],
    ), BudgetLimits(plan_revisions=2))
    written = execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    prior_result = store.path(f"runs/{run.id}/results/{written.id}.json").read_bytes()
    prior_call = run.calls[0].model_dump(mode="json")
    base = store.load_artifact(written.observations["written_artifact"]["artifact_id"])
    patch = Step(id="patch", logical_id="patch", tool="workspace.write",
                 parameters=_parameters(base))
    revised = initial.model_copy(update={"version": 2, "steps": [writer, patch]})
    run = store.commit_revision(run, revised, decision_id="native_input_patch",
                                basis=current_basis(store, run))
    result = execute_call(store, run, patch.tool, patch.parameters.model_dump(), step=patch)
    assert result.operation_status == "completed"
    assert not run.permission.artifact_ids
    assert run.calls[0].model_dump(mode="json") == prior_call
    assert store.path(f"runs/{run.id}/results/{written.id}.json").read_bytes() == prior_result
    assert store.artifact_path(base.id).read_bytes() == ORIGINAL
    assert run.usage.file_writes == 2 and not run.attempts


def test_legacy_snapshot_without_policy_rejects_native_patch_before_charge(tmp_path):
    store, run, step, _ = _case(tmp_path, policy=None)
    assert "workflow_policy" not in run.configuration.model_dump(mode="json")
    persisted_before = store.path(f"runs/{run.id}/run.json").read_bytes()
    configuration_before = store.path(f"runs/{run.id}/configuration.json").read_bytes()
    # A new host default must not promote the existing Run's frozen policy.
    store.configuration = RunConfiguration(workflow_policy="simplified-1")
    _assert_rejected_without_reservation(store, run, step, "workflow_policy=simplified-1")
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == persisted_before
    assert store.path(f"runs/{run.id}/configuration.json").read_bytes() == configuration_before


@pytest.mark.parametrize("original,edits,message", [
    (b"MaxIter 64\nMaxIter 64\n", [{"old": "MaxIter 64", "new": "MaxIter 128"}], "exactly once"),
    (ORIGINAL, [{"old": "missing block", "new": "MaxIter 128"}], "exactly once"),
    (ORIGINAL, [{"old": "MaxIter 64", "new": "MaxIter 128"},
                {"old": "64", "new": "256"}], "overlap"),
    (ORIGINAL, [{"old": "MaxIter 64", "new": "MaxIter 64"}], "change text"),
])
def test_native_patch_rejects_ambiguous_missing_overlapping_and_unchanged_edits(
    tmp_path, original, edits, message,
):
    store, run, step, base = _case(tmp_path, original=original, edits=edits)
    _assert_rejected_without_reservation(store, run, step, message)
    assert store.artifact_path(base.id).read_bytes() == original


@pytest.mark.parametrize("authorized,wrong_hash,message", [
    (False, False, "outside authorized"), (True, True, "hash differs"),
])
def test_native_patch_requires_permission_and_exact_base_hash(
    tmp_path, authorized, wrong_hash, message,
):
    store, run, step, _ = _case(tmp_path, authorized=authorized, wrong_hash=wrong_hash)
    _assert_rejected_without_reservation(store, run, step, message)


def test_native_patch_rechecks_tampered_bytes_after_reservation(tmp_path):
    store, run, step, base = _case(tmp_path)
    path = store.artifact_path(base.id)

    def tamper(point):
        if point == "after_call_reserved":
            path.write_bytes(ORIGINAL.replace(b"MaxIter 64", b"MaxIter 32"))

    result = execute_call(store, run, step.tool, step.parameters.model_dump(),
                          step=step, fault=tamper)
    assert result.operation_status == "failed" and not result.artifact_ids
    assert not result.observations and not result.qualified_outputs
    assert run.calls[0].state == "failed" and run.usage.file_writes == 1
    assert not store.path(f"runs/{run.id}/calls/{run.calls[0].id}/files/repaired.inp").exists()
    assert not run.attempts and run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("role,language,accepted", [
    ("orca", "orca", True), ("orca", "python", False),
    ("python", "orca", False), ("text", "orca", False),
])
def test_native_patch_schema_and_runtime_require_matching_language(role, language, accepted):
    payload = {
        "name": "fixed.inp", "role": role,
        "base": {"artifact_id": "source", "sha256": "a" * 64, "language": language},
        "edits": [{"old": "MaxIter 64", "new": "MaxIter 128"}],
    }
    union = {"oneOf": get_tool("workspace.write").parameter_schema["oneOf"]}
    assert matches(union, payload, union) is accepted
    if accepted:
        assert WriteParameters.model_validate(payload).base.language == "orca"
    else:
        with pytest.raises(ValueError, match="matching role"):
            WriteParameters.model_validate(payload)


def test_old_content_and_python_patch_serialization_stay_byte_identical():
    content = {"name": "original.inp", "content": ORIGINAL.decode(), "role": "orca"}
    python_patch = {
        "name": "fixed.py", "role": "python",
        "base": {"artifact_id": "source", "sha256": "a" * 64, "language": "python", "version": 1},
        "edits": [{"old": "old", "new": "new"}],
    }
    for payload in (content, python_patch):
        parsed = WriteParameters.model_validate(payload)
        assert parsed.model_dump(mode="json") == payload
        assert json.loads(parsed.model_dump_json()) == payload
        assert fingerprint(parsed) == fingerprint(payload)
