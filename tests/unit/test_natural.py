"""User bundles and explicit corrections preserve purpose and invalidate stale Plans."""

import json
from pathlib import Path

import pytest

from orca_agent.config import Config
from orca_agent.models import (
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.natural import agent_budget, apply_user_update, initialize_agent, initialize_bundle
from orca_agent.store import Store, StoreError, sha256_file
from orca_agent.tools.dispatch import execute_call


def store_at(tmp_path):
    return Store(tmp_path / "data", environment_root=tmp_path / "environment")


def bundle_at(tmp_path, **changes):
    data = {"text": "Read the registered evidence.", "allowed_tools": ["evidence.value"],
            "goals": [{"id": "read", "port": "value_observation",
                       "minimum_check_version": "evidence-read-1"}], **changes}
    path = tmp_path / "request.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def query_run(tmp_path):
    store = store_at(tmp_path)
    path = tmp_path / "raw.json"
    path.write_text('{"a": 1, "b": 2}', encoding="utf-8")
    artifact = store.import_artifact(path, "synthetic_test_evidence")
    query = {"artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}
    request = Request(original_text="Read field a", goals=[
        Goal(id="read", port="value_observation", minimum_check_version="evidence-read-1",
             original_text="Read field", conditions={"query": query})])
    permission = PermissionSnapshot(model_execution=True, allowed_tools=["evidence.value"],
                                    artifact_ids=[artifact.id])
    run = initialize_agent(store, Config(), request, permission)
    step = Step(id="read_a", logical_id="query", tool="evidence.value", parameters=query)
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"read": OutputBinding(step_id=step.id, port="value_observation")})
    run = store.commit_revision(run, plan, decision_id="initial", basis={
        "request_version": run.request_version, "plan_version": run.plan_version,
        "permission_version": run.permission.version, "control_generation": 0})
    return store, run, artifact, plan


def test_query_bundle_needs_no_orca_or_doctor_and_no_handwritten_plan(tmp_path, monkeypatch):
    from orca_agent import doctor
    calls = []
    monkeypatch.setattr(doctor, "diagnose", lambda _: calls.append("doctor"))
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path))
    request = store.load_request(run)
    assert run.agent_enabled and run.plan_id is None
    assert run.budget.orca_starts == run.budget.extra_orca_starts == 0
    assert request.charge is None and request.multiplicity is None
    assert request.method is None and request.basis is None
    assert request.conditions_source == {}
    assert request.normalization_status == "normalized"
    assert request.goals[0].unresolved == []
    assert calls == [] and not run.attempts


@pytest.mark.parametrize("field", ["steps", "plan", "request_id", "permission"])
def test_bundle_rejects_execution_plan_and_undeclared_authority(tmp_path, field):
    store = store_at(tmp_path)
    with pytest.raises(ValueError):
        initialize_bundle(store, Config(), bundle_at(tmp_path, **{field: []}))
    assert not (store.root / "runs").exists()


def test_missing_scientific_information_is_explicit_unknown_without_default_scf(tmp_path):
    store = store_at(tmp_path)
    path = bundle_at(tmp_path, goals=[{"id": "energy", "port": "energy",
                                      "minimum_check_version": "orca-hf-2"}])
    run = initialize_bundle(store, Config(), path)
    request = store.load_request(run)
    assert request.method is request.basis is request.charge is request.multiplicity is None
    assert set(request.goals[0].unresolved) == {
        "missing:geometry", "missing:method", "missing:basis", "missing:charge", "missing:multiplicity"}
    assert request.normalization_status == "clarification"
    assert "initial_parameters" not in request.conditions
    assert run.permission.allowed_repairs == {}


