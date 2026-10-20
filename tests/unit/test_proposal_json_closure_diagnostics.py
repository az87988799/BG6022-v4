"""EOF hints describe immutable rejected data, never repair a model proposal."""

import hashlib
import json
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import pytest

from orca_agent.agent import _proposal_json_error
from orca_agent.llm import _parse_response, _strict_json, prepare_request

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live-t2-a006-json-eof.json"
CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["model_id"])
def test_real_a006_eof_hints_keep_original_rejected_and_reason_in_place(case):
    reply = deepcopy(case["reply"])
    original = deepcopy(reply)
    raw = reply["raw_content"]
    assert hashlib.sha256(raw.encode()).hexdigest() == case["raw_content_sha256"]

    detail = _proposal_json_error(reply)
    assert reply == original
    assert detail["missing_closing_containers"] == "}"
    assert detail["character"] == len(raw)
    assert detail["top_level_keys"] == ["action", "parameters"]
    assert detail["parameter_keys"] == ["request_version", "choices", "reason"]
    assert detail["syntax_completion_notice"] == (
        "Syntax completion is not schema approval; verify top-level required fields.")
    window = detail["rejected_data_window"]
    assert window["text"] == raw[window["start"]:window["end"]]
    assert len(window["text"]) <= 320
    with pytest.raises(json.JSONDecodeError):
        _strict_json(raw)

    # Re-enter the actual transport parser without a transport/network call.
    request = prepare_request([{"role": "user", "content": "Return JSON."}])
    response = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": raw}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 14, "completion_tokens": 8, "total_tokens": 22},
    }, ensure_ascii=False).encode()
    parsed = _parse_response(request, response, 200, None)
    assert parsed.proposal is None
    assert parsed.error_category == "invalid_proposal_json"
    assert parsed.raw_content == raw and parsed.usage.total_tokens == 22
    assert _proposal_json_error(asdict(parsed)) == detail


@pytest.mark.parametrize("raw,finish", [
    ('{"action":"stop","parameters":{"reason":"unfinished', "stop"),
    ('{"action":"stop","parameters":{"reason":', "stop"),
    ('{"action":"stop","parameters":{"x":1,', "stop"),
    ('{"action":"stop" "parameters":{}', "stop"),
    ('{"action":"stop","action":"stop","parameters":{}', "stop"),
    ('{"action":"stop","parameters":{"x":NaN}', "stop"),
    ('{"action":"stop","parameters":{"x":1e999}', "stop"),
    ('{"action":"stop","parameters":{}', "length"),
    ('{"action":"stop","parameters":{}', None),
    ('{"action":"stop","parameters":{', "stop"),
    ('{"action":"stop","parameters":{}}', "stop"),
])
def test_incomplete_values_ambiguous_syntax_and_truncation_have_no_closure_hint(raw, finish):
    detail = _proposal_json_error({"raw_content": raw, "finish_reason": finish})
    assert not isinstance(detail, dict) or "missing_closing_containers" not in detail


def test_closure_hint_ignores_string_brackets_and_only_lists_bounded_keys():
    value = {"action": "stop", "parameters": {
        "long_" + "x" * 80: 'quote " and literal brackets } ] { [',
        **{f"key_{index}": "value is not a key" for index in range(20)},
    }}
    raw = json.dumps(value)[:-2]
    detail = _proposal_json_error({"raw_content": raw, "finish_reason": "stop"})
    assert detail["missing_closing_containers"] == "}}"
    assert detail["top_level_keys"] == ["action", "parameters"]
    assert len(detail["parameter_keys"]) == 16
    assert max(map(len, detail["parameter_keys"])) == 64
    assert all("value is not a key" not in key for key in detail["parameter_keys"])
