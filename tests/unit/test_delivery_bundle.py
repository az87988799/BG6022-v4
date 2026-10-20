"""Synthetic offline exports exercise real report, Store and ZIP boundaries."""

import hashlib
import json
import os
import re
import zipfile
from pathlib import Path

import pytest

from orca_agent import delivery_bundle
from orca_agent.config import Config
from orca_agent.delivery_bundle import BundleLimits, export_bundle
from orca_agent.models import (
    Check,
    Goal,
    Observation,
    ObservationSource,
    PermissionSnapshot,
    Request,
    Result,
)
from orca_agent.report import build_report
from orca_agent.store import Store, StoreError


def snapshot(root):
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


@pytest.fixture
def records(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    raw = tmp_path / "synthetic.out"
    raw.write_bytes(b"Synthetic output fixture; NOT real ORCA evidence.\n")
    artifact = store.import_artifact(raw, "stdout")
    request = Request(
        original_text="Synthetic failed/unverified reporting fixture.",
        goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    run = store.create_run(request, None, PermissionSnapshot(artifact_ids=[artifact.id]))
    result = Result(
        run_id=run.id, operation_status="failed", artifact_ids=[artifact.id],
        checks={"energy": [Check(name="converged", status="failed", rule_version="orca-hf-2")]},
        observation_records=[Observation(
            name="synthetic value", value=1.2, validation_status="unverified",
            sources=[ObservationSource(artifact_id=artifact.id, sha256=artifact.sha256)],
            limitations=["No scientific qualification; synthetic fixture."])],
        diagnostics=[{"category": "synthetic_failure"}])
    # Fixture metadata only; no Attempt or execution is simulated as real evidence.
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result, immutable=True)
    run.result_ids = [result.id]
    run.state = "failed"
    store.save_run(run)
    return store, run, artifact, result


def test_explicit_bundle_is_deterministic_and_preserves_sources_and_qualification(records, tmp_path):
    store, run, artifact, _ = records
    before = snapshot(store.root), snapshot(store.environment_root)
    report = build_report(store, run)
    first = export_bundle(store, run.id, tmp_path / "first.zip")
    second = export_bundle(store, run.id, tmp_path / "second.zip")
    assert (tmp_path / "first.zip").read_bytes() == (tmp_path / "second.zip").read_bytes()
    assert first["sha256"] == second["sha256"]
    assert first["model_calls"] == first["orca_starts"] == 0
    assert first["report_delivery_status"] == "partial" and not first["user_goal_complete"]
    assert before == (snapshot(store.root), snapshot(store.environment_root))
    with zipfile.ZipFile(first["path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        validation = json.loads(archive.read("validation.json"))
        saved_report = json.loads(archive.read("report.json"))
        source = manifest["sources"][0]
        assert (source["artifact_id"], source["sha256"], source["role"]) == (
            artifact.id, artifact.sha256, "stdout")
        assert hashlib.sha256(archive.read(source["archive_path"])).hexdigest() == artifact.sha256
        assert validation["results"][0]["operation_status"] == "failed"
        statuses = {item["name"]: item for item in validation["results"][0]["output_validation"]}
        assert statuses["energy"]["status"] == "failed"
        assert statuses["synthetic value"]["status"] == "unverified"
        assert all(not item["scientific_qualification"] for item in statuses.values())
        for key in ("goals", "goal_facts", "delivery", "budget", "model_explanation"):
            assert saved_report.get(key) == report.get(key)
        for item in saved_report["artifact_index"]:
            assert item["path"] in archive.namelist()
        assert source["archive_path"] in archive.read("report.md").decode("utf-8")
        assert set(archive.namelist()) == {
            "manifest.json", "validation.json", "report.json", "report.md", source["archive_path"]}
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
        for row in manifest["files"]:
            content = archive.read(row["archive_path"])
            assert len(content) == row["size_bytes"]
            assert hashlib.sha256(content).hexdigest() == row["sha256"]


@pytest.mark.parametrize("damage", ["bytes", "missing"])
def test_corrupt_or_missing_indexed_file_refuses_entire_bundle(records, tmp_path, damage):
    store, run, artifact, _ = records
    path = store.artifact_path(artifact.id)
    if damage == "bytes":
        path.write_bytes(b"tampered")
    else:
        path.unlink()
    target = tmp_path / "refused.zip"
    with pytest.raises(StoreError, match="missing, unauthorized, corrupt"):
        export_bundle(store, run.id, target)
    assert not target.exists()
    assert not list(tmp_path.glob(".orca-bundle-*"))


def test_unauthorized_report_source_is_never_read_or_exported(records, tmp_path, monkeypatch):
    store, run, _, result = records
    secret = tmp_path / "unrelated.txt"
    secret.write_bytes(b"Unrelated private fixture.")
    other = store.import_artifact(secret, "unrelated")
    result.source = {"artifact_id": other.id, "sha256": other.sha256}
    # Explicitly construct an adversarial saved source link, not a new permission.
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result)
    original = store.artifact_path
    seen = []

    def record(identifier, **kwargs):
        seen.append(identifier)
        assert identifier != other.id, "unauthorized payload must not be opened"
        return original(identifier, **kwargs)

    monkeypatch.setattr(store, "artifact_path", record)
    with pytest.raises(StoreError, match="unauthorized"):
        export_bundle(store, run.id, tmp_path / "refused.zip")
    assert other.id not in seen and not (tmp_path / "refused.zip").exists()


def test_unreferenced_authorized_file_is_not_swept_into_bundle(records, tmp_path):
    store, original_run, artifact, result = records
    spare = tmp_path / "spare.txt"
    spare.write_text("Allowed but not used by this report.", encoding="utf-8")
    other = store.import_artifact(spare, "spare")
    run = store.create_run(store.load_request(original_run), None,
                           PermissionSnapshot(artifact_ids=[artifact.id, other.id]))
    result = result.model_copy(update={"run_id": run.id})
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result, immutable=True)
    run.result_ids = [result.id]
    store.save_run(run)
    output = export_bundle(store, run.id, tmp_path / "result.zip")
    with zipfile.ZipFile(output["path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
    assert other.id not in {row["artifact_id"] for row in manifest["sources"]}


@pytest.mark.parametrize("limits", [
    BundleLimits(max_files=4), BundleLimits(max_file_bytes=1),
    BundleLimits(max_total_bytes=1), BundleLimits(max_total_bytes=100),
])
def test_source_and_document_bounds_do_not_publish_partial_zip(records, tmp_path, limits):
    store, run, _, _ = records
    target = tmp_path / "bounded.zip"
    with pytest.raises(StoreError, match="bound|limit"):
        export_bundle(store, run.id, target, limits=limits)
    assert not target.exists() and not list(tmp_path.glob(".orca-bundle-*"))


def test_oversized_source_is_rejected_before_hashing(records, tmp_path, monkeypatch):
    store, run, _, _ = records
    monkeypatch.setattr(store, "artifact_path", lambda *_: pytest.fail("over-limit file was read"))
    with pytest.raises(StoreError):
        export_bundle(store, run.id, tmp_path / "bounded.zip",
                      limits=BundleLimits(max_file_bytes=1))


@pytest.mark.parametrize("place", ["data", "environment", "existing", "extension", "ads", "parent"])
def test_destination_does_not_overwrite_or_escape_into_history(records, tmp_path, place):
    store, run, _, _ = records
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"retain prior bytes")
    targets = {
        "data": store.root / "new.zip", "environment": store.environment_root / "new.zip",
        "existing": existing, "extension": tmp_path / "new.txt",
        "ads": tmp_path / "new:stream.zip", "parent": tmp_path / ".." / "new.zip",
    }
    before = snapshot(store.root), snapshot(store.environment_root)
    with pytest.raises(StoreError):
        export_bundle(store, run.id, targets[place])
    assert existing.read_bytes() == b"retain prior bytes"
    assert before == (snapshot(store.root), snapshot(store.environment_root))


def test_source_change_while_writing_does_not_publish(records, tmp_path, monkeypatch):
    store, run, artifact, _ = records
    original = delivery_bundle._copy_source

    def changed(*args):
        path = store.artifact_path(artifact.id)
        path.write_bytes(b"changed during export")
        return original(*args)

    monkeypatch.setattr(delivery_bundle, "_copy_source", changed)
    with pytest.raises(StoreError, match="changed"):
        export_bundle(store, run.id, tmp_path / "refused.zip")
    assert not (tmp_path / "refused.zip").exists()
    assert not list(tmp_path.glob(".orca-bundle-*"))


def test_run_change_before_publication_requires_explicit_retry(records, tmp_path, monkeypatch):
    store, run, _, _ = records
    original = delivery_bundle._verify_archive

    def changed(*args):
        original(*args)
        store.signal(run.id, "pause")

    monkeypatch.setattr(delivery_bundle, "_verify_archive", changed)
    with pytest.raises(StoreError, match="changed"):
        export_bundle(store, run.id, tmp_path / "refused.zip")
    assert not (tmp_path / "refused.zip").exists()
    assert not list(tmp_path.glob(".orca-bundle-*"))


def test_final_publication_race_preserves_the_other_file(records, tmp_path, monkeypatch):
    store, run, _, _ = records
    original = os.link

    def race(source, target):
        Path(target).write_bytes(b"another writer won")
        original(source, target)

    monkeypatch.setattr(delivery_bundle.os, "link", race)
    with pytest.raises(FileExistsError):
        export_bundle(store, run.id, tmp_path / "race.zip")
    assert (tmp_path / "race.zip").read_bytes() == b"another writer won"
    assert not list(tmp_path.glob(".orca-bundle-*"))


def test_equal_payloads_keep_both_artifact_identities_with_one_zip_member(records, tmp_path):
    store, run, artifact, result = records
    duplicate = store.import_artifact(store.artifact_path(artifact.id), "duplicate_fixture")
    result.artifact_ids.append(duplicate.id)
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result)
    output = export_bundle(store, run.id, tmp_path / "deduplicated.zip")
    with zipfile.ZipFile(output["path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert len(manifest["sources"]) == 2
        assert len({row["archive_path"] for row in manifest["sources"]}) == 1
        assert len(archive.namelist()) == 5


def test_archive_payload_hash_is_independently_verified_before_publication(
        records, tmp_path, monkeypatch):
    store, run, _, _ = records

    def faulty_copy(archive, name, path, artifact):
        archive.writestr(delivery_bundle._zip_info(name), b"x" * artifact.size)

    monkeypatch.setattr(delivery_bundle, "_copy_source", faulty_copy)
    with pytest.raises(StoreError, match="archive content hash"):
        export_bundle(store, run.id, tmp_path / "refused.zip")
    assert not (tmp_path / "refused.zip").exists()
    assert not list(tmp_path.glob(".orca-bundle-*"))


def test_untrusted_original_name_cannot_supply_zip_path_syntax(records, tmp_path):
    store, run, _, result = records
    original = tmp_path / "报告[1].out"
    original.write_bytes(b"Additional synthetic fixture with a nonportable original filename.")
    artifact = store.import_artifact(original, "stdout")
    result.artifact_ids.append(artifact.id)
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result)
    output = export_bundle(store, run.id, tmp_path / "portable.zip")
    with zipfile.ZipFile(output["path"]) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        source = next(row for row in manifest["sources"] if row["artifact_id"] == artifact.id)
        assert source["archive_path"].endswith("/artifact.out")
        assert archive.read(source["archive_path"]) == original.read_bytes()
        for name in archive.namelist():
            assert re.fullmatch(r"[A-Za-z0-9_./-]+", name)
            assert not name.startswith("/") and ".." not in name.split("/")


def test_cli_export_is_explicit_and_does_not_enter_execution(records, tmp_path, monkeypatch, capsys):
    from orca_agent import cli, entrypoints, runner

    store, run, _, _ = records
    monkeypatch.setattr(cli, "load_config", lambda _: Config(data_root=store.root))
    monkeypatch.setattr("orca_agent.store.default_environment_root", lambda: store.environment_root)

    def prohibited(*args, **kwargs):
        pytest.fail("export entered an execution path")

    monkeypatch.setattr(entrypoints, "execute", prohibited)
    monkeypatch.setattr(runner, "initialize", prohibited)
    before = snapshot(store.root), snapshot(store.environment_root)
    target = tmp_path / "cli.zip"
    assert cli.main(["export", run.id, "--output", str(target)]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["path"] == str(target)
    assert emitted["model_calls"] == emitted["orca_starts"] == 0
    assert before == (snapshot(store.root), snapshot(store.environment_root))
    assert cli.main(["export", run.id, "--output", str(target)]) == 2
    assert json.loads(capsys.readouterr().out)["error"] == "StoreError"
    assert before == (snapshot(store.root), snapshot(store.environment_root))
