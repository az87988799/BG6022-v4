"""Run-local input lifecycle through production reservation, dispatch and recovery."""

import copy

import pytest

from orca_agent.goals import validate_goal_evidence
from orca_agent.input_bindings import prepared_geometry, resolved_request
from orca_agent.models import (
    BudgetLimits,
    EvidenceRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
    SystemInput,
)
from orca_agent.natural import apply_user_update
from orca_agent.proposals import materialize_plan
from orca_agent.store import Store, StoreError
from orca_agent.tools import structure
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_structure_tools import mock_generator, response


def input_run(tmp_path, monkeypatch, *, environment_root=None):
    store = Store(tmp_path / "data", environment_root=environment_root or tmp_path / "environment")
    quote = "Optimize water and report its optimized geometry and energy."
    evidence = {"message_id": "initial_message", "text_basis": quote}
    identity = {"canonical_names": ["water"], "text_evidence": evidence}
    request = Request(original_text=quote, messages=[{"id": "initial_message", "text": quote}],
        systems=[SystemInput(id="water", identity=identity, geometry_source="prepare")],
        goals=[Goal(id="energy", port="energy", system_ids=["water"], identity=identity,
                    text_evidence=evidence, minimum_check_version="orca-hf-2",
                    conditions={"geometry_relation": "optimized"}),
               Goal(id="geometry", port="optimized_geometry", system_ids=["water"],
                    identity=identity, text_evidence=evidence, minimum_check_version="orca-hf-2")])
    permission = PermissionSnapshot(allowed_tools=["structure.resolve", "structure.prepare", "orca.opt", "orca.sp"],
        artifact_writes=True, external_identity_queries=True, geometry_preparation=True, scientific_execution=True)
    run = store.create_run(request, None, permission, BudgetLimits(
        identity_queries=4, structure_preparations=4, plan_revisions=2),
        science_baseline_policy="first_science_plan")
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="water",
                   parameters={"system_id": "water"})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="water",
        parameters={"system_id": "water", "charge": 0, "multiplicity": 1}, depends_on=["resolve"],
        inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity",
                                        rule_version=structure.IDENTITY_RULE)})
    plan = Plan(request_id=request.id, steps=[resolve, prepare], goal_map={
        goal.id: OutputBinding(port=goal.port, gap="Initial structure acquisition pending; science remains required")
        for goal in request.goals})
    run = store.commit_revision(run, plan, decision_id="initial_plan", basis={
        "request_version": 1, "plan_version": None, "permission_version": 1, "control_generation": 0})
    queries = []
    def query(_, url, **kwargs):
        queries.append(url)
        return 200, response(), True
    monkeypatch.setattr(structure, "_paced_query", query)
    generated = mock_generator(monkeypatch)
    return store, run, plan, queries, generated


def acquire(store, run, plan, *, fault=None):
    identity = execute_call(store, run, "structure.resolve", {"system_id": "water"}, step=plan.steps[0])
    prepared = execute_call(store, run, "structure.prepare", plan.steps[1].parameters.model_dump(),
        step=plan.steps[1], results={"resolve": identity}, fault=fault)
    return identity, prepared


def science_plan(store, run, *, direct=False):
    prior = store.load_plan(run)
    request = store.load_request(run)
    geometry = prepared_geometry(store, run, request, "water")
    step = Step(id="opt", logical_id="opt", tool="orca.opt", system_id="water", depends_on=["prepare"],
        geometry=InputRef(artifact_id=geometry) if direct else InputRef(
            producer_step_id="prepare", port="prepared_geometry"))
    plan = Plan(id=prior.id, version=prior.version + 1, request_id=request.id, request_version=request.version,
        steps=[*prior.steps, step], goal_map={goal.id: OutputBinding(step_id="opt", port=goal.port)
                                           for goal in request.goals})
    return store.commit_revision(run, plan, decision_id="science_plan", basis={
        "request_version": run.request_version, "plan_version": run.plan_version,
        "permission_version": run.permission.version, "control_generation": run.control_generation}), step


