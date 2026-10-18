"""Offline identity and history checks; no Docker or scientific process is started."""

import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from orca_agent import doctor
from orca_agent.config import Config, load_config
from orca_agent.models import (
    ExecutionConfiguration,
    Goal,
    PermissionSnapshot,
    Request,
    RunConfiguration,
)
from orca_agent.natural import initialize_agent
from orca_agent.store import Store, StoreError


def container_fixture(tmp_path):
    manifest = {
        "schema_version": 1, "version": "local-linux-runtime-1", "platform": "linux/amd64",
        "orca": {"version": "6.1.1", "path": "/opt/orca/orca", "sha256": "1" * 64},
        "mpi": {"version": "4.1.8", "path": "/opt/openmpi/bin/mpirun", "sha256": "2" * 64},
        "python": {"version": "3.11.2", "path": "/opt/runtime/bin/python", "sha256": "3" * 64},
    }
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True,
        separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
    execution = ExecutionConfiguration(backend="linux-container", image_id="sha256:" + "a" * 64,
        manifest_sha256=digest, docker_context="desktop-linux", docker_path=tmp_path / "docker.exe")
    report = {"backend": "linux-container", "image_id": execution.image_id,
              "manifest_sha256": digest, "manifest": manifest,
              "daemon_id": "synthetic-daemon-1", "verified": True, "issues": []}
    return Config(execution=execution, data_root=tmp_path / "data"), report


def run_for(tmp_path, config):
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    run = store.create_run(Request(goals=[Goal(id="g", port="observation")]), None,
                           configuration=config.run_configuration())
    return store, run


@pytest.mark.parametrize("changed", [
    {"image_id": "orca:latest"}, {"image_id": "sha256:" + "A" * 64},
    {"manifest_sha256": "short"}, {"docker_context": "--host=other"},
    {"docker_path": "docker.exe"}, {"docker_path": r"\\server\share\docker.exe"},
    {"docker_path": "/tools/../docker"}, {"image_id": None}, {"docker_context": None},
])
def test_container_configuration_requires_explicit_local_pinned_identity(tmp_path, changed):
    config, _ = container_fixture(tmp_path)
    with pytest.raises(ValidationError):
        ExecutionConfiguration.model_validate({**config.execution.model_dump(), **changed})


def test_linux_config_never_discovers_windows_executables(tmp_path, monkeypatch):
    config, _ = container_fixture(tmp_path)
    path = tmp_path / "config.toml"
    values = config.execution.model_dump(mode="json")
    path.write_text("[execution]\n" + "\n".join(f"{key} = {json.dumps(value)}"
                                               for key, value in values.items()))
    monkeypatch.setattr("orca_agent.config.shutil.which",
                        lambda _: pytest.fail("Linux selection must not discover Windows programs"))
    loaded = load_config(path)
    assert loaded.orca_path is None and loaded.mpi_path is None
    assert loaded.run_configuration().version == 2
    assert loaded.run_configuration().execution == config.execution
    with pytest.raises(ValidationError):
        loaded.execution.image_id = "sha256:" + "b" * 64
    with pytest.raises(ValidationError):
        RunConfiguration(version=1, execution=loaded.execution)
    with pytest.raises(ValidationError):
        ExecutionConfiguration(image_id=loaded.execution.image_id)


def test_legacy_version_one_remains_windows_without_rewriting_history(tmp_path, monkeypatch):
    config, _ = container_fixture(tmp_path)
    store, run = run_for(tmp_path, Config(data_root=config.data_root))
    snapshot_path = store.path(f"runs/{run.id}/configuration.json")
    run_path = store.path(f"runs/{run.id}/run.json")
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    snapshot["version"] = 1
    snapshot.pop("execution")
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    payload["configuration"] = snapshot
    payload["usage"]["orca_starts_reserved"] = 2
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    run_path.write_text(json.dumps(payload), encoding="utf-8")
    before = (snapshot_path.read_bytes(), run_path.read_bytes())
    store.configuration = config.run_configuration()
    loaded = store.load_run(run.id)
    assert loaded.configuration.version == 1
    assert loaded.configuration.execution.backend == "windows-lpac"
    assert loaded.usage.orca_starts_reserved == 2
    monkeypatch.setattr(doctor, "_inspect_container_environment",
                        lambda _: pytest.fail("backend change must fail before environment lookup"))
    with pytest.raises(StoreError, match="frozen Run configuration"):
        doctor.freeze_environment(store, config, loaded)
    assert (snapshot_path.read_bytes(), run_path.read_bytes()) == before
    assert not store.path(f"runs/{run.id}/environment.json").exists()


