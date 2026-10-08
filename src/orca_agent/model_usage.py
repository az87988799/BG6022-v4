"""Durable per-Run HTTP reservations, receipts and conservative crash recovery."""

import hashlib
import json
import math
from dataclasses import asdict, fields
from decimal import Decimal

from orca_agent.decision_purpose import (
    DECISION_CONTRACT_PROMPT_VERSIONS,
    DecisionPurpose,
    prepared_authority,
    prepared_decision_purpose,
)
from orca_agent.llm import (
    MAX_PROPOSAL_BYTES,
    MAX_RESPONSE_BYTES,
    ModelReply,
    ModelUsage,
    _canonical,
    _has_credential,
    _metadata,
    _recordable_raw_content,
    _strict_json,
    model_profile_parameters,
    request_model_profile,
)
from orca_agent.models import new_id, utc_now
from orca_agent.store import BudgetExceeded, StoreError, _id, _json_bytes, atomic_write

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
_LEGACY_REPLY_FIELDS = {
    "request_hash", "proposal", "error_category", "retryable", "usage", "response_hash",
    "response_bytes", "response_model", "provider_request_id", "completion_id", "finish_reason",
    "http_status", "retry_after_seconds", "elapsed_seconds",
}


def _cost(prompt, completion):
    return str((prompt * INPUT_USD_PER_MILLION + completion * OUTPUT_USD_PER_MILLION)
               / Decimal(1_000_000))


def current_basis(store, run):
    return {"request_version": run.request_version, "plan_version": run.plan_version,
            "permission_version": run.permission.version,
            "control_generation": store.read_control(run.id)["generation"]}


TERMINAL_INPUT_ENVELOPE = 10000
TERMINAL_SECONDS = 60


