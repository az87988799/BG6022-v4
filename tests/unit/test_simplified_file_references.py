"""Producer file bindings through real local writes and reservation, never execution."""

import copy
import json

import pytest

from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    InputManifestEntry,
    PermissionSnapshot,
    ProducerFileRef,
    ProducerInputEntry,
    Request,
    Result,
    RunConfiguration,
    Step,
    fingerprint,
)
from orca_agent.proposals import materialize_plan
from orca_agent.store import Store, StoreError
from orca_agent.tools.dispatch import execute_call

SCRIPT = "print('this test never executes task code')\n"


@pytest.mark.parametrize("model", [ProducerFileRef, ProducerInputEntry])
@pytest.mark.parametrize("selector,accepted", [
    ({}, False),
    ({"port": None, "file_key": None}, False),
    ({"port": "prepared_geometry"}, True),
    ({"file_key": "task/result.xyz"}, True),
    ({"port": "prepared_geometry", "file_key": None}, True),
    ({"port": None, "file_key": "task/result.xyz"}, True),
    ({"port": "prepared_geometry", "file_key": "task/result.xyz"}, False),
    ({"port": "latest"}, False),
])
def test_producer_selector_schema_matches_runtime_exactly(model, selector, accepted):
    from orca_agent.context import _schema
    from tests.unit.test_plan_goal_binding_contract import matches

    values = {"producer_step_id": "prepare", **selector}
    if model is ProducerInputEntry:
        values.update(target_path="initial.xyz", purpose="current_geometry")
    schema = _schema(model.model_json_schema())
    assert matches(schema, values, schema) is accepted
    if accepted:
        model.model_validate(values)
    else:
        with pytest.raises(ValueError):
            model.model_validate(values)


def _case(tmp_path, *, policy="simplified-1", external_files=()):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment",
                  configuration=RunConfiguration(workflow_policy=policy))
    artifacts = []
    for name, content, role in external_files:
        path = tmp_path / name
        path.write_bytes(content)
        artifacts.append(store.import_artifact(path, role))
    request = Request(goals=[Goal(
        id="report", port="artifacts", evidence_contract_version="generic-observation-1",
        minimum_check_version="generic-observation-1", verification="reported_with_limitations",
    )])
    run = store.create_run(request, None, PermissionSnapshot(
        code_execution=True, artifact_writes=True,
        allowed_tools=["workspace.write", "python.execute"],
        artifact_ids=[artifact.id for artifact in artifacts],
    ), BudgetLimits(analysis_executions=2, plan_revisions=2))
    return store, run, request, artifacts


def _proposal(*, writer_role="python", code=None, inputs=None):
    parameters = {
        "code": code if code is not None else {"producer_key": "write", "port": "written_artifact"},
        "sdk_orca": False,
    }
    if inputs is not None:
        parameters["inputs"] = {"version": 1, "entries": inputs}
    return {
        "steps": [
            {"key": "write", "tool": "workspace.write", "parameters": {
                "name": "task.py", "role": writer_role, "content": SCRIPT,
            }},
            {"key": "consume", "tool": "python.execute", "parameters": parameters},
        ],
        "goal_map": {"report": {"step_key": "consume", "port": "artifacts"}},
    }


def _commit(store, run, proposal):
    plan = materialize_plan(store, run, proposal)
    run = store.commit_revision(run, plan, basis=current_basis(store, run),
                                decision_id="explicit_file_dependency")
    return run, plan


def _write(store, run, step):
    result = execute_call(store, run, step.tool, step.parameters.model_dump(mode="json"), step=step)
    assert result.operation_status == "completed", result.diagnostics
    return result


