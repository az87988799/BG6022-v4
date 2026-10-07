import json
from dataclasses import replace
from decimal import Decimal

import pytest

from orca_agent.llm import (
    MAX_RAW_CONTENT_BYTES,
    ModelReply,
    ModelUsage,
    _parse_response,
    prepare_request,
)
from orca_agent.model_usage import current_basis, read_model_reply, recover_models, send_model
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import BudgetExceeded, Store, StoreError


class ScriptedTransport:
    def __init__(self, reply=None, *, during_send=None):
        self.reply = reply
        self.during_send = during_send
        self.sends = 0

    def send(self, request, *, reserve, settle):
        ticket = reserve(request)
        self.sends += 1
        if self.during_send:
            self.during_send()
        reply = self.reply or ModelReply(request_hash=request.request_hash,
                                        proposal={"action": "stop"}, usage=ModelUsage(10, 5, 15))
        settle(ticket, reply)
        return reply


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "data")
    request = Request(goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    run = store.create_run(request, None, PermissionSnapshot(model_execution=True),
                           BudgetLimits(model_calls=8, model_tokens=48000,
                                        input_tokens=12000, output_tokens=2000))
    prepared = prepare_request([{"role": "user", "content": "Return JSON."}], max_output_tokens=100)
    return store, run, prepared


def invoke(setup, transport, *, logical_id="decision_1", basis=None, **kwargs):
    store, run, prepared = setup
    return send_model(store, run, prepared, transport,
                      basis=basis or current_basis(store, run), logical_id=logical_id, **kwargs)


def test_reservation_is_durable_before_transmission_and_known_usage_settles_once(setup):
    store, run, prepared = setup

    def inspect():
        durable = store.load_run(run.id)
        assert durable.usage.model_calls == 1
        assert durable.usage.model_tokens_unknown == prepared.reserved_tokens
        assert durable.model_records[0]["status"] == "reserved"

    transport = ScriptedTransport(during_send=inspect)
    reply = invoke(setup, transport)
    durable = store.load_run(run.id)
    assert durable == run
    assert durable.usage.model_calls == 1 and transport.sends == 1
    assert durable.usage.model_tokens_unknown == 0
    assert durable.usage.model_tokens_used == 15
    record = durable.model_records[0]
    assert record["status"] == "known"
    assert Decimal(record["cost_known_usd"]) == Decimal("0.000009")
    assert len(record["response_record_sha256"]) == 64
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert invoke(setup, transport) == reply
    assert transport.sends == 1
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before


@pytest.mark.parametrize("category", [None, "timeout", "authentication", "rate_limit"])
def test_missing_usage_and_http_failures_keep_full_occupancy(setup, category):
    store, run, prepared = setup
    reply = ModelReply(request_hash=prepared.request_hash,
                       proposal={"action": "stop"} if category is None else None,
                       error_category=category)
    transport = ScriptedTransport(reply)
    assert invoke(setup, transport) == reply
    assert run.usage.model_tokens_unknown == prepared.reserved_tokens
    assert run.usage.model_tokens_used == 0
    assert run.model_records[0]["status"] == "unknown"
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    recovered = recover_models(store, run)
    assert recovered[run.model_records[0]["id"]] == reply
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert transport.sends == 1


@pytest.mark.parametrize("limit", ["calls", "input", "output", "total"])
def test_insufficient_run_balance_prevents_transmission(setup, limit):
    store, original, prepared = setup
    values = original.budget.model_dump()
    values.update({"calls": {"model_calls": 0},
                   "input": {"input_tokens": prepared.input_token_bound - 1},
                   "output": {"output_tokens": 99},
                   "total": {"model_tokens": prepared.reserved_tokens - 1}}[limit])
    run = store.create_run(store.load_request(original), None, original.permission, BudgetLimits(**values))
    transport = ScriptedTransport()
    with pytest.raises(BudgetExceeded, match="budget exhausted"):
        invoke((store, run, prepared), transport)
    assert transport.sends == 0
    assert run.model_records == []
    assert store.load_run(run.id).usage.model_calls == 0


def test_user_message_and_pause_during_http_preserve_original_response_basis(setup):
    store, run, _ = setup
    basis = current_basis(store, run)

    def interrupted():
        store.enqueue_message(run.id, "Use the other geometry after clarification.")
        store.signal(run.id, "pause")

    transport = ScriptedTransport(during_send=interrupted)
    invoke(setup, transport, basis=basis)
    assert run.model_records[0]["basis"] == basis
    assert store.read_control(run.id)["generation"] == 2
    assert store.read_control(run.id)["messages"][0]["text"].startswith("Use the other")
    assert store.read_signal(run.id) == "pause"
    assert run.usage.model_calls == 1 and run.usage.model_tokens_used == 15
    next_transport = ScriptedTransport()
    with pytest.raises(StoreError, match="stale model basis"):
        invoke(setup, next_transport, logical_id="next", basis=basis)
    assert next_transport.sends == 0


def test_already_paused_run_blocks_fresh_send(setup):
    store, run, _ = setup
    store.signal(run.id, "pause")
    transport = ScriptedTransport()
    with pytest.raises(BudgetExceeded, match="control"):
        invoke(setup, transport)
    assert transport.sends == 0


def test_new_run_does_not_release_old_unknown_occupancy(setup):
    store, old, prepared = setup
    invoke(setup, ScriptedTransport(ModelReply(request_hash=prepared.request_hash,
                                              error_category="timeout")))
    old_bytes = store.path(f"runs/{old.id}/run.json").read_bytes()
    other = store.create_run(store.load_request(old), None, old.permission, old.budget)
    invoke((store, other, prepared), ScriptedTransport())
    assert store.path(f"runs/{old.id}/run.json").read_bytes() == old_bytes
    assert old.usage.model_tokens_unknown == prepared.reserved_tokens
    assert other.usage.model_tokens_used == 15


@pytest.mark.parametrize("stage", ["after_model_reserved", "after_model_response_saved"])
def test_crash_recovery_twice_never_resends_or_double_charges(setup, stage):
    store, run, prepared = setup
    original_basis = current_basis(store, run)
    transport = ScriptedTransport()

    def crash(point):
        if point == stage:
            raise OSError("injected process loss")

    with pytest.raises(OSError, match="injected"):
        invoke(setup, transport, fault=crash)
    restarted = store.load_run(run.id)
    assert restarted.model_records[0]["status"] == "reserved"
    assert restarted.usage.model_tokens_unknown == prepared.reserved_tokens
    first = recover_models(store, restarted)
    saved = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert recover_models(store, restarted) == first
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == saved
    if stage == "after_model_reserved":
        assert transport.sends == 0
        assert restarted.usage.model_tokens_unknown == prepared.reserved_tokens
        assert first[restarted.model_records[0]["id"]].error_category == "response_missing"
    else:
        assert transport.sends == 1
        assert restarted.usage.model_tokens_unknown == 0
        assert restarted.usage.model_tokens_used == 15
        assert first[restarted.model_records[0]["id"]].proposal == {"action": "stop"}
    assert restarted.usage.model_calls == 1
    replay_transport = ScriptedTransport()
    reply = invoke((store, restarted, prepared), replay_transport, basis=original_basis)
    assert reply == next(iter(first.values()))
    assert replay_transport.sends == 0


def test_saved_receipt_recovery_does_not_relabel_after_user_message(setup):
    store, run, _ = setup
    basis = current_basis(store, run)

    def crash(point):
        if point == "after_model_response_saved":
            raise OSError("crash")

    with pytest.raises(OSError):
        invoke(setup, ScriptedTransport(), fault=crash)
    store.enqueue_message(run.id, "Change the requested comparison.")
    resumed = store.load_run(run.id)
    recover_models(store, resumed)
    assert resumed.model_records[0]["basis"] == basis
    assert current_basis(store, resumed)["control_generation"] == 1


def test_ordinary_save_cannot_refund_unknown_or_forge_settlement(setup):
    store, run, prepared = setup
    invoke(setup, ScriptedTransport(ModelReply(request_hash=prepared.request_hash,
                                              error_category="timeout")))
    changed = run.model_copy(deep=True)
    changed.usage.model_tokens_unknown = 0
    with pytest.raises(StoreError, match="cannot decrease"):
        store.save_run(changed)
    changed = run.model_copy(deep=True)
    changed.model_records[0]["status"] = "known"
    with pytest.raises(StoreError, match="dedicated"):
        store.save_run(changed)


def test_reserved_record_also_requires_dedicated_settlement(setup):
    store, run, _ = setup

    def crash(point):
        raise OSError("crash")

    with pytest.raises(OSError):
        invoke(setup, ScriptedTransport(), fault=crash)
    changed = store.load_run(run.id)
    changed.model_records[0]["status"] = "known"
    with pytest.raises(StoreError, match="dedicated"):
        store.save_run(changed)


@pytest.mark.parametrize("evidence", ["request", "response"])
def test_changed_immutable_model_evidence_blocks_replay(setup, evidence):
    store, run, _ = setup
    invoke(setup, ScriptedTransport())
    ticket = run.model_records[0]["id"]
    path = store.path(f"runs/{run.id}/model/{ticket}.{evidence}.json")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(StoreError, match="changed"):
        recover_models(store, run)
    assert run.usage.model_calls == 1


def test_missing_settled_response_never_becomes_zero_cost(setup):
    store, run, _ = setup
    invoke(setup, ScriptedTransport())
    ticket = run.model_records[0]["id"]
    store.path(f"runs/{run.id}/model/{ticket}.response.json").unlink()
    with pytest.raises(StoreError, match="missing"):
        recover_models(store, run)
    assert run.usage.model_tokens_used == 15


def test_logical_identity_cannot_rebind_basis_or_request(setup):
    store, run, prepared = setup
    invoke(setup, ScriptedTransport())
    changed = prepare_request([{"role": "user", "content": "Different JSON request"}])
    with pytest.raises(StoreError, match="cannot be rebound"):
        invoke((store, run, changed), ScriptedTransport())
    store.enqueue_message(run.id, "different goal")
    with pytest.raises(StoreError, match="cannot be rebound"):
        invoke((store, run, prepared), ScriptedTransport())


@pytest.mark.parametrize("malformed", ["negative", "total", "nan", "foreign", "credential"])
def test_malformed_receipt_cannot_release_occupancy(setup, malformed):
    store, run, prepared = setup
    reply = ModelReply(request_hash=prepared.request_hash, proposal={"action": "stop"},
                       usage=ModelUsage(10, 5, 15))
    if malformed == "negative":
        reply = replace(reply, usage=ModelUsage(-1, 5, 4))
    elif malformed == "total":
        reply = replace(reply, usage=ModelUsage(10, 5, 20))
    elif malformed == "nan":
        reply = replace(reply, elapsed_seconds=float("nan"))
    elif malformed == "foreign":
        reply = replace(reply, request_hash="f" * 64)
    else:
        reply = replace(reply, proposal={"text": "Bearer fake-credential"})
    with pytest.raises(StoreError):
        invoke(setup, ScriptedTransport(reply))
    assert store.load_run(run.id).usage.model_tokens_unknown == prepared.reserved_tokens
    assert store.load_run(run.id).model_records[0]["status"] == "reserved"


def test_observed_usage_overrun_is_charged_and_prevents_future_sends(setup):
    store, run, prepared = setup
    reply = ModelReply(request_hash=prepared.request_hash, error_category="token_bound_exceeded",
                       usage=ModelUsage(prepared.input_token_bound + 1, 1,
                                        prepared.input_token_bound + 2))
    invoke(setup, ScriptedTransport(reply))
    assert run.usage.model_tokens_used == prepared.input_token_bound + 2
    assert run.usage.model_tokens_unknown == 0
    transport = ScriptedTransport()
    with pytest.raises(StoreError, match="further transmission"):
        invoke(setup, transport, logical_id="next")
    assert transport.sends == 0


def test_batch_reservation_precedes_send_and_settlement_recovery_is_idempotent(setup):
    store, run, prepared = setup

    class Batch:
        def __init__(self):
            self.records = {}

        def reserve_model(self, run, record):
            assert record["id"] not in self.records
            self.records[record["id"]] = dict(record)

        def settle_model(self, run, record):
            assert record["id"] in self.records
            assert Decimal(record["cost_reserved_usd"]) > 0
            self.records[record["id"]] = dict(record)

    batch = Batch()

    def inspect_batch_reservation():
        assert len(batch.records) == 1
        assert next(iter(batch.records.values()))["status"] == "reserved"

    transport = ScriptedTransport(during_send=inspect_batch_reservation)
    invoke(setup, transport, batch=batch)
    snapshot = json.dumps(batch.records, sort_keys=True)
    recover_models(store, run, batch=batch)
    assert json.dumps(batch.records, sort_keys=True) == snapshot
    assert len(batch.records) == 1 and transport.sends == 1
    assert Decimal(next(iter(batch.records.values()))["cost_known_usd"]) == Decimal("0.000009")


@pytest.mark.parametrize("content,error", [
    (' \n{"action":"stop"} \n', None),
    ('{"action": "stop",', "invalid_proposal_json"),
    ('{"a":1,"a":2}', "invalid_proposal_json"),
    ('{"action":', "truncated"),
], ids=["success", "invalid-json", "duplicate", "truncated"])
def test_raw_content_survives_saved_response_crash_and_replay_without_new_http(setup, content, error):
    store, run, prepared = setup
    response = {"choices": [{"message": {"content": content},
                            "finish_reason": "length" if error == "truncated" else "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    reply = _parse_response(prepared, json.dumps(response).encode(), 200, None)
    assert reply.raw_content == content and reply.error_category == error
    transport = ScriptedTransport(reply)
    basis = current_basis(store, run)

    def crash(stage):
        if stage == "after_model_response_saved":
            raise OSError("receipt saved, settlement interrupted")

    with pytest.raises(OSError, match="settlement interrupted"):
        invoke(setup, transport, fault=crash)
    restarted = store.load_run(run.id)
    ticket = restarted.model_records[0]["id"]
    path = store.path(f"runs/{run.id}/model/{ticket}.response.json")
    original = path.read_bytes()
    assert json.loads(original)["raw_content"] == content
    assert read_model_reply(store, restarted, restarted.model_records[0])[0] == reply
    assert recover_models(store, restarted)[ticket] == reply
    saved = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert invoke((store, restarted, prepared), transport, basis=basis) == reply
    assert transport.sends == 1 and restarted.usage.model_calls == 1
    assert restarted.usage.model_tokens_used == 15 and restarted.usage.model_tokens_unknown == 0
    assert recover_models(store, restarted)[ticket] == reply
    assert path.read_bytes() == original and store.path(f"runs/{run.id}/run.json").read_bytes() == saved


@pytest.mark.parametrize("raw_content", [
    42, False, [], {}, "x" * (MAX_RAW_CONTENT_BYTES + 1),
    "水" * (MAX_RAW_CONTENT_BYTES // 3 + 1), "\x00" * 12000,
    "Bearer do-not-persist", 'bad JSON "\\u0073k-abcdefghijklmnopqr',
], ids=["integer", "boolean", "list", "dict", "ascii-over-limit", "utf8-over-limit",
        "json-expansion", "bearer", "escaped-credential"])
def test_invalid_or_sensitive_raw_receipt_never_reaches_disk_or_releases_budget(setup, raw_content):
    store, run, prepared = setup
    reply = ModelReply(request_hash=prepared.request_hash, raw_content=raw_content,
                       error_category="invalid_proposal_json", usage=ModelUsage(10, 5, 15))
    with pytest.raises(StoreError, match="raw content"):
        invoke(setup, ScriptedTransport(reply))
    assert not list(store.path(f"runs/{run.id}/model").glob("*.response.json"))
    assert run.usage.model_tokens_unknown == prepared.reserved_tokens and run.usage.model_tokens_used == 0


@pytest.mark.parametrize("raw_content", [
    '{"action":"clarify"}', '{"action":"stop","action":"stop"}',
    '```json\n{"action":"stop"}\n```', '{"action":"stop","value":NaN}',
    "[]", '{"a":' * 26 + '0' + '}' * 26,
], ids=["different", "duplicate", "fenced", "nan", "array", "deep"])
def test_raw_text_must_strictly_parse_to_the_same_successful_proposal(setup, raw_content):
    store, run, prepared = setup
    reply = ModelReply(request_hash=prepared.request_hash, raw_content=raw_content,
                       proposal={"action": "stop"}, usage=ModelUsage(10, 5, 15))
    with pytest.raises(StoreError, match="raw content differs"):
        invoke(setup, ScriptedTransport(reply))
    assert not list(store.path(f"runs/{run.id}/model").glob("*.response.json"))
    assert run.usage.model_tokens_unknown == prepared.reserved_tokens


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_credential_response_settles_usage_without_writing_sensitive_raw_text(setup, monkeypatch, finish):
    store, run, prepared = setup
    secret = "test-secret-credential"
    monkeypatch.setenv("DEEPSEEK_API_KEY", secret)
    response = {"choices": [{"message": {"content": '{"text":"' + secret}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
    reply = _parse_response(prepared, json.dumps(response).encode(), 200, None)
    transport = ScriptedTransport(reply)
    invoke(setup, transport)
    assert reply.raw_content is None and reply.proposal is None and reply.error_category
    assert all(secret.encode() not in path.read_bytes() for path in store.path(f"runs/{run.id}/model").glob("*.json"))
    assert run.usage.model_tokens_used == 15 and run.usage.model_tokens_unknown == 0
    assert invoke(setup, transport) == reply and transport.sends == 1
