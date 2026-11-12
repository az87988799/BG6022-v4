"""Historical acceptance browsing stays offline, source-bound and read-only."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from test_llm import socket_free_fake_client_loop  # noqa: F401
from test_text_entry import text_environment

from orca_agent import acceptance_browser, entrypoints, runner
from orca_agent.models import Goal, PermissionSnapshot, Request
from orca_agent.store import Store, StoreError
from orca_agent.web import create_app


def _snapshot(root):
    return {
        path.relative_to(root).as_posix(): (
            "directory" if path.is_dir() else
            (path.stat().st_size, path.stat().st_mtime_ns) if path.name == "web-service.lock" else
            hashlib.sha256(path.read_bytes()).hexdigest()
        )
        for path in root.rglob("*")
    }


def _catalog(repository, run_id, *, store_root="historical/data"):
    path = repository / acceptance_browser.CATALOG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "version": 1,
        "selection_note": "Historical numerical evidence is not same-version acceptance.",
        "tasks": [{
            "id": "T3", "title": "Saved scan", "summary": "Six saved points only.",
            "limitations": ["Missing +180 degree point; scientific observations unverified."],
            "variants": [{"id": "refreeze3", "label": "Historical candidate",
                          "store_root": store_root, "run_id": run_id,
                          "report_path": "historical/report.json",
                          "report_sha256": hashlib.sha256(
                              (repository / "historical/report.json").read_bytes()).hexdigest(),
                          "run_sha256": hashlib.sha256((repository /
                              f"historical/data/runs/{run_id}/run.json").read_bytes()).hexdigest()}],
        }],
    }), encoding="utf-8")


@pytest.fixture
def archive(tmp_path):
    store, _ = text_environment(tmp_path / "historical")
    files = {}
    for name, role, content in (
        ("analysis.py", "code", b"# Saved task; do not execute.\nprint('saved')\n"),
        ("job.inp", "native_input", b"! HF STO-3G\n# historical input\n"),
        ("stdout.out", "raw_evidence", b"first line\nsecond line\nthird line\n"),
        ("job.gbw", "raw_evidence", b"\x00\xff\x10saved-binary\r\n"),
    ):
        source = tmp_path / name
        source.write_bytes(content)
        files[name] = store.import_artifact(source, role)
    request = Request(original_text="Saved scientific task", goals=[
        Goal(id="scan", port="unresolved", minimum_check_version="unresolved-1"),
    ])
    run = store.create_run(request, None, PermissionSnapshot(
        artifact_ids=[artifact.id for artifact in files.values()],
    ))
    # This is a synthetic settled historical fixture, never an executed calculation.
    run.state, run.delivery_status = "failed", "partial"
    store._write_json(f"runs/{run.id}/run.json", run)
    other = store.create_run(request.model_copy(update={"id": "request_other"}), None)
    source = tmp_path / "foreign.txt"
    source.write_text("Other Run's evidence", encoding="utf-8")
    foreign = store.import_artifact(source, "raw_evidence", run_id=other.id)
    (tmp_path / "historical/report.json").write_text(json.dumps({
        "run_id": run.id, "run_state": run.state, "recorded_delivery_status": run.delivery_status,
        "user_goal_complete": False, "results": [], "qualified_outputs": {},
    }), encoding="utf-8")
    _catalog(tmp_path, run.id)
    return tmp_path, store, run, files, foreign


@pytest.fixture
def browser_client(archive):
    repository, _, _, _, _ = archive
    normal_store, config = text_environment(repository / "normal")
    app = create_app(config, store=normal_store, acceptance_repository=repository)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        token = client.get("/api/bootstrap").json()["token"]
        client.headers.update({"X-Local-Token": token, "Origin": "http://127.0.0.1:8765"})
        yield client


def test_gets_preserve_entire_history_and_never_initialize_or_execute(archive, monkeypatch):
    repository, store, run, files, _ = archive
    before = _snapshot(repository)

    def forbidden(*args, **kwargs):
        pytest.fail("read-only acceptance browsing initialized storage or started work")

    monkeypatch.setattr(Store, "__init__", forbidden)
    monkeypatch.setattr(Store, "save_run", forbidden)
    monkeypatch.setattr(Store, "_write_json", forbidden)
    monkeypatch.setattr(entrypoints, "execute", forbidden)
    monkeypatch.setattr(runner, "execute", forbidden)
    listing = acceptance_browser.tasks(repository)
    assert listing["read_only"] and listing["tasks"][0]["variants"][0]["available"]
    assert "store_root" not in listing["tasks"][0]["variants"][0]
    result = acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert result["variant"]["run_id"] == run.id
    assert (result["state"], result["delivery_status"]) == ("failed", "partial")
    assert result["report"]["run_state"] == "failed"
    assert result["report"]["recorded_delivery_status"] == "partial"
    assert result["report"]["user_goal_complete"] is False
    assert "Missing +180" in result["limitations"][0]
    page = acceptance_browser.artifacts("T3", "refreeze3", repository=repository)
    assert page["total"] == len(files)
    text = acceptance_browser.text("T3", "refreeze3", files["stdout.out"].id,
                                   start_line=2, lines=1, repository=repository)
    assert text["lines"] == [{"line": 2, "text": "second line"}]
    content, _, digest = acceptance_browser.download(
        "T3", "refreeze3", files["job.gbw"].id, repository=repository)
    assert content == store.artifact_path(files["job.gbw"].id).read_bytes()
    assert digest == hashlib.sha256(content).hexdigest()
    assert _snapshot(repository) == before


def test_http_download_paging_and_normal_workspace_are_separate(archive, browser_client):
    repository, _, run, files, _ = archive
    before = _snapshot(repository)
    base = "/api/acceptance/tasks/T3/runs/refreeze3"
    assert browser_client.get("/api/acceptance/tasks").json()["read_only"]
    assert browser_client.get(base).json()["variant"]["run_id"] == run.id
    first = browser_client.get(base + "/artifacts?limit=2").json()
    second = browser_client.get(base + "/artifacts?limit=2&offset=2").json()
    assert first["next_offset"] == 2 and second["next_offset"] is None
    assert {item["id"] for item in first["artifacts"] + second["artifacts"]} == {
        artifact.id for artifact in files.values()
    }
    script = browser_client.get(base + "/artifacts?category=script").json()
    assert [row["id"] for row in script["artifacts"]] == [files["analysis.py"].id]
    response = browser_client.get(base + f"/artifacts/{files['job.gbw'].id}/download")
    assert response.content == b"\x00\xff\x10saved-binary\r\n"
    assert response.headers["x-artifact-sha256"] == files["job.gbw"].sha256
    assert response.headers["content-type"] == "application/octet-stream"
    assert browser_client.get("/api/runs").json()["total"] == 0
    assert browser_client.get(f"/api/runs/{run.id}").status_code == 409
    assert _snapshot(repository) == before


@pytest.mark.parametrize("suffix", ["", "/control", "/messages", "/questions", "/evidence"])
def test_acceptance_namespace_rejects_post_actions(archive, browser_client, suffix):
    repository, _, _, _, _ = archive
    before = _snapshot(repository)
    response = browser_client.post("/api/acceptance/tasks/T3/runs/refreeze3" + suffix,
                                   json={"action": "resume", "text": "run again"})
    assert response.status_code == 405
    assert _snapshot(repository) == before


def test_foreign_artifact_and_unregistered_selection_cannot_be_read(archive, browser_client):
    _, _, _, _, foreign = archive
    base = "/api/acceptance/tasks/T3/runs/refreeze3"
    for operation in ("text", "download"):
        response = browser_client.get(base + f"/artifacts/{foreign.id}/{operation}")
        assert response.status_code == 409
        assert "Other Run's evidence" not in response.text
    for path in ("/api/acceptance/tasks/T3/runs/arbitrary",
                 "/api/acceptance/tasks/unknown/runs/refreeze3",
                 base + "/artifacts/..%5cforeign/download"):
        assert browser_client.get(path).status_code in {404, 409}


def test_changed_same_length_evidence_is_withheld(archive, browser_client):
    repository, store, _, files, _ = archive
    artifact = files["stdout.out"]
    store.artifact_path(artifact.id).write_bytes(b"X" * artifact.size)
    before = _snapshot(repository)
    base = "/api/acceptance/tasks/T3/runs/refreeze3"
    page = browser_client.get(base + "/artifacts").json()
    row = next(item for item in page["artifacts"] if item["id"] == artifact.id)
    assert row["integrity"]["status"] == "unavailable" and not row["downloadable"]
    for operation in ("text", "download"):
        assert browser_client.get(base + f"/artifacts/{artifact.id}/{operation}").status_code == 409
    assert _snapshot(repository) == before


@pytest.mark.parametrize("relative", ["../outside", "/outside", "C:/outside",
                                      "historical/../data", "historical/data:stream"])
def test_catalog_cannot_grant_paths_outside_controlled_repository(archive, relative):
    repository, _, run, _, _ = archive
    _catalog(repository, run.id, store_root=relative)
    before = _snapshot(repository)
    assert not acceptance_browser.tasks(repository)["tasks"][0]["variants"][0]["available"]
    with pytest.raises(StoreError):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert _snapshot(repository) == before


def test_missing_workspace_or_catalog_never_creates_directories(archive, tmp_path):
    repository, _, run, _, _ = archive
    _catalog(repository, run.id, store_root="missing/nested/data")
    before = _snapshot(repository)
    assert not acceptance_browser.tasks(repository)["tasks"][0]["variants"][0]["available"]
    with pytest.raises(StoreError):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert _snapshot(repository) == before and not (repository / "missing").exists()
    absent = tmp_path / "absent-repository"
    assert acceptance_browser.tasks(absent)["tasks"] == []
    assert not absent.exists()


def test_parent_directory_symlink_cannot_redirect_catalog_workspace(archive):
    repository, _, run, _, _ = archive
    link = repository / "linked"
    try:
        link.symlink_to(repository / "historical", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("real directory symlink unavailable in this environment")
    _catalog(repository, run.id, store_root="linked/data")
    assert not acceptance_browser.tasks(repository)["tasks"][0]["variants"][0]["available"]
    with pytest.raises(StoreError, match="links"):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)


def test_artifact_symlink_is_not_a_download_or_text_source(archive):
    repository, store, _, files, _ = archive
    artifact = files["stdout.out"]
    path = store.artifact_path(artifact.id)
    original = path.read_bytes()
    target = repository / "linked-source.txt"
    target.write_bytes(original)
    path.unlink()
    try:
        path.symlink_to(target)
    except (OSError, NotImplementedError):
        path.write_bytes(original)
        pytest.skip("real file symlink unavailable in this environment")
    for operation in (acceptance_browser.text, acceptance_browser.download):
        with pytest.raises(StoreError, match="links"):
            operation("T3", "refreeze3", artifact.id, repository=repository)


def test_binary_text_and_out_of_bounds_queries_fail_without_conversion(archive, browser_client):
    repository, _, _, files, _ = archive
    before = _snapshot(repository)
    base = "/api/acceptance/tasks/T3/runs/refreeze3/artifacts"
    assert browser_client.get(base + f"/{files['job.gbw'].id}/text").status_code == 409
    for query in ("limit=0", "limit=101", "offset=-1", "category=execute"):
        assert browser_client.get(base + "?" + query).status_code == 409
    for query in ("start_line=0", "lines=201"):
        assert browser_client.get(base + f"/{files['stdout.out'].id}/text?" + query).status_code == 409
    assert _snapshot(repository) == before


def test_oversized_existing_artifact_is_listed_but_not_downloaded(archive):
    repository, store, run, _, _ = archive
    source = repository / "large.gbw"
    with source.open("wb") as stream:
        stream.seek(acceptance_browser.DOWNLOAD_BYTES)
        stream.write(b"x")
    artifact = store.import_artifact(source, "raw_evidence")
    path = store.path(f"runs/{run.id}/run.json")
    payload = json.loads(path.read_bytes())
    payload["permission"]["artifact_ids"].append(artifact.id)
    path.write_text(json.dumps(payload), encoding="utf-8")
    _catalog(repository, run.id)
    before = _snapshot(repository)
    page = acceptance_browser.artifacts("T3", "refreeze3", repository=repository)
    row = next(item for item in page["artifacts"] if item["id"] == artifact.id)
    assert row["integrity"]["status"] == "verified" and not row["downloadable"]
    with pytest.raises(StoreError, match="16 MiB"):
        acceptance_browser.download("T3", "refreeze3", artifact.id, repository=repository)
    with pytest.raises(ValueError, match="16 MiB"):
        acceptance_browser.text("T3", "refreeze3", artifact.id, repository=repository)
    assert _snapshot(repository) == before


def test_extensionless_native_dependency_is_available_as_an_input(archive):
    repository, store, run, _, _ = archive
    source = repository / "native_dependency"
    source.write_text("saved input coordinates\n", encoding="utf-8")
    artifact = store.import_artifact(source, "native_dependency")
    path = store.path(f"runs/{run.id}/run.json")
    payload = json.loads(path.read_bytes())
    payload["permission"]["artifact_ids"].append(artifact.id)
    path.write_text(json.dumps(payload), encoding="utf-8")
    _catalog(repository, run.id)
    page = acceptance_browser.artifacts("T3", "refreeze3", category="input",
                                        repository=repository)
    assert any(item["id"] == artifact.id and item["category"] == "input"
               for item in page["artifacts"])


def test_missing_saved_report_is_not_regenerated(archive, monkeypatch):
    repository, _, _, _, _ = archive
    (repository / "historical/report.json").unlink()
    before = _snapshot(repository)
    monkeypatch.setattr(entrypoints, "report", lambda *a, **k: pytest.fail("rebuilt report"))
    with pytest.raises(OSError):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert _snapshot(repository) == before


@pytest.mark.parametrize("kind", ["Run", "report"])
def test_saved_metadata_is_bounded_before_parsing(archive, kind):
    repository, store, run, _, _ = archive
    path = (store.path(f"runs/{run.id}/run.json") if kind == "Run" else
            repository / "historical/report.json")
    with path.open("ab") as stream:
        stream.truncate(acceptance_browser.METADATA_BYTES + 1)
    _catalog(repository, run.id)
    before = _snapshot(repository)
    with pytest.raises(StoreError, match="exceeds its read bound"):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert _snapshot(repository) == before


@pytest.mark.parametrize("change", ["bytes", "run_id", "run_state", "recorded_delivery_status"])
def test_saved_report_requires_hash_and_exact_run_snapshot_binding(archive, change):
    repository, _, _, _, foreign = archive
    path = repository / "historical/report.json"
    if change == "bytes":
        path.write_bytes(path.read_bytes() + b"\n")
    else:
        report = json.loads(path.read_bytes())
        report[change] = {"run_id": foreign.run_id, "run_state": "completed",
                          "recorded_delivery_status": "complete"}[change]
        path.write_text(json.dumps(report), encoding="utf-8")
        catalog_path = repository / acceptance_browser.CATALOG
        catalog = json.loads(catalog_path.read_bytes())
        catalog["tasks"][0]["variants"][0]["report_sha256"] = hashlib.sha256(
            path.read_bytes()).hexdigest()
        catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    before = _snapshot(repository)
    with pytest.raises(StoreError, match="report"):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert _snapshot(repository) == before


def test_changed_run_snapshot_cannot_be_silently_reinterpreted(archive):
    repository, store, run, _, _ = archive
    path = store.path(f"runs/{run.id}/run.json")
    path.write_bytes(path.read_bytes() + b"\n")
    before = _snapshot(repository)
    assert not acceptance_browser.tasks(repository)["tasks"][0]["variants"][0]["available"]
    with pytest.raises(StoreError, match="Run differs"):
        acceptance_browser.detail("T3", "refreeze3", repository=repository)
    assert _snapshot(repository) == before


def test_run_replacement_after_open_cannot_expand_artifact_permission(archive, monkeypatch):
    repository, store, run, _, foreign = archive
    path = store.path(f"runs/{run.id}/run.json")
    original = entrypoints.artifact_ids

    def replace_before_authorization(selected_store, run_id):
        changed = json.loads(path.read_bytes())
        changed["permission"]["artifact_ids"].append(foreign.id)
        path.write_text(json.dumps(changed), encoding="utf-8")
        return original(selected_store, run_id)

    monkeypatch.setattr(entrypoints, "artifact_ids", replace_before_authorization)
    with pytest.raises(StoreError):
        acceptance_browser.download("T3", "refreeze3", foreign.id, repository=repository)