def test_explicit_single_system_conditions_are_inherited_with_provenance(tmp_path):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nSynthetic\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n")
    run = initialize_bundle(store, Config(), bundle_at(tmp_path,
        geometries=[{"id": "water", "file": "water.xyz", "conditions": {
            "method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1}}],
        goals=[{"id": "energy", "port": "energy", "minimum_check_version": "orca-hf-2",
                "system_ids": ["water"]}]))
    request = store.load_request(run)
    assert request.charge == 0 and request.method == "HF"
    assert request.conditions_source["charge"] == "inherited"
    assert request.systems[0].conditions_source["charge"] == "explicit"
    assert request.goals[0].unresolved == []
    assert request.geometry_artifact_id == request.systems[0].geometry_artifact_id


def test_unknown_system_does_not_obscure_independent_goal(tmp_path):
    store = store_at(tmp_path)
    path = bundle_at(tmp_path, goals=[
        {"id": "read", "port": "value_observation", "minimum_check_version": "evidence-read-1"},
        {"id": "energy", "port": "energy", "minimum_check_version": "orca-hf-2", "system_ids": ["absent"]},
    ])
    request = store.load_request(initialize_bundle(store, Config(), path))
    assert request.goals[0].unresolved == []
    assert request.goals[1].unresolved == ["missing:system:absent"]
    assert request.unresolved == []


def test_undefined_quantity_stays_unresolved_and_additional_user_conditions_keep_provenance(tmp_path):
    store = store_at(tmp_path)
    path = bundle_at(tmp_path, conditions={"initial_parameters": {"scf_maxiter": 1}},
                     goals=[{"id": "purpose", "port": "unresolved", "minimum_check_version": "unresolved-1"}])
    run = initialize_bundle(store, Config(), path)
    request = store.load_request(run)
    assert request.normalization_status == "clarification"
    assert request.goals[0].unresolved == ["missing:goal_definition"]
    assert request.conditions_source["initial_parameters"] == "explicit"
    message = store.enqueue_message(run.id, "Keep the explicit source conditions")
    updated = apply_user_update(store, run.id, message, {"conditions": {"temperature_K": 298.15}})
    request = store.load_request(updated)
    assert request.conditions["initial_parameters"] == {"scf_maxiter": 1}
    assert request.conditions_source["temperature_K"] == "explicit"


def test_external_source_registration_uses_controlled_ids_hashes_and_partial_files(tmp_path):
    store = store_at(tmp_path)
    raw = tmp_path / "external.out"
    raw.write_text("partial original evidence", encoding="utf-8")
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, sources={"source1": {
        "files": [{"path": "external.out", "role": "external_output"},
                  {"path": "missing.json", "role": "external_json"}]}}))
    registry = store._read_json(f"runs/{run.id}/sources.json")
    assert registry["source1"]["files"][0]["sha256"] == sha256_file(raw)
    assert Path(registry["source1"]["files"][0]["path"]) == raw
    assert registry["source1"]["files"][1]["sha256"] is None
    assert run.permission.source_ids == ["source1"]
    assert not (store.root / "artifacts").exists()
    assert not (tmp_path / "missing.json").exists()


def test_bundle_rejects_parent_traversal_and_unregistered_sources_before_run_creation(tmp_path):
    store = store_at(tmp_path)
    with pytest.raises(StoreError, match="inside"):
        initialize_bundle(store, Config(), bundle_at(tmp_path, sources={"source1": {
            "files": [{"path": "../outside.out", "role": "external_output"}]}}))
    assert not (store.root / "runs").exists()
    request = Request(goals=[Goal(id="read", port="value_observation",
                                 minimum_check_version="evidence-read-1")])
    with pytest.raises(StoreError, match="source registry"):
        initialize_agent(store, Config(), request,
                         PermissionSnapshot(model_execution=True, source_ids=["missing"]))
    assert not (store.root / "runs").exists()


def test_user_goal_correction_invalidates_plan_preserves_evidence_and_cumulative_cost(tmp_path):
    store, run, artifact, plan = query_run(tmp_path)
    result = execute_call(store, run, plan.steps[0].tool, plan.steps[0].parameters.model_dump(),
                          step=plan.steps[0])
    run = store.load_run(run.id)
    previous = run.model_dump()
    old_request = store.load_request(run)
    changed_goal = old_request.goals[0].model_dump()
    changed_goal["conditions"]["query"]["path"] = [{"kind": "key", "key": "b"}]
    message = store.enqueue_message(run.id, "Read field b instead")
    updated = apply_user_update(store, run.id, message, {"goals": [changed_goal]})
    assert updated.plan_id is None and store.load_plan(updated) is None
    assert updated.request_version == 2
    assert updated.usage.model_dump() == previous["usage"]
    assert updated.deadline == run.deadline and updated.permission == run.permission
    assert updated.result_ids == [result.id]
    assert updated.goal_status == {"read": "insufficient_evidence"}
    assert store.load_request_revision(updated, 1) == old_request
    assert store.load_plan_revision(updated, 1) == plan
    assert store.artifact_path(artifact.id).read_text(encoding="utf-8") == '{"a": 1, "b": 2}'
    assert store.load_request(updated).original_text == "Read field a"
    assert store.load_request(updated).messages[-1]["id"] == message


def test_multi_turn_clarification_fills_unknown_fields_but_never_refunds_usage(tmp_path):
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=[
        {"id": "energy", "port": "energy", "minimum_check_version": "orca-hf-2"}]))
    first = store.enqueue_message(run.id, "Neutral singlet, HF and STO-3G")
    updated = apply_user_update(store, run.id, first,
                               {"charge": 0, "multiplicity": 1, "method": "HF", "basis": "STO-3G"})
    request = store.load_request(updated)
    assert request.goals[0].unresolved == ["missing:geometry"]
    assert all(request.conditions_source[name] == "explicit" for name in ("charge", "multiplicity", "method", "basis"))
    assert updated.deadline == run.deadline and updated.budget == run.budget
    second = store.enqueue_message(run.id, "Still waiting for the geometry")
    latest = apply_user_update(store, run.id, second, {"unresolved": ["user will provide geometry"]})
    assert latest.request_version == 3
    assert latest.usage == updated.usage
    assert len(store.load_request(latest).messages) == 2
    with pytest.raises(StoreError, match="new trusted"):
        apply_user_update(store, run.id, first, {"charge": 0})


