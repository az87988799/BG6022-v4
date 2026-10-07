"""Requested-window coverage stays separate from reading success and model previews."""

import json

import pytest

from orca_agent.context import _evidence_observation
from orca_agent.goals import validate_goal_evidence
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import Store, StoreError
from orca_agent.tools import evidence
from orca_agent.tools.dispatch import execute_call


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data", environment_root=tmp_path / "environment")


def registered(store, tmp_path, content, *, name="source.json"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return store.import_artifact(path, "synthetic_test_evidence")


def query(store, artifact, tool, port, parameters, *, partial=False):
    goal = Goal(id="query", port=port, minimum_check_version="evidence-read-1",
                conditions={"query": parameters, "accept_partial_observations": partial})
    request = Request(goals=[goal])
    run = store.create_run(request, None, PermissionSnapshot(
        scientific_execution=False, artifact_ids=[artifact.id], allowed_tools=[tool]),
        BudgetLimits(orca_starts=0, evidence_reads=8))
    result = execute_call(store, run, tool, parameters)
    return run, request, goal, result


@pytest.mark.parametrize("partial", [False, True])
def test_legacy_array_prefix_cannot_complete_full_query_without_explicit_partial(store, tmp_path, partial):
    artifact = registered(store, tmp_path, json.dumps({"values": list(range(100))}))
    run, request, goal, result = query(store, artifact, "evidence.field", "field_observation",
        {"artifact_id": artifact.id, "field": "values"}, partial=partial)
    observation = result.observations[goal.port]
    assert result.operation_status == "completed" and not result.qualified_outputs
    assert observation["status"] == "partial"
    assert observation["value"] == list(range(32))
    coverage = observation["coverage"]
    assert coverage["requested"] == {"kind": "array", "start": 0, "stop": 100}
    assert coverage["returned"] == {"kind": "array", "start": 0, "stop": 32}
    assert coverage["reason"] == "element_limit"
    cursor = coverage["next_cursor"]
    assert (cursor["artifact_id"], cursor["sha256"], cursor["path"], cursor["next_start"]) == (
        artifact.id, artifact.sha256, [{"kind": "key", "key": "values"}], 32)
    assert result.checks[goal.port][0].source["coverage"] == coverage
    assert validate_goal_evidence(store, run, request, goal, result) is partial
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0


def test_one_oversized_wrapped_line_fails_instead_of_empty_complete_read(store, tmp_path):
    artifact = registered(store, tmp_path, "x" * 32701, name="long.out")
    with pytest.raises(ValueError, match="including metadata exceeds the 32 KiB"):
        evidence.inspect_artifact(store, artifact.id, lines=1)
    run, request, goal, result = query(store, artifact, "evidence.text", "text_window",
        {"artifact_id": artifact.id, "lines": 1})
    assert result.operation_status == "failed"
    assert result.diagnostics[0]["reason"] == "evidence_response_byte_limit"
    assert not validate_goal_evidence(store, run, request, goal, result)
    assert not result.observations


def test_text_byte_limit_is_partial_but_explicit_line_window_is_complete(store, tmp_path):
    artifact = registered(store, tmp_path, "a" * 20000 + "\n" + "b" * 20000 + "\n", name="two.out")
    observed = evidence.inspect_artifact(store, artifact.id, lines=2)
    assert observed["status"] == "partial" and not evidence.observation_complete(observed)
    assert observed["coverage"]["returned"] == {"kind": "lines", "start": 1, "stop": 2}
    assert observed["coverage"]["next_cursor"]["next_start"] == 2
    second = evidence.inspect_artifact(store, artifact.id, start_line=2, lines=1)
    assert second["coverage"]["complete"]
    assert second["coverage"]["requested"] == {"kind": "lines", "start": 2, "stop": 3}
    assert second["sha256"] == observed["sha256"]
    # Source binding is checked again when following a continuation.
    store.artifact_path(artifact.id).write_text("changed", encoding="utf-8")
    with pytest.raises(StoreError, match="hash changed"):
        evidence.inspect_artifact(store, artifact.id, start_line=2, lines=1)


@pytest.mark.parametrize("path,value", [([{"kind": "key", "key": "empty"}], []),
    ([{"kind": "key", "key": "values"}], [1, 2, 3]),
    ([{"kind": "key", "key": "scalar"}], 7),
    ([{"kind": "key", "key": "values"}, {"kind": "slice", "start": 1, "stop": 2}], [2]),
    ([{"kind": "key", "key": "values"}, {"kind": "slice", "start": 5, "stop": 8}], [])])
def test_empty_scalar_array_and_explicit_slice_are_complete(store, tmp_path, path, value):
    artifact = registered(store, tmp_path, json.dumps({"empty": [], "values": [1, 2, 3], "scalar": 7}))
    observation = evidence.read_value(store, artifact.id, path)
    assert observation["value"] == value
    assert observation["status"] == "observed"
    assert observation["coverage"]["complete"] and not observation["coverage"]["truncated"]


def test_eof_missing_field_and_missing_json_are_distinct(store, tmp_path):
    raw = registered(store, tmp_path, "one\n", name="source.out")
    end = evidence.inspect_artifact(store, raw.id, start_line=4, lines=2)
    assert end["lines"] == [] and end["coverage"]["complete"]
    assert end["coverage"]["returned"] == {"kind": "lines", "start": 4, "stop": 4}
    artifact = registered(store, tmp_path, "{}")
    missing = evidence.read_value(store, artifact.id, [{"kind": "key", "key": "missing"}])
    assert missing["status"] == "missing" and not evidence.observation_complete(missing)
    run, request, goal, result = query(store, raw, "evidence.value", "value_observation",
        {"artifact_id": raw.id})
    assert result.observations[goal.port]["status"] == "missing_json"
    assert result.checks[goal.port][0].status == "unverified"
    assert not validate_goal_evidence(store, run, request, goal, result)


def test_search_hit_limit_marks_unscanned_window_partial(store, tmp_path):
    artifact = registered(store, tmp_path, "hit\nmiss\nhit\n", name="search.out")
    first = evidence.search_text(store, artifact.id, "hit", max_lines=3, max_hits=1)
    assert first["coverage"]["reason"] == "hit_limit" and first["status"] == "partial"
    assert first["coverage"]["returned"] == {"kind": "lines", "start": 1, "stop": 2}
    rest = evidence.search_text(store, artifact.id, "hit", start_line=2, max_lines=2)
    assert rest["coverage"]["complete"]
    assert rest["matches"] == [{"line": 3, "text": "hit"}]
    window = evidence.search_text(store, artifact.id, "hit", max_lines=1)
    assert window["coverage"]["complete"]  # Later lines are outside this request.


def test_discovery_page_is_complete_for_requested_window_even_with_more_source(store, tmp_path):
    artifact = registered(store, tmp_path, json.dumps(list(range(100))))
    page = evidence.discover_content(store, artifact.id, offset=10, limit=4)
    assert page["next_offset"] == 14
    assert page["coverage"]["complete"] and page["coverage"]["next_cursor"] is None
    assert page["coverage"]["requested"] == page["coverage"]["returned"] == {
        "kind": "entries", "start": 10, "stop": 14}


def test_model_preview_omission_preserves_actual_tool_coverage(store, tmp_path):
    artifact = registered(store, tmp_path, json.dumps({"values": ["x" * 300] * 20}))
    observed = evidence.read_value(store, artifact.id, [{"kind": "key", "key": "values"}])
    projected = _evidence_observation(observed, 256)
    assert projected["value"] == [] and projected["value_projection"]["partial"]
    assert projected["coverage"] == observed["coverage"]
    assert projected["coverage"]["complete"] and projected["status"] == "observed"
    assert "empty preview does not mean empty source" in projected["value_projection"]["meaning"]


def test_coverage_incomplete_cannot_be_overridden_by_observed_status(store, tmp_path):
    artifact = registered(store, tmp_path, json.dumps({"values": list(range(100))}))
    run, request, goal, result = query(store, artifact, "evidence.field", "field_observation",
        {"artifact_id": artifact.id, "field": "values"})
    result.observations[goal.port]["status"] = "observed"
    assert not validate_goal_evidence(store, run, request, goal, result)
    # Legacy persisted results cannot prove the silently sliced boundary either.
    result.observations[goal.port].pop("coverage")
    assert not validate_goal_evidence(store, run, request, goal, result)


def test_metadata_list_skips_large_content_verification_but_keeps_other_members(store, tmp_path, monkeypatch):
    large = registered(store, tmp_path, "x" * (evidence.MAX_SOURCE_BYTES + 1), name="large.out")
    small = registered(store, tmp_path, "small", name="small.out")
    request = Request(goals=[Goal(id="metadata", port="artifact_metadata", minimum_check_version="evidence-read-1")])
    run = store.create_run(request, None, PermissionSnapshot(
        scientific_execution=False, allowed_tools=["evidence.list"], artifact_ids=[large.id, small.id]),
        BudgetLimits(orca_starts=0, evidence_reads=2))
    checked = []
    original = store.artifact_path

    def bounded_verification(artifact_id, **kwargs):
        checked.append(artifact_id)
        assert artifact_id != large.id, "metadata enumeration must not hash oversized content"
        return original(artifact_id, **kwargs)

    monkeypatch.setattr(store, "artifact_path", bounded_verification)
    response = evidence.list_artifacts(store, run.id)
    assert [item["id"] for item in response["artifacts"]] == [large.id, small.id]
    assert response["artifacts"][0]["integrity"]["status"] == "not_verified"
    assert response["artifacts"][1]["integrity"]["status"] == "verified"
    assert response["coverage"]["complete"] and checked == [small.id]
