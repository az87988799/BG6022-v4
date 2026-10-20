"""Exact EOF/envelope failures and immutable receipt recovery; no network or science."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent.condition_selection import OptionalSelectionParameters
from orca_agent.llm import _canonical, _parse_response, prepare_request
from orca_agent.model_usage import _validate_reply, read_model_reply
from orca_agent.models import Goal, PermissionSnapshot, Request
from orca_agent.proposals import DecisionIntent
from orca_agent.store import Store, StoreError


def request(version="agent-json-v85"):
    return prepare_request([{"role": "user", "content": "Return JSON."}], prompt_version=version)


def response(content, finish="stop"):
    return json.dumps({"model": "deepseek-flash", "choices": [{
        "message": {"role": "assistant", "content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}).encode()


RAW = ('{"action":"select_conditions","parameters":{"request_version":2,"choices":'
       '{"method":{"value":"RHF","reason":"authorized selection"}},"reason":"choose RHF"}')


def record_for(prepared):
    return {"id": "model_format_test", "request_hash": prepared.request_hash,
            "prompt_version": prepared.prompt_version, "input_reserved": prepared.input_token_bound,
            "output_reserved": prepared.output_token_bound,
            "transport_limits": prepared.limits.model_dump(mode="json")}


def test_unique_eof_and_selection_prose_preserve_all_values_and_raw_hashes():
    prepared = request()
    raw = response(RAW)
    reply = _parse_response(prepared, raw, 200, None)
    assert reply.error_category is None and reply.raw_content == RAW
    assert reply.response_hash == hashlib.sha256(raw).hexdigest()
    expected = json.loads(RAW + "}")
    reason = expected["parameters"].pop("reason")
    expected["reason"] = reason
    assert reply.proposal == expected
    intent = DecisionIntent.model_validate(reply.proposal)
    parameters = OptionalSelectionParameters.model_validate(intent.parameters)
    assert parameters.request_version == 2 and parameters.choices["method"].value == "RHF"
    normalization = reply.format_normalization
    assert normalization["appended_suffix"] == "}"
    assert normalization["field_moves"] == [{"from": "parameters.reason", "to": "reason"}]
    assert normalization["raw_content_sha256"] == hashlib.sha256(RAW.encode()).hexdigest()
    assert normalization["normalized_proposal_sha256"] == hashlib.sha256(_canonical(expected).encode()).hexdigest()
    assert _validate_reply(asdict(reply), record_for(prepared)) == reply


@pytest.mark.parametrize("raw", [
    '{"action":"stop","reason":"unfinished',
    '{"action":"stop","parameters":',
    '{"action":"stop" "parameters":{}',
    '{"action":"stop","action":"clarify","parameters":{}',
    '{"action":"stop","parameters":{"value":NaN}',
    '{"action":"stop","parameters":{]',
    '{"action":"stop","parameters":{"x":{"y":{"z":{"w":{}',
])
def test_missing_content_ambiguous_json_and_excess_suffix_still_fail(raw):
    reply = _parse_response(request(), response(raw), 200, None)
    assert reply.error_category == "invalid_proposal_json"
    assert reply.proposal is None and reply.format_normalization is None


def test_old_version_and_length_finish_never_normalize():
    old = _parse_response(request("agent-json-v84"), response(RAW), 200, None)
    short = _parse_response(request(), response(RAW, "length"), 200, None)
    assert old.error_category == "invalid_proposal_json" and old.format_normalization is None
    assert short.error_category == "truncated" and short.format_normalization is None


def test_credential_content_cannot_enter_normalization_audit():
    content = RAW.replace("choose RHF", "sk-" + "a" * 32)
    reply = _parse_response(request(), response(content), 200, None)
    assert reply.error_category == "credential_in_response"
    assert reply.raw_content is reply.proposal is reply.format_normalization is None


def test_byte_limit_includes_appended_closure_and_schema_is_not_approved():
    prepared = request()
    tight = replace(prepared, limits=prepared.limits.model_copy(update={"proposal_bytes": len(RAW.encode())}))
    assert _parse_response(tight, response(RAW), 200, None).error_category == "proposal_too_large"
    missing_version = RAW.replace('"request_version":2,', '')
    reply = _parse_response(prepared, response(missing_version), 200, None)
    assert reply.proposal is not None  # Format accepted; required content still absent.
    with pytest.raises(ValidationError):
        OptionalSelectionParameters.model_validate(reply.proposal["parameters"])
    extra = RAW.replace('"request_version":2,', '"request_version":2,"allow_shell":true,')
    reply = _parse_response(prepared, response(extra), 200, None)
    assert reply.proposal["parameters"]["allow_shell"] is True
    assert reply.format_normalization["field_moves"] == []
    with pytest.raises(ValidationError):
        DecisionIntent.model_validate(reply.proposal)


def test_normalized_receipt_reloads_exactly_and_cannot_rebind_audit_or_version(tmp_path):
    prepared = request()
    reply = _parse_response(prepared, response(RAW), 200, None)
    store = Store(tmp_path / "data")
    run = store.create_run(Request(goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")]),
                           None, PermissionSnapshot())
    record = record_for(prepared)
    run.model_records.append(record)
    folder = f"runs/{run.id}/model/{record['id']}"
    path = store.path(folder + ".request.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(prepared.canonical_body, encoding="utf-8")
    store._write_json(folder + ".response.json", asdict(reply), immutable=True)
    response_path = store.path(folder + ".response.json")
    before = response_path.read_bytes()
    record["response_record_sha256"] = hashlib.sha256(before).hexdigest()
    restored, digest = read_model_reply(store, run, record)
    assert restored == reply and digest == record["response_record_sha256"]
    assert response_path.read_bytes() == before
    tampered = asdict(reply)
    tampered["format_normalization"]["appended_suffix"] = "]}"
    with pytest.raises(StoreError, match="raw content differs"):
        _validate_reply(tampered, record)
    with pytest.raises(StoreError, match="raw content differs"):
        _validate_reply(asdict(reply), {**record, "prompt_version": "agent-json-v84"})
    plain = asdict(_parse_response(request("agent-json-v84"), response('{"action":"stop"}'), 200, None))
    plain.pop("format_normalization")
    assert _validate_reply(plain, record_for(request("agent-json-v84"))).proposal == {"action": "stop"}


def test_actual_a016_four_eof_responses_are_format_only_under_new_version():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a016/data/runs"
    paths = list(root.glob("*/run.json"))
    if not paths:
        pytest.skip("local immutable a016 archive unavailable")
    run = json.loads(paths[0].read_bytes())
    records = [row for row in run["model_records"] if row.get("error_category") == "invalid_proposal_json"]
    assert len(records) == 4
    originals = {}
    for record in records:
        path = paths[0].parent / "model" / (record["id"] + ".response.json")
        raw = path.read_bytes()
        originals[path] = hashlib.sha256(raw).hexdigest()
        assert originals[path] == record["response_record_sha256"]
        old = json.loads(raw)
        content = old["raw_content"]
        reply = _parse_response(request(), response(content), 200, None)
        assert reply.error_category is None and reply.raw_content == content
        intent = DecisionIntent.model_validate(reply.proposal)
        assert intent.action == "select_conditions"
        OptionalSelectionParameters.model_validate(intent.parameters)
        original = json.loads(content + reply.format_normalization["appended_suffix"])
        original_choices = deepcopy(original["parameters"]["choices"])
        assert intent.parameters["choices"] == original_choices
        assert _parse_response(request("agent-json-v84"), response(content), 200, None).error_category
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in originals} == originals
