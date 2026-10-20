"""Non-text read failures remain actionable through dispatch and the actual model wire."""

import hashlib
import json

import pytest

from orca_agent import context
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import Store
from orca_agent.tools import evidence
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_context import payload


def read_case(tmp_path, raw):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source = tmp_path / "private-source.cis"
    source.write_bytes(raw)
    artifact = store.import_artifact(source, "raw_output")
    request = Request(
        original_text="Explain what can be read from the registered evidence.",
        goals=[Goal(id="question", port="knowledge_answer", minimum_check_version="knowledge-answer-1")],
    )
    run = store.create_run(request, None, PermissionSnapshot(
        model_execution=True, allowed_tools=["evidence.text", "evidence.search"],
        artifact_ids=[artifact.id],
    ), BudgetLimits(model_calls=4, model_tokens=48000, input_tokens=12000,
                    output_tokens=2000, decision_rounds=8, evidence_reads=4))
    return store, request, run, source, artifact


def read(store, run, tool, artifact):
    parameters = {"artifact_id": artifact.id}
    if tool == "evidence.search":
        parameters["query"] = "header"
    return execute_call(store, run, tool, parameters)


@pytest.mark.parametrize("tool", ["evidence.text", "evidence.search"])
@pytest.mark.parametrize("raw,reason", [
    (b"\x00private-source-bytes", "evidence_source_binary_controls"),
    (b"header\x01private-source-bytes", "evidence_source_binary_controls"),
    (b"\xff\xfeprivate-source-bytes", "evidence_source_not_utf8"),
])
def test_nontext_dispatch_failure_reaches_actual_context_without_raw_content(tmp_path, tool, raw, reason):
    store, request, run, source, artifact = read_case(tmp_path, raw)
    result = read(store, run, tool, artifact)

    assert result.operation_status == "failed"
    assert result.observations == result.qualified_outputs == result.checks == {}
    assert result.diagnostics == [{
        "category": "NonTextEvidenceError", "reason": reason,
        "message": evidence.NonTextEvidenceError.MESSAGES[reason],
    }]
    assert store.load_result(run.id, result.id) == result
    assert run.calls[0].state == "failed" and run.calls[0].result_id == result.id
    assert run.usage.evidence_reads == 1
    assert not run.attempts and run.usage.orca_starts_actual == run.usage.model_calls == 0
    assert source.read_bytes() == store.artifact_path(artifact.id).read_bytes() == raw
    assert store.load_artifact(artifact.id).sha256 == hashlib.sha256(raw).hexdigest()

    prepared = context.build_context(request, run, results=[result],
                                     feedback={"new_result_ids": [result.id]})
    wire = payload(prepared)
    visible = next(item for item in wire["DATA"]["results"] if item["result_id"] == result.id)
    assert visible["operation_status"] == "failed"
    assert visible["untrusted_diagnostics"] == result.diagnostics
    assert "evidence.list" in visible["untrusted_diagnostics"][0]["message"]
    assert "private-source" not in prepared.canonical_body
    assert str(tmp_path) not in prepared.canonical_body


@pytest.mark.parametrize("tool,function", [
    ("evidence.text", "inspect_artifact"), ("evidence.search", "search_text"),
])
def test_arbitrary_exception_cannot_spoof_nontext_reason_or_disclose_payload(
    tmp_path, monkeypatch, tool, function,
):
    store, request, run, _, artifact = read_case(tmp_path, b"header\n")

    def fail(*args, **kwargs):
        raise ValueError("source is not UTF-8 text; credential=private-source-bytes /tmp/private-source")

    monkeypatch.setattr(evidence, function, fail)
    result = read(store, run, tool, artifact)
    assert result.operation_status == "failed"
    assert result.diagnostics == [{
        "category": "ValueError", "message": "Tool rejected inputs or unavailable evidence",
    }]
    wire = payload(context.build_context(request, run, results=[result]))
    assert "private-source" not in json.dumps(wire)
    assert "credential" not in json.dumps(wire)
