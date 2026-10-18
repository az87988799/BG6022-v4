"""Private, explicitly enumerated runtime copies for isolated task processes.

No host site-packages directory or repository is granted to task processes.
Copies contain ordinary files only; the manifest pins every exposed byte.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

MAX_RUNTIME_BYTES = 4 * 1024 * 1024 * 1024
RUNTIME_VERSION = "private-task-runtime-1"
_DISTRIBUTIONS = {
    "orca-pi": {"opi"},
    "rdkit": {"rdkit", "rdkit.libs"},
    "numpy": {"numpy", "numpy.libs"},
    "Pillow": {"PIL"},
    "platformdirs": {"platformdirs"},
    "pydantic": {"pydantic"},
    "pydantic_core": {"pydantic_core"},
    "annotated-types": {"annotated_types"},
    "typing_extensions": {"typing_extensions.py"},
    "typing-inspection": {"typing_inspection"},
    "semantic-version": {"semantic_version"},
}
_OMIT = {"__pycache__", "site-packages", "test", "tests", "testing", "idlelib", "turtledemo",
         "ensurepip", "venv", "docs", "doc", "benchmarks", ".git"}
ORCA_MODULES = (
    "orca_startup", "orca_guess", "orca_leanscf", "orca_prop", "orca_scfgrad", "orca_util",
)


def _regular(path: Path) -> None:
    for parent in (path, *path.parents):
        value = parent.lstat()
        if parent.is_symlink() or getattr(value, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"runtime source traverses a linked path: {path.name}")
    value = path.stat()
    # Windows system binaries can be hardlinked into WinSxS. Copy their bytes
    # into a new file; never grant ACLs or execution access to the source inode.
    if not stat.S_ISREG(value.st_mode):
        raise ValueError(f"runtime source is not an ordinary file: {path.name}")


def _fresh(destination: Path) -> Path:
    destination = Path(destination).absolute()
    for parent in (destination, *destination.parents):
        if parent.exists():
            value = parent.lstat()
            if parent.is_symlink() or getattr(value, "st_file_attributes", 0) & 0x400:
                raise ValueError("runtime destination traverses a linked path")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise FileExistsError("runtime destination must be fresh; existing runtime is never replaced")
    return destination


def _copy(source: Path, destination: Path, root: Path, files: dict) -> None:
    _regular(source)
    if source.stat().st_size + sum(item["bytes"] for item in files.values()) > MAX_RUNTIME_BYTES:
        raise ValueError("private runtime exceeds its 4 GiB copy bound")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as incoming, destination.open("xb") as outgoing:
        while chunk := incoming.read(1024 * 1024):
            size += len(chunk)
            if size > source.stat().st_size or size + sum(item["bytes"] for item in files.values()) > MAX_RUNTIME_BYTES:
                raise ValueError("runtime source changed or exceeded copy bound")
            digest.update(chunk)
            outgoing.write(chunk)
    files[destination.relative_to(root).as_posix()] = {
        "sha256": digest.hexdigest(), "bytes": size, "source": str(source),
    }


def _generated(destination: Path, data: bytes, root: Path, files: dict) -> None:
    with destination.open("xb") as stream:
        stream.write(data)
    files[destination.relative_to(root).as_posix()] = {
        "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "source": "adapter_generated",
    }


def _finish(root: Path, executable: Path, files: dict, **identity) -> dict:
    manifest = {"version": RUNTIME_VERSION, "files": files,
                "total_bytes": sum(item["bytes"] for item in files.values()), **identity}
    with (root / "runtime-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return {"executable": executable, "manifest": manifest}


def stage_python_runtime(destination: Path, sdk_source: Path) -> dict:
    """Copy base CPython, selected distribution records and the trusted thin SDK."""
    if os.name != "nt":
        raise RuntimeError("private CPython runtime staging is currently Windows-only")
    root = _fresh(destination)
    base = Path(sys.base_prefix).absolute()
    tag = f"python{sys.version_info.major}{sys.version_info.minor}"
    files = {}
    for name in ("python.exe", f"{tag}.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        _copy(base / name, root / name, root, files)
    # Never expose the venv launcher, pyvenv.cfg, host .pth files or the user's
    # site-packages. The explicit _pth file ignores registry/environment paths.
    for source in sorted((base / "Lib").rglob("*")):
        relative = source.relative_to(base / "Lib")
        if (source.is_file() and not set(relative.parts) & _OMIT
                and source.suffix.lower() not in {".pyc", ".pyo", ".pth", ".exe", ".bat", ".cmd"}):
            _copy(source, root / "Lib" / relative, root, files)
    for source in sorted((base / "DLLs").iterdir()):
        if source.is_file() and source.suffix.lower() in {".pyd", ".dll"}:
            _copy(source, root / "DLLs" / source.name, root, files)
    distributions = {}
    for name, packages in _DISTRIBUTIONS.items():
        distribution = importlib.metadata.distribution(name)
        distributions[name] = distribution.version
        if not distribution.files:
            raise ValueError(f"runtime distribution lacks an installed file manifest: {name}")
        copied = 0
        for item in sorted(distribution.files, key=str):
            relative = PurePosixPath(str(item).replace("\\", "/"))
            if relative.is_absolute() or ".." in relative.parts:
                continue  # Distribution entry-point executables are not runtime libraries.
            if relative.parts[0] not in packages and not relative.parts[0].endswith(".dist-info"):
                continue
            if set(relative.parts) & _OMIT or relative.suffix.lower() in {".pyc", ".pyo", ".pth", ".exe", ".bat", ".cmd"}:
                continue
            source = Path(distribution.locate_file(item)).absolute()
            _copy(source, root / "site-packages" / Path(*relative.parts), root, files)
            copied += 1
        if not copied:
            raise ValueError(f"runtime distribution has no admitted files: {name}")
    _copy(Path(sdk_source).absolute(), root / "orca_task_sdk.py", root, files)
    _generated(root / f"{tag}._pth", b"Lib\nDLLs\nsite-packages\n.\n", root, files)
    return _finish(root, root / "python.exe", files, kind="python", python_version=sys.version,
                   distributions=distributions, site_initialization=False,
                   omitted="host paths, project source, credentials, .pth, entry-point executables")


def _copy_mpi(root: Path, executable: Path, files: dict) -> dict:
    from orca_agent.doctor import file_version

    executable = Path(executable).absolute()
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    sources = {"mpiexec.exe": executable, "smpd.exe": executable.parent / "smpd.exe",
               "msmpi.dll": windows / "System32" / "msmpi.dll",
               "msmpires.dll": windows / "System32" / "msmpires.dll"}
    for name, source in sources.items():
        _copy(source, root / name, root, files)
    return {"path": str(executable), "file_version": file_version(executable),
            "files": list(sources), "launch_service_staged": False,
            "validation_scope": "local process launch; MPI communication requires separate live evidence"}


def stage_mpi_runtime(destination: Path, executable: Path) -> dict:
    """Copy the four fixed local MS-MPI runtime files, without launch services."""
    if os.name != "nt":
        raise RuntimeError("private MS-MPI runtime staging is currently Windows-only")
    root = _fresh(destination)
    files = {}
    identity = _copy_mpi(root, executable, files)
    return _finish(root, root / "mpiexec.exe", files, kind="msmpi", mpi=identity)


def stage_orca_runtime(destination: Path, executable: Path, *, mpi_path: Path | None = None) -> dict:
    """Copy the installed ORCA driver and fixed SP/legacy Opt runtime helpers.

    An input requiring a missing module must report ORCA's actual failure. No
    arbitrary helper is discovered/copied as a fallback and no converter exists
    in this runtime. MPI service/runtime compatibility is validated separately.
    """
    if os.name != "nt":
        raise RuntimeError("private ORCA runtime staging is currently Windows-only")
    root = _fresh(destination)
    executable = Path(executable).absolute()
    files = {}
    _copy(executable, root / "orca.exe", root, files)
    for module in ORCA_MODULES:
        for suffix in (".exe", "_mpi.exe"):
            name = module + suffix
            _copy(executable.parent / name, root / name, root, files)
    # ORCA's Windows driver uses system() for managed helper launches. Copy its
    # exact system command interpreter into the same restricted native runtime;
    # this is not made available to task Python, whose child processes are denied.
    windows = Path(os.environ.get("SystemRoot", "C:/Windows"))
    _copy(windows / "System32" / "cmd.exe", root / "cmd.exe", root, files)
    for source in sorted(executable.parent.glob("*.dll")):
        _copy(source, root / source.name, root, files)
    mpi = _copy_mpi(root, mpi_path, files) if mpi_path is not None else None
    return _finish(root, root / "orca.exe", files, kind="orca",
                   modules=[f"{name}{suffix}" for name in ORCA_MODULES for suffix in (".exe", "_mpi.exe")],
                   command_interpreter="cmd.exe", mpi=mpi,
                   mpi_runtime="private local MS-MPI files; communication separately verified" if mpi else "not staged",
                   omitted="orca_2json, other scientific modules, external tools, user scripts")
