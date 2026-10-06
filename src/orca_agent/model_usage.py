"""Durable per-Run HTTP reservations, receipts and conservative crash recovery."""

import hashlib
import json
import math
from dataclasses import asdict, fields
from decimal import Decimal

from orca_agent.llm import (
    MAX_PROPOSAL_BYTES,
    MAX_RESPONSE_BYTES,
    ModelReply,
    ModelUsage,
    _has_credential,
    _metadata,
    _strict_json,
)
from orca_agent.models import new_id, utc_now
from orca_agent.store import BudgetExceeded, StoreError, _id, atomic_write

# Frozen uncached peak prices. Discounts never enlarge reservations.
INPUT_USD_PER_MILLION = Decimal("0.30")
OUTPUT_USD_PER_MILLION = Decimal("1.20")
_ERRORS = {
    "invalid_response_json", "invalid_response_shape", "invalid_usage", "token_bound_exceeded",
    "truncated", "unexpected_finish_reason", "unexpected_thinking", "empty_content",
    "proposal_too_large", "credential_in_response", "invalid_proposal_json", "proposal_not_object",
    "response_too_large", "timeout", "authentication", "permission", "rate_limit",
    "redirect_rejected", "http_error", "connection", "transport_error", "response_missing",
    "response_encoding_rejected",
}


def _cost(prompt, completion):
    return str((prompt * INPUT_USD_PER_MILLION + completion * OUTPUT_USD_PER_MILLION)
               / Decimal(1_000_000))


def current_basis(store, run):
    return {"request_version": run.request_version, "plan_version": run.plan_version,
            "permission_version": run.permission.version,
            "control_generation": store.read_control(run.id)["generation"]}