@pytest.mark.parametrize("activation", ["create", "commit"])
def test_one_plan_writer_then_python_reservation_freezes_files_and_keeps_declaration(
    tmp_path, activation,
):
    store, run, request, _ = _case(tmp_path)
    proposal = _proposal()
    original_proposal = copy.deepcopy(proposal)
    plan = materialize_plan(store, run, proposal)
    writer, consumer = plan.steps
    assert proposal == original_proposal
    assert isinstance(consumer.parameters.code, ProducerFileRef)
    assert consumer.parameters.code.producer_step_id == writer.id
    assert consumer.parameters.code.port == "written_artifact"
    assert consumer.depends_on == [writer.id]
    assert not run.calls and not run.attempts
    if activation == "create":
        run = store.create_run(request, plan, run.permission, run.budget)
        plan_path = store.path(f"runs/{run.id}/plan-revisions/{plan.version}.json")
    else:
        run = store.commit_revision(run, plan, basis=current_basis(store, run),
                                    decision_id="write_and_consume")
        plan_path = store.path(f"runs/{run.id}/decisions/write_and_consume.json")
    frozen_plan_bytes = plan_path.read_bytes()
    result = _write(store, run, writer)
    file = result.observations["written_artifact"]
    assert not run.permission.artifact_ids
    assert not run.attempts
    attempt = store.reserve_attempt(run, consumer)
    resolved = attempt.frozen_step.parameters.code
    assert isinstance(resolved, CodeRef)
    assert (resolved.artifact_id, resolved.sha256, resolved.language) == (
        file["artifact_id"], file["sha256"], "python",
    )
    assert attempt.consumption["code"] == resolved.model_dump(mode="json")
    assert attempt.consumption["_declared_step"] == consumer.model_dump(mode="json")
    assert attempt.consumption["_file_sources"]["code"] == {
        "declaration": consumer.parameters.code.model_dump(mode="json"),
        "artifact_id": file["artifact_id"], "sha256": file["sha256"],
        "run_id": run.id, "result_id": result.id, "result_fingerprint": fingerprint(result),
    }
    assert isinstance(store.load_plan(run).steps[-1].parameters.code, ProducerFileRef)
    assert plan_path.read_bytes() == frozen_plan_bytes
    assert store.artifact_path(resolved.artifact_id).read_text(encoding="utf-8") == SCRIPT
    assert attempt.execution_kind == "python" and attempt.state == "intent"
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0
    assert not run.internal_calls


def test_pending_producer_cannot_reserve_or_acquire_environment_lease(tmp_path):
    store, run, _, _ = _case(tmp_path)
    run, plan = _commit(store, run, _proposal())
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError)):
        store.reserve_attempt(run, plan.steps[-1])
    assert run.model_dump(mode="json") == before
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert not run.calls and not run.attempts
    assert store.environment_lease() is None


def test_legacy_policy_rejects_future_file_reference_without_changing_run(tmp_path):
    store, run, _, _ = _case(tmp_path, policy=None)
    assert "workflow_policy" not in run.configuration.model_dump(mode="json")
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    # Current host settings cannot grant this historical Run the new reference contract.
    store.configuration = RunConfiguration(workflow_policy="simplified-1")
    with pytest.raises((ValueError, StoreError)):
        plan = materialize_plan(store, run, _proposal())
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="legacy_future")
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert store.load_plan(run) is None and not run.calls and not run.attempts


def test_concrete_artifact_shorthand_derives_code_and_manifest_hashes(tmp_path):
    store, run, _, artifacts = _case(tmp_path, external_files=[
        ("analysis.py", SCRIPT.encode(), "code"),
        ("data.json", b'{"value":"fixture"}\n', "task_text"),
    ])
    code, data = artifacts
    proposal = {
        "steps": [{"key": "consume", "tool": "python.execute", "parameters": {
            "code": {"artifact_id": code.id}, "sdk_orca": False,
            "inputs": {"version": 1, "entries": [{
                "artifact_id": data.id, "target_path": "data.json", "purpose": "explicit input data",
            }]},
        }}],
        "goal_map": {"report": {"step_key": "consume", "port": "artifacts"}},
    }
    run, plan = _commit(store, run, proposal)
    consumer = plan.steps[0]
    assert isinstance(consumer.parameters.code, CodeRef)
    assert consumer.parameters.code.sha256 == code.sha256
    assert consumer.parameters.code.language == "python"
    entry = consumer.parameters.inputs.entries[0]
    assert isinstance(entry, InputManifestEntry) and entry.sha256 == data.sha256
    attempt = store.reserve_attempt(run, consumer)
    assert attempt.consumption["inputs"]["entries"][0]["sha256"] == data.sha256
    assert attempt.execution_kind == "python" and run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("code", [
    {"producer_key": "write", "port": "written_artifact", "language": "orca"},
    {"producer_key": "missing", "port": "written_artifact"},
    {"producer_key": "write", "port": "resolved_identity"},
    {"producer_key": "write", "port": "prepared_geometry"},
])
def test_invalid_producer_language_identity_and_port_are_rejected_before_activation(tmp_path, code):
    store, run, _, _ = _case(tmp_path)
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError)):
        plan = materialize_plan(store, run, _proposal(code=code))
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="invalid_source")
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert store.load_plan(run) is None and not run.calls and not run.attempts


