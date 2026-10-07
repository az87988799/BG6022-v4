"""Bounded optional model configuration; all replies and transmissions are offline."""

import hashlib
import json
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from pydantic import ValidationError
from test_agent import ScriptedTransport as AgentTransport
from test_agent import initial_proposal, make_run
from test_context import objects, payload
from test_llm import completion
from test_model_usage import ScriptedTransport, invoke
from test_model_usage import setup as setup

from orca_agent import agent
from orca_agent.config import Config, load_config
from orca_agent.context import build_context
from orca_agent.llm import DeepSeekTransport, _canonical, _parse_response, prepare_request
from orca_agent.model_usage import read_model_reply, recover_models, validate_model_profile
from orca_agent.store import StoreError


@pytest.mark.parametrize("profile", ["disabled", "thinking_low"])
def test_explicit_program_config_roundtrips_without_environment_default(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("MODEL_PROFILE", "thinking_low")
    assert Config().model_profile == "disabled"
    config = tmp_path / "config.toml"
    config.write_text(f'model_profile = "{profile}"\n', encoding="utf-8")
    assert load_config(config).model_profile == profile


@pytest.mark.parametrize("value", [None, True, 1, "enabled", "high", "", {"thinking": "enabled"}])
def test_unknown_profile_is_rejected_before_preparing_any_request(value):
    with pytest.raises(ValidationError):
        Config(model_profile=value)
    with pytest.raises(ValueError, match="model_profile"):
        prepare_request([{"role": "user", "content": "Return JSON"}], model_profile=value)


def test_disabled_body_is_byte_identical_and_low_changes_only_explicit_mode():
    messages = [{"role": "user", "content": "Return JSON"}]
    historical = {"model": "deepseek-flash", "messages": messages, "stream": False,
                  "temperature": 0, "max_tokens": 2000,
                  "response_format": {"type": "json_object"}, "thinking": {"type": "disabled"}}
    disabled = prepare_request(messages)
    assert disabled.canonical_body == _canonical(historical)
    assert disabled.request_hash == hashlib.sha256(_canonical(historical).encode()).hexdigest()
    low = prepare_request(messages, model_profile="thinking_low")
    assert low.body() == {**historical, "thinking": {"type": "enabled"}, "reasoning_effort": "low"}
    assert low.input_token_bound == disabled.input_token_bound
    assert low.output_token_bound == disabled.output_token_bound == 2000
    assert low.request_hash != disabled.request_hash
    assert low.model_profile == "thinking_low" and disabled.model_profile == "disabled"


@pytest.mark.parametrize("change", [
    {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
    {"reasoning_effort": "low"}, {"thinking": {"type": "disabled", "budget_tokens": 2000}},
    {"temperature": 1}, {"extra_body": {"thinking": {"type": "enabled"}}},
])
def test_transport_revalidates_mode_and_every_frozen_parameter_before_reservation(change):
    original = prepare_request([{"role": "user", "content": "Return JSON"}])
    body = {**original.body(), **change}
    changed = _canonical(body)
    request = replace(original, canonical_body=changed, request_hash=hashlib.sha256(changed.encode()).hexdigest())
    def forbidden(*args, **kwargs):
        pytest.fail("invalid configuration must not construct client, reserve or settle")
    with pytest.raises(ValueError):
        DeepSeekTransport(client_factory=forbidden).send(request, reserve=forbidden, settle=forbidden)


@pytest.mark.parametrize(("content", "finish", "error"), [
    ('{"action":"stop"}', "stop", None),
    ("", "stop", "empty_content"), (None, "stop", "empty_content"),
    ("{", "length", "truncated"), ("not JSON", "stop", "invalid_proposal_json"),
])
def test_reasoning_never_becomes_final_content_or_a_receipt(content, finish, error):
    prepared = prepare_request([{"role": "user", "content": "Return JSON"}], model_profile="thinking_low")
    raw = completion(choices=[{"finish_reason": finish, "message": {
        "role": "assistant", "content": content, "reasoning_content": "PRIVATE_REASONING_SENTINEL",
    }}])
    reply = _parse_response(prepared, raw, 200, None)
    assert reply.error_category == error
    assert reply.raw_content == content
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(asdict(reply))
    assert "reasoning_content" not in asdict(reply)
    assert reply.proposal == ({"action": "stop"} if error is None else None)
    disabled = prepare_request(prepared.body()["messages"])
    if finish == "stop":
        assert _parse_response(disabled, raw, 200, None).error_category == "unexpected_thinking"


def low_setup(setup):
    store, run, prepared = setup
    low = prepare_request(prepared.body()["messages"], model_profile="thinking_low")
    return store, run, low


@pytest.mark.parametrize("usage_case", ["known", "unknown", "excess", "truncated"])
def test_reasoning_completion_is_charged_once_and_crash_recovery_preserves_evidence(setup, usage_case):
    setup = low_setup(setup)
    store, run, prepared = setup
    used = 2001 if usage_case == "excess" else 1900
    usage = False if usage_case == "unknown" else {
        "prompt_tokens": 14, "completion_tokens": used, "total_tokens": used + 14,
        "completion_tokens_details": {"reasoning_tokens": 1700},
    }
    response = completion(usage=usage, choices=[{
        "finish_reason": "length" if usage_case == "truncated" else "stop",
        "message": {"role": "assistant", "reasoning_content": "PRIVATE_REASONING_SENTINEL",
                    "content": "{" if usage_case == "truncated" else '{"action":"stop"}'},
    }])
    reply = _parse_response(prepared, response, 200, None)
    transport = ScriptedTransport(reply)
    def crash(stage):
        if stage == "after_model_response_saved":
            raise OSError("offline crash after saved receipt")
    with pytest.raises(OSError, match="offline crash"):
        invoke(setup, transport, fault=crash)
    restarted = store.load_run(run.id)
    assert restarted.model_records[0]["status"] == "reserved"
    assert restarted.usage.model_tokens_unknown == prepared.reserved_tokens
    assert recover_models(store, restarted)[restarted.model_records[0]["id"]] == reply
    record = restarted.model_records[0]
    assert record["model_profile"] == "thinking_low"
    assert transport.sends == restarted.usage.model_calls == 1
    if usage_case == "unknown":
        assert record["status"] == "unknown"
        assert restarted.usage.model_tokens_unknown == prepared.reserved_tokens
    else:
        assert record["output_tokens"] == used
        assert record["total_tokens"] == used + 14
        assert Decimal(record["cost_known_usd"]) == (Decimal(14) * Decimal("0.30")
                                                       + Decimal(used) * Decimal("1.20")) / 1000000
        assert restarted.usage.model_tokens_unknown == 0
    files = {p: p.read_bytes() for p in store.path(f"runs/{run.id}").rglob("*.json")}
    assert all(b"PRIVATE_REASONING_SENTINEL" not in raw for raw in files.values())
    assert all(b'"reasoning_content"' not in raw for raw in files.values())
    assert recover_models(store, restarted)[record["id"]] == reply
    assert {p: p.read_bytes() for p in files} == files
    if usage_case == "excess":
        with pytest.raises(StoreError, match="token bound violated"):
            invoke((store, restarted, prepared), ScriptedTransport(), logical_id="decision_2")


@pytest.mark.parametrize(("first", "other"), [("disabled", "thinking_low"), ("thinking_low", "disabled")])
def test_zero_http_crash_freezes_profile_before_first_reservation(setup, first, other):
    store, run, original = setup
    prepared = prepare_request(original.body()["messages"], model_profile=first)
    transport = ScriptedTransport()
    def crash(stage):
        if stage == "after_model_profile_frozen":
            raise OSError("offline crash before reservation")
    with pytest.raises(OSError, match="before reservation"):
        invoke((store, run, prepared), transport, fault=crash)
    run = store.load_run(run.id)
    assert transport.sends == run.usage.model_calls == 0 and run.model_records == []
    snapshot = store.path(f"runs/{run.id}/model-profile.json")
    before = snapshot.read_bytes()
    assert validate_model_profile(store, run) == first
    with pytest.raises(StoreError, match="profile is frozen"):
        agent.execute(store, Config(model_profile=other), run.id, resume=True, transport=transport)
    changed = prepare_request(original.body()["messages"], model_profile=other)
    with pytest.raises(StoreError, match="profile is frozen"):
        invoke((store, run, changed), transport)
    assert snapshot.read_bytes() == before and transport.sends == 0
    invoke((store, run, prepared), transport)
    assert transport.sends == 1


def test_legacy_disabled_is_derived_from_hashed_request_without_rewriting(setup):
    store, run, prepared = setup
    invoke(setup, ScriptedTransport())
    # Construct an offline historical fixture, as written before profile support.
    record = run.model_records[0]
    record.pop("model_profile")
    store.path(f"runs/{run.id}/model-profile.json").unlink()
    store._write_json(f"runs/{run.id}/run.json", run)
    before = {p: p.read_bytes() for p in store.path(f"runs/{run.id}").rglob("*.json")}
    assert validate_model_profile(store, run) == "disabled"
    assert read_model_reply(store, run, record)[0].proposal == {"action": "stop"}
    assert recover_models(store, run)[record["id"]].proposal == {"action": "stop"}
    with pytest.raises(StoreError, match="profile is frozen"):
        validate_model_profile(store, run, "thinking_low")
    assert {p: p.read_bytes() for p in before} == before
    assert not store.path(f"runs/{run.id}/model-profile.json").exists()
    path = store.path(f"runs/{run.id}/model/{record['id']}.request.json")
    body = json.loads(path.read_text(encoding="utf-8"))
    body["thinking"] = {"type": "enabled"}
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(StoreError, match="frozen model request changed"):
        recover_models(store, run)


@pytest.mark.parametrize("damage", ["missing_snapshot", "changed_snapshot", "changed_record"])
def test_new_profile_binding_tampering_blocks_read_recovery_and_new_send(setup, damage):
    setup = low_setup(setup)
    store, run, prepared = setup
    invoke(setup, ScriptedTransport())
    snapshot = store.path(f"runs/{run.id}/model-profile.json")
    if damage == "missing_snapshot":
        snapshot.unlink()
    elif damage == "changed_snapshot":
        data = json.loads(snapshot.read_text(encoding="utf-8"))
        data["model_profile"] = "disabled"
        snapshot.write_text(json.dumps(data), encoding="utf-8")
    else:
        run.model_records[0]["model_profile"] = "disabled"
        store._write_json(f"runs/{run.id}/run.json", run)
    for operation in [lambda: read_model_reply(store, run, run.model_records[0]),
                      lambda: recover_models(store, run),
                      lambda: invoke(setup, ScriptedTransport(), logical_id="decision_2")]:
        with pytest.raises(StoreError, match="profile"):
            operation()
    assert store.load_run(run.id).usage.model_calls == 1


def test_agent_uses_explicit_profile_and_resume_cannot_change_it(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = AgentTransport(initial_proposal)
    completed = agent.execute(store, Config(model_profile="thinking_low"), run.id, transport=transport)
    assert completed.state == "completed"
    record = completed.model_records[0]
    assert record["model_profile"] == "thinking_low"
    request = json.loads(store.path(f"runs/{run.id}/model/{record['id']}.request.json").read_text(encoding="utf-8"))
    assert request["thinking"] == {"type": "enabled"} and request["reasoning_effort"] == "low"
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(StoreError, match="profile is frozen"):
        agent.execute(store, Config(), run.id, resume=True, transport=AgentTransport())
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    resumed = agent.execute(store, Config(model_profile="thinking_low"), run.id,
                            resume=True, transport=AgentTransport())
    assert resumed.state == "completed" and resumed.usage.model_calls == 1


def test_private_reasoning_is_not_replayed_and_final_extra_field_still_needs_correction(tmp_path):
    store, run, _ = make_run(tmp_path)
    class ParsedTransport:
        def __init__(self):
            self.requests = []

        def send(self, prepared, *, reserve, settle):
            ticket = reserve(prepared)
            self.requests.append(prepared.body())
            data = payload(prepared)
            proposal = {**data["AUTHORITY"]["basis"],
                        "related_results": data["AUTHORITY"]["related_results"],
                        "reason": "Offline fixture requests only a registered read.",
                        **initial_proposal(data)}
            if len(self.requests) == 1:
                proposal["type"] = "json_object"
            reply = _parse_response(prepared, completion(choices=[{
                "finish_reason": "stop", "message": {
                    "role": "assistant", "content": json.dumps(proposal),
                    "reasoning_content": "PRIVATE_REASONING_SENTINEL",
                },
            }]), 200, None)
            settle(ticket, reply)
            return reply
    transport = ParsedTransport()
    completed = agent.execute(store, Config(model_profile="thinking_low"), run.id, transport=transport)
    assert completed.state == "completed" and len(transport.requests) == 2
    rejected = [item for item in completed.diagnostics if item["category"] == "proposal_rejected"]
    assert len(rejected) == 1 and rejected[0]["error_category"] == "ValidationError"
    rejection = next(item for item in completed.decisions if item.get("action") == "rejected")
    assert rejection["parameters"]["requirement"] == [{"type": "extra_forbidden", "loc": ["type"]}]
    assert completed.plan_id is not None
    for body in transport.requests:
        assert body["reasoning_effort"] == "low"
        assert "reasoning_content" not in json.dumps(body)
        assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(body)
    saved = [p.read_bytes() for p in store.path(f"runs/{run.id}").rglob("*.json")]
    assert all(b"PRIVATE_REASONING_SENTINEL" not in raw for raw in saved)
    assert completed.usage.orca_starts_actual == 0


def test_profile_mismatch_does_not_block_settling_saved_http_before_refusing_resume(setup):
    setup = low_setup(setup)
    store, run, prepared = setup
    def crash(stage):
        if stage == "after_model_response_saved":
            raise OSError("offline crash before settlement")
    transport = ScriptedTransport()
    with pytest.raises(OSError, match="before settlement"):
        invoke(setup, transport, fault=crash)
    run = store.load_run(run.id)
    assert run.model_records[0]["status"] == "reserved"
    store.signal(run.id, "pause")
    before_control = store.read_control(run.id)
    generation = run.control_generation
    before_inputs = {p: p.read_bytes() for p in store.path(f"runs/{run.id}/model").glob("*.json")}
    with pytest.raises(StoreError, match="profile is frozen"):
        agent.execute(store, Config(), run.id, resume=True, transport=transport)
    recovered = store.load_run(run.id)
    assert recovered.model_records[0]["status"] == "known"
    assert recovered.usage.model_tokens_used == 15 and recovered.usage.model_tokens_unknown == 0
    assert transport.sends == recovered.usage.model_calls == 1
    assert not recovered.calls and not recovered.decisions and not recovered.attempts
    assert recovered.control_generation == generation
    assert store.read_control(run.id) == before_control
    assert {p: p.read_bytes() for p in before_inputs} == before_inputs


def test_profile_does_not_change_context_facts_or_input_reservation():
    request, run = objects(scientific=False)
    now = run.created_at
    disabled = build_context(request, run, now=now)
    low = build_context(request, run, now=now, model_profile="thinking_low")
    assert low.body()["messages"] == disabled.body()["messages"]
    assert payload(low) == payload(disabled)
    assert low.input_token_bound == disabled.input_token_bound <= 12000
    assert low.output_token_bound == 2000
