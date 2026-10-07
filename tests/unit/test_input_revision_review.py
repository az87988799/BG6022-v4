"""Independent offline regressions for new text purpose and acquisition boundaries."""

import pytest
from test_semantic_control import candidate
from test_text_entry import defaults, text_environment

from orca_agent.model_usage import current_basis
from orca_agent.models import EvidenceRef, InputRef, OutputBinding, Plan, Step
from orca_agent.natural import initialize_text
from orca_agent.semantic import commit_candidate
from orca_agent.store import StoreError


def test_newer_unknown_geometry_relation_cannot_inherit_earlier_explicit_sp(tmp_path):
    store, config = text_environment(tmp_path)
    text = "Calculate the initial geometry single-point electronic energy of water."
    run = initialize_text(store, config, text)
    store.enqueue_message(run.id, "The geometry relation for water is now unknown.")
    with pytest.raises((StoreError, ValueError)):
        commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=defaults(config),
            goals=[{"key": "energy", "port": "energy", "system_refs": ["water"], "text_basis": text,
                    "geometry_relation": "fixed_initial"}]), decision_id="stale_relation",
            basis=current_basis(store, run))
    assert not store.load_run(run.id).calls and not store.load_run(run.id).attempts


def test_named_solvent_in_water_cannot_be_overwritten_by_gas_phase_default(tmp_path):
    store, config = text_environment(tmp_path)
    text = "Calculate the initial geometry single-point electronic energy of methane in water."
    run = initialize_text(store, config, text)
    assert store.load_request(run).systems[0].id == "methane"
    with pytest.raises((StoreError, ValueError)):
        commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=defaults(config),
            goals=[{"key": "energy", "port": "energy", "system_refs": ["methane"], "text_basis": text,
                    "geometry_relation": "fixed_initial"}]), decision_id="default_erases_solvent",
            basis=current_basis(store, run))
    assert not store.load_run(run.id).calls and not store.load_run(run.id).attempts


@pytest.mark.parametrize(("text", "relation", "wrong_tool"), [
    ("Optimize water and report its electronic energy.", "optimized", "orca.sp"),
    ("Report water's initial geometry single-point electronic energy.", "fixed_initial", "orca.opt"),
])
def test_first_science_plan_cannot_bind_the_opposite_geometry_relation(tmp_path, text, relation, wrong_tool):
    store, config = text_environment(tmp_path, permission={"model_execution": True,
        "scientific_execution": True, "external_identity_queries": True, "geometry_preparation": True,
        "artifact_writes": True, "allowed_tools": ["structure.resolve", "structure.prepare", "orca.sp", "orca.opt"]})
    run = initialize_text(store, config, text)
    run = commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=defaults(config),
        goals=[{"key": "energy", "port": "energy", "system_refs": ["water"], "text_basis": text,
                "geometry_relation": relation}]), decision_id="normalize", basis=current_basis(store, run))
    request = store.load_request(run)
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="water",
                   parameters={"system_id": "water"})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="water",
        parameters={"system_id": "water", "charge": 0, "multiplicity": 1}, depends_on=["resolve"],
        inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity")})
    science = Step(id="science", logical_id="science", tool=wrong_tool, system_id="water",
        geometry=InputRef(producer_step_id="prepare", port="prepared_geometry"), depends_on=["prepare"])
    plan = Plan(request_id=request.id, request_version=request.version, steps=[resolve, prepare, science],
                goal_map={request.goals[0].id: OutputBinding(step_id="science", port="energy")})
    with pytest.raises((StoreError, ValueError)):
        store.commit_revision(run, plan, decision_id="wrong_relation", basis=current_basis(store, run))
    assert not store.load_run(run.id).calls and not store.load_run(run.id).attempts


def test_first_science_plan_can_bind_sp_after_qualified_optimization(tmp_path):
    store, config = text_environment(tmp_path, permission={"model_execution": True,
        "scientific_execution": True, "external_identity_queries": True, "geometry_preparation": True,
        "artifact_writes": True, "allowed_tools": ["structure.resolve", "structure.prepare", "orca.sp", "orca.opt"]})
    text = "Optimize water and report its electronic energy."
    run = initialize_text(store, config, text)
    run = commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=defaults(config),
        goals=[{"key": "energy", "port": "energy", "system_refs": ["water"], "text_basis": text,
                "geometry_relation": "optimized"}]), decision_id="normalize", basis=current_basis(store, run))
    request = store.load_request(run)
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="water",
                   parameters={"system_id": "water"})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="water",
        parameters={"system_id": "water", "charge": 0, "multiplicity": 1}, depends_on=["resolve"],
        inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity")})
    optimize = Step(id="optimize", logical_id="optimize", tool="orca.opt", system_id="water",
        geometry=InputRef(producer_step_id="prepare", port="prepared_geometry"), depends_on=["prepare"])
    energy = Step(id="energy", logical_id="energy", tool="orca.sp", system_id="water",
        geometry=InputRef(producer_step_id="optimize", port="optimized_geometry"), depends_on=["optimize"])
    plan = Plan(request_id=request.id, request_version=request.version, steps=[resolve, prepare, optimize, energy],
                goal_map={request.goals[0].id: OutputBinding(step_id="energy", port="energy")})
    run = store.commit_revision(run, plan, decision_id="valid_chain", basis=current_basis(store, run))
    assert run.initial_science_steps == ["optimize", "energy"]
    assert not run.permission.allow_additional_science and not run.calls and not run.attempts


def test_no_execution_clause_does_not_erase_explicit_optimized_registration_scope(tmp_path):
    store, config = text_environment(tmp_path)
    text = "Register water's optimized electronic energy but do not execute."
    run = initialize_text(store, config, text)
    run = commit_candidate(store, run, candidate(store, run, kind="normalize", conditions=defaults(config),
        goals=[{"key": "energy", "port": "energy", "system_refs": ["water"], "text_basis": text,
                "geometry_relation": "optimized"}], notices=["Registered water's optimized energy requirement."]),
        decision_id="register_known_relation", basis=current_basis(store, run))
    request = store.load_request(run)
    assert request.goals[0].conditions["geometry_relation"] == "optimized"
    assert not run.decisions[-1]["semantics"]["awaiting_reply"]
    assert not run.calls and not run.attempts
