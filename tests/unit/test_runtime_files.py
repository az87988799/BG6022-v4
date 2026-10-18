"""Runtime copy admission/provenance tests, using files rather than OS launch mocks."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.backends import runtime_files

pytestmark = pytest.mark.skipif(os.name != "nt", reason="private runtime is Windows-specific")


def test_python_copy_has_explicit_packages_no_host_paths_or_pth(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    tag = f"python{runtime_files.sys.version_info.major}{runtime_files.sys.version_info.minor}"
    for name in ("python.exe", f"{tag}.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        (base / name).write_bytes(name.encode())
    for name in ("Lib/encodings/__init__.py", "Lib/os.py", "DLLs/_hashlib.pyd", "Lib/test/secret.py",
                 "Lib/site-packages/private_key.py", "Lib/injected.pth", "Lib/ensurepip/pip.whl"):
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    installed = tmp_path / "installed"
    entries = ["admitted/__init__.py", "admitted-1.dist-info/METADATA", "injected.pth",
               "unrelated/secrets.py", "admitted/tests/private.py", "../Scripts/launch.exe"]
    for entry in entries:
        path = installed / entry
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry.encode())
    distribution = SimpleNamespace(version="1", files=[Path(entry) for entry in entries],
                                   locate_file=lambda entry: installed / entry)
    monkeypatch.setattr(runtime_files.sys, "base_prefix", str(base))
    monkeypatch.setattr(runtime_files, "_DISTRIBUTIONS", {"approved": {"admitted"}})
    monkeypatch.setattr(runtime_files.importlib.metadata, "distribution", lambda name: distribution)
    sdk = tmp_path / "sdk.py"
    sdk.write_text("def run(): pass\n")
    target = tmp_path / "private"
    result = runtime_files.stage_python_runtime(target, sdk)
    names = set(result["manifest"]["files"])
    assert "site-packages/admitted/__init__.py" in names
    assert not any("secret" in name or "test/" in name or "tests/" in name for name in names)
    assert not any(name.endswith(".pth") for name in names)
    assert (target / f"{tag}._pth").read_text() == "Lib\nDLLs\nsite-packages\n.\n"
    assert str(base) not in (target / f"{tag}._pth").read_text()
    assert result["manifest"]["distributions"] == {"approved": "1"}
    for name, item in result["manifest"]["files"].items():
        assert hashlib.sha256((target / name).read_bytes()).hexdigest() == item["sha256"]
        assert (target / name).stat().st_nlink == 1
    assert json.loads((target / "runtime-manifest.json").read_text(encoding="utf-8"))["files"] == result["manifest"]["files"]
    with pytest.raises(FileExistsError):
        runtime_files.stage_python_runtime(target, sdk)


def test_orca_private_copy_contains_only_pinned_helpers(tmp_path, monkeypatch):
    installed = tmp_path / "orca"
    installed.mkdir()
    wanted = {"orca.exe", "required.dll"}
    wanted.update(name + suffix for name in runtime_files.ORCA_MODULES for suffix in (".exe", "_mpi.exe"))
    for name in wanted | {"orca_2json.exe", "otool_external.exe", "unrelated.py"}:
        (installed / name).write_bytes(name.encode())
    system = tmp_path / "windows/System32"
    system.mkdir(parents=True)
    (system / "cmd.exe").write_bytes(b"trusted system cmd")
    os.link(system / "cmd.exe", system / "cmd-winsxs-hardlink.exe")
    monkeypatch.setenv("SystemRoot", str(system.parent))
    result = runtime_files.stage_orca_runtime(tmp_path / "private", installed / "orca.exe")
    assert set(result["manifest"]["files"]) == wanted | {"cmd.exe"}
    assert (tmp_path / "private/cmd.exe").stat().st_nlink == 1
    assert result["executable"] == tmp_path / "private/orca.exe"


def test_missing_orca_helper_has_no_discovery_fallback(tmp_path, monkeypatch):
    installed = tmp_path / "orca"
    installed.mkdir()
    (installed / "orca.exe").write_bytes(b"driver")
    with pytest.raises(FileNotFoundError):
        runtime_files.stage_orca_runtime(tmp_path / "private", installed / "orca.exe")
    assert not (tmp_path / "private/runtime-manifest.json").exists()


def test_runtime_size_bound_keeps_incomplete_copy_unusable(tmp_path, monkeypatch):
    source = tmp_path / "large"
    source.write_bytes(b"too large")
    root = tmp_path / "target"
    root.mkdir()
    monkeypatch.setattr(runtime_files, "MAX_RUNTIME_BYTES", 2)
    with pytest.raises(ValueError, match="copy bound"):
        runtime_files._copy(source, root / "copy", root, {})
    assert not (root / "copy").exists()


def test_private_mpi_is_fixed_local_runtime_without_service(tmp_path, monkeypatch):
    installed = tmp_path / "mpi"
    installed.mkdir()
    for name in ("mpiexec.exe", "smpd.exe", "msmpilaunchsvc.exe"):
        (installed / name).write_bytes(name.encode())
    system = tmp_path / "windows/System32"
    system.mkdir(parents=True)
    for name in ("msmpi.dll", "msmpires.dll"):
        (system / name).write_bytes(name.encode())
    monkeypatch.setenv("SystemRoot", str(system.parent))
    result = runtime_files.stage_mpi_runtime(tmp_path / "private", installed / "mpiexec.exe")
    assert set(result["manifest"]["files"]) == {"mpiexec.exe", "smpd.exe", "msmpi.dll", "msmpires.dll"}
    assert result["manifest"]["mpi"]["launch_service_staged"] is False
    assert result["executable"] == tmp_path / "private/mpiexec.exe"


def test_orca_optional_mpi_files_share_the_same_provenance_manifest(tmp_path, monkeypatch):
    installed = tmp_path / "orca"
    installed.mkdir()
    for name in {"orca.exe", *(module + suffix for module in runtime_files.ORCA_MODULES for suffix in (".exe", "_mpi.exe"))}:
        (installed / name).write_bytes(name.encode())
    mpi = tmp_path / "mpi"
    mpi.mkdir()
    for name in ("mpiexec.exe", "smpd.exe"):
        (mpi / name).write_bytes(name.encode())
    system = tmp_path / "windows/System32"
    system.mkdir(parents=True)
    for name in ("cmd.exe", "msmpi.dll", "msmpires.dll"):
        (system / name).write_bytes(name.encode())
    monkeypatch.setenv("SystemRoot", str(system.parent))
    result = runtime_files.stage_orca_runtime(tmp_path / "private", installed / "orca.exe", mpi_path=mpi / "mpiexec.exe")
    assert result["manifest"]["files"]["mpiexec.exe"]["sha256"] == hashlib.sha256(b"mpiexec.exe").hexdigest()
    assert result["manifest"]["mpi"]["path"] == str(mpi / "mpiexec.exe")
