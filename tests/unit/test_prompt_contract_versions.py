"""Prompt upgrades retain pre-send and post-reply guards; all transport is offline."""

import json
from dataclasses import replace

import pytest
from test_agent import ScriptedTransport as ProposalTransport
from test_agent_diagnostics import analysis_with_missing_evidence
from test_decision_purpose_capacity import terminal_run
from test_model_usage import ScriptedTransport

from orca_agent import agent, context
from orca_agent.decision_purpose import DECISION_CONTRACT_PROMPT_VERSIONS
from orca_agent.llm import prepare_request
from orca_agent.model_usage import current_basis, send_model
from orca_agent.store import StoreError


def test_current_prompt_version_cannot_skip_decision_contract_guards():
    assert context.PROMPT_VERSION in DECISION_CONTRACT_PROMPT_VERSIONS


@pytest.mark.parametrize("version", sorted(DECISION_CONTRACT_PROMPT_VERSIONS))
@pytest.mark.parametrize("missing", ["purpose", "contract", "sidecar"])
def test_current_prompt_rejects_missing_contract_before_reserving_or_sending(tmp_path, version, missing):
    store, run, request, snapshot = terminal_run(tmp_path)
    prepared = context.build_context(request, run, delivery_snapshot=snapshot)
    messages = prepared.body()["messages"]
    wire = json.loads(messages[-1]["content"])
    if missing == "purpose":
        del wire["AUTHORITY"]["decision_purpose"]
        expected = "explicit decision purpose"
    elif missing == "contract":
        del wire["AUTHORITY"]["contract_required"]
        expected = "delivery snapshot contract"
    else:
        expected = "exact prepared delivery snapshot"
    messages[-1]["content"] = json.dumps(wire, ensure_ascii=False, separators=(",", ":"))
    prepared = prepare_request(messages, prompt_version=version,
                               timeout_seconds=prepared.timeout_seconds)
    transport = ScriptedTransport()
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(StoreError, match=expected):
        send_model(store, run, prepared, transport, basis=current_basis(store, run),
                   logical_id="missing_contract", delivery_snapshot=None if missing == "sidecar" else snapshot)
    assert transport.sends == 0 and not run.model_records
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before


@pytest.mark.parametrize("version", sorted(DECISION_CONTRACT_PROMPT_VERSIONS))
def test_current_prompt_rejects_action_outside_sent_purpose_and_accepts_legal_correction(
    tmp_path, monkeypatch, version,
):
    store, run, plan, result = analysis_with_missing_evidence(tmp_path)
    build = agent.build_context

    def build_version(*args, **kwargs):
        return replace(build(*args, **kwargs), prompt_version=version)

    monkeypatch.setattr(agent, "build_context", build_version)
    transport = ProposalTransport(
        {"action": "call_tool", "parameters": {"system_id": "unplanned"}},
        {"action": "clarify", "parameters": {"questions": ["Provide the missing evidence?"],
                                               "unresolved": ["qualified energy sources"]}},
    )
    stopped, action, _ = agent._decision(store, run, plan, {result.step_id: result}, transport, None, None)
    diagnostic = transport.sent[1]["CONTROL"]["validation_error"]["requirement"]
    assert diagnostic["code"] == "decision_purpose_action"
    assert diagnostic["path"] == ["action"]
    assert diagnostic["allowed_actions"] == ["clarify", "revise_plan", "stop"]
    assert [item["action"] for item in stopped.decisions] == ["rejected", "clarify"]
    assert all(record["prompt_version"] == version for record in stopped.model_records)
    assert action == "stop" and stopped.state == "waiting_user"
    assert stopped.usage.model_calls == 2 and stopped.usage.model_tokens_used == 150
    assert len(stopped.calls) == stopped.usage.analysis_executions == 1
    assert stopped.usage.evidence_reads == stopped.usage.orca_starts_actual == 0
