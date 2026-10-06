"""One bounded DeepSeek HTTP transmission; persistence and decisions belong to the caller.

The SDK transport never retries.  ``reserve`` must durably reserve both Run and
shared evaluation budgets before returning; ``settle`` appends the returned
facts.  A missing/failed settlement leaves that reservation unknown, not free.
Neither syntactically valid JSON nor a successful HTTP request certifies science.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import logging
import math
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"
SDK_VERSION = "2.28.0"
HTTPX_VERSION = "0.28.1"
MAX_INPUT_TOKENS = 12_000
MAX_OUTPUT_TOKENS = 2_000
MAX_RESPONSE_BYTES = 128 * 1024
MAX_PROPOSAL_BYTES = 32 * 1024
MAX_MESSAGES = 64
TOKEN_BOUND_VERSION = "deepseek-v41-utf8-upper-v2"

# Inspected official source, frozen rather than following the moving API alias:
# deepseek-ai/deepseek-recipe @ 8cadfede7063c896b944e7bae05daa3549ae97ea
# static/tokenizers/v41/tokenizer.json SHA256:
# 81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99
# It uses identity normalization, byte-level pretokenization (no prefix space)
# and BPE without affixes: each content token consumes >= 1 UTF-8 byte.
# src/v4/mod.rs renders text-only non-thinking messages with <= 64 framing
# bytes per message, plus BOS/assistant prefix and JSON-format instructions
# totaling < 512 bytes. Message content is counted AFTER JSON decoding: wire
# escape backslashes are not fed to the tokenizer. All other input fields are
# fixed by prepare_request and their rendering is covered by this framing bound.
# Special-token literals cannot invalidate this byte bound even when the API
# treats them as ordinary text.  There are no native tools/images/names here.
_FRAMING_BASE = 512
_FRAMING_PER_MESSAGE = 64
_CREDENTIAL = re.compile(r"(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+\S+)", re.IGNORECASE)
_SAFE_METADATA = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")


class ModelConfigurationError(RuntimeError):
    """Safe local configuration failure; no transmission was reserved or sent."""


class _ResponseTooLarge(RuntimeError):
    pass


class _ResponseEncodingRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedRequest:
    """Immutable, credential-free request plus conservative pre-send reservation."""

    canonical_body: str = field(repr=False)
    request_hash: str
    input_token_bound: int
    output_token_bound: int
    timeout_seconds: float
    prompt_version: str
    model: str = MODEL
    sdk_version: str = SDK_VERSION
    token_bound_version: str = TOKEN_BOUND_VERSION

    @property
    def reserved_tokens(self) -> int:
        return self.input_token_bound + self.output_token_bound

    def body(self) -> dict[str, Any]:
        """Return a fresh copy so callers cannot change the frozen payload."""
        return json.loads(self.canonical_body)


@dataclass(frozen=True)
class ModelUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    prompt_cache_hit_tokens: int | None = None
    prompt_cache_miss_tokens: int | None = None


@dataclass(frozen=True)
class ModelReply:
    """Only whitelisted facts, never a provider exception or authentication header."""

    request_hash: str
    proposal: dict[str, Any] | None = field(default=None, repr=False)
    error_category: str | None = None
    retryable: bool = False
    usage: ModelUsage | None = None
    response_hash: str | None = None
    response_bytes: int = 0
    response_model: str | None = None
    provider_request_id: str | None = None
    completion_id: str | None = None
    finish_reason: str | None = None
    http_status: int | None = None
    retry_after_seconds: float | None = None
    elapsed_seconds: float = 0

    @property
    def usage_known(self) -> bool:
        return self.usage is not None


def _has_credential(text: str) -> bool:
    secret = os.environ.get("DEEPSEEK_API_KEY")
    return bool((secret and secret in text) or _CREDENTIAL.search(text))


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def _silence_sdk_logging() -> None:
    """Keep vendor payloads/exception traces out of application log handlers.

    ModelReply is the sole transport diagnostic surface. Suppression is
    persistent, avoiding races from restoring debug logging between sends.
    Application loggers are untouched, including the caller's accounting logs.
    """
    namespaces = ("openai", "httpx", "httpcore")
    for name in namespaces:
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
        logger.disabled = True
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and any(
            name.startswith(prefix + ".") for prefix in namespaces
        ):
            logger.disabled = True


def input_token_upper_bound(body: Mapping[str, Any]) -> int:
    """Byte-level BPE upper bound, including all supported framing overhead.

    This is deliberately not an average characters-per-token estimate.  The
    bound only applies to the fixed text-only protocol built by prepare_request.
    A provider usage count exceeding it invalidates the reply, preserving actual
    reported consumption so the caller can stop and investigate alias drift.
    """
    return (sum(len(message["content"].encode("utf-8")) for message in body["messages"])
            + _FRAMING_BASE
            + _FRAMING_PER_MESSAGE * len(body["messages"]))


def prepare_request(
    messages: Sequence[Mapping[str, str]],
    *,
    prompt_version: str = "agent-json-v4",
    max_output_tokens: int = MAX_OUTPUT_TOKENS,
    timeout_seconds: float = 60,
) -> PreparedRequest:
    """Validate fixed transport parameters before any budget reservation or send."""
    if not 1 <= len(messages) <= MAX_MESSAGES:
        raise ValueError("model message count exceeds supported bounds")
    if (type(max_output_tokens) is not int
            or not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS):
        raise ValueError("model output token limit exceeds supported bounds")
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60):
        raise ValueError("model timeout must be within the remaining 60 second bound")
    if (not isinstance(prompt_version, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}", prompt_version)
            or _has_credential(prompt_version)):
        raise ValueError("invalid prompt version")
    clean = []
    for message in messages:
        if (set(message) != {"role", "content"}
                or message["role"] not in {"system", "user", "assistant"}
                or not isinstance(message["content"], str)):
            raise ValueError("only text messages with role and content are supported")
        if _has_credential(message["content"]):
            raise ValueError("credential-like content is prohibited in model context")
        clean.append(dict(message))
    if not any("json" in message["content"].lower() for message in clean):
        raise ValueError("the prompt must explicitly request JSON")
    body = {
        "model": MODEL, "messages": clean, "stream": False, "temperature": 0,
        "max_tokens": max_output_tokens, "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    bound = input_token_upper_bound(body)
    if bound > MAX_INPUT_TOKENS:
        raise ValueError("conservative input token bound exceeds 12000")
    canonical = _canonical(body)
    return PreparedRequest(
        canonical_body=canonical,
        request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        input_token_bound=bound, output_token_bound=max_output_tokens,
        timeout_seconds=float(timeout_seconds), prompt_version=prompt_version,
    )


def _metadata(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_METADATA.fullmatch(value) and not _has_credential(value):
        return value
    return None


def _strict_json(raw: str | bytes) -> Any:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def reject_constant(_):
        raise ValueError("non-finite JSON constant")

    result = json.loads(raw, object_pairs_hook=pairs, parse_constant=reject_constant)
    pending = [(result, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > 24:
            raise ValueError("JSON nesting exceeds bound")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif isinstance(value, float) and not math.isfinite(value):
            raise ValueError("non-finite JSON number")
    return result


def _usage(value: Any, request: PreparedRequest) -> ModelUsage | None:
    if not isinstance(value, dict):
        return None
    counts = [value.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")]
    if any(type(count) is not int or count < 0 for count in counts):
        return None
    prompt, completion, total = counts
    if prompt + completion != total:
        return None
    cache = [value.get(name) for name in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens")]
    cache = [count if type(count) is int and 0 <= count <= prompt else None for count in cache]
    return ModelUsage(prompt, completion, total, *cache)


def _parse_response(request: PreparedRequest, raw: bytes, status: int, request_id: str | None):
    facts = {
        "request_hash": request.request_hash,
        "response_hash": hashlib.sha256(raw).hexdigest(), "response_bytes": len(raw),
        "http_status": status, "provider_request_id": _metadata(request_id),
    }
    try:
        response = _strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        return ModelReply(**facts, error_category="invalid_response_json")
    if not isinstance(response, dict):
        return ModelReply(**facts, error_category="invalid_response_shape")
    usage = _usage(response.get("usage"), request)
    facts.update(usage=usage, response_model=_metadata(response.get("model")),
                 completion_id=_metadata(response.get("id")))
    # Positive provider counts outside the reservation must not appear as free
    # or successful usage. Keep the full reservation and surface a hard failure.
    if isinstance(response.get("usage"), dict) and usage is None:
        return ModelReply(**facts, error_category="invalid_usage")
    if (usage is not None and (usage.prompt_tokens > request.input_token_bound
                              or usage.completion_tokens > request.output_token_bound)):
        # Observed excess usage is still real expenditure. Settle it as known,
        # reject the proposal, and let the caller stop further model activity.
        return ModelReply(**facts, error_category="token_bound_exceeded")
    choices = response.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        return ModelReply(**facts, error_category="invalid_response_shape")
    choice = choices[0]
    finish = _metadata(choice.get("finish_reason"))
    facts["finish_reason"] = finish
    if finish == "length":
        return ModelReply(**facts, error_category="truncated")
    if finish != "stop":
        return ModelReply(**facts, error_category="unexpected_finish_reason")
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("tool_calls") or message.get("function_call"):
        return ModelReply(**facts, error_category="invalid_response_shape")
    if message.get("reasoning_content"):
        return ModelReply(**facts, error_category="unexpected_thinking")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return ModelReply(**facts, error_category="empty_content")
    if len(content.encode("utf-8")) > MAX_PROPOSAL_BYTES:
        return ModelReply(**facts, error_category="proposal_too_large")
    if _has_credential(content):
        return ModelReply(**facts, error_category="credential_in_response")
    try:
        proposal = _strict_json(content)
    except (ValueError, UnicodeError, RecursionError):
        return ModelReply(**facts, error_category="invalid_proposal_json")
    if not isinstance(proposal, dict):
        return ModelReply(**facts, error_category="proposal_not_object")
    return ModelReply(**facts, proposal=proposal)


def _exception_reply(request: PreparedRequest, exc: Exception) -> ModelReply:
    """Never stringify an exception: providers can include headers/body in it."""
    status = getattr(exc, "status_code", None)
    if type(status) is not int or not 100 <= status <= 599:
        status = None
    name = type(exc).__name__
    causes = []
    cause = exc
    for _ in range(8):
        causes.append(cause)
        cause = cause.__cause__
        if cause is None:
            break
    if any(isinstance(cause, _ResponseTooLarge) for cause in causes):
        category, retryable = "response_too_large", False
    elif any(isinstance(cause, _ResponseEncodingRejected) for cause in causes):
        category, retryable = "response_encoding_rejected", False
    elif isinstance(exc, TimeoutError) or name in {"APITimeoutError", "ReadTimeout"}:
        category, retryable = "timeout", True
    elif status in {401, 403}:
        category, retryable = "authentication" if status == 401 else "permission", False
    elif status == 429:
        category, retryable = "rate_limit", True
    elif status is not None and 300 <= status < 400:
        category, retryable = "redirect_rejected", False
    elif status is not None:
        category, retryable = "http_error", False
    elif name in {"APIConnectionError", "ConnectError", "ConnectTimeout"}:
        category, retryable = "connection", True
    else:
        category, retryable = "transport_error", False
    retry_after = None
    response = getattr(exc, "response", None)
    if status == 429 and response is not None:
        try:
            value = float(response.headers.get("retry-after", ""))
            if math.isfinite(value) and value >= 0:
                retry_after = min(value, request.timeout_seconds)
        except (AttributeError, TypeError, ValueError):
            pass
    return ModelReply(
        request_hash=request.request_hash, error_category=category, retryable=retryable,
        http_status=status, provider_request_id=_metadata(getattr(exc, "request_id", None)),
        retry_after_seconds=retry_after,
    )


def _real_client(*, api_key: str, timeout: float):
    """Lazy imports keep no-model operation independent of credential availability."""
    try:
        import httpx
        from openai import AsyncOpenAI

        _silence_sdk_logging()

        if (importlib.metadata.version("openai") != SDK_VERSION
                or importlib.metadata.version("httpx") != HTTPX_VERSION):
            raise ModelConfigurationError("the pinned model transport dependencies are required")
    except ImportError:
        raise ModelConfigurationError("the pinned model SDK is not installed") from None

    class BoundedStream(httpx.AsyncByteStream):
        def __init__(self, stream):
            self.stream = stream

        async def __aiter__(self):
            total = 0
            async for chunk in self.stream:
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    raise _ResponseTooLarge()
                yield chunk

        async def aclose(self):
            await self.stream.aclose()

    class BoundedTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport(retries=0)

        async def handle_async_request(self, request):
            if (str(request.url) != BASE_URL + "/chat/completions"
                    or request.method != "POST"):
                raise ModelConfigurationError("model transport destination is not allowed")
            response = await self.inner.handle_async_request(request)
            # Identity encoding is requested; reject unsolicited compression so
            # a tiny compressed error cannot expand inside the SDK's aread().
            if response.headers.get("content-encoding", "identity").lower() != "identity":
                await response.aclose()
                raise _ResponseEncodingRejected()
            size = response.headers.get("content-length", "")
            if size.isdecimal() and int(size) > MAX_RESPONSE_BYTES:
                await response.aclose()
                raise _ResponseTooLarge()
            response.stream = BoundedStream(response.stream)
            return response

        async def aclose(self):
            await self.inner.aclose()

    return AsyncOpenAI(
        api_key=api_key, base_url=BASE_URL, max_retries=0, timeout=timeout,
        http_client=httpx.AsyncClient(
            transport=BoundedTransport(), follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(timeout), headers={"Accept-Encoding": "identity"},
        ),
    )


class DeepSeekTransport:
    """Single-send adapter; an injected async SDK-shaped factory supports offline tests."""

    def __init__(self, *, client_factory: Callable[..., Any] | None = None):
        self._client_factory = client_factory or _real_client

    def send(
        self,
        request: PreparedRequest,
        *,
        reserve: Callable[[PreparedRequest], Any],
        settle: Callable[[Any, ModelReply], None],
    ) -> ModelReply:
        # Revalidate the immutable payload, not any caller-supplied count/hash.
        body = request.body()
        checked = prepare_request(
            body["messages"], prompt_version=request.prompt_version,
            max_output_tokens=body["max_tokens"], timeout_seconds=request.timeout_seconds,
        )
        if request != checked:
            raise ValueError("prepared model request integrity mismatch")
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key or not key.strip():
            raise ModelConfigurationError("DEEPSEEK_API_KEY is not configured")
        _silence_sdk_logging()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise ModelConfigurationError("synchronous model transport requires a worker thread")
        # Construction does not transmit; fail missing dependencies before reserve.
        client = self._client_factory(api_key=key, timeout=request.timeout_seconds)
        try:
            ticket = reserve(request)
        except BaseException:
            # The constructor has not sent anything, but release its HTTP pool.
            async def close_unsent():
                async with client:
                    pass

            asyncio.run(close_unsent())
            raise
        started = time.monotonic()
        try:
            reply = asyncio.run(asyncio.wait_for(self._exchange(client, request),
                                                timeout=request.timeout_seconds))
        except Exception as exc:
            reply = _exception_reply(request, exc)
        reply = ModelReply(**{**reply.__dict__, "elapsed_seconds": time.monotonic() - started})
        settle(ticket, reply)
        return reply

    @staticmethod
    async def _exchange(client: Any, request: PreparedRequest) -> ModelReply:
        body = request.body()
        thinking = body.pop("thinking")
        async with client:
            # This streams the HTTP body only; API generation stays stream=False.
            # SDK error responses are bounded by BoundedTransport as well.
            async with client.chat.completions.with_streaming_response.create(
                **body, extra_body={"thinking": thinking}, timeout=request.timeout_seconds,
            ) as response:
                raw = bytearray()
                async for chunk in response.iter_bytes():
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise _ResponseTooLarge()
                    raw.extend(chunk)
                return _parse_response(request, bytes(raw), response.status_code,
                                       response.headers.get("x-request-id"))