@pytest.mark.parametrize("writer_role", ["orca", "text"])
def test_pending_writer_role_cannot_be_relabelled_as_python(tmp_path, writer_role):
    store, run, _, _ = _case(tmp_path)
    with pytest.raises((ValueError, StoreError)):
        plan = materialize_plan(store, run, _proposal(writer_role=writer_role))
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="wrong_role")
    assert store.load_plan(run) is None and not run.attempts and not run.calls


def test_exact_missing_file_key_does_not_fall_back_to_the_only_written_file(tmp_path):
    from orca_agent.file_references import resolve_step_files

    store, run, _, _ = _case(tmp_path)
    run, plan = _commit(store, run, _proposal())
    result = _write(store, run, plan.steps[0])
    consumer = plan.steps[-1]
    invalid = consumer.model_copy(deep=True)
    invalid.parameters.code = ProducerFileRef(
        producer_step_id=plan.steps[0].id, file_key="missing.py",
    )
    invalid_plan = plan.model_copy(deep=True, update={"steps": [plan.steps[0], invalid]})
    with pytest.raises((ValueError, StoreError)):
        resolve_step_files(store, run, invalid, plan=invalid_plan)
    assert run.selected_results[plan.steps[0].id] == result.id
    assert not run.attempts


def test_file_reference_rechecks_artifact_bytes_before_reservation(tmp_path):
    store, run, _, _ = _case(tmp_path)
    run, plan = _commit(store, run, _proposal())
    result = _write(store, run, plan.steps[0])
    file = result.observations["written_artifact"]
    source = store.artifact_path(file["artifact_id"])
    source.write_text("print('changed after archival')\n", encoding="utf-8")
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError), match="hash|changed|pinned"):
        store.reserve_attempt(run, plan.steps[-1])
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert not run.attempts and store.environment_lease() is None


def test_explicit_wrong_concrete_hash_is_not_replaced_by_inferred_hash(tmp_path):
    store, run, _, artifacts = _case(tmp_path, external_files=[
        ("analysis.py", SCRIPT.encode(), "code"),
    ])
    proposal = _proposal(code={
        "artifact_id": artifacts[0].id, "sha256": "0" * 64, "language": "python",
    })
    with pytest.raises((ValueError, StoreError), match="hash|pinned"):
        plan = materialize_plan(store, run, proposal)
        store.commit_revision(run, plan, basis=current_basis(store, run), decision_id="wrong_hash")
    assert store.load_plan(run) is None and not run.attempts and not run.calls


def test_identity_json_cannot_supply_a_qualified_prepared_geometry_port(tmp_path):
    from orca_agent.file_references import resolve_step_files

    store, run, _, _ = _case(tmp_path)
    proposal = _proposal()
    proposal["steps"].insert(1, {
        "key": "identity_json", "tool": "workspace.write", "parameters": {
            "name": "identity.json", "role": "text", "content": '{"identity":"CH2O"}\n',
        },
    })
    run, plan = _commit(store, run, proposal)
    identity_writer = plan.steps[1]
    _write(store, run, plan.steps[0])
    _write(store, run, identity_writer)
    consumer = plan.steps[-1].model_copy(deep=True)
    consumer.depends_on.append(identity_writer.id)
    consumer.parameters.inputs.entries = [ProducerInputEntry(
        producer_step_id=identity_writer.id, port="prepared_geometry",
        target_path="initial.xyz", purpose="qualified initial geometry",
    )]
    candidate = plan.model_copy(deep=True, update={"steps": [*plan.steps[:-1], consumer]})
    with pytest.raises((ValueError, StoreError)):
        resolve_step_files(store, run, consumer, plan=candidate)
    assert not run.attempts and store.environment_lease() is None


