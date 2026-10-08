"""Expensive effects cannot consume the final delivery reserve."""

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.context import ContextLimitError
from orca_agent.models import Request
from tests.unit.test_agent import ScriptedTransport, make_run
from tests.unit.test_natural import scientific_run


def test_capacity_failure_precedes_any_cost_or_tool_reservation(tmp_path, monkeypatch):
    store, original, _ = make_run(tmp_path)
    request = Request(original_text="Read and explain the requested field.",
        goals=store.load_request(original).goals, conditions={"explain_results": True})
    run = store.create_run(request, None, original.permission, original.budget)
    run.agent_enabled = True
    store.save_run(run)
    seen = []

    def no_capacity(request, run, snapshot, **kwargs):
        seen.append(snapshot)
        raise ContextLimitError("synthetic terminal capacity overflow")

    monkeypatch.setattr("orca_agent.context.assess_terminal_capacity", no_capacity)
    before = run.model_dump(mode="json")
    with pytest.raises(ContextLimitError, match="capacity overflow"):
        agent._delivery_preflight(store, run, "analysis.finite_sampling", {}, None, {},
                                  model_profile="disabled")
    assert seen[0]["basis"]["request_id"] == request.id
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert not run.calls and not run.attempts and not run.model_records


def test_step_path_checks_delivery_before_scientific_reservation(tmp_path, monkeypatch):
    store, run, _, _ = scientific_run(tmp_path, monkeypatch)
    seen = []

    def stop_before_effect(*args, **kwargs):
        seen.append(args[2])
        raise ContextLimitError("synthetic delivery capacity failure")

    monkeypatch.setattr(agent, "_delivery_preflight", stop_before_effect)
    monkeypatch.setattr(agent, "_science", lambda *a, **k: pytest.fail("science started"))
    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert seen == ["orca.sp"] and ended.state == "failed"
    assert not ended.attempts and not ended.calls and not ended.model_records
    assert ended.usage.orca_starts_reserved == 0 and not store.environment_lease()


def test_direct_query_path_checks_delivery_before_dispatch(tmp_path, monkeypatch):
    store, run, artifact = make_run(tmp_path)
    from orca_agent.model_usage import current_basis
    run.decisions.append({"id": "decision_saved", "action": "call_tool", "basis": current_basis(store, run),
        "parameters": {"tool": "evidence.value", "parameters": {"artifact_id": artifact.id,
            "path": [{"kind": "key", "key": "a"}]}}})
    store.save_run(run)
    seen = []

    def stop_before_effect(*args, **kwargs):
        seen.append(args[2])
        raise ContextLimitError("synthetic pre-dispatch failure")

    monkeypatch.setattr(agent, "_delivery_preflight", stop_before_effect)
    monkeypatch.setattr(agent, "execute_call", lambda *a, **k: pytest.fail("query dispatched"))
    ended = agent.execute(store, Config(), run.id, transport=ScriptedTransport())
    assert seen == ["evidence.value"] and ended.state == "failed"
    assert not ended.calls and not ended.model_records and ended.usage.evidence_reads == 0


def test_reference_pause_hook_is_after_geometry_validation_before_any_reservation(tmp_path, monkeypatch):
    store, run, plan, _ = scientific_run(tmp_path, monkeypatch)
    monkeypatch.setattr("orca_agent.natural.ensure_scientific_environment", lambda *a: None)
    monkeypatch.setattr("orca_agent.runner._validate_execution_rules", lambda *a: None)
    stages = []

    class ReferenceNeeded(BaseException):
        pass

    def pause(stage):
        stages.append(stage)
        raise ReferenceNeeded()

    before = store.load_run(run.id).model_dump(mode="json")
    with pytest.raises(ReferenceNeeded):
        agent._science(store, Config(), run, plan.steps[0], {}, None, pause)
    assert stages == ["before_science_reservation"]
    assert store.load_run(run.id).model_dump(mode="json") == before
    assert not run.attempts and not run.calls and not store.environment_lease()
