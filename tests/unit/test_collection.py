"""A parsed value must refer to the exact bytes archived for its Result."""

import pytest

from orca_agent.models import (
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store
from orca_agent.tools import calculation


@pytest.fixture
def collection_attempt(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    geometry = tmp_path / "initial.xyz"
    geometry.write_text("3\nSynthetic fixture\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n")
    initial = store.import_artifact(geometry, "initial_geometry")
    request = Request(geometry_artifact_id=initial.id, goals=[Goal(id="e", port="energy")])
    step = Step(id="sp", logical_id="energy", tool="orca.sp", geometry=InputRef(artifact_id=initial.id))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"e": OutputBinding(step_id="sp", port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[initial.id]))
    attempt = store.reserve_attempt(run, step, initial.id)
    workdir = store.path(attempt.directory)
    (workdir / "stdout.out").write_bytes(b"Original synthetic evidence.\n")
    return store, run, step, attempt, workdir


def parsed_fixture():
    return {
        "checks": {"energy": [{"name": "synthetic_fixture", "status": "passed",
                               "detail": "Synthetic parser fixture, not scientific evidence"}]},
        "qualified_outputs": {"energy": {"value": -1.0, "unit": "Eh",
                                         "source": {"synthetic_fixture": True}}},
        "observations": {"synthetic_fixture": True, "energy_eh": -1.0},
        "diagnostics": [],
    }


def completed_fixture():
    return {"state": "completed", "reason": "synthetic fixture; no process was launched"}


@pytest.mark.parametrize("mutation", ["modified", "added", "deleted"])
def test_archive_parse_conflict_preserves_evidence_without_publishing_port(
    collection_attempt, monkeypatch, mutation
):
    store, run, step, attempt, workdir = collection_attempt

    def read_and_mutate(*args):
        if mutation == "modified":
            (workdir / "stdout.out").write_bytes(b"Different evidence after archival.\n")
        elif mutation == "added":
            (workdir / "unexpected.property.json").write_bytes(b"{}\n")
        else:
            (workdir / "stdout.out").unlink()
        return parsed_fixture()

    monkeypatch.setattr(calculation, "read_outputs", read_and_mutate)
    result = calculation.collect_result(store, run, step, attempt, completed_fixture())
    assert result.operation_status == "completed"  # Execution and scientific status stay distinct.
    assert result.qualified_outputs == {}
    assert result.observations["synthetic_fixture"] is True
    assert any(item["category"] == "source_conflict" for item in result.diagnostics)
    assert any(check.name == "archived_source_integrity" and check.status == "failed"
               for check in result.checks["energy"])
    original = result.source["files"]["stdout.out"]
    assert store.artifact_path(original["artifact_id"]).read_bytes() == b"Original synthetic evidence.\n"
    assert store.load_artifact(original["artifact_id"]).sha256 == original["sha256"]
    store.save_result(result)
    assert not store.load_result(run.id, result.id).qualified_outputs


def test_unchanged_archive_and_parser_can_publish_only_checked_fixture(collection_attempt, monkeypatch):
    store, run, step, attempt, _ = collection_attempt
    monkeypatch.setattr(calculation, "read_outputs", lambda *args: parsed_fixture())
    result = calculation.collect_result(store, run, step, attempt, completed_fixture())
    assert result.qualified_outputs["energy"].value == -1.0
    assert result.qualified_outputs["energy"].source["synthetic_fixture"] is True
    assert not any(item["category"] == "source_conflict" for item in result.diagnostics)


def test_parse_failure_keeps_already_archived_raw_files(collection_attempt, monkeypatch):
    store, run, step, attempt, _ = collection_attempt

    def broken_parser(*args):
        raise ValueError("synthetic malformed JSON")

    monkeypatch.setattr(calculation, "read_outputs", broken_parser)
    result = calculation.collect_result(store, run, step, attempt, completed_fixture())
    assert result.artifact_ids
    assert result.qualified_outputs == {}
    assert result.diagnostics[0]["category"] == "parse_error"
    for artifact_id in result.artifact_ids:
        store.artifact_path(artifact_id)
