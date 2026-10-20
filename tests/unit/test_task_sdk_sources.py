"""Explicit SDK source declarations; real archive hashes, synthetic native receipts."""

import copy
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from orca_agent import task_sdk
from orca_agent.models import NativeInputPolicy
from orca_agent.task_observations import collect
from orca_agent.tools.managed import _archive
from tests.unit.test_task_observation_conditions import archived_case


def response():
    return {"request_key": "native", "input_fingerprint": "d" * 64,
        "operation_status": "completed", "scientific_validation": "unverified",
        "actual_conditions": {"method": "HF", "environment": "gas_phase"},
        "source_attempt_id": "attempt_original",
        "files": {"stdout.out": "/task/responses/files-000001/stdout.out",
                  "job.inp": "/task/responses/files-000001/job.inp"}}


@pytest.mark.parametrize("status", ["completed", "failed", "timed_out", "cancelled"])
def test_receipt_source_declares_only_explicit_original_filename_without_mutation(status):
    receipt = response()
    receipt["operation_status"] = status
    before = copy.deepcopy(receipt)
    source = task_sdk.receipt_source(receipt, "stdout.out", location="lines 10-12")
    assert source == {"internal_request_key": "native", "filename": "stdout.out", "location": "lines 10-12"}
    assert task_sdk.receipt_source(receipt, "stdout.out") == {
        "internal_request_key": "native", "filename": "stdout.out"}
    assert receipt == before
    assert "job.inp" not in source.values()


@pytest.mark.parametrize("change", [
    {"request_key": None}, {"request_key": ""}, {"request_key": "../native"},
    {"request_key": "native\n"}, {"request_key": "n" * 97}, {"request_key": []},
    {"operation_status": "running"}, {"operation_status": "reserved"},
    {"operation_status": "unknown"}, {"operation_status": "passed"},
    {"operation_status": []}, {"files": None}, {"files": {"stdout.out": {"artifact_id": "a"}}},
])
def test_receipt_source_rejects_invalid_identity_state_or_non_sdk_file_mapping(change):
    with pytest.raises(ValueError):
        task_sdk.receipt_source({**response(), **change}, "stdout.out")


@pytest.mark.parametrize("filename", [
    None, 1, "", "unknown.out", "STDOUT.OUT", "../stdout.out", "task/../stdout.out",
    "/task/responses/files-000001/stdout.out", r"C:\responses\stdout.out",
])
def test_receipt_source_rejects_copy_paths_unknown_names_and_path_normalization(filename):
    with pytest.raises(ValueError):
        task_sdk.receipt_source(response(), filename)


@pytest.mark.parametrize("location", [False, [], "x" * 1001])
def test_receipt_source_rejects_location_outside_existing_manifest_contract(location):
    with pytest.raises(ValueError, match="location"):
        task_sdk.receipt_source(response(), "stdout.out", location=location)


def test_standalone_copied_sdk_imports_without_product_dependencies(tmp_path):
    # Both backends copy this standalone source; importing the copy needs no
    # product modules, installed task framework or optional dependency.
    copied = tmp_path / "orca_task_sdk.py"
    shutil.copyfile(task_sdk.__file__, copied)
    assert copied.read_bytes() == Path(task_sdk.__file__).read_bytes()
    spec = importlib.util.spec_from_file_location("offline_copied_orca_task_sdk", copied)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.receipt_source(response(), "stdout.out") == {
        "internal_request_key": "native", "filename": "stdout.out"}


def collected(tmp_path, store, run, result, source):
    attempt = next(item for item in run.attempts if item.id == result.attempt_id)
    manifest = tmp_path / "helper-observations.json"
    manifest.write_text(json.dumps({"version": 1, "observations": [{
        "name": "frequencies", "value": [1.0], "unit": "cm^-1", "sources": [source]}]}), encoding="utf-8")
    artifact = store.import_artifact(manifest, "task_output", run_id=run.id, attempt_id=attempt.id)
    files = {"task/observations.json": {"artifact_id": artifact.id, "sha256": artifact.sha256}}
    return collect(store, run, attempt.frozen_step, attempt, files)


def archived_response(store, run):
    receipt = run.internal_calls[0]
    return {**response(), "request_key": receipt.request_key, "input_fingerprint": receipt.input_fingerprint,
        "source_attempt_id": receipt.parent_attempt_id, "operation_status": receipt.state,
        "actual_conditions": copy.deepcopy(receipt.actual_conditions),
        "files": {Path(store.load_artifact(identifier).path).name: str(store.artifact_path(identifier))
                  for identifier in receipt.artifact_ids}}


