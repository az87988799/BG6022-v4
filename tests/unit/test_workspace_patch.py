"""Exact immutable task-file patches; offline archives, no code execution."""

import copy
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
    Step,
)
from orca_agent.planning import PlanningError, validate_code_inputs
from orca_agent.store import Store, StoreError
from orca_agent.tools.dispatch import execute_call
from orca_agent.tools.registry import get_tool
from orca_agent.tools.workspace import WriteParameters


def request():
    return Request(goals=[Goal(id="report", port="artifacts",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])


def plan_for(request, steps):
    return Plan(request_id=request.id, steps=steps,
                goal_map={"report": OutputBinding(port="artifacts", gap="no execution requested")})


def permission(artifacts=()):
    return PermissionSnapshot(code_execution=True, artifact_writes=True,
        allowed_tools=["workspace.write", "python.execute"], artifact_ids=list(artifacts))


def patch_parameters(base, *, edits=None):
    return {"name": "repaired.py", "role": "python",
        "base": {"artifact_id": base.id, "sha256": base.sha256},
        "edits": edits if edits is not None else [{"old": "OLD", "new": "NEW"}]}


def case(tmp_path, *, original=b"print('OLD')\n", edits=None, authorized=True, wrong_hash=False):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    path = tmp_path / "original.py"
    path.write_bytes(original)
    base = store.import_artifact(path, "code")
    parameters = patch_parameters(base, edits=edits)
    if wrong_hash:
        parameters["base"]["sha256"] = "0" * 64
    step = Step(id="patch", logical_id="patch", tool="workspace.write", parameters=parameters)
    purpose = request()
    run = store.create_run(purpose, plan_for(purpose, [step]),
        permission([base.id] if authorized else []),
        BudgetLimits(analysis_executions=3, plan_revisions=3))
    return store, run, step, base


def reserve_rejected_without_charge(store, run, step, match):
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError), match=match):
        store.reserve_call(run, step.tool, step.parameters.model_dump(mode="json"), step=step)
    assert run.model_dump(mode="json") == before
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert not run.calls
    assert run.usage.analysis_executions == 0
    assert store.environment_lease() is None


@pytest.mark.parametrize("role", ["python", "orca", "text"])
@pytest.mark.parametrize("content", ["", "原文\n"])
def test_original_content_form_keeps_exact_three_field_serialization(role, content):
    original = {"name": "task.txt", "content": content, "role": role}
    parsed = WriteParameters.model_validate(original)
    assert parsed.model_dump(mode="json") == original
    assert json.loads(parsed.model_dump_json()) == original
    assert WriteParameters.model_validate({**original, "base": None, "edits": []}).model_dump() == original


@pytest.mark.parametrize("change", [
    {"content": "replacement"}, {"base": None}, {"edits": []},
    {"role": "text"}, {"role": "orca"}, {"content": 1},
    {"base": {"artifact_id": "source", "sha256": "a" * 64, "language": "orca"}},
])
def test_content_and_patch_forms_cannot_be_mixed_or_underspecified(change):
    value = {"name": "fixed.py", "role": "python",
        "base": {"artifact_id": "source", "sha256": "a" * 64},
        "edits": [{"old": "old", "new": "new"}]}
    with pytest.raises(ValueError):
        WriteParameters.model_validate({**value, **change})


@pytest.mark.parametrize("shape,accepted", [
    ({"content": "x"}, True),
    ({"content": ""}, True),
    ({"base": {"artifact_id": "source", "sha256": "a" * 64}, "edits": [{"old": "x", "new": "y"}]}, True),
    ({"content": "x", "base": {"artifact_id": "source", "sha256": "a" * 64},
      "edits": [{"old": "x", "new": "y"}]}, False),
    ({"content": None}, False),
    ({"base": {"artifact_id": "source", "sha256": "a" * 64}, "edits": []}, False),
    ({"base": None, "edits": [{"old": "x", "new": "y"}]}, False),
    ({"role": "text", "base": {"artifact_id": "source", "sha256": "a" * 64},
      "edits": [{"old": "x", "new": "y"}]}, False),
    ({"base": {"artifact_id": "source", "sha256": "a" * 64, "language": "orca"},
      "edits": [{"old": "x", "new": "y"}]}, False),
])
def test_registry_schema_independently_enforces_disjoint_content_and_python_patch_forms(shape, accepted):
    schema = get_tool("workspace.write").parameter_schema
    union = {"oneOf": schema["oneOf"]}
    payload = {"name": "fixed.py", "role": "python", **shape}
    # The independent matcher evaluates the actual union. Primitive/CodeRef
    # constraints remain in the unchanged registry schema and runtime types.
    assert matches(union, payload, union) is accepted
    if accepted:
        WriteParameters.model_validate(payload)
    else:
        with pytest.raises(ValueError):
            WriteParameters.model_validate(payload)
    assert schema["properties"]["edits"]["maxItems"] == 16
    replacement = schema["$defs"]["ExactReplacement"]
    assert replacement["properties"]["old"]["minLength"] == 1
    assert replacement["properties"]["old"]["maxLength"] == 16384
    assert replacement["properties"]["new"]["maxLength"] == 16384


