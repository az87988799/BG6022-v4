"""Symbolic model plans become program-owned IDs without granting permission."""

import pytest

from orca_agent.models import (
    BudgetLimits,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.planning import validate_revision
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.store import Store


def source(tmp_path, *, initial=False):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    path = tmp_path / "geometry.xyz"
    path.write_text("3\nSynthetic fixture\nO 0 0 0\nH 0 0.757 0.587\nH 0 -0.757 0.587\n")
    geometry = store.import_artifact(path, "initial_geometry")
    request = Request(geometry_artifact_id=geometry.id,
                      goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    prior = None
    if initial:
        step = Step(id="old_sp", logical_id="old_logical", tool="orca.sp",
                    geometry=InputRef(artifact_id=geometry.id), parameters={"scf_maxiter": 1})
        prior = Plan(request_id=request.id, steps=[step],
                     goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    run = store.create_run(request, prior,
        PermissionSnapshot(scientific_execution=True, artifact_ids=[geometry.id],
                           allowed_repairs={"scf_maxiter": [100]}, allow_additional_science=True),
        BudgetLimits(plan_revisions=2))
    return store, run, request, geometry, prior


def single():
    return {"steps": [{"key": "energy", "tool": "orca.sp"}],
            "goal_map": {"energy": {"step_key": "energy", "port": "energy"}}}


def test_new_plan_owns_ids_and_inherits_registered_geometry(tmp_path):
    store, run, request, geometry, _ = source(tmp_path)
    candidate = materialize_plan(store, run, single())
    assert candidate.id.startswith("plan_") and candidate.version == 1
    step = candidate.steps[0]
    assert step.id.startswith("step_") and step.id != "energy"
    assert step.logical_id.startswith("logical_")
    assert step.geometry.artifact_id == geometry.id
    assert candidate.goal_map["energy"].step_id == step.id
    validate_revision(request, None, request, candidate, run)
    assert store.load_plan(run) is None


def test_invalid_step_parameter_diagnostic_names_tool_and_schema_path_without_value(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposal = single()
    rejected_value = "sk-offline-fixture-never-a-credential"
    proposal["steps"][0]["parameters"] = {"scf_maxiter": rejected_value}
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["tool"] == "orca.sp"
    assert detail["errors"][0]["loc"] == ["parameters", "steps", 0, "parameters", "scf_maxiter"]
    assert rejected_value not in str(detail) and "input" not in detail["errors"][0]
    assert not store.load_run(run.id).attempts


def test_future_import_step_key_is_rejected_as_artifact_with_actionable_gap_instruction(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposal = {"steps": [
        {"key": "import", "tool": "evidence.import", "parameters": {"source_id": "authorized_source"}},
        {"key": "read", "tool": "evidence.search", "parameters": {
            "artifact_id": "import", "query": "FINAL SINGLE POINT ENERGY"}, "depends_on": ["import"]}],
        "goal_map": {"energy": {"gap": "await evidence", "port": "energy"}}}
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["tool"] == "evidence.search"
    assert caught.value.detail["path"] == ["parameters", "steps", 1, "parameters", "artifact_id"]
    assert "not a Step key" in str(caught.value) and "{gap,port}" in str(caught.value)
    assert "parameters.goal_map[Goal.id]" in str(caught.value)
    assert not store.load_run(run.id).calls


@pytest.mark.parametrize("tool,values", [
    ("evidence.search", {"query": "FINAL SINGLE POINT ENERGY"}),
    ("evidence.value", {"path": []}),
])
def test_gap_cannot_be_an_artifact_parameter_and_diagnostic_does_not_echo_values(tmp_path, tool, values):
    store, run, _, _, _ = source(tmp_path)
    secret_shaped = "untrusted-gap-reason-with-arbitrary-path"
    proposed = {"steps": [{"key": "read", "tool": tool, "parameters": {**values,
        "artifact_id": {"gap": secret_shaped, "port": "untrusted-port"}}}],
        "goal_map": {"energy": {"gap": "await evidence", "port": "energy"}}}
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposed)
    detail = caught.value.detail
    assert detail["tool"] == tool
    assert detail["path"] == ["parameters", "steps", 0, "parameters", "artifact_id"]
    assert "parameters.goal_map[Goal.id]" in detail["requirement"]
    assert "omit Steps" in detail["requirement"] and "revise_plan" in detail["requirement"]
    assert detail["goal_gap_shape"] == {"parameters": {"goal_map": {
        "<Goal.id>": {"port": "<unchanged Goal.port>", "gap": "<missing evidence>"}}}}
    assert secret_shaped not in str(detail) and "untrusted-port" not in str(detail)
    assert store.load_plan(run) is None and not run.calls and not run.attempts


def test_wrong_goal_port_diagnostic_preserves_exact_requested_port(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposal = single()
    proposal["goal_map"]["energy"]["port"] = "text_window"
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    assert caught.value.detail["expected"] == "energy"
    assert caught.value.detail["path"] == ["parameters", "goal_map", "energy", "port"]
    assert store.load_plan(run) is None


@pytest.mark.parametrize("goal_id,port", [
    ("gibbs_free_energy_difference", "gibbs_free_energy_difference"),
    ("imported_stdout_energy_line", "search_hits"),
])
def test_missing_required_goal_diagnostic_lists_exact_gap_without_mutating_request(tmp_path, goal_id, port):
    store, original, request, _, _ = source(tmp_path)
    request = request.model_copy(deep=True, update={"goals": [*request.goals,
        Goal(id=goal_id, port=port, minimum_check_version="unresolved-1"),
        Goal(id="optional", port="other", required=False, minimum_check_version="unresolved-1")]})
    run = store.create_run(request, None, original.permission, original.budget)
    proposal = single()
    proposal["steps"][0]["logical_key"] = "untrusted-value-must-not-be-echoed"
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["path"] == ["parameters", "goal_map"]
    assert detail["missing_goals"] == [{"goal_id": goal_id, "port": port}]
    assert detail["gap_bindings"] == {
        goal_id: {"port": port, "gap": "<describe missing evidence or capability>"}}
    assert "optional" not in str(detail) and "untrusted-value" not in str(detail)
    proposal["goal_map"].update(detail["gap_bindings"])
    candidate = materialize_plan(store, run, proposal)
    validate_revision(request, None, request, candidate, run)
    assert candidate.goal_map[goal_id].gap is not None
    assert candidate.goal_map[goal_id].port == port
    assert store.load_request(run) == request and store.load_plan(run) is None
    assert not run.attempts and not run.calls and run.goal_status.get(goal_id) != "satisfied"


def test_symbolic_geometry_reference_adds_exact_dependency_and_port(tmp_path):
    store, run, request, _, _ = source(tmp_path)
    proposal = {"steps": [
        {"key": "relax", "tool": "orca.opt"},
        {"key": "measure", "tool": "orca.sp",
         "geometry": {"producer_key": "relax", "port": "optimized_geometry"}},
    ], "goal_map": {"energy": {"step_key": "measure", "port": "energy"}}}
    candidate = materialize_plan(store, run, proposal)
    producer, consumer = candidate.steps
    assert consumer.depends_on == [producer.id]
    assert consumer.geometry.producer_step_id == producer.id
    assert consumer.geometry.artifact_id is None
    validate_revision(request, None, request, candidate, run)


def test_retained_step_is_copied_without_mutating_prior_version(tmp_path):
    store, run, _, _, prior = source(tmp_path, initial=True)
    candidate = materialize_plan(store, run, {"steps": [{"key": "old_sp"}],
        "goal_map": {"energy": {"step_key": "old_sp", "port": "energy"}}})
    assert candidate.id == prior.id and candidate.version == 2
    assert candidate.steps[0] == prior.steps[0]
    assert candidate.steps[0] is not prior.steps[0]
    candidate.steps[0].parameters.scf_maxiter = 100
    assert store.load_plan(run).steps[0].parameters.scf_maxiter == 1


def test_repair_uses_new_step_id_but_exact_existing_logical_identity(tmp_path):
    store, run, request, _, prior = source(tmp_path, initial=True)
    proposal = {"steps": [{"key": "repair", "logical_key": "old_logical", "tool": "orca.sp",
                           "parameters": {"scf_maxiter": 100}}],
                "goal_map": {"energy": {"step_key": "repair", "port": "energy"}}}
    candidate = materialize_plan(store, run, proposal)
    assert candidate.steps[0].id != "old_sp"
    assert candidate.steps[0].logical_id == "old_logical"
    validate_revision(request, prior, request, candidate, run)


@pytest.mark.parametrize("extra", ["permission", "request_id", "id", "version", "shell"])
def test_model_cannot_supply_authority_or_persistent_plan_identity(tmp_path, extra):
    store, run, _, _, _ = source(tmp_path)
    with pytest.raises(ValueError):
        materialize_plan(store, run, {**single(), extra: "untrusted"})


@pytest.mark.parametrize("step", [
    {"key": "missing"},
    {"key": "old_sp", "parameters": {"scf_maxiter": 100}},
    {"key": "old_sp", "logical_key": "new_budget"},
    {"key": "old_sp", "depends_on": []},
])
def test_retained_steps_cannot_mix_shorthand_with_changes(tmp_path, step):
    store, run, _, _, _ = source(tmp_path, initial=True)
    with pytest.raises(ValueError, match="retained Step"):
        materialize_plan(store, run, {"steps": [step],
            "goal_map": {"energy": {"step_key": step["key"], "port": "energy"}}})


def test_duplicate_symbolic_keys_and_unknown_dependencies_rejected(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposal = single()
    proposal["steps"] *= 2
    with pytest.raises(ValueError, match="unique"):
        materialize_plan(store, run, proposal)
    proposal = single()
    proposal["steps"][0]["depends_on"] = ["missing"]
    with pytest.raises((ValueError, KeyError)):
        materialize_plan(store, run, proposal)


def test_cyclic_symbolic_graph_rejected(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposal = {"steps": [
        {"key": "a", "tool": "orca.opt", "geometry": {"producer_key": "b", "port": "optimized_geometry"}},
        {"key": "b", "tool": "orca.opt", "geometry": {"producer_key": "a", "port": "optimized_geometry"}},
    ], "goal_map": {"energy": {"step_key": "b", "port": "energy"}}}
    with pytest.raises(ValueError, match="cyclic"):
        materialize_plan(store, run, proposal)


def test_disk_path_in_geometry_cannot_become_an_artifact_reference(tmp_path):
    store, run, _, _, _ = source(tmp_path)
    proposal = single()
    proposal["steps"][0]["geometry"] = {"artifact_id": "C:/outside/input.xyz"}
    with pytest.raises(ValueError):
        materialize_plan(store, run, proposal)


def test_ambiguous_symbolic_geometry_is_rejected_without_dropping_fields(tmp_path):
    store, run, _, geometry, _ = source(tmp_path)
    proposal = {"steps": [
        {"key": "a", "tool": "orca.opt"},
        {"key": "b", "tool": "orca.sp", "geometry": {
            "producer_key": "a", "port": "optimized_geometry", "artifact_id": geometry.id}},
    ], "goal_map": {"energy": {"step_key": "b", "port": "energy"}}}
    with pytest.raises(ValueError):
        materialize_plan(store, run, proposal)


def test_suspended_initial_plan_materializes_continuous_identity(tmp_path):
    store, run, request, _, prior = source(tmp_path, initial=True)
    message_id = store.enqueue_message(run.id, "Clarify the geometry before running")
    pending = request.model_copy(deep=True, update={"version": 2,
        "messages": store.read_control(run.id)["messages"], "unresolved": ["geometry"]})
    basis = {"request_version": 1, "plan_version": 1, "permission_version": 1,
             "control_generation": store.read_control(run.id)["generation"]}
    run = store.commit_revision(run, None, request=pending, decision_id="suspend", basis=basis,
                                user_message_ids=[message_id])
    candidate = materialize_plan(store, run, single())
    assert candidate.id == prior.id and candidate.version == prior.version + 1
    assert candidate.request_version == 2