def test_source_helper_and_manual_declaration_collect_identical_original_hashes_without_extra_sources(tmp_path):
    store, run, _, _, result = archived_case(tmp_path)
    receipt = archived_response(store, run)
    unused = run.internal_calls[0].model_copy(deep=True, update={
        "request_key": "unused", "actual_conditions": {"method": "different", "charge": 1}})
    run.internal_calls.append(unused)
    before = run.model_dump(mode="json")
    records = collected(tmp_path, store, run, result, task_sdk.receipt_source(receipt, "stdout.out"))
    assert records == result.observation_records
    source, = records[0].sources
    original = store.load_artifact(run.internal_calls[0].artifact_ids[0])
    assert (source.artifact_id, source.sha256, source.attempt_id) == (
        original.id, original.sha256, run.internal_calls[0].parent_attempt_id)
    assert source.internal_request_key == "native"
    assert records[0].actual_conditions == run.internal_calls[0].actual_conditions
    assert not records[0].artifact_ids
    assert records[0].validation_status == "unverified"
    assert run.model_dump(mode="json") == before
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0


@pytest.mark.parametrize("tampering", ["unknown_receipt", "invented_filename", "unsettled_receipt"])
def test_collector_independently_rejects_declarations_from_tampered_sdk_response(tmp_path, tampering):
    store, run, _, _, result = archived_case(tmp_path)
    receipt = archived_response(store, run)
    filename = "stdout.out"
    if tampering == "unknown_receipt":
        receipt["request_key"] = "another_run_native"
    elif tampering == "invented_filename":
        filename = "invented.out"
        receipt["files"][filename] = receipt["files"]["stdout.out"]
    else:
        run.internal_calls[0].state = "running"
        # The mutable task-side copy falsely claims completion.
        assert receipt["operation_status"] == "completed"
    source = task_sdk.receipt_source(receipt, filename)
    with pytest.raises(ValueError, match="settled receipt|filename is missing or ambiguous"):
        collected(tmp_path, store, run, result, source)


def test_task_side_conditions_and_scientific_claims_cannot_override_archived_receipt(tmp_path):
    store, run, _, _, result = archived_case(tmp_path)
    receipt = archived_response(store, run)
    receipt.update(actual_conditions={"method": "fake"}, scientific_validation="passed",
                   checks=[{"status": "passed"}], status="passed")
    source = task_sdk.receipt_source(receipt, "stdout.out")
    assert set(source) == {"internal_request_key", "filename"}
    records = collected(tmp_path, store, run, result, source)
    assert records == result.observation_records
    assert records[0].validation_status == "unverified"
    assert records[0].actual_conditions == run.internal_calls[0].actual_conditions


@pytest.mark.parametrize("field", ["checks", "status", "actual_conditions"])
def test_collector_rejects_privileged_fields_added_to_source_declaration(tmp_path, field):
    store, run, _, _, result = archived_case(tmp_path)
    source = task_sdk.receipt_source(archived_response(store, run), "stdout.out")
    source[field] = {"forged": "passed"}
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        collected(tmp_path, store, run, result, source)


def pinned_source_case(tmp_path, *, names=("first/data.out", "second/data.out")):
    """Actual archive mapping, with synthetic settled receipts and no execution."""
    store, run, _, _, result = archived_case(tmp_path)
    run.configuration = run.configuration.model_copy(update={"execution":
        run.configuration.execution.model_copy(update={
            "native_input_policy": NativeInputPolicy(profile="managed-native-1")})})
    folder = tmp_path / "native-files"
    for name in names:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("explicit source: " + name + "\n", encoding="utf-8")
    files = _archive(store, run, run.attempts[0], folder, "")
    receipt = run.internal_calls[0]
    receipt.artifact_ids = [reference["artifact_id"] for reference in files.values()]
    receipt.response = {**response(), "files": files}
    task_response = {**response(), "files": {
        name: str(store.artifact_path(reference["artifact_id"])) for name, reference in files.items()}}
    return store, run, result, task_response