@pytest.mark.parametrize("original,edits,message", [
    (b"OLD OLD", [{"old": "OLD", "new": "NEW"}], "exactly once"),
    (b"OLD", [{"old": "missing", "new": "NEW"}], "exactly once"),
    (b"aaa", [{"old": "aa", "new": "b"}], "exactly once"),
    (b"abcd", [{"old": "abc", "new": "x"}, {"old": "bcd", "new": "y"}], "overlap"),
    (b"OLD", [{"old": "OLD", "new": "OLD"}], "change text"),
    (b"abcd", [{"old": "ab", "new": "a"}, {"old": "cd", "new": "bcd"}], "change the original"),
])
def test_ambiguous_overlapping_and_zero_change_edits_are_rejected_before_reservation(
    tmp_path, original, edits, message,
):
    store, run, step, _ = case(tmp_path, original=original, edits=edits)
    reserve_rejected_without_charge(store, run, step, message)


@pytest.mark.parametrize("original,edits,expected", [
    ("# 中文原文\r\nx='旧'\r\n", [{"old": "'旧'", "new": "'新'"}], "# 中文原文\r\nx='新'\r\n"),
    ("abc def", [{"old": "abc", "new": "def"}, {"old": "def", "new": "ghi"}], "def ghi"),
    ("abcd", [{"old": "ab", "new": "A"}, {"old": "cd", "new": "C"}], "AC"),
    ("old delete tail", [{"old": "delete ", "new": ""}], "old tail"),
])
def test_patch_uses_original_nonoverlapping_offsets_and_preserves_original_artifact(
    tmp_path, original, edits, expected,
):
    raw = original.encode("utf-8")
    store, run, step, base = case(tmp_path, original=raw, edits=edits)
    old_hash = hashlib.sha256(raw).hexdigest()
    old_permission = run.permission.model_dump(mode="json")
    result = execute_call(store, run, step.tool, step.parameters.model_dump(mode="json"), step=step)
    assert result.operation_status == "completed"
    created = result.observations["written_artifact"]
    assert created["artifact_id"] != base.id and created["sha256"] != old_hash
    assert store.artifact_path(created["artifact_id"]).read_bytes() == expected.encode("utf-8")
    assert created["bytes"] == len(expected.encode("utf-8"))
    assert store.artifact_path(base.id).read_bytes() == raw
    assert store.load_artifact(base.id).sha256 == old_hash
    assert run.permission.model_dump(mode="json") == old_permission
    assert run.usage.file_writes == 1 and run.usage.analysis_executions == 0
    assert not run.attempts and not result.qualified_outputs
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0


@pytest.mark.parametrize("original,edits,message", [
    (b"OLD\xff", [{"old": "OLD", "new": "NEW"}], "utf-8"),
    (b"OLD\x00", [{"old": "OLD", "new": "NEW"}], "without NUL"),
    (b"OLD", [{"old": "OLD", "new": "\x00"}], "without NUL"),
    (b"a" * 65537, [{"old": "a", "new": "b"}], "64 KiB"),
    (b"a" * 30000 + b"OLD", [{"old": "OLD", "new": "汉" * 12000}], "64 KiB"),
], ids=["invalid_utf8", "base_nul", "replacement_nul", "base_byte_limit", "output_byte_limit"])
def test_patch_keeps_utf8_nul_and_whole_file_byte_bounds(tmp_path, original, edits, message):
    store, run, step, _ = case(tmp_path, original=original, edits=edits)
    reserve_rejected_without_charge(store, run, step, message)


def test_exact_64k_output_boundary_accepts_multibyte_replacement(tmp_path):
    store, run, step, _ = case(tmp_path, original=b"a" * 65530 + b"TARGET",
                               edits=[{"old": "TARGET", "new": "替换"}])
    result = execute_call(store, run, step.tool, step.parameters.model_dump(mode="json"), step=step)
    assert result.operation_status == "completed"
    assert result.observations["written_artifact"]["bytes"] == 65536


@pytest.mark.parametrize("edits", [
    [{"old": "", "new": "x"}], [{"old": "x" * 16385, "new": ""}],
    [{"old": "x", "new": "x" * 16385}], [{"old": "x", "new": "y"}] * 17,
])
def test_edit_count_and_fragment_bounds_are_validated_before_store_work(edits):
    with pytest.raises(ValueError):
        WriteParameters(name="fixed.py", role="python",
            base={"artifact_id": "source", "sha256": "a" * 64}, edits=edits)


@pytest.mark.parametrize("authorized,wrong_hash,reason", [
    (False, False, "outside authorized"), (True, True, "hash differs"),
])
def test_external_base_requires_exact_authorized_hash_before_quota(tmp_path, authorized, wrong_hash, reason):
    store, run, step, _ = case(tmp_path, authorized=authorized, wrong_hash=wrong_hash)
    reserve_rejected_without_charge(store, run, step, reason)