def _managed_result_case(tmp_path, *, file_key="report.json", foreign_attempt=False,
                          collection_complete=True, archived_key="report.json"):
    """Persist synthetic managed evidence through Store; never start its reserved task."""
    store, run, _, _ = _case(tmp_path)
    proposal = _proposal()
    proposal["steps"][1]["key"] = "produce"
    proposal["steps"].append({
        "key": "consume", "tool": "python.execute", "parameters": {
            "code": {"producer_key": "write", "port": "written_artifact"},
            "sdk_orca": False, "inputs": {"version": 1, "entries": [{
                "producer_key": "produce", "file_key": file_key,
                "target_path": "evidence.json", "purpose": "explicit archived producer file",
            }]},
        },
    })
    run, plan = _commit(store, run, proposal)
    _write(store, run, plan.steps[0])
    producer = plan.steps[1]
    attempt = store.reserve_attempt(run, producer)
    output = tmp_path / "synthetic-report.json"
    output.write_bytes(b'{"fixture":"synthetic archived evidence, not computed"}\n')
    artifact = store.import_artifact(output, "raw_evidence", run_id=run.id, attempt_id=attempt.id)
    result = Result(
        run_id=run.id, attempt_id=attempt.id, step_id=producer.id, operation_status="completed",
        artifact_ids=[artifact.id], observations={"evidence_collection_complete": collection_complete},
        source={"synthetic_fixture": True, "files": {
            archived_key: {"artifact_id": artifact.id, "sha256": artifact.sha256},
        }},
    )
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         started=False, termination_confirmed=True)
    assert not attempt.started and store.environment_lease() is None
    if foreign_attempt:
        # Store already rejects foreign provenance at save_result. Corrupt the
        # archived metadata afterwards to test the separate consumption check.
        altered = artifact.model_dump(mode="json")
        altered["attempt_id"] = "foreign_attempt"
        store.path(f"artifacts/{artifact.id}/artifact.json").write_text(
            json.dumps(altered), encoding="utf-8",
        )
    return store, run, plan, result, artifact


def test_exact_managed_file_key_freezes_manifest_with_archived_result_provenance(tmp_path):
    store, run, plan, result, artifact = _managed_result_case(tmp_path)
    consumer = plan.steps[-1]
    assert set(consumer.depends_on) == {plan.steps[0].id, plan.steps[1].id}
    attempt = store.reserve_attempt(run, consumer)
    entry = attempt.frozen_step.parameters.inputs.entries[0]
    assert isinstance(entry, InputManifestEntry)
    assert (entry.artifact_id, entry.sha256, entry.target_path) == (
        artifact.id, artifact.sha256, "evidence.json",
    )
    provenance = attempt.consumption["_file_sources"]["inputs/0"]
    assert provenance == {
        "declaration": consumer.parameters.inputs.entries[0].model_dump(mode="json"),
        "artifact_id": artifact.id, "sha256": artifact.sha256, "run_id": run.id,
        "result_id": result.id, "result_fingerprint": fingerprint(result),
    }
    assert all(not item.started for item in run.attempts)
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0


def test_t2_task_relative_selected_file_freezes_the_original_archived_artifact(tmp_path):
    store, run, plan, result, artifact = _managed_result_case(
        tmp_path, file_key="selected.json", archived_key="task/selected.json")
    attempt = store.reserve_attempt(run, plan.steps[-1])
    entry = attempt.frozen_step.parameters.inputs.entries[0]
    assert (entry.artifact_id, entry.sha256) == (artifact.id, artifact.sha256)
    source = attempt.consumption["_file_sources"]["inputs/0"]
    assert source["declaration"]["file_key"] == "selected.json"
    assert source["resolved_file_key"] == "task/selected.json"
    assert source["result_id"] == result.id
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0


