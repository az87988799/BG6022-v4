"""Bounded installation diagnostics. Version flags never submit scientific input."""

import ctypes
import hashlib
import importlib.metadata
import json
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

import psutil

from orca_agent.config import Config
from orca_agent.versions import (
    OPI_MINIMUM_ORCA_VERSION,
    SUPPORTED_ORCA_VERSIONS,
    extract_orca_version,
    is_supported_orca_version,
    orca_version_tokens,
)


def file_version(path: Path) -> str | None:
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    lib = ctypes.WinDLL("version", use_last_error=True)
    lib.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    lib.GetFileVersionInfoSizeW.restype = wintypes.DWORD
    lib.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    lib.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    size = lib.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not lib.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    if not lib.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    high, low = words[2], words[3]
    return f"{high >> 16}.{high & 65535}.{low >> 16}.{low & 65535}"


def _windows_runtime(config: Config) -> tuple[dict, dict, list[str]]:
    issues = []
    orca = {"path": str(config.orca_path) if config.orca_path else None, "version": None,
            "probe": "not_run", "compatible": None,
            "opi_minimum_version": OPI_MINIMUM_ORCA_VERSION,
            "enabled_versions": list(SUPPORTED_ORCA_VERSIONS), "observed_version_tokens": []}
    if config.orca_path and config.orca_path.is_file():
        try:
            with tempfile.TemporaryDirectory(prefix="orca-agent-doctor-") as temporary:
                proc = subprocess.run([str(config.orca_path), "--version"], capture_output=True,
                                      timeout=5, text=True, errors="replace", shell=False,
                                      cwd=temporary)
            token = extract_orca_version(proc.stdout)
            orca.update(probe="version_banner_only_no_input", returncode=proc.returncode)
            orca["observed_version_tokens"] = list(orca_version_tokens(proc.stdout))
            if token is not None:
                orca["version"] = token
                orca["compatible"] = is_supported_orca_version(token)
        except (OSError, subprocess.SubprocessError) as exc:
            orca["probe"] = f"failed: {type(exc).__name__}"
    if orca["compatible"] is not True:
        issues.append("ORCA version is missing, ambiguous or not enabled; project enables only "
                      + ", ".join(SUPPORTED_ORCA_VERSIONS))
    mpi = {"path": str(config.mpi_path) if config.mpi_path else None, "file_version": None,
           "parallel_execution": "unverified"}
    if config.mpi_path and config.mpi_path.is_file():
        mpi["file_version"] = file_version(config.mpi_path)
    else:
        issues.append("MPI executable not found")
    return orca, mpi, issues


def _inspect_container_environment(execution) -> dict:
    from orca_agent.backends.container import inspect_environment

    return inspect_environment(execution)


def _container_identity(execution, container: dict) -> tuple[str, dict]:
    """Validate the bounded backend observation, not a scientific success claim."""
    if container.get("verified") is not True:
        raise ValueError("Linux container environment is unavailable or unverified")
    if container.get("image_id") != execution.image_id:
        raise ValueError("container image differs from the frozen immutable image ID")
    if container.get("manifest_sha256") != execution.manifest_sha256:
        raise ValueError("container manifest differs from the configured hash")
    manifest = container.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("container runtime manifest is missing")
    raw = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    if hashlib.sha256(raw).hexdigest() != execution.manifest_sha256:
        raise ValueError("container runtime manifest content does not match its hash")
    daemon_id = container.get("daemon_id") or container.get("engine_id")
    if not isinstance(daemon_id, str) or not daemon_id or len(daemon_id) > 256:
        raise ValueError("container daemon identity is missing")
    for name in ("orca", "mpi", "python"):
        item = manifest.get(name)
        if not isinstance(item, dict) or not all(item.get(key) for key in ("version", "path", "sha256")):
            raise ValueError(f"container {name} identity is missing from the runtime manifest")
    return daemon_id, manifest


def _linux_runtime(config: Config) -> tuple[dict, dict, dict, list[str]]:
    container = {"verified": False, "issues": []}
    try:
        container = _inspect_container_environment(config.execution)
        _, manifest = _container_identity(config.execution, container)
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        container = {**container, "verified": False,
                     "issues": [*container.get("issues", []), f"environment_unverified: {exc}"]}
        manifest = {}
    orca = {**manifest.get("orca", {}), "probe": "immutable_image_manifest_no_task_start",
            "compatible": is_supported_orca_version(manifest.get("orca", {}).get("version")),
            "opi_minimum_version": OPI_MINIMUM_ORCA_VERSION,
            "enabled_versions": list(SUPPORTED_ORCA_VERSIONS)}
    mpi = {**manifest.get("mpi", {}), "parallel_execution": "requires managed backend acceptance tests"}
    issues = list(container.get("issues", []))
    if orca["compatible"] is not True:
        issues.append("container ORCA version is missing or not enabled")
    return orca, mpi, container, issues