@pytest.mark.parametrize("changes", [{"permission": {}}, {"budget": {}}, {"plan": {}},
                                     {"original_text": "replacement"}, {"version": 5}, []])
def test_update_cannot_change_permission_budget_or_initial_text(tmp_path, changes):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "User correction")
    with pytest.raises(StoreError, match="undeclared"):
        apply_user_update(store, run.id, message, changes)
    assert store.load_run(run.id).request_version == 1


def test_conflicting_or_inferred_relabelled_user_conditions_are_rejected(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "Set the charge")
    with pytest.raises(StoreError, match="conflict"):
        apply_user_update(store, run.id, message, {"charge": 0, "conditions": {"charge": 1}})
    with pytest.raises(StoreError, match="relabeled"):
        apply_user_update(store, run.id, message, {"charge": 0, "conditions_source": {"charge": "inferred"}})


def test_unauthorized_geometry_cannot_enter_a_request_revision(tmp_path):
    store, run, _, _ = query_run(tmp_path)
    path = tmp_path / "outside.xyz"
    path.write_text("3\nSynthetic\nO 0 0 0\nH 0 .7 .5\nH 0 -.7 .5\n")
    outside = store.import_artifact(path, "external_geometry")
    message = store.enqueue_message(run.id, "Use another geometry")
    with pytest.raises(StoreError, match="permission"):
        apply_user_update(store, run.id, message, {"geometry_artifact_id": outside.id})
    assert store.load_run(run.id).request_version == 1


def test_initialization_without_explicit_model_permission_creates_no_run(tmp_path):
    store = store_at(tmp_path)
    request = Request(goals=[Goal(id="g", port="energy")])
    with pytest.raises(StoreError, match="model permission"):
        initialize_agent(store, Config(), request, PermissionSnapshot(), agent_budget())
    assert not (store.root / "runs").exists()


