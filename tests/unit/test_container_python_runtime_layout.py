"""Pure fixed-helper/transport checks; no container or task process is started."""

import hashlib
import io
import json
from types import SimpleNamespace

import pytest

from orca_agent.backends import container
from orca_agent.backends.container_runtime import helper


def _folders(tmp_path):
    task, logs = tmp_path / "task", tmp_path / "python-runtime"
    task.mkdir()
    logs.mkdir()
    for name in ("stdout.out", "stderr.txt", ".launch.json"):
        (task / name).write_bytes(b"original input: " + name.encode())
    return task, logs


def test_new_launch_keeps_input_names_and_cwd(monkeypatch, tmp_path):
    task, logs = _folders(tmp_path)
    original = {path.name: path.read_bytes() for path in task.iterdir()}
    calls = []
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "require_identity", lambda _area: None)
    monkeypatch.setattr(helper.os, "chdir", lambda folder: calls.append(("cwd", folder)))
    monkeypatch.setattr(helper.os, "dup2", lambda _fd, target: calls.append(("fd", target)))

    def gated(folder, area, timeout):
        calls.append(("gate", folder, area, timeout))
        (folder / ".launch.json").write_text('{"pid": 123}', encoding="utf-8")

    def execve(executable, argv, env):
        calls.append(("exec", executable, argv, env))

    monkeypatch.setattr(helper, "gated_child", gated)
    monkeypatch.setattr(helper.os, "execve", execve)
    helper.launch("python", 10, "python", "isolated-1")
    assert calls[0] == ("cwd", task)
    assert calls[1] == ("gate", logs, "python", 10)
    assert calls[-1][0] == "exec"
    assert calls[-1][3]["HOME"] == str(task)
    assert {path.name: path.read_bytes() for path in task.iterdir()} == original
    assert {path.name for path in logs.iterdir()} == {"stdout.out", "stderr.txt", ".launch.json"}


def _export(monkeypatch, tmp_path, limits, *, layout="isolated-1"):
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "require_identity", lambda _area: None)
    monkeypatch.setattr(helper.signal, "SIGALRM", 14, raising=False)
    monkeypatch.setattr(helper.signal, "ITIMER_REAL", 0, raising=False)
    monkeypatch.setattr(helper.signal, "signal", lambda *args: None)
    monkeypatch.setattr(helper.signal, "setitimer", lambda *args: None, raising=False)
    monkeypatch.setattr(helper, "output_files", lambda directory, *_: sorted(directory.iterdir()))
    monkeypatch.setattr(helper, "open_file", lambda area, name, _: helper.os.open(
        helper.area_root(area) / name, helper.os.O_RDONLY))
    stream = io.BytesIO()
    monkeypatch.setattr(helper.sys, "stdout", SimpleNamespace(buffer=stream))
    helper.export("python", limits, layout)
    stream.seek(0)
    return stream


def test_combined_export_preserves_inputs_and_shares_original_budget(monkeypatch, tmp_path):
    task, logs = _folders(tmp_path)
    (logs / ".launch.json").write_text('{"pid": 123}', encoding="utf-8")
    (logs / "stdout.out").write_bytes(b"runtime stdout")
    (logs / "stderr.txt").write_bytes(b"runtime error")
    limits = {"count": 5, "file_bytes": 1000, "total_bytes": 1000, "seconds": 10}
    stream = _export(monkeypatch, tmp_path, limits)
    destination = tmp_path / "collected"
    destination.mkdir()
    result = container._decode_export(stream, destination, limits)
    expected = {"task/" + path.name: path.read_bytes() for path in task.iterdir()}
    expected.update({"runtime-logs/stdout.out": b"runtime stdout", "runtime-logs/stderr.txt": b"runtime error"})
    assert set(result) == set(expected)
    for name, value in expected.items():
        assert (destination / name).read_bytes() == value
        assert result[name]["sha256"] == hashlib.sha256(value).hexdigest()


