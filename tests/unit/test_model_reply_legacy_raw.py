"""Read old receipt bytes without migration; raw text remains bounded evidence."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from orca_agent.llm import prepare_request
from orca_agent.model_usage import current_basis, read_model_reply, recover_models, send_model
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import Store, StoreError

ARCHIVED_RUN = Path(__file__).resolve().parents[2] / "docs/acceptance/phase-b/b04-b05-live/run"


class NoSend:
    def send(self, *args, **kwargs):
        pytest.fail("receipt recovery must not invoke a model transport")


class ReserveOnly:
    def send(self, request, *, reserve, settle):
        reserve(request)
        pytest.fail("the injected reservation crash must precede transmission")


@pytest.fixture
def reserved(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    request = Request(goals=[Goal(id="read", port="value_observation", minimum_check_version="evidence-read-1")])
    run = store.create_run(request, None, PermissionSnapshot(model_execution=True),
                           BudgetLimits(model_calls=2, model_tokens=48000, input_tokens=12000, output_tokens=2000))
    prepared = prepare_request([{"role": "user", "content": "Return JSON."}], max_output_tokens=100)
    basis = current_basis(store, run)

    def crash(stage):
        if stage == "after_model_profile_frozen":
            return
        assert stage == "after_model_reserved"
        raise OSError("offline reservation crash")

    with pytest.raises(OSError, match="offline reservation crash"):
        send_model(store, run, prepared, ReserveOnly(), basis=basis, logical_id="legacy_probe", fault=crash)
    return store, store.load_run(run.id), prepared, basis


def legacy_receipt(request_hash, kind="success"):
    """Frozen historical schema, independent of the current dataclass fields.

    Failure/unknown examples are synthetic old-format records: the checked-in
    real archive contains successful replies only, not real transport failures.
    """
    unknown = kind == "unknown"
    return {
        "request_hash": request_hash,
        "proposal": {"action": "stop"} if kind == "success" else None,
        "error_category": {"success": None, "failure": "invalid_proposal_json", "unknown": "timeout"}[kind],
        "retryable": unknown,
        "usage": None if unknown else {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                                        "prompt_cache_hit_tokens": None, "prompt_cache_miss_tokens": None},
        "response_hash": None if unknown else "a" * 64,
        "response_bytes": 0 if unknown else 321,
        "response_model": None if unknown else "deepseek-flash",
        "provider_request_id": None,
        "completion_id": None if unknown else "old-completion-1",
        "finish_reason": None if unknown else "stop",
        "http_status": None if unknown else 200,
        "retry_after_seconds": None,
        "elapsed_seconds": 0.25,
    }


def save_receipt(store, run, data):
    record = run.model_records[0]
    path = store.path(f"runs/{run.id}/model/{record['id']}.response.json")
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return path, raw


def test_original_real_legacy_success_read_and_recovery_preserve_all_archived_bytes_and_cost(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    original = json.loads((ARCHIVED_RUN / "run.json").read_text(encoding="utf-8"))
    destination = store.path(f"runs/{original['id']}")
    (destination / "model").mkdir(parents=True)
    for source in [ARCHIVED_RUN / "run.json", *sorted((ARCHIVED_RUN / "model").glob("*.json"))]:
        target = destination / source.relative_to(ARCHIVED_RUN)
        target.write_bytes(source.read_bytes())
    frozen = {path: path.read_bytes() for path in destination.rglob("*.json")}
    run = store.load_run(original["id"])
    accounting = run.usage.model_dump_json(), json.dumps(run.model_records, sort_keys=True)
    for record in run.model_records:
        path = destination / "model" / f"{record['id']}.response.json"
        assert len(json.loads(path.read_bytes())) == 14
        assert "raw_content" not in json.loads(path.read_bytes())
        reply, digest = read_model_reply(store, run, record)
        assert reply.raw_content is None and reply.proposal is not None
        assert reply.usage.total_tokens == record["total_tokens"]
        assert digest == record["response_record_sha256"] == hashlib.sha256(frozen[path]).hexdigest()
    first = recover_models(store, run)
    assert recover_models(store, run) == first
    assert (run.usage.model_dump_json(), json.dumps(run.model_records, sort_keys=True)) == accounting
    assert all(path.read_bytes() == contents for path, contents in frozen.items())


@pytest.mark.parametrize("kind", ["success", "failure", "unknown"])
def test_legacy_complete_receipt_recovers_once_without_schema_rewrite_or_new_transmission(reserved, kind):
    store, run, prepared, basis = reserved
    data = legacy_receipt(prepared.request_hash, kind)
    assert len(data) == 14 and "raw_content" not in data
    path, original = save_receipt(store, run, data)
    reserved_bytes = store.path(f"runs/{run.id}/run.json").read_bytes()
    reply, digest = read_model_reply(store, run, run.model_records[0])
    assert reply.raw_content is None and reply.error_category == data["error_category"]
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == reserved_bytes
    assert run.usage.model_tokens_unknown == prepared.reserved_tokens
    ticket = run.model_records[0]["id"]
    assert recover_models(store, run)[ticket] == reply
    record = run.model_records[0]
    assert record["response_record_sha256"] == digest == hashlib.sha256(original).hexdigest()
    if kind == "unknown":
        assert record["status"] == "unknown" and "cost_known_usd" not in record
        assert run.usage.model_tokens_unknown == prepared.reserved_tokens and run.usage.model_tokens_used == 0
    else:
        assert record["status"] == "known" and Decimal(record["cost_known_usd"]) == Decimal("0.000009")
        assert run.usage.model_tokens_unknown == 0 and run.usage.model_tokens_used == 15
    settled = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert read_model_reply(store, run, record)[0] == reply
    assert recover_models(store, run)[ticket] == reply
    assert send_model(store, run, prepared, NoSend(), basis=basis, logical_id="legacy_probe") == reply
    assert path.read_bytes() == original
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == settled
    assert run.usage.model_calls == 1 and run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("change", ["missing_old", "new_missing_old", "extra_old", "extra_new", "wrong_request"])
def test_partial_or_mixed_receipt_shapes_cannot_settle_reserved_cost(reserved, change):
    store, run, prepared, _ = reserved
    data = legacy_receipt(prepared.request_hash)
    if change in {"new_missing_old", "extra_new"}:
        data["raw_content"] = '{"action":"stop"}'
    if change in {"missing_old", "new_missing_old"}:
        data.pop("retryable")
    elif change in {"extra_old", "extra_new"}:
        data["receipt_version"] = "untrusted-migration"
    else:
        data["request_hash"] = "f" * 64
    path, original = save_receipt(store, run, data)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(StoreError, match="shape|identity"):
        recover_models(store, run)
    assert path.read_bytes() == original
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert run.model_records[0]["status"] == "reserved"
    assert run.usage.model_tokens_unknown == prepared.reserved_tokens and run.usage.model_tokens_used == 0


def test_settled_legacy_receipt_cannot_be_rewritten_as_new_shape_even_with_identical_proposal(reserved):
    store, run, prepared, _ = reserved
    data = legacy_receipt(prepared.request_hash)
    path, _ = save_receipt(store, run, data)
    recover_models(store, run)
    settled = store.path(f"runs/{run.id}/run.json").read_bytes()
    data["raw_content"] = '{"action":"stop"}'
    save_receipt(store, run, data)
    with pytest.raises(StoreError, match="settled model response changed"):
        recover_models(store, run)
    assert json.loads(path.read_bytes())["raw_content"] == '{"action":"stop"}'
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == settled
    assert run.usage.model_tokens_used == 15 and run.usage.model_calls == 1


@pytest.mark.parametrize("raw,value", [('{"value":true}', 1), ('{"value":0}', False), ('{"value":1.0}', 1)])
def test_new_raw_proposal_consistency_is_stricter_than_python_numeric_equality(reserved, raw, value):
    store, run, prepared, _ = reserved
    data = {**legacy_receipt(prepared.request_hash), "proposal": {"value": value}, "raw_content": raw}
    assert json.loads(raw) == data["proposal"]
    save_receipt(store, run, data)
    with pytest.raises(StoreError, match="raw content differs"):
        recover_models(store, run)
    assert run.model_records[0]["status"] == "reserved"
    assert run.usage.model_tokens_unknown == prepared.reserved_tokens


@pytest.mark.parametrize("raw,proposal,error", [
    (' \n{ "说明": "未决", "action": "stop" }\t', {"action": "stop", "说明": "未决"}, None),
    ('{"action": "stop",', None, "invalid_proposal_json"),
    ('{"a":1,"a":2}', None, "invalid_proposal_json"),
    ("水" * (32768 // 3) + "ab", None, "invalid_proposal_json"),
    ("\x00" * 10922, None, "invalid_proposal_json"),
], ids=["success-whitespace-unicode", "invalid-json", "duplicate-json-keys", "utf8-exact-bound", "escaped-near-bound"])
def test_new_raw_success_and_bounded_failed_json_are_readable_unmodified_evidence(reserved, raw, proposal, error):
    store, run, prepared, _ = reserved
    data = {**legacy_receipt(prepared.request_hash), "proposal": proposal, "error_category": error, "raw_content": raw}
    path, original = save_receipt(store, run, data)
    reply, _ = read_model_reply(store, run, run.model_records[0])
    assert reply.raw_content == raw and reply.proposal == proposal and reply.error_category == error
    assert "raw_content=" not in repr(reply)
    recovered = recover_models(store, run)
    assert next(iter(recovered.values())) == reply
    settled = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert recover_models(store, run) == recovered
    assert path.read_bytes() == original
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == settled
    assert run.usage.model_tokens_used == 15 and run.usage.model_tokens_unknown == 0
