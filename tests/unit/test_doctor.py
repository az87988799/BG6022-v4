import subprocess

import pytest

from orca_agent.config import Config
from orca_agent.doctor import diagnose


def test_missing_environment_is_reported_without_launch(tmp_path):
    report = diagnose(Config(data_root=tmp_path))
    assert report["orca"]["compatible"] is None
    assert report["issues"]
    assert report["scientific_execution"] == "not performed by doctor"


def test_banner_probe_does_not_mistake_libxc_for_orca(tmp_path, monkeypatch):
    executable = tmp_path / "orca.exe"
    executable.touch()
    calls = []

    def probe(args, **kwargs):
        calls.append(args)
        assert args == [str(executable), "--version"]
        return subprocess.CompletedProcess(args, 2, "Program Version 6.1.1 - RELEASE\nlibXC 7.0.0", "")

    monkeypatch.setattr(subprocess, "run", probe)
    report = diagnose(Config(orca_path=executable, data_root=tmp_path))
    assert report["orca"]["version"] == "6.1.1"
    assert report["orca"]["compatible"] is True
    assert len(calls) == 1


@pytest.mark.parametrize("token,accepted", [
    ("6.1.1", True), ("6.1.0", False), ("6.1.2", False), ("6.2.0", False),
    ("6.1.1-f.1", False), ("6.1.1+unknown", False),
])
def test_probe_only_enables_exact_project_version_and_preserves_token(
    tmp_path, monkeypatch, token, accepted
):
    executable = tmp_path / "orca.exe"
    executable.touch()
    calls = []

    def probe(args, **kwargs):
        calls.append(args)
        assert args == [str(executable), "--version"]
        return subprocess.CompletedProcess(args, 2, f"Program Version {token} - RELEASE\n", "")

    monkeypatch.setattr(subprocess, "run", probe)
    report = diagnose(Config(orca_path=executable, data_root=tmp_path))
    assert report["orca"]["version"] == token
    assert report["orca"]["compatible"] is accepted
    assert len(calls) == 1


@pytest.mark.parametrize("banner", [
    "libXC 7.0.0\n", "Program Version\n6.1.1\n", "Program Version RELEASE\n",
    "Program Version 6.1.1\nProgram Version 6.1.1\n",
    "Program Version 6.1.1\nProgram Version 6.2.0\n",
    "Program Version 6.1.1\nProgram Version\n",
    "Program Version\nProgram Version 6.1.1\n",
])
def test_missing_ambiguous_or_incomplete_banner_never_enables_execution(tmp_path, monkeypatch, banner):
    executable = tmp_path / "orca.exe"
    executable.touch()
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(
        args, 2, banner, ""))
    report = diagnose(Config(orca_path=executable, data_root=tmp_path))
    assert report["orca"]["compatible"] is not True
    assert report["issues"]
