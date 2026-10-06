"""Bounded installation diagnostics. Version flags never submit scientific input."""

import ctypes
import hashlib
import importlib.metadata
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


def diagnose(config: Config) -> dict:
    packages = {}
    for name in ("orca-agent", "orca-pi", "pydantic", "psutil", "filelock", "pytest", "ruff"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
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
    writable = False
    try:
        config.data_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=config.data_root):
            writable = True
    except OSError:
        issues.append("data_root is not writable")
    os_description = (f"Windows {sys.getwindowsversion()}" if sys.platform == "win32"
                      else platform.platform())
    return {"os": os_description, "python": sys.version.split()[0],
            "python_executable": sys.executable, "packages": packages, "orca": orca, "mpi": mpi,
            "data_root": str(config.data_root), "data_root_writable": writable,
            "host_resources": {"logical_cpus": psutil.cpu_count(),
                               "available_memory_bytes": psutil.virtual_memory().available},
            "configured_limits": {"cores": 4, "job_commit_memory_mb": 1024,
                                  "maxcore_mb_per_process": 192, "global_concurrency": 1},
            "job_control": "requires managed backend acceptance tests",
            "source_hashes": {str(p.relative_to(Path(__file__).parent)):
                              hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in sorted(Path(__file__).parent.rglob("*.py"))},
            "scientific_execution": "not performed by doctor", "issues": issues}
