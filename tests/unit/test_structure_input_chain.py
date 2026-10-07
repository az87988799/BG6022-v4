"""Synthetic full input chain, with the production Store, plans and consumption gates."""

from pathlib import Path

import pytest
from test_semantic_control import candidate
from test_structure_tools import mock_generator, response

from orca_agent.config import Config, TextProfile
from orca_agent.input_bindings import prepared_geometry
from orca_agent.model_usage import current_basis
from orca_agent.models import EvidenceRef, InputRef, OutputBinding, Plan, Step
from orca_agent.natural import agent_budget, apply_user_update, initialize_text
from orca_agent.semantic import commit_candidate
from orca_agent.store import EnvironmentBusy, Store
from orca_agent.tools import structure
from orca_agent.tools.dispatch import execute_call


def intake(tmp_path, name="water", text=None, memory_mb=1024):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    config = Config(text=TextProfile(enabled=True, permission={
        "model_execution": True, "scientific_execution": True, "artifact_writes": True,
        "external_identity_queries": True, "geometry_preparation": True, "max_memory_mb": memory_mb,
        "allowed_tools": ["structure.resolve", "structure.prepare", "orca.sp"]},
        budget=agent_budget(identity_queries=2, structure_preparations=2)))
    text = text or f"Calculate the initial geometry single-point electronic energy of {name}."
    run = initialize_text(store, config, text)
    original = store.load_request(run)
    run = commit_candidate(store, run, candidate(store, run, kind="normalize",
        goals=[{"key": "energy", "port": "energy", "system_refs": [name], "text_basis": text,
                "geometry_relation": "fixed_initial"}],
        conditions={k: {"value": v, "source": "default", "default_rule": "local-hf-1"}
                    for k, v in config.text.defaults.items()}),
        decision_id="normalize", basis=current_basis(store, run))
    assert original.normalization_status == "pending" and original.geometry_artifact_id is None
    assert not store.load_request(run).goals[0].unresolved
    return store, run


def plan_for(store, run, *, prior=None, include_prepare=True, include_science=True):
    request = store.load_request(run)
    system = request.systems[0].id
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id=system,
                   parameters={"system_id": system})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id=system,
        parameters={"system_id": system, "charge": 0, "multiplicity": 1}, depends_on=["resolve"],
        inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity")})
    science = Step(id="science", logical_id="science", tool="orca.sp", system_id=system,
        geometry=InputRef(producer_step_id="prepare", port="prepared_geometry"), depends_on=["prepare"])
    return Plan(**({"id": prior.id, "version": prior.version + 1} if prior else {}),
        request_id=request.id, request_version=request.version,
        steps=[resolve] + ([prepare] if include_prepare else []) + ([science] if include_science else []),
        goal_map={request.goals[0].id: OutputBinding(port="energy", **(
            {"step_id": "science"} if include_science else {"gap": "input acquisition pending"}))})


def activate(store, run, plan, key="plan"):
    return store.commit_revision(run, plan, decision_id=key, basis=current_basis(store, run))


def resolved_chain(store, run, monkeypatch):
    plan = plan_for(store, run)
    run = activate(store, run, plan)
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (200, response(), True))
    calls = mock_generator(monkeypatch)
    identity = execute_call(store, run, "structure.resolve", {"system_id": "water"}, step=plan.steps[0])
    return run, plan, identity, calls


def prepare_call(store, run, plan, identity, fault=None):
    step = plan.steps[1]
    return execute_call(store, run, step.tool, step.parameters.model_dump(), step=step,
                        results={"resolve": identity}, fault=fault)


def test_text_identity_preparation_and_scientific_reservation_share_existing_chain(tmp_path, monkeypatch):
    store, run = intake(tmp_path)
    request_bytes = store.path(f"runs/{run.id}/request-revisions/1.json").read_bytes()
    permission = run.permission.model_dump()
    run, plan, identity, generated = resolved_chain(store, run, monkeypatch)
    result = prepare_call(store, run, plan, identity)
    artifact = prepared_geometry(store, run, store.load_request(run), "water")
    assert result.qualified_outputs["prepared_geometry"].artifact_id == artifact
    assert store.load_request(run).geometry_artifact_id is None
    assert store.path(f"runs/{run.id}/request-revisions/1.json").read_bytes() == request_bytes
    assert run.permission.model_dump() == permission and artifact not in run.permission.artifact_ids
    assert run.usage.identity_queries == run.usage.structure_preparations == len(generated) == 1
    assert store.environment_lease() is None
    attempt = store.reserve_attempt(run, plan.steps[2], artifact)
    assert attempt.geometry_artifact_id == artifact and not attempt.started
    assert run.usage.orca_starts_reserved == 1 and run.usage.extra_orca_starts_reserved == 0
    assert run.usage.orca_starts_actual == 0
    store.release_environment(run.id, attempt.id, termination_confirmed=True)


@pytest.mark.parametrize("point", ["after_input_artifacts_saved", "after_result_saved", "after_run_updated"])
def test_two_resumes_recover_inputs_without_regeneration_or_recharging(tmp_path, monkeypatch, point):
    store, run = intake(tmp_path)
    run, plan, identity, generated = resolved_chain(store, run, monkeypatch)
    def crash(at):
        if at == point:
            raise KeyboardInterrupt("synthetic crash")
    with pytest.raises(KeyboardInterrupt):
        prepare_call(store, run, plan, identity, crash)
    run = store.load_run(run.id)
    for _ in range(2):
        assert store.recover_calls(run)
        run = store.load_run(run.id)
    assert prepared_geometry(store, run, store.load_request(run), "water")
    assert len(generated) == run.usage.structure_preparations == run.usage.identity_queries == 1
    assert len(run.result_ids) == 2 and store.environment_lease() is None


