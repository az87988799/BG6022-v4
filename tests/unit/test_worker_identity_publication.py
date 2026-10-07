"""Deterministically reproduce the old half-write window, then check publication."""

import json
from pathlib import Path

import pytest

from tests.helpers import backend_worker
from tests.integration.test_backend_windows import records


def test_legacy_direct_identity_write_has_a_visible_invalid_window(tmp_path):
    # This is the previous Path.write_text algorithm stopped after creation,
    # using the actual project's concurrent reader and its filename pattern.
    path = tmp_path / "node-123.json"
    with path.open("w", encoding="utf-8") as stream:
        stream.write('{"pid":')
        stream.flush()
        with pytest.raises(json.JSONDecodeError):
            records(tmp_path)
        stream.write('123,"create_time":1.0}')
    assert records(tmp_path) == [{"pid": 123, "create_time": 1.0}]


def test_worker_identity_is_published_only_as_complete_json(tmp_path, monkeypatch):
    from orca_agent import store

    replace = store.os.replace
    observations = []

    def inspect_before_publish(source, destination):
        if Path(destination).name.startswith("node-"):
            observations.append(records(tmp_path))
            assert json.loads(Path(source).read_text(encoding="utf-8"))["pid"] > 0
        return replace(source, destination)

    monkeypatch.setattr(store.os, "replace", inspect_before_publish)
    value = backend_worker.record(tmp_path)
    assert observations == [[]]
    assert records(tmp_path) == [value]
    assert not list(tmp_path.glob("*.tmp"))


def test_bad_published_identity_is_not_treated_as_absent(tmp_path):
    (tmp_path / "node-123.json").write_text('{"pid":', encoding="utf-8")
    with pytest.raises(json.JSONDecodeError):
        records(tmp_path)