@pytest.mark.parametrize("key", ["../selected.json", "/selected.json", "task/../selected.json", "C:/selected.json"])
def test_producer_file_path_rejection_is_correctable_without_echoing_host_paths(key):
    from orca_agent.file_references import FileReferenceError, _selected_file

    result = Result(run_id="run", operation_status="completed", source={"files": {}})
    reference = ProducerFileRef(producer_step_id="producer", file_key=key)
    with pytest.raises(FileReferenceError) as raised:
        _selected_file(result, reference)
    assert raised.value.detail["code"] == "invalid_producer_file_key"
    assert "requested_file_key" not in raised.value.detail


def test_alias_ambiguity_does_not_pick_a_different_artifact_and_exact_key_resolves():
    from orca_agent.file_references import FileReferenceError, _selected_file

    result = Result(run_id="run", operation_status="completed", source={"files": {
        "selected.json": {"artifact_id": "first", "sha256": "a" * 64},
        "task/selected.json": {"artifact_id": "second", "sha256": "b" * 64},
        "internal/child/task/selected.json": {"artifact_id": "third", "sha256": "c" * 64},
    }})
    with pytest.raises(FileReferenceError) as raised:
        _selected_file(result, ProducerFileRef(producer_step_id="producer", file_key="selected.json"))
    assert raised.value.detail["code"] == "producer_file_ambiguous"
    key, value = _selected_file(result, ProducerFileRef(producer_step_id="producer", file_key="task/selected.json"))
    assert key == "task/selected.json" and value["artifact_id"] == "second"


def test_missing_alias_does_not_search_nested_or_internal_basenames():
    from orca_agent.file_references import FileReferenceError, _selected_file

    result = Result(run_id="run", operation_status="completed", source={"files": {
        "task/nested/selected.json": {"artifact_id": "nested", "sha256": "a" * 64},
        "internal/child/task/selected.json": {"artifact_id": "internal", "sha256": "b" * 64},
    }})
    with pytest.raises(FileReferenceError) as raised:
        _selected_file(result, ProducerFileRef(producer_step_id="producer", file_key="selected.json"))
    assert raised.value.detail["code"] == "producer_file_missing"


def test_task_relative_alias_keeps_hash_failures_hard(tmp_path):
    from orca_agent.file_references import FileReferenceError

    store, run, plan, _, artifact = _managed_result_case(
        tmp_path, file_key="selected.json", archived_key="task/selected.json")
    store.artifact_path(artifact.id).write_bytes(b"changed")
    with pytest.raises((ValueError, StoreError)) as raised:
        store.reserve_attempt(run, plan.steps[-1])
    assert not isinstance(raised.value, FileReferenceError)
    assert len(run.attempts) == 1 and store.environment_lease() is None


@pytest.mark.parametrize("problem,message", [
    ("missing_key", "does not exist"),
    ("foreign_attempt", "producing Attempt|bound.*Attempt"),
    ("tampered_bytes", "hash|changed|pinned"),
    ("incomplete_collection", "complete archived"),
])
def test_managed_file_key_rejects_missing_foreign_tampered_or_incomplete_evidence(
    tmp_path, problem, message,
):
    store, run, plan, _, artifact = _managed_result_case(
        tmp_path, file_key="missing.json" if problem == "missing_key" else "report.json",
        foreign_attempt=problem == "foreign_attempt",
        collection_complete=problem != "incomplete_collection",
    )
    if problem == "tampered_bytes":
        store.artifact_path(artifact.id).write_bytes(b'{"fixture":"changed"}\n')
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError), match=message):
        store.reserve_attempt(run, plan.steps[-1])
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert len(run.attempts) == 1 and not run.attempts[0].started
    assert store.environment_lease() is None


