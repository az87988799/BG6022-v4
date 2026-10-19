"""The thin SDK transports bytes; these mailbox tests launch no process or network."""

import base64
import json
import threading
import time

import pytest

from orca_agent import task_sdk
from orca_agent.models import fingerprint


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    requests, responses = tmp_path / "requests", tmp_path / "responses"
    requests.mkdir()
    responses.mkdir()
    monkeypatch.setenv("ORCA_TASK_REQUESTS", str(requests))
    monkeypatch.setenv("ORCA_TASK_RESPONSES", str(responses))
    monkeypatch.setenv("ORCA_TASK_TIMEOUT_SECONDS", "1")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "job.inp").write_text("! HF STO-3G\n* xyzfile 0 1 geometry.xyz\n", encoding="utf-8")
    return requests, responses


def exchange(mailbox, response, action):
    """A file-only coordinator stand-in records the exact transmitted request."""
    requests, responses = mailbox
    sequence = len(list(requests.glob("request-*.json"))) + 1
    request_path = requests / f"request-{sequence:06d}.json"
    response_path = responses / f"response-{sequence:06d}.json"
    captured, errors = [], []
    stop = threading.Event()

    def peer():
        deadline = time.monotonic() + 2
        try:
            while not request_path.exists():
                if stop.wait(0.005):
                    return
                if time.monotonic() >= deadline:
                    raise TimeoutError("test peer saw no request")
            captured.append(json.loads(request_path.read_bytes()))
            payload = response(captured[0]) if callable(response) else response
            encoded = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            pending = responses / f"response-{sequence:06d}.pending"
            pending.write_bytes(encoded)
            pending.replace(response_path)
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=peer)
    worker.start()
    try:
        return action(), captured
    finally:
        stop.set()
        worker.join(timeout=3)
        assert not worker.is_alive(), "mailbox test thread did not stop"
        assert not errors, errors


def test_sdk_transports_exact_input_and_dependency_bytes_without_scientific_claim(mailbox):
    dependency = b"2\nH2\nH 0 0 0\nH 0 0 .74\n"
    from pathlib import Path

    Path("geometry.xyz").write_bytes(dependency)
    reply = {"request_key": "single", "operation_status": "completed",
             "scientific_validation": "unverified", "files": {"stdout.out": "raw/stdout.out"}}
    result, captured = exchange(mailbox, lambda request: {**reply, "input_fingerprint": fingerprint(request)},
        lambda: task_sdk.run_orca(
        "job.inp", dependencies=["geometry.xyz"], request_key="single"))
    assert result == {**reply, "input_fingerprint": fingerprint(captured[0])}
    assert len(captured) == 1
    assert captured[0] == {"version": 2, "kind": "orca", "purpose": "new_candidate", "retry_of": None, "request_key": "single",
        "input": "! HF STO-3G\n* xyzfile 0 1 geometry.xyz\n",
        "dependencies": [{"target_path": "geometry.xyz",
                           "content_base64": base64.b64encode(dependency).decode("ascii")}]}
    assert "qualified_outputs" not in result


def test_sdk_sequential_calls_keep_distinct_messages_and_receipts(mailbox):
    def reply(request):
        return {"request_key": request["request_key"], "input_fingerprint": fingerprint(request),
                "operation_status": "completed", "files": {}}
    first, _ = exchange(mailbox, reply, lambda: task_sdk.run_orca("job.inp", request_key="first"))
    original = (mailbox[0] / "request-000001.json").read_bytes()
    second, _ = exchange(mailbox, reply, lambda: task_sdk.run_orca("job.inp", request_key="second"))
    assert first["request_key"] == "first" and second["request_key"] == "second"
    assert (mailbox[0] / "request-000001.json").read_bytes() == original
    assert (mailbox[0] / "request-000002.json").exists()


def test_checkpoint_returns_only_when_no_decision_is_needed(mailbox):
    def reply(request):
        return {"request_key": request["request_key"], "input_fingerprint": fingerprint(request),
                "checkpoint_sha256": "a" * 64}
    response, captured = exchange(mailbox, reply, lambda: task_sdk.checkpoint(
        {"completed": ["first"]}, request_key="batch-one", requires_decision=False))
    assert captured[0]["kind"] == "checkpoint"
    assert response["checkpoint_sha256"] == "a" * 64
    with pytest.raises(SystemExit) as stopped:
        exchange(mailbox, reply, lambda: task_sdk.checkpoint({"next": "ask model"}))
    assert stopped.value.code == 0


def test_competing_sdk_calls_publish_only_one_request(mailbox):
    ready = threading.Barrier(2)
    results, rejected = [], []

    def request(key):
        ready.wait(timeout=2)
        try:
            results.append(task_sdk.run_orca("job.inp", request_key=key))
        except (FileExistsError, RuntimeError) as exc:
            rejected.append(exc)

    def competing_calls():
        workers = [threading.Thread(target=request, args=(key,)) for key in ("first", "second")]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)
            assert not worker.is_alive()

    _, captured = exchange(mailbox, lambda request: {
        "request_key": request["request_key"], "input_fingerprint": fingerprint(request),
        "operation_status": "completed"}, competing_calls)
    assert len(results) == len(rejected) == len(captured) == 1
    assert results[0]["request_key"] == captured[0]["request_key"]


@pytest.mark.parametrize("reply", [
    {"request_key": "other", "operation_status": "completed"},
    {"operation_status": "completed"},
    ["completed"],
])
def test_sdk_rejects_response_without_exact_request_identity(mailbox, reply):
    with pytest.raises(ValueError):
        exchange(mailbox, lambda request: ({**reply, "input_fingerprint": fingerprint(request)}
                                           if isinstance(reply, dict) else reply),
                 lambda: task_sdk.run_orca("job.inp", request_key="single"))


def test_sdk_rejects_same_key_response_with_different_input_fingerprint(mailbox):
    with pytest.raises(ValueError, match="match this input request"):
        exchange(mailbox, {"request_key": "single", "input_fingerprint": "a" * 64,
                           "operation_status": "completed"},
                 lambda: task_sdk.run_orca("job.inp", request_key="single"))


def test_sdk_surfaces_broker_rejection_without_a_success_receipt(mailbox):
    reply = {"request_key": "single", "error": "internal work unit budget exhausted"}
    with pytest.raises(RuntimeError, match="work unit budget exhausted"):
        exchange(mailbox, reply, lambda: task_sdk.run_orca("job.inp", request_key="single"))


def test_sdk_request_limit_precedes_publication(mailbox, monkeypatch):
    monkeypatch.setattr(task_sdk, "MAX_REQUEST_BYTES", 32)
    with pytest.raises(ValueError, match="bounded transport size"):
        task_sdk.run_orca("job.inp")
    assert not (mailbox[0] / "request-000001.json").exists()


def test_sdk_response_read_is_bounded(mailbox, monkeypatch):
    monkeypatch.setattr(task_sdk, "MAX_RESPONSE_BYTES", 32)
    with pytest.raises(ValueError, match="bounded transport size"):
        exchange(mailbox, b"x" * 33, lambda: task_sdk.run_orca("job.inp"))


def test_sdk_timeout_preserves_submitted_request_for_coordinator_reconciliation(mailbox, monkeypatch):
    monkeypatch.setenv("ORCA_TASK_TIMEOUT_SECONDS", "0.01")
    with pytest.raises(TimeoutError, match="deadline"):
        task_sdk.run_orca("job.inp", request_key="single")
    request = json.loads((mailbox[0] / "request-000001.json").read_bytes())
    assert request["request_key"] == "single"
    assert not (mailbox[1] / "response-000001.json").exists()