@pytest.mark.parametrize("direct", [False, True])
def test_prepared_input_consumption_does_not_change_request_permission_or_claim_optimization(tmp_path, monkeypatch, direct):
    store, run, plan, queries, generated = input_run(tmp_path, monkeypatch)
    request = store.load_request(run)
    permission = run.permission.model_copy(deep=True)
    assert run.initial_science_steps is None
    _, prepared = acquire(store, run, plan)
    assert store.load_request(run) == request and run.permission == permission
    assert request.systems[0].geometry_artifact_id is None and request.geometry_artifact_id is None
    projected = resolved_request(store, run)
    geometry = projected.systems[0].geometry_artifact_id
    assert geometry == prepared.qualified_outputs["prepared_geometry"].artifact_id
    assert all(not validate_goal_evidence(store, run, request, goal, prepared) for goal in request.goals)
    run, step = science_plan(store, run, direct=direct)
    assert run.initial_science_steps == ["opt"] and run.usage.plan_revisions == 1
    attempt = store.reserve_attempt(run, step, geometry)
    assert len(queries) == len(generated) == 1
    assert run.usage.extra_orca_starts_reserved == run.usage.orca_starts_actual == 0
    store.finish_attempt(run, attempt.id, state="cancelled", started=False, termination_confirmed=True)


@pytest.mark.parametrize("point", ["after_input_artifacts_saved", "after_result_saved"])
def test_preparation_interruption_recovers_same_evidence_twice_without_regeneration_or_charge(tmp_path, monkeypatch, point):
    store, run, plan, queries, generated = input_run(tmp_path, monkeypatch)
    def crash(where):
        if where == point:
            raise KeyboardInterrupt("synthetic publication interruption")
    with pytest.raises(KeyboardInterrupt):
        acquire(store, run, plan, fault=crash)
    recovered = store.load_run(run.id)
    before = recovered.usage.model_dump()
    assert recovered.calls[-1].state == "reserved"
    assert store.environment_lease()["attempt_id"] == recovered.calls[-1].id
    assert store.recover_calls(recovered)
    binding = copy.deepcopy(recovered.input_bindings)
    result_ids = list(recovered.result_ids)
    assert store.environment_lease() is None
    recovered = store.load_run(run.id)
    assert store.recover_calls(recovered)
    assert recovered.input_bindings == binding and recovered.result_ids == result_ids
    assert recovered.usage.model_dump() == before
    assert len(queries) == len(generated) == 1
    assert prepared_geometry(store, recovered, store.load_request(recovered), "water")
    assert not recovered.attempts and recovered.usage.orca_starts_actual == 0


def test_request_revision_releases_current_input_binding_and_can_plan_new_acquisition(tmp_path, monkeypatch):
    store, run, plan, queries, generated = input_run(tmp_path, monkeypatch)
    _, prepared = acquire(store, run, plan)
    old_id = prepared.qualified_outputs["prepared_geometry"].artifact_id
    old_result = store.load_result(run.id, prepared.id).model_dump_json()
    old_request = store.load_request(run)
    usage = run.usage.model_dump()
    message = store.enqueue_message(run.id, "The solvent condition is now explicitly unknown.")
    updated = apply_user_update(store, run.id, message, {"conditions": {"environment": "unknown"}})
    request = store.load_request(updated)
    assert not updated.input_bindings
    assert prepared_geometry(store, updated, request, "water") is None
    assert resolved_request(store, updated).systems[0].geometry_artifact_id is None
    assert store.load_request_revision(updated, 1) == old_request
    assert store.load_result(updated.id, prepared.id).model_dump_json() == old_result
    assert store.artifact_path(old_id).exists() and updated.usage.model_dump() == usage
    proposal = {"steps": [{"key": "resolve_v2", "tool": "structure.resolve", "system_id": "water",
                            "parameters": {"system_id": "water"}}],
                "goal_map": {goal.id: {"port": goal.port, "gap": "Reconfirm current identity and conditions"}
                             for goal in request.goals}}
    new_plan = materialize_plan(store, updated, proposal)
    updated = store.commit_revision(updated, new_plan, decision_id="fresh_acquisition", basis={
        "request_version": updated.request_version, "plan_version": None,
        "permission_version": updated.permission.version, "control_generation": updated.control_generation})
    assert updated.plan_version == plan.version + 1 and updated.usage.plan_revisions == 1
    assert len(queries) == len(generated) == 1 and not updated.attempts


def test_unreceipted_identity_query_cannot_reexecute_after_two_recoveries(tmp_path, monkeypatch):
    store, run, plan, queries, generated = input_run(tmp_path, monkeypatch)
    with monkeypatch.context() as patch:
        def crash(*_):
            raise KeyboardInterrupt("synthetic lost receipt")
        patch.setattr(structure, "_finish", crash)
        with pytest.raises(KeyboardInterrupt):
            execute_call(store, run, "structure.resolve", {"system_id": "water"}, step=plan.steps[0])
    for _ in range(2):
        run = store.load_run(run.id)
        assert not store.recover_calls(run)
    before = run.usage.model_dump()
    with pytest.raises((StoreError, ValueError)):
        execute_call(store, run, "structure.resolve", {"system_id": "water"}, step=plan.steps[0])
    assert run.usage.model_dump() == before
    assert len(queries) == 1 and not generated and len(run.calls) == 1
    assert run.state == "unknown" and run.calls[0].state == "unknown"


