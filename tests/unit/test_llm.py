"""Offline protocol, pre-send accounting and bounded transport failure tests."""

import asyncio
import json
import logging
import time
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from orca_agent.llm import (
    MAX_RAW_CONTENT_BYTES,
    MAX_RESPONSE_BYTES,
    DeepSeekTransport,
    ModelConfigurationError,
    prepare_request,
)


def completion(content='{"action":"stop"}', *, finish="stop", usage=True, **overrides):
    result = {
        "id": "completion-1", "model": "deepseek-flash",
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": finish}],
    }
    if isinstance(usage, dict):
        result["usage"] = usage
    elif usage:
        result["usage"] = {"prompt_tokens": 14, "completion_tokens": 8, "total_tokens": 22}
    result.update(overrides)
    return json.dumps(result, ensure_ascii=False).encode("utf-8")


@pytest.fixture(autouse=True)
def socket_free_fake_client_loop(monkeypatch):
    # Windows' standard event loop creates a local socketpair even for an
    # asyncio.sleep. These fake-client tests need only ready callbacks/timers;
    # retain the repository's blanket socket ban instead of bypassing it.
    class TimerLoop(asyncio.SelectorEventLoop):
        def _make_self_pipe(self):
            pass

        def _close_self_pipe(self):
            pass

        def _write_to_self(self):
            pass

    def make_loop():
        loop = TimerLoop()
        # SDK platform discovery can use a worker thread; poll the ready queue
        # briefly because this deliberately socket-free loop has no self-pipe.
        loop._selector.select = lambda timeout: time.sleep(min(timeout or 0, .01)) or []
        return loop

    monkeypatch.setattr(asyncio.events, "new_event_loop", make_loop)


class FakeResponse:
    status_code = 200
    headers = {"x-request-id": "provider-1"}

    def __init__(self, raw, delay=0):
        self.raw = raw
        self.delay = delay

    async def __aenter__(self):
        if isinstance(self.raw, Exception):
            raise self.raw
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_bytes(self):
        await asyncio.sleep(self.delay)
        for offset in range(0, len(self.raw), 127):
            yield self.raw[offset:offset + 127]


class FakeClient:
    def __init__(self, raw=None, delay=0):
        self.raw = completion() if raw is None else raw
        self.delay = delay
        self.requests = []
        self.events = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(with_streaming_response=self))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def create(self, **kwargs):
        self.events.append("send")
        self.requests.append(kwargs)
        return FakeResponse(self.raw, self.delay)


