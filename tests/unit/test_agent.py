"""Offline scripted proposals exercise the real durable loop, never a real model/ORCA."""

import copy

import pytest
from test_context import payload

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.llm import ModelReply, ModelUsage
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
    fingerprint,
)
from orca_agent.report import build_report, render_report
from orca_agent.store import Store


class ScriptedTransport:
    """Synthetic replies, with actual pre-send reservation and settlement callbacks."""

    def __init__(self, *scripts, after_reserve=None, terminal_contract=True):
        self.scripts = list(scripts)
        self.after_reserve = after_reserve
        self.terminal_contract = terminal_contract
        self.sent = []
        self.tickets = []

    def send(self, prepared, *, reserve, settle):
        ticket = reserve(prepared)
        data = payload(prepared)
        self.sent.append(data)
        self.tickets.append(ticket)
        if self.after_reserve:
            self.after_reserve(data)
        assert self.scripts, "loop attempted an unscripted model transmission"
        script = self.scripts.pop(0)
        values = script(data) if callable(script) else copy.deepcopy(script)
        proposal = {
            **data["AUTHORITY"]["basis"],
            "related_results": data["AUTHORITY"]["related_results"],
            "reason": "Synthetic offline protocol test; not scientific evidence.",
            "action": "stop", "parameters": {"reason": "scripted stop"},
            **values,
        }
        if data["AUTHORITY"].get("response_contract") in {"decision-intent-1", "decision-intent-2"}:
            proposal = {key: value for key, value in proposal.items()
                        if key in {"action", "parameters", "reason"} or key in values}
        if (self.terminal_contract and proposal["action"] == "stop"
                and data["AUTHORITY"].get("contract_required")
                and set(proposal["parameters"]) <= {"reason"}):
            # Migrate successful *synthetic* scripts to the advertised contract.
            # Adversarial tests opt out or provide an explicit invalid delivery;
            # no actual model response is ever changed or repaired here.
            snapshot = data["DATA"]["delivery"]
            proposal["parameters"]["delivery"] = {"version": snapshot["version"], "snapshot_ref": "current",
                "goal_explanations": [{"goal_ref": goal["ref"], "fact_refs": goal["required_fact_refs"],
                    "explanation_ref": goal["explanation_refs"][0],
                    "blocker_refs": goal["required_blocker_refs"], "next_action_ref": goal["next_action_refs"][0]}
                    for goal in snapshot["goals"]]}
        reply = ModelReply(request_hash=prepared.request_hash, proposal=proposal,
                           usage=ModelUsage(prompt_tokens=50, completion_tokens=25, total_tokens=75),
                           response_hash=fingerprint(proposal), response_model="offline-fake",
                           provider_request_id=f"offline-{len(self.sent)}", http_status=200,
                           finish_reason="stop")
        settle(ticket, reply)
        return reply


def make_run(tmp_path, *, two_goals=False, agent_enabled=True, initial_plan=False, model_calls=8):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    raw = tmp_path / "raw.json"
    raw.write_text('{"a": 1, "b": 2}', encoding="utf-8")
    artifact = store.import_artifact(raw, "synthetic_test_evidence")
    keys = ["a", "b"] if two_goals else ["a"]
    goals = [Goal(id=key, port="value_observation", minimum_check_version="evidence-read-1",
                  conditions={"query": {"artifact_id": artifact.id,
                                         "path": [{"kind": "key", "key": key}]}}) for key in keys]
    request = Request(original_text="Read the requested fields from the registered JSON.", goals=goals)
    permission = PermissionSnapshot(model_execution=agent_enabled, allowed_tools=["evidence.value"],
                                    artifact_ids=[artifact.id])
    budget = BudgetLimits(orca_starts=0, extra_orca_starts=0, model_calls=model_calls,
                          model_tokens=48000, input_tokens=12000, output_tokens=2000,
                          decision_rounds=12, corrections_per_proposal=1,
                          evidence_reads=24, plan_revisions=2)
    plan = None
    if initial_plan:
        steps = [Step(id=f"read_{key}", logical_id=f"read_{key}", tool="evidence.value",
                      parameters=goals[i].conditions["query"],
                      depends_on=[f"read_{keys[i - 1]}"] if i else []) for i, key in enumerate(keys)]
        plan = Plan(request_id=request.id, steps=steps,
                    goal_map={key: OutputBinding(step_id=f"read_{key}", port="value_observation")
                              for key in keys})
    run = store.create_run(request, plan, permission, budget)
    run.agent_enabled = agent_enabled
    store.save_run(run)
    return store, run, artifact