def test_container_configuration_snapshot_cannot_be_changed_on_resume(tmp_path):
    config, _ = container_fixture(tmp_path)
    store, run = run_for(tmp_path, config)
    path = store.path(f"runs/{run.id}/configuration.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["execution"]["image_id"] = "sha256:" + "b" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(StoreError, match="snapshot"):
        store.load_run(run.id)


def test_version_two_snapshot_cannot_silently_default_missing_backend():
    payload = RunConfiguration().model_dump(mode="json")
    payload.pop("execution")
    with pytest.raises(ValidationError, match="explicit execution snapshot"):
        RunConfiguration.model_validate(payload)
    payload["version"] = 1
    assert RunConfiguration.model_validate(payload).execution.backend == "windows-lpac"


def test_linux_natural_initialization_freezes_manifest_without_windows_paths(tmp_path, monkeypatch):
    config, observed = container_fixture(tmp_path)
    calls = []

    def inspect(execution):
        assert execution == config.execution
        calls.append(execution)
        return copy.deepcopy(observed)

    monkeypatch.setattr(doctor, "_inspect_container_environment", inspect)
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    run = initialize_agent(store, config, Request(goals=[Goal(id="g", port="observation")]),
                           PermissionSnapshot(model_execution=True, scientific_execution=True))
    path = store.path(f"runs/{run.id}/environment.json")
    before = path.read_bytes()
    frozen = doctor.validate_environment(store, config, run)
    assert len(calls) == 2
    assert frozen["orca"]["path"] == "/opt/orca/orca"
    assert frozen["mpi"]["version"] == "4.1.8"
    assert frozen["container"]["daemon_id"] == "synthetic-daemon-1"
    assert run.configuration.execution == config.execution
    assert not run.attempts and store.environment_lease() is None
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == 0
    assert path.read_bytes() == before
    assert frozen["scientific_execution"] == "not performed by doctor"


@pytest.mark.parametrize("changed,reason", [
    ({"daemon_id": "synthetic-daemon-2"}, "daemon identity changed"),
    ({"image_id": "sha256:" + "b" * 64}, "immutable image ID"),
    ({"manifest_sha256": "0" * 64}, "configured hash"),
    ({"verified": False}, "unverified"),
])
def test_runtime_change_fails_without_replacing_frozen_environment(tmp_path, monkeypatch, changed, reason):
    config, observed = container_fixture(tmp_path)
    store, run = run_for(tmp_path, config)
    monkeypatch.setattr(doctor, "_inspect_container_environment", lambda _: copy.deepcopy(observed))
    doctor.freeze_environment(store, config, run)
    path = store.path(f"runs/{run.id}/environment.json")
    before = path.read_bytes()
    observed.update(changed)
    with pytest.raises(StoreError, match=reason):
        doctor.freeze_environment(store, config, run)
    assert path.read_bytes() == before
    assert store.load_run(run.id).usage.orca_starts_reserved == 0


def test_manifest_content_hash_is_checked_even_if_backend_claims_verified(tmp_path, monkeypatch):
    config, observed = container_fixture(tmp_path)
    observed["manifest"]["orca"]["version"] = "6.2.0"
    monkeypatch.setattr(doctor, "_inspect_container_environment", lambda _: observed)
    report = doctor.diagnose(config)
    assert report["container"]["verified"] is False
    assert report["orca"]["compatible"] is False
    assert any("content does not match" in issue for issue in report["issues"])
    with pytest.raises(StoreError, match="unverified"):
        doctor.capture_environment(config)


def test_missing_container_environment_reports_unverified_without_fallback(tmp_path, monkeypatch):
    config, _ = container_fixture(tmp_path)

    def unavailable(_):
        raise OSError("synthetic Docker executable unavailable")

    monkeypatch.setattr(doctor, "_inspect_container_environment", unavailable)
    monkeypatch.setattr(doctor, "_windows_runtime", lambda _: pytest.fail("no backend fallback"))
    report = doctor.diagnose(config)
    assert report["backend"] == "linux-container"
    assert report["container"]["verified"] is False
    assert report["orca"]["compatible"] is False
    assert report["issues"]
    assert report["job_control"] == "requires managed backend acceptance tests"


def test_windows_serial_snapshot_needs_no_mpi_but_binary_change_is_rejected(tmp_path, monkeypatch):
    executable = tmp_path / "orca.fixture"
    executable.write_bytes(b"synthetic program identity, never executed")
    config = Config(orca_path=executable, data_root=tmp_path / "data")
    monkeypatch.setattr(doctor, "diagnose", lambda _: {
        "orca": {"version": "6.1.1", "compatible": True}, "mpi": {}})
    store, run = run_for(tmp_path, config)
    doctor.freeze_environment(store, config, run, require_mpi=False)
    doctor.validate_environment(store, config, run, require_mpi=False)
    with pytest.raises(StoreError, match="MPI executable changed"):
        doctor.validate_environment(store, config, run)
    executable.write_bytes(b"changed synthetic bytes")
    with pytest.raises(StoreError, match="ORCA executable changed"):
        doctor.validate_environment(store, config, run, require_mpi=False)
