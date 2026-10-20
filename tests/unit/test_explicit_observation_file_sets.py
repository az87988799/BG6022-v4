"""The a005 declaration failures, without any model or scientific execution."""

import pytest

from tests.unit.test_bounded_observation_admission import manifest_case, read_records
from tests.unit.test_task_analysis_traceability import source_item


def file_set_case(tmp_path, names, *, sources=None, simplified=True, duplicate=False):
    item = source_item(artifacts=names + names if duplicate else names)
    if sources is not None:
        item["sources"] = sources
    store, run, request, goal, result, payload = manifest_case(tmp_path, [item], simplified=simplified)
    for index, name in enumerate(dict.fromkeys(names)):
        path = tmp_path / f"product-{index}.json"
        path.write_text('{"reported": 1}', encoding="utf-8")
        artifact = store.import_artifact(path, "task_output", run_id=run.id,
            attempt_id=run.attempts[0].id, source={"task_path": "task/" + name})
        result.source["files"]["task/" + name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        result.artifact_ids.append(artifact.id)
    return store, run, result, payload


def test_66_explicit_products_use_unique_names_and_frozen_run_allowance(tmp_path):
    names = [f"candidates/cand_{i:02d}.xyz" for i in range(64)] + ["summary.json", "seed.xyz"]
    store, run, result, payload = file_set_case(tmp_path, names, duplicate=True)
    run.budget.artifact_count = 66
    diagnostics = []
    record, = read_records(store, run, result, diagnostics)
    assert len(record.artifact_ids) == 66 and len(set(record.artifact_ids)) == 66
    assert record.validation_status == "unverified" and not result.qualified_outputs
    assert diagnostics[-1]["artifact_count_limit"] == 66
    assert diagnostics[-1]["rule_version"] == "bounded-observation-admission-3"
    assert diagnostics[-1]["rejected_count"] == 0
    assert store.artifact_path(diagnostics[-1]["manifest"]["artifact_id"]).read_bytes() == payload


@pytest.mark.parametrize("simplified,limit", [(False, 800), (True, 65)])
def test_legacy_64_or_frozen_run_count_still_rejects_excess(tmp_path, simplified, limit):
    names = [f"candidates/{i}.xyz" for i in range(66)]
    store, run, result, _ = file_set_case(tmp_path, names, simplified=simplified)
    run.budget.artifact_count = limit
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert ("frozen Run artifact_count" if simplified else "legacy observation artifacts exceed 64") in diagnostics[0]["detail"]


def test_directory_summary_expands_only_this_records_explicit_validated_files(tmp_path):
    names = [f"orca_results/cand_{i}.json" for i in (47, 2, 26, 5)]
    original = {"filename": "orca_results", "location": "per-candidate JSON"}
    sdk_source = {"filename": "stdout.out", "internal_request_key": "native"}
    store, run, result, payload = file_set_case(tmp_path, names, sources=[sdk_source, original])
    # A same-prefix archive entry not explicitly declared by this record is not included.
    result.source["files"]["task/orca_results/undeclared.json"] = result.source["files"]["task/" + names[0]]
    diagnostics = []
    record, = read_records(store, run, result, diagnostics)
    assert len(record.sources) == 5
    assert record.sources[0].internal_request_key == "native"
    assert [source.artifact_id for source in record.sources[1:]] == record.artifact_ids
    assert all(source.internal_request_key is None and source.location == "per-candidate JSON"
               for source in record.sources[1:])
    assert record.validation_status == "unverified"
    normalization = diagnostics[0]["normalizations"][0]
    assert normalization["original_source"] == original and normalization["source_index"] == 1
    assert normalization["expanded_count"] == 4 and normalization["expanded_preview_complete"] is True
    assert [ref["filename"] for ref in normalization["expanded_preview"]] == names
    assert diagnostics[-1]["rejected_count"] == 0
    assert store.artifact_path(diagnostics[-1]["manifest"]["artifact_id"]).read_bytes() == payload


@pytest.mark.parametrize("source", [
    {"filename": "missing"}, {"filename": "orca_result"}, {"filename": "../orca_results"},
    {"filename": "orca_results", "internal_request_key": "native"},
])
def test_missing_prefix_escape_or_sdk_source_is_never_silently_dropped(tmp_path, source):
    store, run, result, _ = file_set_case(tmp_path, ["orca_results/cand_47.json"], sources=[source])
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert diagnostics[0]["category"] == "observation_manifest_rejected"
    assert diagnostics[-1]["rejected_count"] == 1
    assert not any(row["category"] == "observation_sources_normalized" for row in diagnostics)


@pytest.mark.parametrize("damage", ["missing", "hash", "foreign_attempt"])
def test_directory_expansion_requires_every_explicit_archive_binding(tmp_path, monkeypatch, damage):
    name = "orca_results/cand_47.json"
    store, run, result, _ = file_set_case(tmp_path, [name], sources=[{"filename": "orca_results"}])
    reference = result.source["files"]["task/" + name]
    if damage == "missing":
        del result.source["files"]["task/" + name]
    elif damage == "hash":
        reference["sha256"] = "f" * 64
    else:
        original = store.load_artifact

        def load(identifier):
            artifact = original(identifier)
            return (artifact.model_copy(update={"attempt_id": "another-attempt"})
                    if identifier == reference["artifact_id"] else artifact)

        monkeypatch.setattr(store, "load_artifact", load)
    diagnostics = []
    assert read_records(store, run, result, diagnostics) == []
    assert {"missing": "was not archived", "hash": "archive hash differs",
            "foreign_attempt": "different Attempt"}[damage] in diagnostics[0]["detail"]
    assert not result.qualified_outputs