def final_explanation_budget(request, run, *, final_only=False, purpose=None,
                             prepared=None, now=None):
    """Account for required final prose within the existing frozen Run limits.

    The caller has already charged the current decision round. These are
    New purpose-aware requests reserve the declared delivery envelope within
    the frozen budget. This is planning occupancy, never a token receipt.
    ``final_only`` remains for historical callers; production uses purpose.
    """
    required = request.conditions.get("explain_results") is True
    calls = max(0, run.budget.model_calls - run.usage.model_calls)
    rounds = max(0, run.budget.decision_rounds - run.usage.decision_rounds)
    tokens = max(0, run.budget.model_tokens - run.usage.model_tokens_used - run.usage.model_tokens_unknown)
    terminal = purpose.kind == "terminal" if isinstance(purpose, DecisionPurpose) else final_only
    future = 1 if required and not terminal else 0
    input_bound = min(run.budget.input_tokens, TERMINAL_INPUT_ENVELOPE)
    final_tokens = input_bound + run.budget.output_tokens
    current_tokens = prepared.reserved_tokens if prepared is not None else 0
    seconds = max(0.0, (run.deadline - (now or utc_now())).total_seconds())
    current_seconds = prepared.timeout_seconds if prepared is not None else 0
    bounded = purpose is not None
    future_tokens = future * final_tokens if bounded else 0
    future_seconds = future * TERMINAL_SECONDS if bounded else 0
    corrections = min(run.budget.corrections_per_proposal, max(0, calls - 1 - future), max(0, rounds - future))
    if bounded:
        corrections = min(corrections, max(0, (tokens - current_tokens - future_tokens) // max(1, final_tokens)),
                          max(0, int((seconds - current_seconds - future_seconds) // TERMINAL_SECONDS)))
    return {"required": required, "final_only": final_only,
            "purpose": purpose.kind if isinstance(purpose, DecisionPurpose) else "legacy",
            "future_answer_calls": future,
            "future_answer_tokens": future_tokens, "future_answer_seconds": future_seconds,
            "terminal_input_envelope": input_bound,
            "available_future_correction_calls": corrections,
            "configured_corrections_per_proposal": run.budget.corrections_per_proposal,
            "remaining_calls_before_this_request": calls,
            "remaining_decision_rounds_after_this_round": rounds,
            "remaining_tokens_before_this_request": tokens,
            "can_send_before_final": (calls >= future + 1 and rounds >= future
                and tokens >= current_tokens + future_tokens
                and seconds >= current_seconds + future_seconds),
            "token_count_basis": "planning occupancy only; actual request reserved once, unknown usage remains occupied"}


def validate_final_explanation_capacity(request, run, *, final_only=False, **kwargs):
    assessment = final_explanation_budget(request, run, final_only=final_only, **kwargs)
    if assessment["required"] and not assessment["can_send_before_final"]:
        raise BudgetExceeded("required final explanation lacks remaining call/decision budget")
    return assessment


def validate_delivery_margin(request, run, *, purpose="planning", prepared=None,
                             execution_seconds=0, now=None):
    """Read-only preflight for a decision or costly Tool; never charge usage."""
    if isinstance(purpose, str):
        purpose = DecisionPurpose(purpose, ())
    assessment = final_explanation_budget(request, run, purpose=purpose, prepared=prepared, now=now)
    if not assessment["required"]:
        return assessment
    calls = assessment["remaining_calls_before_this_request"]
    future = assessment["future_answer_calls"]
    needed_calls = future + (prepared is not None)
    remaining = max(0.0, (run.deadline - (now or utc_now())).total_seconds())
    time_needed = execution_seconds + assessment["future_answer_seconds"] + (
        prepared.timeout_seconds if prepared is not None else 0)
    token_needed = assessment["future_answer_tokens"] + (prepared.reserved_tokens if prepared is not None else 0)
    if (calls < needed_calls or assessment["remaining_decision_rounds_after_this_round"] < future
            or assessment["remaining_tokens_before_this_request"] < token_needed
            or remaining < time_needed):
        raise BudgetExceeded("required final explanation lacks remaining call/token/decision/time budget")
    return assessment


def _request_body(store, run, record):
    path = store.path(f"runs/{run.id}/model/{_id(record['id'])}.request.json")
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise StoreError("model request evidence is missing or exceeds its size bound")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != record["request_hash"]:
        raise StoreError("frozen model request changed")
    try:
        body = _strict_json(raw)
        profile = request_model_profile(body)
    except (ValueError, UnicodeError, RecursionError):
        raise StoreError("invalid frozen model request profile") from None
    # Only historical disabled requests predate explicit profile records.
    if record.get("model_profile", "disabled") != profile:
        raise StoreError("model record profile differs from its frozen request")
    return body


def validate_model_profile(store, run, expected=None):
    """Read-only verification of this Run's local immutable execution config.

    Historical requests remain untouched: their hashed mode parameters are the
    authority when no configuration snapshot existed. No missing field may turn
    an old disabled Run into thinking_low during recovery.
    """
    if expected is not None:
        model_profile_parameters(expected)
    path = store.path(f"runs/{run.id}/model-profile.json")
    frozen = None
    if path.exists():
        try:
            if not path.is_file() or path.stat().st_size > 4096:
                raise ValueError("invalid size")
            data = _strict_json(path.read_bytes())
            if (not isinstance(data, dict)
                    or set(data) != {"schema_version", "run_id", "model_profile"}
                    or type(data["schema_version"]) is not int or data["schema_version"] != 1
                    or data["run_id"] != run.id):
                raise ValueError("invalid shape")
            model_profile_parameters(data["model_profile"])
            frozen = data["model_profile"]
        except (ValueError, UnicodeError, RecursionError):
            raise StoreError("invalid immutable model profile snapshot") from None
    elif any("model_profile" in record for record in run.model_records):
        raise StoreError("immutable model profile snapshot is missing")
    for record in run.model_records:
        actual = request_model_profile(_request_body(store, run, record))
        if frozen is not None and actual != frozen:
            raise StoreError("Run contains conflicting frozen model profiles")
        frozen = actual
    if expected is not None and frozen is not None and expected != frozen:
        raise StoreError(f"Run model profile is frozen as {frozen}; cannot select {expected}")
    return frozen


def _freeze_model_profile(store, run, profile):
    # The caller holds the Run lock. Publish before any HTTP/batch reservation,
    # so a crash with zero requests cannot reopen mode selection on resume.
    validate_model_profile(store, run, profile)
    store._write_json(f"runs/{run.id}/model-profile.json", {
        "schema_version": 1, "run_id": run.id, "model_profile": profile,
    }, immutable=True)


def _validate_reply(data, record):
    """Reconstruct bounded, typed and credential-free persisted transport facts."""
    if not isinstance(data, dict):
        raise StoreError("invalid model response record shape")
    if set(data) == _LEGACY_REPLY_FIELDS:
        # This is the one historical complete shape, not a generic defaults
        # migration. Never alter the original receipt or its settled hash.
        data = {**data, "raw_content": None}
    elif set(data) != {field.name for field in fields(ModelReply)}:
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
    raw_content = data["raw_content"]
    if raw_content is not None:
        if not isinstance(raw_content, str) or _recordable_raw_content(raw_content) != raw_content:
            raise StoreError("invalid model raw content receipt")
        if proposal is not None:
            try:
                parsed = _strict_json(raw_content)
                consistent = isinstance(parsed, dict) and _canonical(parsed) == _canonical(proposal)
            except (ValueError, UnicodeError, RecursionError):
                consistent = False
            if not consistent:
                raise StoreError("model raw content differs from its proposal")
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
    validate_model_profile(store, run)
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
        validate_model_profile(store, run)
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


def send_model(store, run, prepared, transport, *, basis, logical_id, batch=None, fault=None,
               purpose=None, delivery_snapshot=None):
    logical_id = _id(logical_id)
    sent_purpose = prepared_decision_purpose(prepared)
    if purpose is not None and purpose != sent_purpose:
        raise StoreError("model decision purpose differs from prepared request")
    purpose = sent_purpose
    authority = prepared_authority(prepared)
    if prepared.prompt_version in DECISION_CONTRACT_PROMPT_VERSIONS and purpose is None:
        raise StoreError("new model requests require an explicit decision purpose")
    validate_model_profile(store, run, prepared.model_profile)
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
            user_request = store.load_request(run)
            pending = any(message["id"] not in run.processed_messages
                          for message in store.read_control(run.id)["messages"])
            if purpose is not None:
                from orca_agent.agent import _ready
                from orca_agent.context import current_decision_purpose
                from orca_agent.runner import _step_results
                plan = store.load_plan(run)
                actual = current_decision_purpose(user_request, run, plan, pending_messages=pending,
                    ready_step_ids=[step.id for step in _ready(plan, _step_results(store, run))])
                if actual.kind != purpose.kind or not set(purpose.allowed_actions) <= set(actual.allowed_actions):
                    raise StoreError("decision purpose no longer matches legal actions")
                if authority.get("basis") != basis:
                    raise StoreError("prepared decision basis differs from reservation")
                validate_delivery_margin(user_request, run, purpose=purpose, prepared=request)
            else:
                final_only = not pending and all(run.goal_status.get(goal.id) == "satisfied"
                                                 for goal in user_request.goals if goal.required)
                validate_final_explanation_capacity(user_request, run, final_only=final_only)
            contract = authority.get("contract_required") is True
            if (request.prompt_version in DECISION_CONTRACT_PROMPT_VERSIONS and purpose is not None
                    and "stop" in purpose.allowed_actions and not contract):
                raise StoreError("new stop decision requires a delivery snapshot contract")
            snapshot_bytes = None
            if delivery_snapshot is not None:
                snapshot_bytes = _json_bytes(delivery_snapshot)
                if len(snapshot_bytes) > 256 * 1024:
                    raise StoreError("delivery snapshot exceeds its size bound")
                snapshot_basis = {key: delivery_snapshot.get("basis", {}).get(key) for key in basis}
                fingerprint = hashlib.sha256(_canonical({key: value for key, value in delivery_snapshot.items()
                                                          if key != "fingerprint"}).encode("utf-8")).hexdigest()
                if snapshot_basis != basis or fingerprint != delivery_snapshot.get("fingerprint"):
                    raise StoreError("delivery snapshot basis or fingerprint differs")
            if contract and (snapshot_bytes is None or authority.get("delivery_snapshot_fingerprint")
                             != delivery_snapshot["fingerprint"]):
                raise StoreError("model contract requires the exact prepared delivery snapshot")
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
            _freeze_model_profile(store, run, request.model_profile)
            if fault:
                fault("after_model_profile_frozen")
            ticket = new_id("model")
            record = {"id": ticket, "logical_id": logical_id, "request_hash": request.request_hash,
                      "basis": dict(basis), "input_reserved": request.input_token_bound,
                      "output_reserved": request.output_token_bound,
                      "cost_reserved_usd": _cost(request.input_token_bound, request.output_token_bound),
                      "created_at": utc_now().isoformat(), "status": "reserved",
                      "prompt_version": request.prompt_version, "sdk_version": request.sdk_version,
                      "model": request.model, "token_bound_version": request.token_bound_version,
                      "model_profile": request.model_profile}
            if snapshot_bytes is not None:
                store._write_json(f"runs/{run.id}/model/{ticket}.delivery.json", delivery_snapshot, immutable=True)
                record["delivery_snapshot_sha256"] = hashlib.sha256(snapshot_bytes).hexdigest()
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
            if len(_json_bytes(data)) > MAX_RESPONSE_BYTES:
                raise StoreError("model response evidence exceeds its size bound")
            store._write_json(f"runs/{run.id}/model/{ticket}.response.json", data, immutable=True)
            if fault:
                fault("after_model_response_saved")
            store.settle_model(run, ticket)
            if batch:
                committed = next(item for item in run.model_records if item["id"] == ticket)
                batch.settle_model(run, committed)

    return transport.send(prepared, reserve=reserve, settle=settle)
