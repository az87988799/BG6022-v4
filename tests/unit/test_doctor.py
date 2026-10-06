import subprocess

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