@pytest.fixture
def send(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")

    def call(raw=None, *, timeout=60, delay=0):
        client = FakeClient(raw, delay)
        request = prepare_request([{"role": "user", "content": "Return JSON."}],
                                  timeout_seconds=timeout)
        settled = []

        def reserve(prepared):
            assert prepared == request
            client.events.append("reserve")
            return "reservation-1"

        def settle(ticket, reply):
            assert ticket == "reservation-1"
            client.events.append("settle")
            settled.append(reply)

        reply = DeepSeekTransport(client_factory=lambda **kwargs: client).send(
            request, reserve=reserve, settle=settle,
        )
        assert client.events == ["reserve", "send", "settle"]
        assert client.closed
        assert len(client.requests) == len(settled) == 1
        assert settled[0] == reply
        return reply, client

    return call


def test_single_transmission_with_frozen_options_and_known_usage(send):
    reply, client = send()
    body = client.requests[0]
    assert body["model"] == "deepseek-flash"
    assert body["stream"] is False
    assert body["temperature"] == 0
    assert body["max_tokens"] == 2000
    assert body["extra_body"] == {"thinking": {"type": "disabled"}}
    assert body["response_format"] == {"type": "json_object"}
    assert "tools" not in body
    assert reply.proposal == {"action": "stop"}
    assert reply.raw_content == '{"action":"stop"}'
    assert reply.usage.total_tokens == 22
    assert reply.provider_request_id == "provider-1"
    assert reply.response_model == "deepseek-flash"
    assert len(reply.response_hash) == 64


def test_missing_usage_keeps_unknown_reservation(send):
    reply, _ = send(completion(usage=False))
    assert reply.proposal == {"action": "stop"}
    assert reply.usage is None
    assert not reply.usage_known


@pytest.mark.parametrize(("raw", "category"), [
    (completion(""), "empty_content"),
    (completion(None), "empty_content"),
    (completion("{", finish="length"), "truncated"),
    (completion("not json"), "invalid_proposal_json"),
    (completion("[]"), "proposal_not_object"),
    (completion('{"a":1,"a":2}'), "invalid_proposal_json"),
    (completion('{"a":NaN}'), "invalid_proposal_json"),
    (completion('{"a":1e999}'), "invalid_proposal_json"),
    (completion('{"a":' * 26 + '0' + '}' * 26), "invalid_proposal_json"),
    (completion(finish="tool_calls"), "unexpected_finish_reason"),
    (completion(choices=[]), "invalid_response_shape"),
    (b"null", "invalid_response_shape"),
    (b"not JSON", "invalid_response_json"),
    (completion('{"a":"' + "x" * 33000 + '"}'), "proposal_too_large"),
    (b"x" * (MAX_RESPONSE_BYTES + 1), "response_too_large"),
], ids=lambda item: f"bytes-{len(item)}" if isinstance(item, bytes) else item)
def test_response_failures_are_bounded_and_do_not_retry(send, raw, category):
    reply, _ = send(raw)
    assert reply.error_category == category
    assert reply.proposal is None


@pytest.mark.parametrize("payload", [
    {"role": "assistant", "content": "{}", "tool_calls": [{"id": "1"}]},
    {"role": "assistant", "content": "{}", "function_call": {"name": "shell"}},
])
def test_native_tool_protocol_cannot_be_injected(send, payload):
    reply, _ = send(completion(choices=[{"finish_reason": "stop", "message": payload}]))
    assert reply.error_category == "invalid_response_shape"


def test_thinking_must_remain_disabled(send):
    reply, _ = send(completion(choices=[{"finish_reason": "stop", "message": {
        "content": "{}", "reasoning_content": "unexpected reasoning",
    }}]))
    assert reply.error_category == "unexpected_thinking"


def test_credentials_cannot_escape_through_exception_or_response(send):
    class APIConnectionError(Exception):
        pass

    error = APIConnectionError("Bearer test-secret-credential raw secret")
    error.request_id = "test-secret-credential"
    reply, _ = send(error)
    assert reply.error_category == "connection"
    assert "test-secret-credential" not in json.dumps(asdict(reply))
    reply, _ = send(completion('{"text":"test-secret-credential"}'))
    assert reply.error_category == "credential_in_response"
    assert "test-secret-credential" not in json.dumps(asdict(reply))


@pytest.mark.parametrize("content,finish,category", [
    (' \n{"action": "stop", "reason": "水"}\n ', "stop", None),
    ("not json", "stop", "invalid_proposal_json"),
    ('```json\n{"action":"stop"}\n```', "stop", "invalid_proposal_json"),
    ('{"a":1,"a":2}', "stop", "invalid_proposal_json"),
    ('{"a":NaN}', "stop", "invalid_proposal_json"),
    ('{"a":1e999}', "stop", "invalid_proposal_json"),
    ('{"a":' * 26 + '0' + '}' * 26, "stop", "invalid_proposal_json"),
    ('{"action":', "length", "truncated"),
    ("[]", "stop", "proposal_not_object"),
    ("", "stop", "empty_content"),
    ("{}", "tool_calls", "unexpected_finish_reason"),
], ids=["exact-success", "plain-text", "fenced", "duplicate", "nan", "infinite", "deep",
        "truncated", "array", "empty", "unexpected-finish"])
def test_original_content_survives_success_or_failure_without_repair(send, content, finish, category):
    reply, _ = send(completion(content, finish=finish))
    assert reply.error_category == category
    assert reply.raw_content == content
    assert content not in repr(reply) if content else "raw_content=" not in repr(reply)
    assert reply.usage.total_tokens == 22 and reply.response_hash and reply.response_bytes
    if category is not None:
        assert reply.proposal is None


@pytest.mark.parametrize("content,retained,category", [
    ("x" * MAX_RAW_CONTENT_BYTES, True, "invalid_proposal_json"),
    ("x" * (MAX_RAW_CONTENT_BYTES + 1), False, "proposal_too_large"),
    ("水" * (MAX_RAW_CONTENT_BYTES // 3 + 1), False, "proposal_too_large"),
    ("\x00" * 12000, False, "invalid_proposal_json"),
], ids=["at-limit", "ascii-over-limit", "utf8-over-limit", "json-expansion"])
def test_raw_content_uses_utf8_and_serialized_receipt_bounds(send, content, retained, category):
    reply, _ = send(completion(content))
    assert reply.error_category == category and reply.proposal is None
    assert reply.raw_content == (content if retained else None)
    assert reply.usage.total_tokens == 22 and reply.response_hash


@pytest.mark.parametrize("content,finish", [
    ('{"text":"test-secret-credential"}', "stop"),
    ('{"text":"Bearer secret-value"}', "stop"),
    ('{"text":"sk-abcdefghijklmnopqr"}', "stop"),
    ('{"text":"' + ''.join(f"\\u{ord(c):04x}" for c in "test-secret-credential") + '"}', "stop"),
    ('bad JSON "' + ''.join(f"\\u{ord(c):04x}" for c in "test-secret-credential"), "stop"),
    ('{"text":"test-secret-credential', "length"),
], ids=["secret", "bearer", "key-pattern", "escaped-secret", "invalid-escaped-secret", "truncated-secret"])
def test_no_sensitive_raw_text_is_retained_even_when_invalid_or_truncated(send, content, finish):
    reply, _ = send(completion(content, finish=finish))
    assert reply.proposal is None and reply.raw_content is None
    assert reply.error_category is not None and reply.usage.total_tokens == 22
    assert content not in json.dumps(asdict(reply), ensure_ascii=False)
    assert "test-secret-credential" not in json.dumps(asdict(reply), ensure_ascii=False)


def test_unencodable_provider_content_keeps_diagnostics_without_raw_text(send):
    raw = json.loads(completion())
    raw["choices"][0]["message"]["content"] = "\ud800"
    reply, _ = send(json.dumps(raw, ensure_ascii=True).encode())
    assert reply.error_category == "invalid_proposal_json" and reply.raw_content is None
    assert reply.usage.total_tokens == 22


def test_complete_receipt_bound_discards_expanded_proposal_but_preserves_usage(send):
    content = '{"a":' * 20 + '[' + ','.join(['0'] * 3200) + ']' + '}' * 20
    assert len(content.encode()) < MAX_RAW_CONTENT_BYTES
    reply, _ = send(completion(content))
    assert reply.error_category == "proposal_too_large"
    assert reply.proposal is None and reply.raw_content is None
    assert reply.usage.total_tokens == 22 and reply.response_hash
    assert len(json.dumps(asdict(reply), ensure_ascii=False, indent=2).encode()) < MAX_RESPONSE_BYTES


def test_escaped_surrogate_is_failed_but_its_safe_original_text_and_usage_survive(send):
    content = '{"text":"\\ud800"}'
    reply, _ = send(completion(content))
    assert reply.error_category == "invalid_proposal_json" and reply.proposal is None
    assert reply.raw_content == content and reply.usage.total_tokens == 22
    json.dumps(asdict(reply), ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize(("status", "category", "retryable"), [
    (401, "authentication", False), (403, "permission", False),
    (429, "rate_limit", True), (302, "redirect_rejected", False),
    (500, "http_error", False), (400, "http_error", False),
])
def test_http_errors_are_sanitized_and_not_automatically_retried(send, status, category, retryable):
    error = RuntimeError("provider body containing test-secret-credential")
    error.status_code = status
    error.request_id = "provider-error-1"
    error.response = SimpleNamespace(headers={"retry-after": "9999"})
    reply, _ = send(error)
    assert reply.error_category == category
    assert reply.retryable is retryable
    assert reply.http_status == status
    assert reply.usage is None
    assert reply.retry_after_seconds == (60 if status == 429 else None)
    assert "test-secret-credential" not in str(asdict(reply))


def test_wall_clock_deadline_cancels_body_receive_and_keeps_unknown_usage(send):
    reply, _ = send(timeout=0.01, delay=1)
    assert reply.error_category == "timeout"
    assert reply.retryable
    assert reply.usage is None
    assert reply.elapsed_seconds < 0.5


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": -1, "completion_tokens": 8, "total_tokens": 7},
    {"prompt_tokens": 14, "completion_tokens": 8, "total_tokens": 23},
    {"prompt_tokens": True, "completion_tokens": 8, "total_tokens": 9},
    {"prompt_tokens": 14},
])
def test_invalid_usage_cannot_refund_reservation(send, usage):
    reply, _ = send(completion(usage=usage))
    assert reply.error_category == "invalid_usage"
    assert reply.usage is None


def test_usage_over_reservation_is_recorded_and_proposal_rejected(send):
    reply, _ = send(completion(usage={
        "prompt_tokens": 13000, "completion_tokens": 2100, "total_tokens": 15100,
    }))
    assert reply.error_category == "token_bound_exceeded"
    assert reply.usage.total_tokens == 15100
    assert reply.proposal is None


def test_utf8_bound_counts_multibyte_text_and_message_framing():
    ascii_request = prepare_request([{"role": "user", "content": "JSON " + "x" * 100}])
    unicode_request = prepare_request([{"role": "user", "content": "JSON " + "水" * 100}])
    assert unicode_request.input_token_bound - ascii_request.input_token_bound == 200
    assert unicode_request.input_token_bound > len(unicode_request.canonical_body.encode("utf-8"))
    assert ascii_request.input_token_bound > len(ascii_request.canonical_body)
    assert unicode_request.reserved_tokens == unicode_request.input_token_bound + 2000
    assert len(unicode_request.request_hash) == 64


def test_request_does_not_reference_mutable_caller_messages():
    message = {"role": "user", "content": "JSON please"}
    prepared = prepare_request([message])
    message["content"] = "changed"
    body = prepared.body()
    body["messages"][0]["content"] = "changed again"
    assert prepared.body()["messages"][0]["content"] == "JSON please"


@pytest.mark.parametrize("kwargs", [
    {"timeout_seconds": 0}, {"timeout_seconds": 61}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": True}, {"max_output_tokens": 2001}, {"max_output_tokens": True},
    {"prompt_version": "../../secret"},
])
def test_local_bound_errors_precede_reservation(kwargs):
    with pytest.raises(ValueError):
        prepare_request([{"role": "user", "content": "JSON"}], **kwargs)


@pytest.mark.parametrize("messages", [
    [], [{"role": "tool", "content": "JSON"}],
    [{"role": "user", "content": [{"type": "text", "text": "JSON"}]}],
    [{"role": "user", "content": "JSON", "name": "unbounded"}],
    [{"role": "user", "content": "no format requested"}],
    [{"role": "user", "content": "JSON " + "水" * 4000}],
    [{"role": "user", "content": "JSON"}] * 65,
    [{"role": "user", "content": "JSON Bearer fake-credential"}],
    [{"role": "user", "content": "JSON sk-abcdefghijklmnopqrs"}],
])
def test_unsupported_or_oversized_context_rejected(messages):
    with pytest.raises(ValueError):
        prepare_request(messages)


def test_no_key_no_reservation_no_network(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    request = prepare_request([{"role": "user", "content": "JSON"}])
    events = []
    with pytest.raises(ModelConfigurationError, match="not configured"):
        DeepSeekTransport(client_factory=lambda **kwargs: events.append("factory")).send(
            request, reserve=lambda _: events.append("reserve"), settle=lambda *args: None,
        )
    assert events == []


def test_corrupt_prepared_request_cannot_underreserve_or_change_destination(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    request = prepare_request([{"role": "user", "content": "JSON"}])
    events = []
    with pytest.raises(ValueError, match="integrity"):
        DeepSeekTransport(client_factory=lambda **kwargs: events.append("factory")).send(
            replace(request, input_token_bound=1), reserve=lambda _: events.append("reserve"),
            settle=lambda *args: None,
        )
    assert events == []


def test_reservation_rejection_prevents_send(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    request = prepare_request([{"role": "user", "content": "JSON"}])
    client = FakeClient()

    def exhausted(_):
        raise ValueError("budget exhausted")

    with pytest.raises(ValueError, match="budget exhausted"):
        DeepSeekTransport(client_factory=lambda **kwargs: client).send(
            request, reserve=exhausted, settle=lambda *args: None,
        )
    assert client.requests == []
    assert client.closed


def test_settlement_failure_propagates_without_retransmission(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    request = prepare_request([{"role": "user", "content": "JSON"}])
    client = FakeClient()

    def crashed(*_):
        raise OSError("settlement unavailable")

    with pytest.raises(OSError, match="settlement unavailable"):
        DeepSeekTransport(client_factory=lambda **kwargs: client).send(
            request, reserve=lambda _: "persisted-reservation", settle=crashed,
        )
    assert len(client.requests) == 1


def test_vendor_debug_exception_logs_cannot_leak_credentials(monkeypatch, caplog):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    monkeypatch.setenv("OPENAI_LOG", "debug")
    request = prepare_request([{"role": "user", "content": "JSON"}])
    client = FakeClient()
    vendor = logging.getLogger("openai._base_client")
    vendor.setLevel(logging.DEBUG)
    vendor.disabled = False
    other_vendor = logging.getLogger("httpcore.http11")
    other_vendor.setLevel(logging.DEBUG)
    other_vendor.disabled = False

    def factory(**kwargs):
        try:
            raise RuntimeError("secret header test-secret-credential")
        except RuntimeError:
            vendor.exception("request contains %s", kwargs)
            other_vendor.exception("secret network data")
        return client

    with caplog.at_level(logging.DEBUG):
        DeepSeekTransport(client_factory=factory).send(
            request, reserve=lambda _: "1", settle=lambda *args: None,
        )
        logging.getLogger("orca_agent.application").warning("safe application diagnostic")
    assert "test-secret-credential" not in caplog.text
    assert "secret network data" not in caplog.text
    assert "safe application diagnostic" in caplog.text


@pytest.mark.parametrize(("status", "category"), [
    (200, None), (401, "authentication"), (429, "rate_limit"), (307, "redirect_rejected"),
])
def test_real_pinned_sdk_serialization_no_hidden_retry_or_redirect(monkeypatch, status, category):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    transmissions = []
    events = []

    def handle(request):
        transmissions.append(request)
        assert events == ["reserve"]
        headers = {"location": "https://untrusted.invalid/steal", "retry-after": "10",
                   "x-request-id": "provider-sdk-1"}
        return httpx.Response(status, content=completion(), headers=headers)

    def transport_factory(*, retries):
        assert retries == 0
        return httpx.MockTransport(handle)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport_factory)
    request = prepare_request([{"role": "user", "content": "Return JSON"}])

    def reserve(prepared):
        assert prepared.request_hash == request.request_hash
        events.append("reserve")
        return "ticket"

    def settle(ticket, reply):
        assert ticket == "ticket"
        events.append("settle")

    reply = DeepSeekTransport().send(request, reserve=reserve, settle=settle)
    assert events == ["reserve", "settle"]
    assert len(transmissions) == 1
    sent = transmissions[0]
    assert str(sent.url) == "https://api.deepseek.com/chat/completions"
    assert sent.headers["accept-encoding"] == "identity"
    body = json.loads(sent.content)
    assert body == request.body()
    assert body["thinking"] == {"type": "disabled"}
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    assert reply.error_category == category
    if status == 200:
        assert reply.proposal == {"action": "stop"}
    assert "test-secret-credential" not in json.dumps(asdict(reply))


@pytest.mark.parametrize("status", [200, 500])
def test_real_sdk_bounds_both_success_and_error_response_bodies(monkeypatch, status):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    transmissions = []

    def handle(request):
        transmissions.append(request)
        # Unknown content length exercises the SDK's error-response aread path.
        return httpx.Response(status, stream=httpx.ByteStream(b"x" * (MAX_RESPONSE_BYTES + 1)))

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    request = prepare_request([{"role": "user", "content": "Return JSON"}])
    reply = DeepSeekTransport().send(request, reserve=lambda _: "ticket", settle=lambda *_: None)
    assert len(transmissions) == 1
    assert reply.error_category == "response_too_large"
    assert reply.usage is None


def test_sdk_rejects_compressed_response_before_decoding(monkeypatch):
    import httpx

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-secret-credential")
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(
        lambda _: httpx.Response(500, stream=httpx.ByteStream(b"not-decoded"),
                                 headers={"content-encoding": "gzip"})))
    request = prepare_request([{"role": "user", "content": "Return JSON"}])
    reply = DeepSeekTransport().send(request, reserve=lambda _: "ticket", settle=lambda *_: None)
    assert reply.error_category == "response_encoding_rejected"
    assert reply.usage is None
