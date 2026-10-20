"""Real staging and archived hashes; synthetic output, no execution or model."""

import json
from types import SimpleNamespace

import pytest

from orca_agent.models import Attempt, InputManifest, InputManifestEntry, Result, Step, utc_now
from orca_agent.orca.native import prepare_native_input
from orca_agent.task_observations import _direct_native_source, assess
from tests.unit.test_general_observation_admission import direct_source_case, general_case
from tests.unit.test_task_analysis_traceability import declaration, source_item


def materialized_case(tmp_path, *, explicit_controls=False):
    store, run, request, goal, result = general_case(tmp_path)
    run.configuration = run.configuration.model_copy(update={"workflow_policy": "simplified-1"})
    original = run.internal_calls.pop()
    path = tmp_path / "original.inp"
    text = "! HF 6-31G* SP\n* xyzfile 0 1 initial.xyz\n"
    if explicit_controls:
        text = ("! HF 6-31G* RHF SP NoAutoStart\n%maxcore 192\n%pal nprocs 4 end\n"
                "%scf MaxIter 100 AutoTRAH false end\n* xyzfile 0 1 initial.xyz\n")
    path.write_text(text, encoding="ascii")
    code = store.import_artifact(path, "native_input")
    direct = Step(id="direct", logical_id="direct", tool="orca.run", system_id="molecule",
        parameters={"input": {"artifact_id": code.id, "sha256": code.sha256, "language": "orca"},
                    "inputs": original.inputs.model_dump()})
    binding = {**original.conditions["input_binding"], "step_id": direct.id,
               "workflow_policy": "simplified-1"}
    prepared = prepare_native_input(tmp_path / "staged", direct.parameters, store.artifact_path,
        run.configuration.resources, policy=run.configuration.execution.native_input_policy,
        binding=binding)
    workload = {name: prepared[name] for name in (
        "admission_version", "native_input_policy", "input_sha256", "input_binding", "execution_impact")}
    parent = Attempt(step_id=direct.id, logical_id=direct.logical_id, number=1, tool="orca.run",
        state="completed", execution_kind="orca", input_fingerprint="e" * 64,
        directory="synthetic-direct", request_version=request.version, frozen_step=direct,
        finished_at=utc_now(), native_workload=workload)
    run.attempts.append(parent)
    executed = store.import_artifact(tmp_path / "staged/job.inp", "raw_evidence",
        run_id=run.id, attempt_id=parent.id, source={"task_path": "task/job.inp"})
    path = tmp_path / "direct.out"
    path.write_text("Synthetic unit-test output; no ORCA process was started.\n", encoding="ascii")
    raw = store.import_artifact(path, "raw_evidence", run_id=run.id, attempt_id=parent.id,
        source={"task_path": "task/stdout.out"})
    path = tmp_path / "prepared.json"
    path.write_text(json.dumps(prepared), encoding="utf-8")
    record = store.import_artifact(path, "execution_record", run_id=run.id, attempt_id=parent.id)
    source = Result(run_id=run.id, step_id=direct.id, attempt_id=parent.id,
        operation_status="completed", observations={"evidence_collection_complete": True},
        artifact_ids=[raw.id, record.id, executed.id], source={"files": {
            "prepared.json": {"artifact_id": record.id, "sha256": record.sha256},
            "task/job.inp": {"artifact_id": executed.id, "sha256": executed.sha256},
            "task/stdout.out": {"artifact_id": raw.id, "sha256": raw.sha256}}})
    parent.result_id = source.id
    run.result_ids.append(source.id)
    store._write_json(f"runs/{run.id}/results/{source.id}.json", source.model_dump(mode="json"),
                      immutable=True)
    run.permission.artifact_ids.append(raw.id)
    run.attempts[0].frozen_step.parameters.inputs = InputManifest(entries=[InputManifestEntry(
        artifact_id=raw.id, sha256=raw.sha256, target_path="direct.out", purpose="previous native output")])
    declaration(store, run, result, tmp_path, [source_item(sources=[{"filename": "direct.out"}])])
    return SimpleNamespace(store=store, run=run, request=request, goal=goal, result=result,
        parent=parent, prepared=prepared, record=record, code=code, executed=executed, raw=raw, source=source)