@pytest.mark.parametrize("names,selected", [
    (("stdout.out",), "stdout.out"),
    (("first/data.out", "second/data.out"), "first/data.out"),
    (("first/data.out", "second/data.out"), "second/data.out"),
])
def test_pinned_receipt_resolves_exact_file_keys_with_duplicate_basenames(tmp_path, names, selected):
    store, run, result, receipt = pinned_source_case(tmp_path, names=names)
    before = run.model_dump(mode="json")
    source = task_sdk.receipt_source(receipt, selected)
    records = collected(tmp_path, store, run, result, source)
    actual, = records[0].sources
    pinned = run.internal_calls[0].response["files"][selected]
    assert (actual.artifact_id, actual.sha256) == (pinned["artifact_id"], pinned["sha256"])
    assert store.load_artifact(actual.artifact_id).source["task_path"] == selected
    assert records[0].validation_status == "unverified"
    assert run.model_dump(mode="json") == before
    assert run.usage.orca_starts_actual == run.usage.model_calls == 0


@pytest.mark.parametrize("change", [
    "missing_key", "invalid_files", "invalid_reference", "hash", "not_member",
    "wrong_original_key", "duplicate_member", "altered_bytes", "wrong_parent",
])
def test_pinned_source_rejects_inconsistent_archived_identity_without_basename_fallback(tmp_path, change):
    store, run, result, receipt = pinned_source_case(tmp_path)
    original = run.internal_calls[0]
    name = "first/data.out"
    source = task_sdk.receipt_source(receipt, name)
    files = original.response["files"]
    reference = files[name]
    if change == "missing_key":
        del files[name]
    elif change == "invalid_files":
        original.response["files"] = []
    elif change == "invalid_reference":
        files[name] = receipt["files"][name]
    elif change == "hash":
        reference["sha256"] = "0" * 64
    elif change == "not_member":
        original.artifact_ids.remove(reference["artifact_id"])
    elif change == "wrong_original_key":
        files[name] = dict(files["second/data.out"])
    elif change == "duplicate_member":
        original.artifact_ids.append(reference["artifact_id"])
    elif change == "altered_bytes":
        store.artifact_path(reference["artifact_id"]).write_text("tampered", encoding="utf-8")
    else:
        path = tmp_path / "other-parent.out"
        path.write_text("different producer", encoding="utf-8")
        artifact = store.import_artifact(path, "raw_evidence", run_id=run.id,
            attempt_id="another_attempt", source={"task_path": name})
        files[name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        original.artifact_ids.append(artifact.id)
    with pytest.raises((ValueError, RuntimeError), match="internal source|original Attempt|hash"):
        collected(tmp_path, store, run, result, source)


@pytest.mark.parametrize("name", ["data.out", "FIRST/data.out", "invented/data.out"])
def test_task_side_aliases_cannot_select_an_archived_file_by_basename(tmp_path, name):
    store, run, result, receipt = pinned_source_case(tmp_path)
    receipt["files"][name] = receipt["files"]["first/data.out"]
    source = task_sdk.receipt_source(receipt, name)
    with pytest.raises(ValueError, match="exact pinned receipt reference"):
        collected(tmp_path, store, run, result, source)


def test_pre_response_flat_receipts_remain_readable_without_adding_provenance(tmp_path):
    store, run, _, _, result = archived_case(tmp_path)
    assert not run.internal_calls[0].response
    run.configuration = run.configuration.model_copy(update={"execution":
        run.configuration.execution.model_copy(update={
            "native_input_policy": NativeInputPolicy(profile="managed-native-1")})})
    source = task_sdk.receipt_source(archived_response(store, run), "stdout.out")
    assert collected(tmp_path, store, run, result, source) == result.observation_records


@pytest.mark.parametrize("name", ["first/data.out", "data.out"])
def test_pre_response_receipts_cannot_guess_nested_source_identity(tmp_path, name):
    store, run, result, receipt = pinned_source_case(tmp_path, names=("first/data.out",))
    run.internal_calls[0].response = {}
    receipt["files"]["data.out"] = receipt["files"]["first/data.out"]
    source = task_sdk.receipt_source(receipt, name)
    with pytest.raises(ValueError, match="missing or ambiguous|exact archived file key"):
        collected(tmp_path, store, run, result, source)


def test_historical_profile_does_not_upgrade_nested_source_admission(tmp_path):
    store, run, result, receipt = pinned_source_case(tmp_path)
    run.configuration = run.configuration.model_copy(update={"execution":
        run.configuration.execution.model_copy(update={"native_input_policy": NativeInputPolicy()})})
    source = task_sdk.receipt_source(receipt, "first/data.out")
    with pytest.raises(ValueError, match="missing or ambiguous"):
        collected(tmp_path, store, run, result, source)
