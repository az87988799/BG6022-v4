"""Regression of real evidence/projection failures; no HTTP or scientific execution."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from orca_agent import context
from orca_agent.store import Store
from orca_agent.tools import evidence, knowledge
from tests.unit.test_context import objects, payload
from tests.unit.test_versioned_knowledge import call, mock_server


@pytest.mark.parametrize("text", [
    "保留全部平移/转动项，不要把程序正常结束当成优化收敛或极小值确认。",
    "比较 α/β、kJ/mol、Opt/Freq；报告缺失项，不伪造。",
    "压力/温度与能量/构型要求保持原文。",
])
def test_scientific_slashes_survive_actual_model_request(text):
    request, run = objects()
    request.original_text = text
    wire = payload(context.build_context(request, run, user_messages=[
        {"id": "message_scientific", "text": text, "source": "user"}]))
    original = wire["AUTHORITY"]["user_originals"][0]
    assert original["text"] == text
    assert not original["path_redacted"]
    assert original["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert wire["AUTHORITY"]["user_messages"][0]["message"]["text"] == text
    assert request.original_text == text


def test_complete_failed_t1_original_survives_projection():
    source = Path(__file__).parents[2] / (
        "docs/acceptance/code-as-tools-step7-refreeze-1/review/t1-source-diagnosis.json")
    original = json.loads(source.read_bytes())["original_text"]
    assert "平移/转动项" in original
    assert context._original(original)["text"] == original


@pytest.mark.parametrize("text", [
    r"read E:\private\water.xyz now",
    "read /tmp/private.out now",
    r"read \\host\share\private.xyz now",
    "read file:///tmp/private.out now",
    "读取/tmp/private.out。",
    "文件位于/tmp/private.out",
    "读取/私密/private.xyz。",
    "读取/private.out。",
])
def test_actual_host_paths_remain_redacted(text):
    projected = context._original(text)
    assert projected["path_redacted"] is True
    assert "private" not in projected["text"]
    assert projected["sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_tool_cost_visible_in_actual_wire_and_shared_with_reservation():
    request, run = objects()
    run.permission.allowed_tools = ["workspace.write", "python.execute", "evidence.text"]
    run.permission.artifact_writes = run.permission.code_execution = True
    run.budget.input_tokens = 64000
    run.budget.model_tokens = 120000
    run.budget.analysis_executions = 4
    run.usage.analysis_executions = 2
    wire = payload(context.build_context(request, run, relevant_tools=["workspace.write"]))
    tools = wire.get("TOOLS", wire.get("TOOL_CATALOG"))
    writer = next(tool for tool in tools if tool["name"] == "workspace.write")
    assert writer["call_budget"]["counter"] == "analysis_executions"
    assert writer["call_budget"]["units_per_call"] == 1
    assert writer["call_budget"]["remaining"] == 2
    assert run.usage.analysis_executions == 2


@pytest.mark.parametrize("raw", [b"\x00\x00binary", b"\xff\xfeabc", b"hello\x01bad"])
@pytest.mark.parametrize("reader", ["text", "search"])
def test_binary_evidence_is_not_successful_text_or_silently_converted(tmp_path, raw, reader):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source = tmp_path / "job.cis"
    source.write_bytes(raw)
    artifact = store.import_artifact(source, "raw_output")
    before = {str(path): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="UTF-8 text|binary control"):
        if reader == "text":
            evidence.inspect_artifact(store, artifact.id)
        else:
            evidence.search_text(store, artifact.id, "hello")
    assert before == {str(path): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
    assert source.read_bytes() == raw


def test_actual_table_lines_are_returned_without_format_invention(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source = tmp_path / "stdout.out"
    raw = "Transition    Energy (eV)    fosc(D2)\n0-1A -> 1-1A    4.123    0.001\n"
    source.write_text(raw, encoding="utf-8")
    artifact = store.import_artifact(source, "raw_output")
    result = evidence.inspect_artifact(store, artifact.id)
    assert [line["text"] for line in result["lines"]] == raw.splitlines()
    assert result["scientific_status"] == "unverified"
    found = evidence.search_text(store, artifact.id, "0-1A")
    assert found["matches"][0]["line"] == 2
    assert source.read_text(encoding="utf-8") == raw


def test_manual_index_is_navigation_and_does_not_implicitly_fetch_section(monkeypatch):
    raw = (b'<article><h1>Manual index</h1><p>NMR parameters</p>'
           b'<a href="contents/spectroscopyproperties/nmr.html">NMR chapter</a></article>')
    requests = mock_server(monkeypatch, lambda request: httpx.Response(200, content=raw))
    store = SimpleNamespace(run_configuration=lambda run: SimpleNamespace(
        execution=SimpleNamespace(native_input_policy=SimpleNamespace(profile="managed-native-1"))))
    result, original, _ = knowledge._retrieve(store, None, call("orca:index.html", "NMR"))
    assert result["status"] == "found"
    assert result["coverage"]["document_scope"] == "navigation"
    assert "do not establish input syntax" in result["coverage"]["limitation"]
    assert result["manual_references"] == ["orca:contents/spectroscopyproperties/nmr.html"]
    assert original == raw and len(requests) == 1
    assert result["scientific_qualification"] is False