def test_preparatory_plans_count_revisions_before_first_science_baseline(tmp_path):
    store, run = intake(tmp_path)
    first = plan_for(store, run, include_prepare=False, include_science=False)
    run = activate(store, run, first)
    assert run.initial_science_steps is None and run.usage.plan_revisions == 0
    second = plan_for(store, run, prior=first, include_science=False)
    run = activate(store, run, second, "prepare_plan")
    assert run.initial_science_steps is None and run.usage.plan_revisions == 1
    third = plan_for(store, run, prior=second)
    run = activate(store, run, third, "science_plan")
    assert run.initial_science_steps == ["science"] and run.usage.plan_revisions == 2
    assert not run.permission.allow_additional_science


def test_preparation_cannot_exceed_frozen_memory_permission(tmp_path):
    store, run = intake(tmp_path, memory_mb=512)
    with pytest.raises(ValueError, match="preparation resources"):
        activate(store, run, plan_for(store, run, include_science=False))
    assert run.usage.structure_preparations == 0 and not run.calls


def test_changed_request_cannot_consume_old_prepared_artifact(tmp_path, monkeypatch):
    store, run = intake(tmp_path)
    run, plan, identity, _ = resolved_chain(store, run, monkeypatch)
    result = prepare_call(store, run, plan, identity)
    old = result.model_dump_json()
    message = store.enqueue_message(run.id, "The charge is now 1.")
    run = apply_user_update(store, run.id, message, {"charge": 1})
    assert prepared_geometry(store, run, store.load_request(run), "water") is None
    assert store.load_result(run.id, result.id).model_dump_json() == old
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0


def test_tampered_prepared_artifact_rejected_before_science(tmp_path, monkeypatch):
    store, run = intake(tmp_path)
    run, plan, identity, _ = resolved_chain(store, run, monkeypatch)
    result = prepare_call(store, run, plan, identity)
    identifier = result.qualified_outputs["prepared_geometry"].artifact_id
    store.artifact_path(identifier).write_text("tampered")
    with pytest.raises(ValueError):
        store.reserve_attempt(run, plan.steps[2], identifier)
    assert run.usage.orca_starts_reserved == 0


def test_unknown_preparation_keeps_global_slot_and_cost(tmp_path, monkeypatch):
    store, run = intake(tmp_path)
    run, plan, identity, _ = resolved_chain(store, run, monkeypatch)
    mock_generator(monkeypatch, state="unknown")
    result = prepare_call(store, run, plan, identity)
    assert result.operation_status == "unknown" and store.environment_lease()
    assert not store.recover_calls(run)
    assert run.usage.structure_preparations == 1
    with pytest.raises((EnvironmentBusy, ValueError), match="reconciliation|occupied"):
        prepare_call(store, run, plan, identity)


def test_text_to_model_plan_to_prepared_input_uses_the_single_feedback_loop(tmp_path, monkeypatch):
    from test_agent import ScriptedTransport

    from orca_agent import agent
    from orca_agent.config import load_config

    config = load_config(Path("config.text.example.toml"))
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    text = "Calculate the initial geometry single-point electronic energy of water."
    run = initialize_text(store, config, text)
    normalization = {"action": "normalize_request", "parameters": candidate(store, run, kind="normalize",
        goals=[{"key": "energy", "port": "energy", "system_refs": ["water"], "text_basis": text,
                "geometry_relation": "fixed_initial"}],
        conditions={key: {"value": value, "source": "default", "default_rule": "local-hf-1"}
                    for key, value in config.text.defaults.items()})}
    proposal = {"action": "initial_plan", "parameters": {"steps": [
        {"key": "identity", "tool": "structure.resolve", "system_id": "water", "parameters": {"system_id": "water"}},
        {"key": "prepare", "tool": "structure.prepare", "system_id": "water",
         "parameters": {"system_id": "water", "charge": 0, "multiplicity": 1},
         "inputs": {"identity": {"producer_key": "identity", "port": "resolved_identity"}}},
        {"key": "science", "tool": "orca.sp", "system_id": "water", "parameters": {},
         "geometry": {"producer_key": "prepare", "port": "prepared_geometry"}}],
        "goal_map": {"goal_energy": {"step_key": "science", "port": "energy"}}}}
    def prepare_next(data):
        current = store.load_run(run.id)
        step = next(s for s in store.load_plan(current).steps if s.tool == "structure.prepare")
        return {"action": "call_tool", "parameters": {"step_id": step.id}}
    transport = ScriptedTransport(normalization, proposal, prepare_next,
        {"action": "stop", "parameters": {"reason": "Offline input-chain test stops before ORCA; energy remains unavailable."}})
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (200, response(), True))
    generated = mock_generator(monkeypatch)
    ended = agent.execute(store, config, run.id, transport=transport)
    assert prepared_geometry(store, ended, store.load_request(ended), "water"), ended.diagnostics
    assert len(transport.sent) == 4 and not transport.scripts
    assert len(generated) == 1 and ended.usage.structure_preparations == 1
    assert ended.usage.orca_starts_actual == ended.usage.orca_starts_reserved == 0
    assert ended.goal_status["goal_energy"] == "insufficient_evidence"