def test_owned_but_unsettled_file_does_not_become_patch_authority(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    purpose = request()
    pending = Step(id="pending", logical_id="pending", tool="workspace.write",
        parameters={"name": "original.py", "role": "python", "content": "print('OLD')\n"})
    initial = plan_for(purpose, [pending])
    run = store.create_run(purpose, initial, permission(), BudgetLimits(plan_revisions=2))
    source = tmp_path / "unsettled.py"
    source.write_bytes(b"print('OLD')\n")
    base = store.import_artifact(source, "code", run_id=run.id, source={"call_id": "unsettled_call"})
    step = Step(id="patch", logical_id="patch", tool="workspace.write", parameters=patch_parameters(base))
    revised = initial.model_copy(update={"version": 2, "steps": [pending, step]})
    run = store.commit_revision(run, revised, decision_id="explicit_patch", basis=current_basis(store, run))
    assert base.run_id == run.id and base.id not in store.verified_own_artifacts(run)
    reserve_rejected_without_charge(store, run, step, "outside authorized")


def test_settled_writer_base_needs_no_expansion_of_permission_and_new_coderef_keeps_checks(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    purpose = request()
    writer = Step(id="writer", logical_id="writer", tool="workspace.write",
        parameters={"name": "original.py", "role": "python", "content": "print('OLD')\n"})
    initial = plan_for(purpose, [writer])
    run = store.create_run(purpose, initial, permission(), BudgetLimits(analysis_executions=3, plan_revisions=2))
    written = execute_call(store, run, writer.tool, writer.parameters.model_dump(), step=writer)
    base = store.load_artifact(written.observations["written_artifact"]["artifact_id"])
    patch = Step(id="patch", logical_id="patch", tool="workspace.write", parameters=patch_parameters(base))
    revised = initial.model_copy(update={"version": 2, "steps": [writer, patch]})
    run = store.commit_revision(run, revised, decision_id="explicit_patch", basis=current_basis(store, run))
    result = execute_call(store, run, patch.tool, patch.parameters.model_dump(), step=patch)
    assert result.operation_status == "completed"
    reference = {key: result.observations["written_artifact"][key] for key in ("artifact_id", "sha256")}
    consumer = Step(id="consumer", logical_id="consumer", tool="python.execute", parameters={"code": reference})
    assert not run.permission.artifact_ids
    validate_code_inputs(store, run.permission, consumer, run=run)
    bad = consumer.model_copy(deep=True)
    bad.parameters.code.sha256 = base.sha256
    with pytest.raises(PlanningError, match="hash differs"):
        validate_code_inputs(store, run.permission, bad, run=run)
    with pytest.raises(PlanningError, match="permission snapshot"):
        validate_code_inputs(store, permission(), consumer)
    assert store.artifact_path(base.id).read_bytes() == b"print('OLD')\n"
    assert not run.attempts and run.usage.file_writes == 2 and run.usage.analysis_executions == 0


def test_write_rechecks_base_changed_after_preflight_and_keeps_charged_failed_receipt(tmp_path):
    store, run, step, base = case(tmp_path)
    source = store.artifact_path(base.id)

    def change_after_reservation(point):
        if point == "after_call_reserved":
            assert run.usage.file_writes == 1 and run.usage.analysis_executions == 0
            source.write_bytes(b"print('CHANGED')\n")

    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step,
                          fault=change_after_reservation)
    assert result.operation_status == "failed" and not result.artifact_ids
    assert not result.observations and not result.qualified_outputs
    assert run.calls[0].state == "failed" and run.usage.file_writes == 1 and run.usage.analysis_executions == 0
    assert not store.path(f"runs/{run.id}/calls/{run.calls[0].id}/files/{step.parameters.name}").exists()


def test_preflight_rechecks_bytes_after_path_hash_validation_without_charging(tmp_path, monkeypatch):
    store, run, step, base = case(tmp_path)
    checked_path = store.artifact_path

    def changed_after_validation(identifier, **kwargs):
        path = checked_path(identifier, **kwargs)
        if identifier == base.id:
            path.write_bytes(b"print('NEW')\n")
        return path

    monkeypatch.setattr(store, "artifact_path", changed_after_validation)
    reserve_rejected_without_charge(store, run, step, "changed while reading")


def test_targeted_patch_serialization_uses_hundreds_of_bytes_for_large_script(tmp_path, record_property):
    original = "# retained task algorithm and provenance\n" * 1200 + "threshold = OLD\n"
    store, run, step, base = case(tmp_path, original=original.encode("utf-8"))
    patch = step.parameters.model_dump(mode="json")
    full = {"name": patch["name"], "role": patch["role"], "content": original.replace("OLD", "NEW")}
    def size(value):
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    patch_bytes, full_bytes = size(patch), size(full)
    record_property("patch_request_utf8_bytes", patch_bytes)
    record_property("full_content_request_utf8_bytes", full_bytes)
    assert 200 <= patch_bytes < 600
    assert full_bytes > 40000 and patch_bytes * 50 < full_bytes
    before = copy.deepcopy(run)
    store.reserve_call(run, step.tool, patch, step=step, validate_only=True)
    assert run == before and not run.calls
    assert store.artifact_path(base.id).read_bytes() == original.encode("utf-8")
