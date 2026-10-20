"""a011 exact task archive key admission, without running task code."""

from tests.unit.test_bounded_observation_admission import read_records
from tests.unit.test_explicit_observation_file_sets import file_set_case
from tests.unit.test_task_analysis_traceability import declaration, source_item


def test_exact_task_key_retains_original_source_and_current_producer(tmp_path):
    original = {"filename": "task/job.property.txt", "location": "total tensor rows"}
    store, run, result, payload = file_set_case(tmp_path, ["job.property.txt"], sources=[original])
    diagnostics = []
    record, = read_records(store, run, result, diagnostics)
    source, = record.sources
    reference = result.source["files"]["task/job.property.txt"]
    assert (source.artifact_id, source.sha256) == (reference["artifact_id"], reference["sha256"])
    assert source.attempt_id == run.attempts[0].id and source.location == original["location"]
    normalization = diagnostics[0]["normalizations"][0]
    assert normalization["original_source"] == original
    assert normalization["resolved_file_key"] == "task/job.property.txt"
    assert normalization["resolved_target_path"] == "job.property.txt"
    assert record.actual_conditions == {} and record.validation_status == "unverified"
    assert store.artifact_path(diagnostics[-1]["manifest"]["artifact_id"]).read_bytes() == payload


def test_exact_key_cannot_hide_an_ambiguous_relative_task_path(tmp_path):
    store, run, result, _ = file_set_case(tmp_path, ["job.property.txt", "task/job.property.txt"],
        sources=[{"filename": "task/job.property.txt"}])
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert "file key is ambiguous" in diagnostics[0]["detail"]
    assert not result.qualified_outputs


def test_exact_key_cannot_bypass_missing_hash_or_producer_validation(tmp_path, monkeypatch):
    for damage in ("missing", "hash", "foreign_attempt"):
        directory = tmp_path / damage
        directory.mkdir()
        store, run, result, _ = file_set_case(directory, [],
            sources=[{"filename": "task/job.property.txt"}])
        path = directory / "job.property.txt"
        path.write_text("inert archived tensor fixture", encoding="utf-8")
        artifact = store.import_artifact(path, "task_output", run_id=run.id,
                                        attempt_id=run.attempts[0].id)
        reference = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        if damage != "missing":
            result.source["files"]["task/job.property.txt"] = reference
        if damage == "hash":
            reference["sha256"] = "f" * 64
        with monkeypatch.context() as scoped:
            original_load = store.load_artifact
            if damage == "foreign_attempt":
                def load(identifier):
                    value = original_load(identifier)
                    return value.model_copy(update={"attempt_id": "foreign-attempt"}) if identifier == artifact.id else value
                scoped.setattr(store, "load_artifact", load)
            diagnostics = []
            assert read_records(store, run, result, diagnostics) == []
            assert {"missing": "was not archived", "hash": "archive hash differs",
                    "foreign_attempt": "different Attempt"}[damage] in diagnostics[0]["detail"]
        assert not result.qualified_outputs


def test_prefixed_declared_products_use_the_same_key_and_directory_scope(tmp_path):
    store, run, result, _ = file_set_case(tmp_path, ["data/table.csv"],
        sources=[{"filename": "data", "location": "declared result directory"}])
    declaration(store, run, result, tmp_path, [source_item(
        artifacts=["task/data/table.csv", "data/table.csv"],
        sources=[{"filename": "data", "location": "declared result directory"}])])
    diagnostics = []
    record, = read_records(store, run, result, diagnostics)
    assert record.artifact_ids == [result.source["files"]["task/data/table.csv"]["artifact_id"]]
    assert record.sources[0].artifact_id == record.artifact_ids[0]
    normalization = diagnostics[0]["normalizations"][0]
    assert normalization["original_artifact"] == "task/data/table.csv"
    assert normalization["resolved_target_path"] == "data/table.csv"
    assert diagnostics[0]["normalizations"][1]["expanded_preview"][0]["filename"] == "data/table.csv"
    assert record.validation_status == "unverified"


def test_prefixed_declared_product_rejects_ambiguous_archive_keys(tmp_path):
    store, run, result, _ = file_set_case(tmp_path, ["table.csv", "task/table.csv"],
        sources=[{"filename": "table.csv"}])
    declaration(store, run, result, tmp_path, [source_item(
        artifacts=["task/table.csv"], sources=[{"filename": "table.csv"}])])
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert "file key is ambiguous" in diagnostics[0]["detail"]
