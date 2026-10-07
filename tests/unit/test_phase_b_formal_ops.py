"""Formal operator records remain immutable even when the offline runner fails."""

import json
from types import SimpleNamespace

import pytest

from orca_agent.store import sha256_file
from tests.helpers import phase_b_formal_ops as ops


@pytest.mark.parametrize("label", ["../outside", "a" * 39])
def test_invalid_label_is_rejected_before_freeze_or_filesystem_access(label, monkeypatch):
    monkeypatch.setattr(ops, "git", lambda *args: pytest.fail("invalid label reached git"))
    monkeypatch.setattr(ops, "validate_freeze", lambda _: pytest.fail("invalid label reached files"))
    with pytest.raises(ValueError, match="38 characters"):
        ops.freeze(label)
    with pytest.raises(ValueError, match="38 characters"):
        ops.offline(label, 1)


def test_offline_failure_is_recorded_and_returns_nonzero_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "PROJECT", tmp_path)
    monkeypatch.setattr(ops, "validate_freeze", lambda _, **kwargs: {
        "freeze_sha256": "a" * 64, "code_commit": "b" * 40})
    path = tmp_path / "docs/acceptance/phase-b/coverage.json"
    path.parent.mkdir(parents=True)
    nodes = [f"tests/unit/test_example.py::test_boundary[{i}]" for i in range(26)]
    path.write_text(json.dumps({"entries": [{"variant_id": f"example-{i}",
        "evidence_requirement": "offline_fault_injection", "formal_slots": [{
            "variant_id": f"example-{i}", "repetition": 1, "pytest_nodeids": [node]}]}
        for i, node in enumerate(nodes)]}), encoding="utf-8")
    calls = []

    def failed_pytest(command, **kwargs):
        calls.append(command)
        assert "--live-model" not in command and "--live-orca" not in command
        assert all(node in command for node in nodes)
        xml = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--junitxml="))
        from pathlib import Path
        Path(xml).write_text('<testsuites><testsuite tests="1" failures="1"/></testsuites>',
                             encoding="utf-8")
        kwargs["stdout"].write("controlled offline failure\n")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(ops.subprocess, "run", failed_pytest)
    assert ops.offline("formal-test", 1) == 1
    directory = tmp_path / "data/phase-b/offline-evaluations/formal-test/1"
    receipt = directory / "receipt.json"
    before = receipt.read_bytes()
    record = json.loads(before)
    assert record["pytest_returncode"] == 1
    assert record["junit_sha256"] == sha256_file(directory / "results.xml")
    assert record["log_sha256"] == sha256_file(directory / "pytest.txt")
    with pytest.raises(FileExistsError):
        ops.offline("formal-test", 1)
    assert len(calls) == 1 and receipt.read_bytes() == before