def initial_proposal(data):
    goals = data["AUTHORITY"]["request"]["goals"]
    return {"action": "initial_plan", "parameters": {
        "steps": [{"key": f"read_{goal['id']}", "tool": "evidence.value",
                   "parameters": goal["conditions"]["query"],
                   "depends_on": [f"read_{goals[i - 1]['id']}"] if i else []}
                  for i, goal in enumerate(goals)],
        "goal_map": {goal["id"]: {"step_key": f"read_{goal['id']}", "port": goal["port"]}
                     for goal in goals},
    }}


def test_model_initial_plan_query_and_program_goal_completion(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport(initial_proposal)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed"
    assert completed.goal_status == {"a": "satisfied"}
    assert completed.delivery_status == "complete"
    assert len(transport.sent) == completed.usage.model_calls == 1
    assert completed.usage.evidence_reads == 1
    assert not completed.attempts and completed.usage.orca_starts_reserved == 0
    assert completed.usage.model_tokens_used == 75 and completed.usage.model_tokens_unknown == 0
    assert completed.model_records[0]["status"] == "known"
    result = store.load_result(run.id, completed.result_ids[0])
    assert result.observations["value_observation"]["value"] == 1
    assert not result.qualified_outputs
    report = build_report(store, completed)
    assert report["user_goal_complete"] is True
    assert report["results"][0]["scientific_status"] == "no_currently_verified_scientific_output"


def test_structured_no_model_entry_uses_same_loop(tmp_path, monkeypatch):
    store, run, _ = make_run(tmp_path, agent_enabled=False, initial_plan=True)
    original = agent.execute
    observed = []

    def traced(*args, **kwargs):
        observed.append(args[2])
        return original(*args, **kwargs)

    monkeypatch.setattr(agent, "execute", traced)
    completed = runner.execute(store, Config(), run.id)
    assert observed == [run.id]
    assert completed.state == "completed"
    assert completed.usage.model_calls == 0 and completed.model_records == []
    assert completed.usage.evidence_reads == 1


def test_invalid_schema_corrected_once_then_accepted(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport({"action": "arbitrary_shell"}, initial_proposal)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed"
    assert completed.usage.model_calls == len(transport.sent) == 2
    assert len([d for d in completed.diagnostics if d["category"] == "proposal_rejected"]) == 1
    assert transport.sent[1]["CONTROL"]["validation_error"] is not None


def test_repeated_invalid_schema_stops_after_one_correction(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport({"action": "arbitrary_shell"}, {"action": "arbitrary_shell"})
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state in {"failed", "budget_exhausted"}
    assert len(transport.sent) == stopped.usage.model_calls == 2
    assert not stopped.calls and not stopped.attempts and stopped.plan_id is None
    assert len([d for d in stopped.diagnostics if d["category"] == "proposal_rejected"]) == 2


def test_new_user_message_while_model_waits_prevents_old_plan_activation(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport(initial_proposal,
        after_reserve=lambda _: store.enqueue_message(run.id, "Read a different field; wait for clarification"))
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "waiting_user"
    assert stopped.plan_id is None and not stopped.calls and not stopped.attempts
    assert stopped.usage.model_calls == 1
    assert any(d["category"] == "stale_model_response" for d in stopped.diagnostics)
    assert stopped.processed_messages == []


@pytest.mark.parametrize("signal, expected", [("pause", "paused"), ("cancel", "cancelled")])
def test_control_during_model_wait_prevents_stale_activation(tmp_path, signal, expected):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport(initial_proposal,
                                   after_reserve=lambda _: store.signal(run.id, signal))
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == expected
    assert stopped.plan_id is None and not stopped.calls


def test_each_new_result_is_assessed_before_next_step(tmp_path):
    store, run, _ = make_run(tmp_path, two_goals=True)
    observations = []

    def assess_then_continue(data):
        current = store.load_run(run.id)
        assert len(current.calls) == 1 and current.usage.evidence_reads == 1
        assert data["AUTHORITY"]["goal_status"] == {"a": "satisfied", "b": "insufficient_evidence"}
        feedback = data["AUTHORITY"]["related_results"]
        assert feedback == current.result_ids
        assert data["DATA"]["results"][0]["unqualified_observations"]["value_observation"]["value"] == 1
        observations.extend(feedback)
        plan = data["AUTHORITY"]["plan"]
        next_id = plan["goal_map"]["b"]["step_id"]
        return {"action": "call_tool", "parameters": {"step_id": next_id}}

    transport = ScriptedTransport(initial_proposal, assess_then_continue)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed"
    assert completed.goal_status == {"a": "satisfied", "b": "satisfied"}
    assert len(transport.sent) == completed.usage.model_calls == 2
    assert len(completed.calls) == completed.usage.evidence_reads == 2
    assert completed.processed_feedback == observations


def test_exhausted_model_budget_sends_nothing_and_deterministic_report_still_works(tmp_path):
    store, run, _ = make_run(tmp_path, model_calls=0)
    transport = ScriptedTransport()
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "budget_exhausted"
    assert not transport.sent and stopped.usage.model_calls == 0
    before = store.load_run(run.id).model_dump()
    report = build_report(store, run.id)
    assert report["run_state"] == "budget_exhausted" and not report["user_goal_complete"]
    assert report["budget"]["usage"]["model_calls"] == 0
    assert isinstance(render_report(report), str)
    assert store.load_run(run.id).model_dump() == before


@pytest.mark.parametrize("window", ["after_model_response_saved", "after_revision_saved",
                                     "after_revision_activated", "after_result_saved"])
def test_crash_recovery_twice_reuses_model_plan_and_query_evidence(tmp_path, window):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport(initial_proposal)

    def crash(point):
        if point == window:
            raise KeyboardInterrupt

    interrupted = agent.execute(store, Config(), run.id, transport=transport, fault=crash)
    assert interrupted.state == "unknown"
    for _ in range(2):
        completed = agent.execute(store, Config(), run.id, transport=transport, resume=True)
        assert completed.state == "completed"
        assert completed.goal_status == {"a": "satisfied"}
        assert completed.usage.model_calls == 1
        assert completed.usage.evidence_reads == 1
        assert len(completed.calls) == len(completed.result_ids) == 1
        assert completed.usage.model_tokens_used == 75
        assert completed.usage.model_tokens_unknown == 0
    assert len(transport.sent) == 1


def test_unknown_query_reservation_is_not_reissued_on_repeated_resume(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport(initial_proposal)

    def crash(point):
        if point == "after_call_reserved":
            raise KeyboardInterrupt

    interrupted = agent.execute(store, Config(), run.id, transport=transport, fault=crash)
    assert interrupted.state == "unknown"
    for _ in range(2):
        stopped = agent.execute(store, Config(), run.id, transport=transport, resume=True)
        assert stopped.state == "unknown"
        assert stopped.usage.evidence_reads == len(stopped.calls) == 1
        assert stopped.usage.model_calls == 1
    assert len(transport.sent) == 1


def test_recovered_immediate_query_binds_goal_without_another_model_request(tmp_path):
    store, run, _ = make_run(tmp_path)

    def query(data):
        goal = data["AUTHORITY"]["request"]["goals"][0]
        return {"action": "call_tool", "parameters": {
            "tool": "evidence.value", "parameters": goal["conditions"]["query"],
        }}

    transport = ScriptedTransport(query)

    def crash(point):
        if point == "after_result_saved":
            raise KeyboardInterrupt

    interrupted = agent.execute(store, Config(), run.id, transport=transport, fault=crash)
    assert interrupted.state == "unknown"
    for _ in range(2):
        completed = agent.execute(store, Config(), run.id, transport=transport, resume=True)
        assert completed.state == "completed"
        assert completed.usage.model_calls == 1 and completed.usage.evidence_reads == 1
        assert completed.plan_id is None and not completed.attempts
        assert completed.goal_status == {"a": "satisfied"}


def test_invalid_immediate_query_parameters_correct_before_any_tool_reservation(tmp_path):
    store, run, _ = make_run(tmp_path)

    def corrected_query(data):
        assert not store.load_run(run.id).calls
        return {"action": "call_tool", "parameters": {
            "tool": "evidence.value",
            "parameters": data["AUTHORITY"]["request"]["goals"][0]["conditions"]["query"],
        }}

    invalid = {"action": "call_tool", "parameters": {
        "tool": "evidence.value", "parameters": {"artifact_id": "../arbitrary-path"},
    }}
    transport = ScriptedTransport(invalid, corrected_query)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed"
    assert completed.usage.model_calls == len(transport.sent) == 2
    assert completed.usage.evidence_reads == len(completed.calls) == 1
    assert len([d for d in completed.decisions if d.get("action") == "rejected"]) == 1


def test_related_result_correction_returns_exact_expected_ids_without_granting_external_evidence(tmp_path):
    store, run, _ = make_run(tmp_path)

    def extra_source(data):
        return {**initial_proposal(data), "related_results": ["external_source_result"]}

    def corrected(data):
        detail = data["CONTROL"]["validation_error"]["requirement"]
        if data["AUTHORITY"].get("response_contract") in {"decision-intent-1", "decision-intent-2"}:
            assert any(item["type"] == "extra_forbidden" and item["loc"] == ["related_results"]
                       for item in detail)
        else:
            assert detail["path"] == ["related_results"] and detail["expected"] == []
            assert "Step.inputs" in detail["requirement"]
        assert data["AUTHORITY"]["related_results"] == []
        assert not store.load_run(run.id).calls
        return initial_proposal(data)

    transport = ScriptedTransport(extra_source, corrected)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed" and completed.usage.model_calls == 2
    assert completed.usage.evidence_reads == 1 and not completed.attempts


def test_unknown_http_reservation_stays_occupied_without_automatic_correction(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport(initial_proposal)

    def crash(point):
        if point == "after_model_reserved":
            raise KeyboardInterrupt

    interrupted = agent.execute(store, Config(), run.id, transport=transport, fault=crash)
    assert interrupted.state == "unknown"
    assert interrupted.usage.model_calls == 1 and interrupted.usage.model_tokens_unknown > 0
    occupancy = interrupted.usage.model_tokens_unknown
    for _ in range(2):
        unknown = agent.execute(store, Config(), run.id, transport=transport, resume=True)
        assert unknown.state == "unknown"
        assert unknown.usage.model_calls == 1
        assert unknown.usage.model_tokens_unknown == occupancy
        assert not unknown.calls and unknown.plan_id is None
    assert not transport.sent


def test_model_claim_of_success_never_completes_an_unevidenced_goal(tmp_path):
    store, run, _ = make_run(tmp_path)
    transport = ScriptedTransport({"action": "stop", "parameters": {
        "reason": "All scientific checks passed and the user goal is complete.",
    }})
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "failed"
    assert stopped.goal_status == {"a": "insufficient_evidence"}
    assert not build_report(store, stopped)["user_goal_complete"]
    assert not stopped.result_ids and not stopped.calls