def diagnose(config: Config) -> dict:
    packages = {}
    for name in ("orca-agent", "orca-pi", "pydantic", "psutil", "filelock", "pytest", "ruff"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    container = None
    if config.execution.backend == "linux-container":
        orca, mpi, container, issues = _linux_runtime(config)
    else:
        orca, mpi, issues = _windows_runtime(config)
    writable = False
    try:
        config.data_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=config.data_root):
            writable = True
    except OSError:
        issues.append("data_root is not writable")
    os_description = (f"Windows {sys.getwindowsversion()}" if sys.platform == "win32"
                      else platform.platform())
    return {"backend": config.execution.backend,
            "execution_configuration": config.execution.model_dump(mode="json"),
            **({"container": container} if container is not None else {}),
            "os": os_description, "python": sys.version.split()[0],
            "python_executable": sys.executable, "packages": packages, "orca": orca, "mpi": mpi,
            "data_root": str(config.data_root), "data_root_writable": writable,
            "host_resources": {"logical_cpus": psutil.cpu_count(),
                               "available_memory_bytes": psutil.virtual_memory().available},
            "configured_limits": {"cores": config.resources.max_cores,
                                  "job_commit_memory_mb": config.resources.total_memory_mb,
                                  "maxcore_mb_per_process": config.resources.maxcore_mb,
                                  "global_concurrency": config.resources.environment_concurrency},
            "job_control": "requires managed backend acceptance tests",
            "source_hashes": {str(p.relative_to(Path(__file__).parent)):
                              hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(Path(__file__).parent.rglob("*.py"))},
            "scientific_execution": "not performed by doctor", "issues": issues}


def capture_environment(config: Config, *, require_mpi: bool = True) -> dict:
    """Capture identity before Run creation; never build, pull or start a container."""
    from orca_agent.store import StoreError, sha256_file

    environment = diagnose(config)
    environment["backend"] = config.execution.backend
    environment["execution_configuration"] = config.execution.model_dump(mode="json")
    if (environment["orca"].get("compatible") is not True
            or not is_supported_orca_version(environment["orca"].get("version"))):
        raise StoreError("ORCA installation is missing, incompatible or unverified")
    if config.execution.backend == "linux-container":
        try:
            _container_identity(config.execution, environment.get("container", {}))
        except ValueError as exc:
            raise StoreError(str(exc)) from exc
    else:
        if not config.orca_path or not config.orca_path.is_file():
            raise StoreError("ORCA executable is missing")
        environment["orca"].update(path=str(config.orca_path), sha256=sha256_file(config.orca_path))
        if require_mpi:
            if not config.mpi_path or not config.mpi_path.is_file():
                raise StoreError("parallel execution requires the explicit MPI executable")
            environment["mpi"].update(path=str(config.mpi_path), sha256=sha256_file(config.mpi_path))
    return environment


def _frozen_execution(store, config, run):
    from orca_agent.store import StoreError

    persisted = store.load_run(run.id)
    execution = store.run_configuration(persisted).execution
    if store.run_configuration(run).execution != execution or config.execution != execution:
        raise StoreError("execution backend configuration differs from the frozen Run configuration")
    return execution


def validate_environment(store, config, run, *, require_mpi: bool = True) -> dict:
    """Compare current trusted runtime identity with the original Run snapshot."""
    from orca_agent.store import StoreError, sha256_file

    execution = _frozen_execution(store, config, run)
    environment = store._read_json(f"runs/{run.id}/environment.json")
    if environment.get("backend", "windows-lpac") != execution.backend:
        raise StoreError("environment backend differs from the frozen Run configuration")
    if ("execution_configuration" in environment
            and environment["execution_configuration"] != execution.model_dump(mode="json")):
        raise StoreError("environment execution configuration changed after validation")
    if not is_supported_orca_version(environment.get("orca", {}).get("version")):
        raise StoreError("frozen ORCA version is not enabled")
    if execution.backend == "linux-container":
        if environment.get("execution_configuration") != execution.model_dump(mode="json"):
            raise StoreError("Linux environment lacks the explicit frozen execution configuration")
        try:
            daemon_id, manifest = _container_identity(execution, environment.get("container", {}))
            observed_daemon, _ = _container_identity(execution, _inspect_container_environment(execution))
        except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
            raise StoreError(f"container environment validation failed: {exc}") from exc
        if observed_daemon != daemon_id:
            raise StoreError("container daemon identity changed after environment validation")
        for name in ("orca", "mpi"):
            for key in ("path", "version", "sha256"):
                if environment.get(name, {}).get(key) != manifest[name][key]:
                    raise StoreError(f"frozen {name} identity differs from its pinned manifest")
    else:
        for name in ("orca", "mpi") if require_mpi else ("orca",):
            path = getattr(config, f"{name}_path")
            frozen = environment.get(name, {})
            if (not path or not path.is_file() or str(path) != frozen.get("path")
                    or sha256_file(path) != frozen.get("sha256")):
                raise StoreError(f"{name.upper()} executable changed after environment validation")
    return environment


def freeze_environment(store, config, run, *, require_mpi: bool = True) -> dict:
    """Freeze first use, or validate the existing snapshot without rewriting it."""
    _frozen_execution(store, config, run)
    relative = f"runs/{run.id}/environment.json"
    if store.path(relative).exists():
        return validate_environment(store, config, run, require_mpi=require_mpi)
    environment = capture_environment(config, require_mpi=require_mpi)
    store._write_json(relative, environment, immutable=True)
    return environment