def scientific_run(tmp_path, monkeypatch):
    """Only synthetic files/doctor observations; this helper never launches ORCA."""
    from orca_agent import doctor
    from orca_agent.model_usage import current_basis
    from orca_agent.models import InputRef

    store = store_at(tmp_path)
    geometries = []
    for index, coordinate in enumerate((".757 .587", ".800 .600")):
        source = tmp_path / f"water{index}.xyz"
        source.write_text(f"3\nSynthetic test input\nO 0 0 0\nH 0 {coordinate}\nH 0 -.757 .587\n")
        geometries.append(store.import_artifact(source, "initial_geometry"))
    orca, mpi = tmp_path / "orca.fixture", tmp_path / "mpi.fixture"
    orca.write_text("non-executable synthetic ORCA fixture")
    mpi.write_text("non-executable synthetic MPI fixture")
    monkeypatch.setattr(doctor, "diagnose", lambda _: {
        "orca": {"compatible": True, "version": "6.1.1"}, "mpi": {"available": True}})
    request = Request(geometry_artifact_id=geometries[0].id,
                      goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    run = initialize_agent(store, Config(orca_path=orca, mpi_path=mpi), request,
        PermissionSnapshot(model_execution=True, scientific_execution=True,
                           artifact_ids=[geometry.id for geometry in geometries]))
    step = Step(id="initial_sp", logical_id="sp", tool="orca.sp",
                geometry=InputRef(artifact_id=geometries[0].id))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    run = store.commit_revision(run, plan, decision_id="initial", basis=current_basis(store, run))
    return store, run, plan, geometries


def test_scientific_initialization_records_mocked_environment_without_starting_process(tmp_path, monkeypatch):
    store, run, _, _ = scientific_run(tmp_path, monkeypatch)
    environment = store._read_json(f"runs/{run.id}/environment.json")
    assert environment["orca"]["sha256"] == sha256_file(tmp_path / "orca.fixture")
    assert environment["mpi"]["sha256"] == sha256_file(tmp_path / "mpi.fixture")
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == 0
    assert not run.attempts and not store.environment_lease()


def test_user_geometry_change_preserves_frozen_attempt_and_recovery_does_not_use_new_request(tmp_path, monkeypatch):
    from orca_agent import runner
    from orca_agent.models import Result
    from orca_agent.tools import calculation

    store, run, plan, geometries = scientific_run(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, plan.steps[0], geometries[0].id)
    old_intent = store.path(f"{attempt.directory}/intent.json").read_bytes()
    store._write_json(f"{attempt.directory}/execution.json", {
        "state": "failed", "exit_code": 1, "handle": None, "resource_usage": {}}, immutable=True)
    message = store.enqueue_message(run.id, "Use the second user-provided geometry")
    changed = apply_user_update(store, run.id, message, {"geometry_artifact_id": geometries[1].id})
    assert changed.plan_id is None and changed.usage.orca_starts_reserved == 1
    assert changed.attempts[0].geometry_artifact_id == geometries[0].id
    assert changed.attempts[0].frozen_step == plan.steps[0]
    assert store.load_request(changed).geometry_artifact_id == geometries[1].id
    seen = []

    def collect(store, run, step, frozen_attempt, outcome):
        seen.append(step)
        assert step.geometry.artifact_id == geometries[0].id
        assert frozen_attempt.request_version == 1
        return Result(run_id=run.id, step_id=step.id, attempt_id=frozen_attempt.id,
                      operation_status="failed", source={"execution": outcome})

    monkeypatch.setattr(calculation, "collect_result", collect)
    assert runner._recover(store, None, changed, None)
    assert runner._recover(store, None, changed, None)
    assert len(seen) == 1 and changed.usage.orca_starts_reserved == 1
    assert changed.usage.orca_starts_actual == 0
    assert store.path(f"{attempt.directory}/intent.json").read_bytes() == old_intent
    assert store.load_plan_revision(changed, 1) == plan


def test_clarified_query_can_replan_without_refunding_prior_query_or_reusing_its_step(tmp_path):
    from orca_agent.model_usage import current_basis

    store, run, artifact, plan = query_run(tmp_path)
    execute_call(store, run, plan.steps[0].tool, plan.steps[0].parameters.model_dump(), step=plan.steps[0])
    goal = store.load_request(run).goals[0].model_dump()
    goal["conditions"]["query"]["path"] = [{"kind": "key", "key": "b"}]
    message = store.enqueue_message(run.id, "Read field b instead")
    changed = apply_user_update(store, run.id, message, {"goals": [goal]})
    replacement = Step(id="read_b", logical_id="query", tool="evidence.value", parameters={
        "artifact_id": artifact.id, "path": [{"kind": "key", "key": "b"}]})
    proposed = Plan(id=plan.id, version=2, request_id=run.request_id, request_version=2,
                    steps=[replacement], goal_map={"read": OutputBinding(step_id="read_b", port="value_observation")})
    changed = store.commit_revision(changed, proposed, decision_id="replan", basis=current_basis(store, changed))
    assert changed.usage.evidence_reads == 1 and changed.usage.plan_revisions == 1
    assert changed.calls[0].frozen_step.id == "read_a"
    result = execute_call(store, changed, replacement.tool, replacement.parameters.model_dump(), step=replacement)
    assert result.observations["value_observation"]["value"] == 2
    assert store.load_run(changed.id).usage.evidence_reads == 2


def test_authorized_geometry_change_can_replan_same_logical_science_without_resetting_budget(tmp_path, monkeypatch):
    from orca_agent.model_usage import current_basis
    from orca_agent.models import InputRef

    store, run, plan, geometries = scientific_run(tmp_path, monkeypatch)
    store.reserve_attempt(run, plan.steps[0], geometries[0].id)
    message = store.enqueue_message(run.id, "Use the other authorized geometry for the same energy goal")
    changed = apply_user_update(store, run.id, message, {"geometry_artifact_id": geometries[1].id})
    replacement = Step(id="updated_sp", logical_id="sp", tool="orca.sp",
                       geometry=InputRef(artifact_id=geometries[1].id))
    proposed = Plan(id=plan.id, version=2, request_id=run.request_id, request_version=2,
                    steps=[replacement], goal_map={"energy": OutputBinding(step_id="updated_sp", port="energy")})
    changed = store.commit_revision(changed, proposed, decision_id="replan", basis=current_basis(store, changed))
    assert changed.usage.orca_starts_reserved == 1 and changed.usage.logical_attempts == {"sp": 1}
    assert changed.usage.plan_revisions == 1 and not changed.permission.allow_additional_science
    assert changed.attempts[0].frozen_step.geometry.artifact_id == geometries[0].id
    assert store.load_plan(changed).steps[0].geometry.artifact_id == geometries[1].id
