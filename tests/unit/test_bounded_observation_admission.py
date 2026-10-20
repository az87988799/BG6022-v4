"""Bounded admission of T2-shaped archives; no model or scientific execution."""

import json

import pytest

from orca_agent import context
from orca_agent.task_observations import assess, collect
from tests.unit.test_task_analysis_traceability import source_item
from tests.unit.test_task_observation_conditions import archived_case


def manifest_case(tmp_path, items, *, simplified=True):
    store, run, request, goal, result = archived_case(tmp_path)
    if simplified:
        run.configuration = run.configuration.model_copy(update={"workflow_policy": "simplified-1"})
    attempt = run.attempts[0]
    attempt.frozen_step.parameters.observation_names = [item["name"] for item in items]
    path = tmp_path / "large-observations.json"
    payload = json.dumps({"version": 1, "observations": items}).encode("utf-8")
    path.write_bytes(payload)
    artifact = store.import_artifact(path, "task_output", run_id=run.id, attempt_id=attempt.id)
    result.source["files"]["task/observations.json"] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    result.artifact_ids.append(artifact.id)
    return store, run, request, goal, result, payload


def read_records(store, run, result, diagnostics):
    attempt = run.attempts[0]
    return collect(store, run, attempt.frozen_step, attempt, result.source["files"], diagnostics=diagnostics)


def test_large_t2_member_table_retains_all_records_and_bounded_queryable_preview(tmp_path):
    members = [{"id": i, "coordinates": [[0.0, 0.0, 0.74]] * 100} for i in range(48)]
    items = [source_item(name="sampling", value={"generated": 48, "selected": 8}),
             source_item(name="sampling_check", value={"pairs": 28, "threshold": 0.15}),
             source_item(name="member_table", value=members)]
    store, run, _, _, result, payload = manifest_case(tmp_path, items)
    assert len(payload) > 65536
    diagnostics = []
    result.observation_records = read_records(store, run, result, diagnostics)
    assert [row.name for row in result.observation_records] == [item["name"] for item in items]
    assert result.observation_records[2].value == members
    assert all(row.validation_status == "unverified" for row in result.observation_records)
    assert not result.qualified_outputs and run.usage.orca_starts_actual == run.usage.model_calls == 0
    summary, = diagnostics
    assert summary["category"] == "observation_admission" and summary["rejected_count"] == 0
    assert summary["rule_version"] == "bounded-observation-admission-3"
    assert summary["accepted_indices"] == [0, 1, 2]
    assert summary["accepted_record_bytes"] <= summary["total_record_bytes_limit"] == 1024 * 1024
    assert summary["manifest_bytes_limit"] == 8 * 1024 * 1024
    assert store.artifact_path(summary["manifest"]["artifact_id"]).read_bytes() == payload
    before = result.model_copy(deep=True)
    projected = context._result(result, 256)
    preview = projected["unqualified_observation_records"][2]
    assert preview["value_projection"]["complete"] is False
    assert projected["observation_record_source"]["artifact_id"] == summary["manifest"]["artifact_id"]
    assert projected["observation_record_source"]["sha256"] == summary["manifest"]["sha256"]
    assert result == before


def test_oversized_or_invalid_record_does_not_erase_valid_siblings(tmp_path):
    items = [source_item(value="x" * (256 * 1024)), source_item(name="missing_source",
        sources=[{"filename": "absent.out", "internal_request_key": "native"}]),
        source_item(name="small_summary", value={"generated": 48})]
    store, run, _, _, result, payload = manifest_case(tmp_path, items)
    diagnostics = []
    records = read_records(store, run, result, diagnostics)
    assert [record.name for record in records] == ["small_summary"]
    assert [item["index"] for item in diagnostics[:-1]] == [0, 1]
    assert "per-record byte allowance" in diagnostics[0]["detail"]
    assert diagnostics[0]["manifest"]["path"][-1] == {"kind": "index", "index": 0}
    assert "filename is missing" in diagnostics[1]["detail"]
    assert diagnostics[-1]["rejected_count"] == 2
    assert store.artifact_path(diagnostics[-1]["manifest"]["artifact_id"]).read_bytes() == payload


def test_accepted_records_share_total_budget_and_later_small_summary_can_still_fit(tmp_path):
    items = [source_item(name=f"table_{i}", value="x" * 250000) for i in range(5)]
    items.append(source_item(name="small_summary", value=48))
    store, run, _, _, result, _ = manifest_case(tmp_path, items)
    diagnostics = []
    records = read_records(store, run, result, diagnostics)
    assert [record.name for record in records] == ["table_0", "table_1", "table_2", "table_3", "small_summary"]
    assert "remaining total record byte allowance" in diagnostics[0]["detail"]
    assert diagnostics[0]["index"] == 4
    assert diagnostics[-1]["accepted_indices"] == [0, 1, 2, 3, 5]
    assert diagnostics[-1]["accepted_record_bytes"] <= 1024 * 1024


@pytest.mark.parametrize("field", ["artifact_bytes", "artifact_total_bytes"])
def test_frozen_artifact_limits_also_bound_manifest_read(tmp_path, field):
    store, run, _, _, result, _ = manifest_case(tmp_path, [source_item(value="x" * 70000)])
    setattr(run.budget, field, 65536)
    diagnostics = []
    with pytest.raises(ValueError, match="bounded read allowance"):
        read_records(store, run, result, diagnostics)
    assert diagnostics[0]["manifest_bytes_limit"] == 65536
    assert diagnostics[0]["manifest"] == result.source["files"]["task/observations.json"]


def test_legacy_runs_keep_original_64kib_read_contract(tmp_path):
    store, run, _, _, result, _ = manifest_case(tmp_path, [source_item(value="x" * 70000)], simplified=False)
    with pytest.raises(ValueError, match="64 KiB"):
        read_records(store, run, result, [])


@pytest.mark.parametrize("change", ["hash", "ownership", "bytes"])
def test_manifest_integrity_and_ownership_are_not_weakened_by_larger_bound(tmp_path, change):
    store, run, _, _, result, _ = manifest_case(tmp_path, [source_item(value="x" * 70000)])
    reference = result.source["files"]["task/observations.json"]
    if change == "hash":
        reference["sha256"] = "e" * 64
    elif change == "ownership":
        run.attempts[0].id = "different_attempt"
    else:
        store.artifact_path(reference["artifact_id"]).write_bytes(b"tampered")
    with pytest.raises((ValueError, RuntimeError), match="binding differs|hash changed"):
        read_records(store, run, result, [])


def test_reassessment_does_not_retroactively_add_observations_or_scientific_qualification(tmp_path):
    store, run, request, goal, result, _ = manifest_case(tmp_path, [source_item(value="x" * 70000)])
    result.observation_records = []  # The original 64 KiB failure recorded no observations.
    before = result.model_copy(deep=True)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved" and answer["scientific_qualification"] is False
    assert "generic_observation_differs_from_archived_manifest" in answer["reasons"]
    assert result == before and not result.qualified_outputs