@pytest.mark.parametrize("field,limit", [("count", 4), ("total_bytes", 65)])
def test_combined_export_does_not_double_limits(monkeypatch, tmp_path, field, limit):
    _folders(tmp_path)
    logs = tmp_path / "python-runtime"
    (logs / "stdout.out").write_bytes(b"runtime stdout")
    (logs / "stderr.txt").write_bytes(b"runtime error")
    limits = {"count": 5, "file_bytes": 1000, "total_bytes": 1000, "seconds": 10, field: limit}
    with pytest.raises(ValueError, match="output budget"):
        _export(monkeypatch, tmp_path, limits)


def test_legacy_export_and_cli_remain_unchanged(monkeypatch, tmp_path):
    task, _ = _folders(tmp_path)
    limits = {"count": 5, "file_bytes": 1000, "total_bytes": 1000, "seconds": 10}
    stream = _export(monkeypatch, tmp_path, limits, layout=None)
    destination = tmp_path / "collected"
    destination.mkdir()
    result = container._decode_export(stream, destination, limits)
    assert set(result) == {"stdout.out", "stderr.txt"}
    assert (destination / "stdout.out").read_bytes() == (task / "stdout.out").read_bytes()
    assert container._python_layout_args({"execution": {}}, "python") == []
    boundary = {"execution": {"python_runtime_layout": "isolated-1"}}
    assert container._python_layout_args(boundary, "python") == ["isolated-1"]
    assert container._python_layout_args(boundary, "orca") == []
    with pytest.raises(ValueError, match="layout"):
        helper.python_runtime_area("orca", "isolated-1")


def test_identity_reads_runtime_metadata_not_same_named_input(monkeypatch, tmp_path, capsys):
    task, logs = _folders(tmp_path)
    identity = {"pid": 123, "start_ticks": "9", "cap_eff": "0", "cap_prm": "0",
                "cap_inh": "0", "cap_amb": "0", "no_new_privs": "1", "seccomp": "2"}
    (logs / ".launch.json").write_text(json.dumps(identity), encoding="utf-8")
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "require_identity", lambda _: None)
    monkeypatch.setattr(helper, "processes", lambda _: [identity])
    monkeypatch.setattr(helper.os, "getuid", lambda: 10001, raising=False)
    monkeypatch.setattr(helper, "open_file", lambda area, name, _: helper.os.open(
        helper.area_root(area) / name, helper.os.O_RDONLY))
    helper.launch_identity("python", layout="isolated-1")
    assert json.loads(capsys.readouterr().out) == identity
    assert (task / ".launch.json").read_bytes() == b"original input: .launch.json"


@pytest.mark.parametrize("layout", [None, "isolated-1"])
def test_collection_uses_only_frozen_handle_layout(monkeypatch, tmp_path, layout):
    execution = {} if layout is None else {"python_runtime_layout": layout}
    handle = {"execution": execution, "image_id": "frozen-image", "volume_name": "frozen-volume"}
    monkeypatch.setattr(container, "_validate_execution", dict)
    boundary = container.boundary_from_handle(handle, local_root=tmp_path)
    boundary["running"] = True
    destination = tmp_path / "task"
    destination.mkdir()
    seen = {}

    def transport(_boundary, _uid, args, **kwargs):
        seen["args"] = args
        kwargs["output_stream"].write(b"bounded synthetic transport")

    def decode(stream, folder, limits, checkpoint, *, deadline):
        seen["destination"] = folder
        return {}

    monkeypatch.setattr(container, "_helper", transport)
    monkeypatch.setattr(container, "_decode_export", decode)
    limits = {"count": 5, "file_bytes": 1000, "total_bytes": 1000, "seconds": 10}
    container.collect(boundary, "python", destination, limits=limits)
    assert seen["args"] == ["export", "python", json.dumps(limits, separators=(",", ":")),
                            *([] if layout is None else [layout])]
    assert seen["destination"] == (destination if layout is None else tmp_path)