def _validate_reply(data, record):
    """Reconstruct bounded, typed and credential-free persisted transport facts."""
    if not isinstance(data, dict) or set(data) != {field.name for field in fields(ModelReply)}:
        raise StoreError("invalid model response record shape")
    if data["request_hash"] != record["request_hash"]:
        raise StoreError("model response request identity mismatch")
    if data["error_category"] not in _ERRORS | {None}:
        raise StoreError("invalid model error category")
    if type(data["retryable"]) is not bool:
        raise StoreError("invalid model retry fact")
    for name in ("response_model", "provider_request_id", "completion_id", "finish_reason"):
        value = data[name]
        if value is not None and _metadata(value) != value:
            raise StoreError("invalid model response metadata")
    if (data["response_hash"] is not None
            and (not isinstance(data["response_hash"], str) or len(data["response_hash"]) != 64
                 or any(c not in "0123456789abcdef" for c in data["response_hash"]))):
        raise StoreError("invalid model response hash")
    if (type(data["response_bytes"]) is not int
            or not 0 <= data["response_bytes"] <= MAX_RESPONSE_BYTES):
        raise StoreError("invalid model response size")
    if data["http_status"] is not None and (
        type(data["http_status"]) is not int or not 100 <= data["http_status"] <= 599
    ):
        raise StoreError("invalid model HTTP status")
    for name in ("elapsed_seconds", "retry_after_seconds"):
        value = data[name]
        if value is None and name == "retry_after_seconds":
            continue
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < 0):
            raise StoreError("invalid model timing fact")
    proposal = data["proposal"]
    if proposal is not None and (
        not isinstance(proposal, dict) or data["error_category"] is not None
        or len(json.dumps(proposal, ensure_ascii=False).encode("utf-8")) > MAX_PROPOSAL_BYTES
    ):
        raise StoreError("invalid model proposal receipt")
    if proposal is None and data["error_category"] is None:
        raise StoreError("model receipt has neither a proposal nor an error")
    usage = data["usage"]
    if usage is not None:
        if not isinstance(usage, dict) or set(usage) != {field.name for field in fields(ModelUsage)}:
            raise StoreError("invalid model usage shape")
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if type(usage[name]) is not int or not 0 <= usage[name] <= 1_000_000_000:
                raise StoreError("invalid model token usage")
        if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
            raise StoreError("inconsistent model token total")
        for name in ("prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
            if usage[name] is not None and (
                type(usage[name]) is not int or not 0 <= usage[name] <= usage["prompt_tokens"]
            ):
                raise StoreError("invalid model cached usage")
        if (usage["prompt_tokens"] > record["input_reserved"]
                or usage["completion_tokens"] > record["output_reserved"]):
            if data["error_category"] != "token_bound_exceeded":
                raise StoreError("model usage exceeds its bound without failure fact")
        usage = ModelUsage(**usage)
    return ModelReply(**{**data, "usage": usage})


def read_model_reply(store, run, record):
    """Read a receipt after checking immutable request identity and bounded size."""
    ticket = _id(record["id"])
    request_path = store.path(f"runs/{run.id}/model/{ticket}.request.json")
    response_path = store.path(f"runs/{run.id}/model/{ticket}.response.json")
    if not request_path.is_file() or not response_path.is_file():
        raise StoreError("model request or response evidence is missing")
    if request_path.stat().st_size > 128 * 1024 or response_path.stat().st_size > MAX_RESPONSE_BYTES:
        raise StoreError("model evidence exceeds its size bound")
    if hashlib.sha256(request_path.read_bytes()).hexdigest() != record["request_hash"]:
        raise StoreError("frozen model request changed")
    raw = response_path.read_bytes()
    receipt_hash = hashlib.sha256(raw).hexdigest()
    if record.get("response_record_sha256", receipt_hash) != receipt_hash:
        raise StoreError("settled model response changed")
    try:
        text = raw.decode("utf-8")
        if _has_credential(text):
            raise StoreError("credential-like content in model receipt")
        data = _strict_json(text)
    except (ValueError, UnicodeError, RecursionError):
        raise StoreError("model response evidence is invalid") from None
    return _validate_reply(data, record), receipt_hash


def settled_model_record(record, reply, receipt_hash):
    settled = {**record, "status": "known" if reply.usage is not None else "unknown",
               "response_record_sha256": receipt_hash,
               "response_hash": reply.response_hash, "error_category": reply.error_category,
               "provider_request_id": reply.provider_request_id,
               "response_model": reply.response_model, "latency_seconds": reply.elapsed_seconds,
               "http_status": reply.http_status, "finish_reason": reply.finish_reason}
    if reply.usage is not None:
        settled.update(usage=asdict(reply.usage), input_tokens=reply.usage.prompt_tokens,
                       output_tokens=reply.usage.completion_tokens, total_tokens=reply.usage.total_tokens,
                       cost_known_usd=_cost(reply.usage.prompt_tokens, reply.usage.completion_tokens))
    return settled


def recover_models(store, run, *, batch=None):
    """Reconnect receipts exactly once, without HTTP or scientific execution.

    An interrupted reservation without a response becomes explicitly unknown.
    Its full occupancy remains, and replay returns that failure rather than
    transmitting twice. Keys are saved model request IDs; proposal basis stays.
    """
    recovered = {}
    with store.run_lock(run.id):
        if store.load_run(run.id) != run:
            raise StoreError("Run changed before model reconciliation")
        for record in list(run.model_records):
            if record.get("status") not in {"reserved", "known", "unknown"}:
                raise StoreError("unknown model reservation state")
            ticket = _id(record["id"])
            response = f"runs/{run.id}/model/{ticket}.response.json"
            if record["status"] == "reserved" and not store.path(response).exists():
                # Crash may precede OR follow HTTP; missing response is not free.
                missing = ModelReply(request_hash=record["request_hash"],
                                     error_category="response_missing")
                store._write_json(response, asdict(missing), immutable=True)
            recovered[ticket] = store.settle_model(run, ticket)
            if batch:
                committed = next(item for item in run.model_records if item["id"] == ticket)
                batch.settle_model(run, committed)
    return recovered


def send_model(store, run, prepared, transport, *, basis, logical_id, batch=None, fault=None):
    logical_id = _id(logical_id)
    # Stable logical identity is idempotency, not an implicit retry switch.
    existing = [record for record in run.model_records if record.get("logical_id") == logical_id]
    if existing:
        if (len(existing) != 1 or existing[0]["request_hash"] != prepared.request_hash
                or existing[0]["basis"] != basis):
            raise StoreError("model logical identity cannot be rebound")
        return recover_models(store, run, batch=batch)[existing[0]["id"]]

    def reserve(request):
        with store.run_lock(run.id), store.control_lock(run.id):
            if store.load_run(run.id) != run or current_basis(store, run) != basis:
                raise StoreError("stale model basis before transmission")
            if store.read_signal(run.id) or utc_now() >= run.deadline:
                raise BudgetExceeded("control or deadline prevents model transmission")
            if not run.permission.model_execution:
                raise StoreError("model transmission is not authorized")
            if any(record.get("status") == "reserved" for record in run.model_records):
                raise StoreError("unknown old model reservation requires reconciliation")
            if any(record.get("error_category") == "token_bound_exceeded"
                   for record in run.model_records):
                raise StoreError("model token bound violated; further transmission is prohibited")
            if (run.usage.model_calls >= run.budget.model_calls
                    or request.input_token_bound > run.budget.input_tokens
                    or request.output_token_bound > run.budget.output_tokens
                    or run.usage.model_tokens_used + run.usage.model_tokens_unknown
                    + request.reserved_tokens > run.budget.model_tokens):
                raise BudgetExceeded("model HTTP/token budget exhausted")
            ticket = new_id("model")
            record = {"id": ticket, "logical_id": logical_id, "request_hash": request.request_hash,
                      "basis": dict(basis), "input_reserved": request.input_token_bound,
                      "output_reserved": request.output_token_bound,
                      "cost_reserved_usd": _cost(request.input_token_bound, request.output_token_bound),
                      "created_at": utc_now().isoformat(), "status": "reserved",
                      "prompt_version": request.prompt_version, "sdk_version": request.sdk_version,
                      "model": request.model, "token_bound_version": request.token_bound_version}
            if batch:
                batch.reserve_model(run, record)
            atomic_write(store.path(f"runs/{run.id}/model/{ticket}.request.json"),
                         request.canonical_body.encode("utf-8"), immutable=True)
            run.model_records.append(record)
            run.usage.model_calls += 1
            run.usage.model_tokens_unknown += request.reserved_tokens
            store.save_run(run)
            if fault:
                fault("after_model_reserved")
            return ticket

    def settle(ticket, reply):
        with store.run_lock(run.id):
            record = next(item for item in run.model_records if item["id"] == ticket)
            if record["status"] != "reserved" or record["request_hash"] != reply.request_hash:
                raise StoreError("model settlement identity mismatch")
            if store.load_run(run.id) != run:
                raise StoreError("Run changed during model transmission")
            data = asdict(reply)
            _validate_reply(data, record)
            if _has_credential(json.dumps(data, ensure_ascii=False)):
                raise StoreError("credential-like content in model receipt")
            store._write_json(f"runs/{run.id}/model/{ticket}.response.json", data, immutable=True)
            if fault:
                fault("after_model_response_saved")
            store.settle_model(run, ticket)
            if batch:
                committed = next(item for item in run.model_records if item["id"] == ticket)
                batch.settle_model(run, committed)

    return transport.send(prepared, reserve=reserve, settle=settle)
