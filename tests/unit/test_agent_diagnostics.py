"""Actual offline proposal rejection/settlement, with no HTTP or science."""

import pytest
from test_agent import ScriptedTransport, make_run

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.models import OutputBinding, Plan, Step


def rejection(transport, index=1):
    error = transport.sent[index]["CONTROL"]["validation_error"]
    assert error["category"] == "ProposalError"
    detail = error["requirement"]
    assert detail["path"] == ["parameters", "step_id"]
    assert "cannot be executed again" in detail["requirement"]
    assert "dependencies are not ready" in detail["requirement"]
    return detail


def test_completed_step_rejection_lists_ready_id_and_does_not_repeat_read(tmp_path):
    store, run, _ = make_run(tmp_path, two_goals=True, initial_plan=True)
    transport = ScriptedTransport(
        {"action": "call_tool", "parameters": {"step_id": "read_a"}},
        {"action": "call_tool", "parameters": {"step_id": "read_b"}},
    )
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed"
    assert rejection(transport)["expected_ready_step_ids"] == ["read_b"]
    assert [call.step_id for call in completed.calls] == ["read_a", "read_b"]
    assert completed.usage.evidence_reads == 2
    assert completed.usage.model_calls == 2 and completed.usage.model_tokens_used == 150


def test_unmet_dependency_rejection_exposes_every_current_ready_step(tmp_path):
    store, original, artifact = make_run(tmp_path, two_goals=True)
    request = store.load_request(original)
    parameters = {"artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}
    plan = Plan(request_id=request.id, steps=[
        Step(id="ready_a", logical_id="a", tool="evidence.value", parameters=parameters),
        Step(id="ready_c", logical_id="c", tool="evidence.value", parameters=parameters),
        Step(id="blocked_b", logical_id="b", tool="evidence.value", parameters=parameters,
             depends_on=["ready_a"]),
    ], goal_map={"a": OutputBinding(step_id="ready_a", port="value_observation"),
                 "b": OutputBinding(step_id="blocked_b", port="value_observation")})
    run = store.create_run(request, plan, original.permission, original.budget)
    transport = ScriptedTransport(
        {"action": "call_tool", "parameters": {"step_id": "blocked_b"}},
        {"action": "call_tool", "parameters": {"step_id": "ready_c"}},
    )
    updated, action, value = agent._decision(store, run, plan, {}, transport, None, None)
    assert action == "step" and value[0].id == "ready_c"
    assert rejection(transport)["expected_ready_step_ids"] == ["ready_a", "ready_c"]
    assert not updated.calls and not updated.attempts
    assert updated.usage.model_calls == 2 and updated.usage.evidence_reads == 0


def test_unknown_step_without_plan_returns_empty_ready_set_without_echoing_input(tmp_path):
    store, run, _ = make_run(tmp_path)
    untrusted = "untrusted-secret-shaped-value-that-must-not-be-echoed"
    transport = ScriptedTransport(
        {"action": "call_tool", "parameters": {"step_id": untrusted}},
        {"action": "stop", "parameters": {}},
    )
    stopped, action, _ = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "stop" and stopped.state == "failed"
    detail = rejection(transport)
    assert detail["expected_ready_step_ids"] == []
    assert untrusted not in str(detail)
    assert not stopped.calls and not stopped.attempts


def test_repeated_completed_id_exhausts_correction_without_additional_tool_execution(tmp_path):
    store, run, _ = make_run(tmp_path, two_goals=True, initial_plan=True)
    bad = {"action": "call_tool", "parameters": {"step_id": "read_a"}}
    transport = ScriptedTransport(bad, bad)
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "failed"
    assert rejection(transport)["expected_ready_step_ids"] == ["read_b"]
    assert [call.step_id for call in stopped.calls] == ["read_a"]
    assert stopped.usage.evidence_reads == 1 and stopped.usage.model_calls == 2
    assert stopped.usage.model_tokens_used == 150
    rejected = [item for item in stopped.decisions if item.get("action") == "rejected"]
    assert len(rejected) == 2
    assert all(item["parameters"]["requirement"]["expected_ready_step_ids"] == ["read_b"]
               for item in rejected)


@pytest.mark.parametrize("field,bad", [
    ("unresolved", ["untrusted-input"] * 6),
    ("questions", ["untrusted-input"] * 6),
    ("questions", []),
    ("unresolved", [""]),
    ("questions", ["untrusted-input" * 100]),
])
def test_clarification_rejection_exposes_exact_bounds_and_accepts_one_correction(tmp_path, field, bad):
    store, run, _ = make_run(tmp_path)
    values = {"questions": ["Which geometry?"], "unresolved": ["geometry"]}
    invalid = {**values, field: bad}
    transport = ScriptedTransport(
        {"action": "clarify", "parameters": invalid},
        {"action": "clarify", "parameters": values},
    )
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    error = transport.sent[1]["CONTROL"]["validation_error"]
    assert error["category"] == "ProposalError"
    detail = error["requirement"]
    assert detail["path"] == ["parameters"]
    assert detail["required_fields"] == ["questions", "unresolved"]
    assert (detail["min_items"], detail["max_items"]) == (1, 5)
    assert (detail["min_string_length"], detail["max_string_length"]) == (1, 1000)
    assert "untrusted-input" not in str(detail)
    assert stopped.state == "waiting_user"
    assert [item["action"] for item in stopped.decisions] == ["rejected", "clarify"]
    assert stopped.usage.model_calls == 2 and stopped.usage.model_tokens_used == 150
    assert not stopped.calls and not stopped.attempts
