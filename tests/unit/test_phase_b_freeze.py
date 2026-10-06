"""The formal gate checks exact frozen inputs before any live reservation."""

import json

import pytest

from tests.helpers import phase_b_freeze as gate


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    source = tmp_path / "agent.py"
    raw = tmp_path / "job.out"
    source.write_bytes(b"value = 1\r\n")
    raw.write_bytes(b"ORCA\r\r\n")
    path = tmp_path / "freeze.json"
    record = {"freeze_label": "formal-test", "code_commit": "a" * 40,
              "files": {"agent.py": gate.freeze_hash(source, source_text=True),
                        "job.out": gate.freeze_hash(raw)},
              "source_lf_normalization": ["agent.py"]}
    path.write_text(json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(gate, "PROJECT", tmp_path)
    monkeypatch.setattr(gate, "FREEZE", path)
    return tmp_path, path, record


def test_source_checkout_line_endings_do_not_relabel_raw_evidence(frozen):
    root, _, _ = frozen
    (root / "agent.py").write_bytes(b"value = 1\n")
    assert gate.validate_freeze("formal-test")["code_commit"] == "a" * 40
    (root / "job.out").write_bytes(b"ORCA\r\n")
    with pytest.raises(ValueError, match="job.out"):
        gate.validate_freeze("formal-test")


def test_code_edit_and_different_label_are_rejected(frozen):
    root, _, _ = frozen
    with pytest.raises(ValueError, match="label differs"):
        gate.validate_freeze("formal-new")
    (root / "agent.py").write_bytes(b"value = 2\n")
    with pytest.raises(ValueError, match="agent.py"):
        gate.validate_freeze("formal-test")


def test_freeze_cannot_reference_a_file_outside_project(frozen):
    root, path, record = frozen
    outside = root.parent / (root.name + "-outside.txt")
    outside.write_bytes(b"external")
    record["files"]["../" + outside.name] = gate.freeze_hash(outside)
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        gate.validate_freeze("formal-test")