@pytest.mark.parametrize("explicit_controls", [False, True])
def test_direct_input_hashes_and_observation_sources_remain_distinct_and_unqualified(tmp_path, explicit_controls):
    case = materialized_case(tmp_path, explicit_controls=explicit_controls)
    before = case.run.model_dump(mode="json")
    if explicit_controls:
        assert case.prepared["input_sha256"] == case.code.sha256
        assert "source_input_sha256" not in case.prepared
    else:
        assert case.prepared["source_input_sha256"] == case.code.sha256
        assert case.prepared["input_sha256"] == case.executed.sha256 != case.code.sha256
    answer = assess(case.store, case.run, case.request, case.goal, case.result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False and not case.result.qualified_outputs
    source, = case.result.observation_records[0].sources
    assert (source.artifact_id, source.sha256, source.attempt_id) == (
        case.raw.id, case.raw.sha256, case.parent.id)
    assert source.actual_conditions["native_attempt_id"] == case.parent.id
    assert case.run.model_dump(mode="json") == before
    assert case.run.usage.orca_starts_actual == 0 and case.run.usage.model_calls == 0


@pytest.mark.parametrize("change", [
    "source_bytes", "executed_bytes", "source_hash", "executed_hash", "result_hash",
    "missing_job", "result_membership", "duplicate_membership", "foreign_run",
    "foreign_attempt", "wrong_role", "wrong_file_key",
])
def test_materialized_direct_source_rejects_each_broken_archive_link(tmp_path, monkeypatch, change):
    case = materialized_case(tmp_path)
    store, run = case.store, case.run
    if change in {"source_bytes", "executed_bytes"}:
        artifact = case.code if change == "source_bytes" else case.executed
        store.artifact_path(artifact.id).write_bytes(b"tampered original archive\n")
    elif change in {"source_hash", "executed_hash"}:
        case.prepared["source_input_sha256" if change == "source_hash" else "input_sha256"] = "0" * 64
        if change == "executed_hash":
            # Even a matching workload cannot substitute for the archived execution bytes.
            case.parent.native_workload["input_sha256"] = "0" * 64
        path = tmp_path / "changed-prepared.json"
        path.write_text(json.dumps(case.prepared), encoding="utf-8")
        record = store.import_artifact(path, "execution_record", run_id=run.id,
                                      attempt_id=case.parent.id)
        case.source.artifact_ids.append(record.id)
        case.source.source["files"]["prepared.json"] = {"artifact_id": record.id, "sha256": record.sha256}
    elif change == "result_hash":
        case.source.source["files"]["task/job.inp"]["sha256"] = "0" * 64
    elif change == "missing_job":
        del case.source.source["files"]["task/job.inp"]
    elif change == "result_membership":
        case.source.artifact_ids.remove(case.executed.id)
    elif change == "duplicate_membership":
        case.source.artifact_ids.append(case.executed.id)
    else:
        changes = {
            "foreign_run": {"run_id": "other"},
            "foreign_attempt": {"attempt_id": "other"},
            "wrong_role": {"role": "execution_record"},
            "wrong_file_key": {"source": {"task_path": "task/other.inp"}},
        }
        original_load = store.load_artifact
        monkeypatch.setattr(store, "load_artifact", lambda identifier: (
            case.executed.model_copy(update=changes[change]) if identifier == case.executed.id
            else original_load(identifier)))
    original_result = store.load_result
    monkeypatch.setattr(store, "load_result", lambda run_id, result_id: (
        case.source if result_id == case.source.id else original_result(run_id, result_id)))
    with pytest.raises((ValueError, RuntimeError, KeyError), match="hash|input|binding|artifact_id"):
        _direct_native_source(store, run, case.raw)
    answer = assess(store, run, case.request, case.goal, case.result)
    assert answer["status"] == "unresolved" and answer["scientific_qualification"] is False


def test_historical_source_neither_requires_execution_archive_nor_accepts_a_different_source_hash(tmp_path):
    store, run, request, goal, result, parent, _ = direct_source_case(tmp_path)
    assert store.run_configuration(run).workflow_policy is None
    source = store.load_result(run.id, parent.result_id)
    assert "task/job.inp" not in source.source["files"]
    assert assess(store, run, request, goal, result)["status"] == "passed"
    parent.frozen_step.parameters.input.sha256 = "0" * 64
    assert assess(store, run, request, goal, result)["status"] == "unresolved"


def test_materialized_archive_is_not_reinterpreted_under_historical_policy(tmp_path):
    case = materialized_case(tmp_path)
    case.run.configuration = case.run.configuration.model_copy(update={"workflow_policy": None})
    answer = assess(case.store, case.run, case.request, case.goal, case.result)
    assert answer["status"] == "unresolved" and answer["scientific_qualification"] is False