def _prepared_geometry_case(tmp_path, monkeypatch):
    from tests.unit.test_general_identity_selection import pubchem
    from tests.unit.test_simple_name_resolution import mock_http, name_run
    from tests.unit.test_structure_tools import mock_generator

    store, seed, request, structure_plan = name_run(tmp_path, monkeypatch, "oxidane", "water")
    resolve, prepare = structure_plan.steps
    writer = Step(id="writer", logical_id="writer", tool="workspace.write", parameters={
        "name": "read_geometry.py", "role": "python", "content": SCRIPT,
    })
    consumer = Step(
        id="geometry_consumer", logical_id="geometry_consumer", tool="python.execute",
        system_id=prepare.system_id, depends_on=[writer.id, prepare.id], parameters={
            "code": {"producer_step_id": writer.id, "port": "written_artifact"},
            "sdk_orca": False, "inputs": {"version": 1, "entries": [{
                "producer_step_id": prepare.id, "port": "prepared_geometry",
                "target_path": "initial.xyz", "purpose": "qualified prepared initial geometry",
            }]},
        },
    )
    plan = structure_plan.model_copy(update={"steps": [resolve, prepare, writer, consumer]})
    permission = seed.permission.model_copy(update={
        "code_execution": True,
        "allowed_tools": [*seed.permission.allowed_tools, "workspace.write", "python.execute"],
    })
    limits = seed.budget.model_copy(update={"analysis_executions": 2})
    run = store.create_run(
        request, plan, permission, limits,
        configuration=seed.configuration.model_copy(update={"workflow_policy": "simplified-1"}),
    )
    requests = mock_http(monkeypatch, [(200, pubchem("O", "H2O"))])
    generators = mock_generator(monkeypatch)
    identity = execute_call(store, run, resolve.tool, resolve.parameters.model_dump(), step=resolve)
    assert identity.operation_status == "completed", identity.diagnostics
    geometry = execute_call(
        store, run, prepare.tool, prepare.parameters.model_dump(),
        step=prepare, results={resolve.id: identity},
    )
    assert geometry.operation_status == "completed", geometry.diagnostics
    _write(store, run, writer)
    assert len(requests) == len(generators) == 1
    return store, run, plan, identity, geometry


def test_prepared_geometry_manifest_uses_exact_qualified_output_in_real_mock_dispatch_chain(
    tmp_path, monkeypatch,
):
    store, run, plan, identity, geometry = _prepared_geometry_case(tmp_path, monkeypatch)
    consumer = plan.steps[-1]
    identity_id = identity.qualified_outputs["resolved_identity"].artifact_id
    geometry_id = geometry.qualified_outputs["prepared_geometry"].artifact_id
    assert identity_id != geometry_id
    attempt = store.reserve_attempt(run, consumer)
    entry = attempt.frozen_step.parameters.inputs.entries[0]
    assert entry.artifact_id == geometry_id and entry.artifact_id != identity_id
    assert entry.sha256 == store.load_artifact(geometry_id).sha256
    assert attempt.consumption["_file_sources"]["inputs/0"]["result_id"] == geometry.id
    assert isinstance(store.load_plan(run).steps[-1].parameters.inputs.entries[0], ProducerInputEntry)
    assert not attempt.started and run.usage.orca_starts_actual == 0


def test_prepared_geometry_reference_cannot_cross_system_scope(tmp_path, monkeypatch):
    from orca_agent.file_references import resolve_step_files

    store, run, plan, _, _ = _prepared_geometry_case(tmp_path, monkeypatch)
    consumer = plan.steps[-1].model_copy(deep=True, update={"system_id": "other_system"})
    candidate = plan.model_copy(deep=True, update={"steps": [*plan.steps[:-1], consumer]})
    before = run.model_dump(mode="json")
    with pytest.raises((ValueError, StoreError), match="same explicit system"):
        resolve_step_files(store, run, consumer, plan=candidate)
    assert run.model_dump(mode="json") == before and not run.attempts
    assert store.environment_lease() is None
