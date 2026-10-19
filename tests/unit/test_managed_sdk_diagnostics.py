"""SDK admission failures remain observable even when task code catches them."""

import hashlib
import json

from orca_agent.tools import managed
from tests.unit.test_managed_attempt_store import make_task
from tests.unit.test_managed_execution import outcome


def test_caught_sdk_failure_is_archived_without_success_payload_or_scientific_upgrade(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    responses = store.path(attempt.directory) / "responses"
    responses.mkdir()
    failed = responses / "response-000001.json"
    raw = json.dumps({"error": "managed_sdk_request_rejected: explicit RKS is required",
                      "unexpected_payload": "must not be exposed"}).encode()
    failed.write_bytes(raw)
    (responses / "response-000002.json").write_text(json.dumps({"error": None, "files": {"secret": "not copied"}}))
    result = managed.collect_result(store, run, step, attempt, outcome())
    assert result.operation_status == "completed" and not result.qualified_outputs
    rejection, = [item for item in result.diagnostics if item["category"] == "managed_sdk_request_rejected"]
    assert "explicit RKS is required" in rejection["detail"]
    assert rejection["raw_response_sha256"] == hashlib.sha256(raw).hexdigest()
    archived = json.loads(store.artifact_path(rejection["artifact_id"]).read_bytes())
    assert archived["classification"] == "bounded_error_only_projection"
    assert "unexpected_payload" not in archived and "must not be exposed" not in json.dumps(result.model_dump(mode="json"))
    assert "sdk_responses/response-000002.json" not in result.source["files"]
    assert failed.read_bytes() == raw and run.usage.orca_starts_actual == 0


def test_sdk_failure_feedback_is_bounded_and_credentials_are_redacted(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, step)
    responses = store.path(attempt.directory) / "responses"
    responses.mkdir()
    for index in range(1, 11):
        error = "sk-" + "x" * 32 if index == 1 else "诊断" * 2000
        (responses / f"response-{index:06d}.json").write_text(json.dumps({"error": error}), encoding="utf-8")
    result = managed.collect_result(store, run, step, attempt, outcome())
    rejections = [item for item in result.diagnostics if item["category"] == "managed_sdk_request_rejected"]
    assert len(rejections) == 8 and all(len(item["detail"].encode("utf-8")) <= 2048 for item in rejections)
    assert rejections[0]["detail"] == "[redacted credential-bearing SDK error]"
    bounded, = [item for item in result.diagnostics if item["category"] == "managed_sdk_diagnostics_bounded"]
    assert bounded["additional_scanned_errors"] == 2
    assert all(item["detail_truncated"] for item in rejections[1:])
    assert not result.qualified_outputs and run.usage.orca_starts_actual == 0