def test_old_preparation_receipt_recovery_after_user_change_does_not_bind_current_inputs(tmp_path, monkeypatch):
    store, run, plan, queries, generated = input_run(tmp_path, monkeypatch)
    def crash(point):
        if point == "after_input_artifacts_saved":
            raise KeyboardInterrupt("synthetic late publication")
    with pytest.raises(KeyboardInterrupt):
        acquire(store, run, plan, fault=crash)
    before = store.load_run(run.id).usage.model_dump()
    message = store.enqueue_message(run.id, "Charge is unknown; keep only the earlier raw evidence.")
    run = apply_user_update(store, run.id, message, {"charge": None})
    for _ in range(2):
        run = store.load_run(run.id)
        assert store.recover_calls(run)
    assert not run.input_bindings and len(run.result_ids) == 2
    assert run.usage.model_dump() == before and len(queries) == len(generated) == 1
    assert store.environment_lease() is None and not run.attempts
    assert all(store.load_result(run.id, identifier).source["request_version"] == 1
               for identifier in run.result_ids)


@pytest.mark.parametrize("change", ["artifact", "check_version", "system", "purpose"])
def test_forged_input_binding_is_rejected_before_persistence_or_scientific_reservation(tmp_path, monkeypatch, change):
    store, run, plan, _, _ = input_run(tmp_path, monkeypatch)
    acquire(store, run, plan)
    forged = run.model_copy(deep=True)
    binding = forged.input_bindings["water"]["prepared_geometry"]
    if change == "artifact":
        binding["artifact_id"] = forged.input_bindings["water"]["resolved_identity"]["artifact_id"]
    elif change == "check_version":
        binding["rule_version"] = "structure-prepare-0"
    elif change == "system":
        forged.input_bindings["methane"] = forged.input_bindings.pop("water")
    else:
        binding["purpose_fingerprint"] = "0" * 64
    with pytest.raises((StoreError, ValueError)):
        store.save_run(forged)
    actual = store.load_run(run.id)
    assert actual.input_bindings == run.input_bindings and actual.usage == run.usage
    assert not actual.attempts and actual.usage.orca_starts_reserved == 0


def test_prepared_artifact_tampering_blocks_downstream_before_any_scientific_reservation(tmp_path, monkeypatch):
    store, run, plan, queries, generated = input_run(tmp_path, monkeypatch)
    _, prepared = acquire(store, run, plan)
    run, step = science_plan(store, run)
    identifier = prepared.qualified_outputs["prepared_geometry"].artifact_id
    store.artifact_path(identifier).write_bytes(b"changed after the validated preparation")
    before = run.usage.model_dump()
    with pytest.raises(StoreError, match="hash"):
        store.reserve_attempt(run, step, identifier)
    assert run.usage.model_dump() == before and not run.attempts
    assert len(queries) == len(generated) == 1 and store.environment_lease() is None


def test_unknown_preparation_preserves_global_lease_across_store_roots_and_recovery(tmp_path, monkeypatch):
    store, run, plan, queries, _ = input_run(tmp_path / "first", monkeypatch)
    generated = mock_generator(monkeypatch, state="unknown")
    _, prepared = acquire(store, run, plan)
    assert prepared.operation_status == "unknown" and not run.input_bindings["water"].get("prepared_geometry")
    lease = copy.deepcopy(store.environment_lease())
    before = run.usage.model_dump()
    for _ in range(2):
        run = store.load_run(run.id)
        assert not store.recover_calls(run)
    assert store.environment_lease() == lease and run.usage.model_dump() == before
    assert len(queries) == len(generated) == 1
    other, second, second_plan, other_queries, other_generated = input_run(
        tmp_path / "second", monkeypatch, environment_root=store.environment_root)
    identity = execute_call(other, second, "structure.resolve", {"system_id": "water"}, step=second_plan.steps[0])
    other_before = second.usage.model_dump()
    with pytest.raises(StoreError, match="quota"):
        execute_call(other, second, "structure.prepare", second_plan.steps[1].parameters.model_dump(),
                     step=second_plan.steps[1], results={"resolve": identity})
    assert second.usage.model_dump() == other_before and len(second.calls) == 1
    assert other.environment_lease() == lease
    assert len(other_queries) == 1 and not other_generated and not second.attempts
