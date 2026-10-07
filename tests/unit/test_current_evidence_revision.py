"""Synthetic G03 regressions through durable user updates; no HTTP or ORCA."""

import pytest

from orca_agent import agent, runner
from orca_agent.applicability import purpose_snapshot, validate_geometry_consumption
from orca_agent.config import Config
from orca_agent.goals import current_goal_evidence
from orca_agent.models import EvidenceRef, Goal, OutputBinding, Plan, Step
from orca_agent.natural import apply_user_update
from orca_agent.report import build_report
from orca_agent.store import StoreError
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_agent import ScriptedTransport, initial_proposal, make_run
from tests.unit.test_dispatch import archived_energy
from tests.unit.test_revision_store import basis


def immediate(data):
    goal = data["AUTHORITY"]["request"]["goals"][0]
    return {"action": "call_tool", "parameters": {
        "tool": "evidence.value", "parameters": goal["conditions"]["query"]}}


def snapshot_evidence(store, run):
    return {identifier: store.path(f"runs/{run.id}/results/{identifier}.json").read_bytes()
            for identifier in run.result_ids}


def assert_recovery_is_readonly(store, run):
    usage, calls, results = run.usage.model_dump(), len(run.calls), list(run.result_ids)
    for _ in range(2):
        run = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert run.state == "completed"
        assert run.usage.model_dump() == usage
        assert len(run.calls) == calls and run.result_ids == results
        assert build_report(store, run)["user_goal_complete"]
    return run


def test_same_id_query_change_releases_direct_and_completes_fresh_plan(tmp_path):
    store, run, _ = make_run(tmp_path)
    run = agent.execute(store, Config(), run.id, transport=ScriptedTransport(immediate))
    old_binding = run.goal_evidence["a"]
    old_evidence = snapshot_evidence(store, run)
    original_permission = run.permission
    goal = store.load_request(run).goals[0].model_copy(deep=True)
    goal.conditions["query"]["path"][0]["key"] = "b"
    message = store.enqueue_message(run.id, "Read b instead, and explain the result")
    run = apply_user_update(store, run.id, message, {
        "goals": [goal.model_dump()], "conditions": {"explain_results": True}})
    assert not run.goal_evidence and run.goal_status == {"a": "insufficient_evidence"}

    def explanation(data):
        current = data["DATA"]["current_goal_use"]
        assert len(current) == 1 and current[0]["goal_id"] == "a"
        assert current[0]["result_id"] != old_binding.result_id
        assert current[0]["status"] == "passed"
        return {"action": "stop", "parameters": {"reason": "requested field read"}}

    run = agent.execute(store, Config(), run.id,
                        transport=ScriptedTransport(initial_proposal, explanation))
    report = build_report(store, run)
    assert report["user_goal_complete"] and run.state == "completed"
    result_id = report["goals"][0]["result_id"]
    assert store.load_result(run.id, result_id).observations["value_observation"]["value"] == 2
    assert run.usage.evidence_reads == 2 and run.usage.model_calls == 3
    assert run.usage.model_tokens_used == 225 and run.permission == original_permission
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 0
    assert all(snapshot_evidence(store, run)[key] == value for key, value in old_evidence.items())
    assert_recovery_is_readonly(store, run)

    # Historical Runs written before invalidation was implemented remain readable.
    run = store.load_run(run.id)
    run.goal_evidence["a"] = old_binding
    assert runner._goals(store, run, store.load_plan(run), runner._step_results(store, run))
    assert build_report(store, run)["goals"][0]["result_id"] == result_id


@pytest.mark.parametrize("initial_plan", [False, True])
def test_unrelated_message_preserves_valid_direct_or_plan_result(tmp_path, initial_plan):
    store, run, _ = make_run(tmp_path, initial_plan=initial_plan)
    run = agent.execute(store, Config(), run.id,
                        transport=ScriptedTransport() if initial_plan else ScriptedTransport(immediate))
    before = snapshot_evidence(store, run)
    usage = run.usage.model_dump()
    message = store.enqueue_message(run.id, "Thank you; keep the same requested field")
    run = apply_user_update(store, run.id, message, {})
    assert run.goal_evidence["a"].result_id == run.result_ids[0]
    assert run.goal_status == {"a": "satisfied"}
    assert run.usage.model_dump() == usage and snapshot_evidence(store, run) == before
    assert_recovery_is_readonly(store, run)


def test_multigoal_revision_releases_only_changed_goal_and_never_requeries_other(tmp_path):
    store, run, _ = make_run(tmp_path, two_goals=True, initial_plan=True)
    run = agent.execute(store, Config(), run.id,
                        transport=ScriptedTransport({"action": "call_tool", "parameters": {"step_id": "read_b"}}))
    before = snapshot_evidence(store, run)
    goals = store.load_request(run).model_copy(deep=True).goals
    old_b = run.selected_results["read_b"]
    goals[0].conditions["query"]["path"] = []
    message = store.enqueue_message(run.id, "Read the whole object for a; keep b")
    run = apply_user_update(store, run.id, message, {"goals": [g.model_dump() for g in goals]})
    assert set(run.goal_evidence) == {"b"}
    assert run.goal_evidence["b"].result_id == old_b
    assert run.goal_status == {"a": "insufficient_evidence", "b": "satisfied"}
    run = agent.execute(store, Config(), run.id, transport=ScriptedTransport(immediate))
    assert run.usage.evidence_reads == 3
    report = build_report(store, run)
    assert report["user_goal_complete"] and report["goals"][1]["result_id"] == old_b
    assert all(snapshot_evidence(store, run)[key] == value for key, value in before.items())
    assert_recovery_is_readonly(store, run)


def test_same_id_quantity_change_cannot_keep_prior_scientific_output(tmp_path):
    store, original, _ = make_run(tmp_path)
    run, result = archived_energy(store)
    run.goal_evidence["energy"] = EvidenceRef(run_id=run.id, result_id=result.id, port="energy")
    store.save_run(run)
    before, usage = snapshot_evidence(store, run), run.usage.model_dump()
    message = store.enqueue_message(run.id, "I require a qualified optimized structure")
    goal = Goal(id="energy", port="optimized_geometry", minimum_check_version="orca-hf-2")
    run = apply_user_update(store, run.id, message, {"goals": [goal.model_dump()]})
    assert not run.goal_evidence and run.goal_status == {"energy": "insufficient_evidence"}
    assert run.usage.model_dump() == usage and snapshot_evidence(store, run) == before
    assert not build_report(store, run)["user_goal_complete"]
    assert original.id != run.id


@pytest.mark.parametrize("changes", [{"charge": 1}, {"conditions": {"environment": "water"}},
                                     {"conditions_source": {"method": "unknown"}}])
def test_condition_change_releases_historical_scientific_binding_without_relabeling(tmp_path, changes):
    store, _, _ = make_run(tmp_path)
    run, result = archived_energy(store)
    run.goal_evidence["energy"] = EvidenceRef(run_id=run.id, result_id=result.id, port="energy")
    store.save_run(run)
    before, usage, permission = snapshot_evidence(store, run), run.usage.model_dump(), run.permission
    message = store.enqueue_message(run.id, "Change the requested conditions")
    run = apply_user_update(store, run.id, message, changes)
    assert not run.goal_evidence and run.goal_status == {"energy": "insufficient_evidence"}
    assert run.usage.model_dump() == usage and run.permission == permission
    for _ in range(2):
        run = store.load_run(run.id)
        assert store.recover_calls(run)
        assert not runner._goals(store, run, None, runner._step_results(store, run))
        assert not build_report(store, run)["user_goal_complete"]
        assert snapshot_evidence(store, run) == before
        assert run.usage.model_dump() == usage


@pytest.mark.parametrize("point", ["after_revision_saved", "after_revision_activated"])
def test_binding_invalidation_shares_atomic_activation_and_replays_once(tmp_path, monkeypatch, point):
    store, run, _ = make_run(tmp_path)
    run = agent.execute(store, Config(), run.id, transport=ScriptedTransport(immediate))
    old_binding, before = run.goal_evidence["a"], snapshot_evidence(store, run)
    usage = run.usage.model_dump()
    goal = store.load_request(run).goals[0].model_copy(deep=True)
    goal.conditions["query"]["path"][0]["key"] = "b"
    message = store.enqueue_message(run.id, "Read b")
    commit = store.commit_revision

    def crash(where):
        if where == point:
            raise KeyboardInterrupt

    def interrupted(*args, **kwargs):
        return commit(*args, **kwargs, fault=crash)

    monkeypatch.setattr(store, "commit_revision", interrupted)
    with pytest.raises(KeyboardInterrupt):
        apply_user_update(store, run.id, message, {"goals": [goal.model_dump()]})
    recovered = store.load_run(run.id)
    assert recovered.goal_evidence == ({"a": old_binding} if point == "after_revision_saved" else {})
    monkeypatch.setattr(store, "commit_revision", commit)
    if point == "after_revision_saved":
        recovered = apply_user_update(store, run.id, message, {"goals": [goal.model_dump()]})
    for _ in range(2):
        replay = commit(recovered, None, decision_id="user_" + message, basis=basis(store, recovered))
        assert replay == recovered
        assert replay.usage.model_dump() == usage and snapshot_evidence(store, replay) == before
    assert not recovered.goal_evidence and recovered.request_version == 2


def test_current_selection_never_picks_unbound_newer_matching_result(tmp_path):
    store, run, _ = make_run(tmp_path)
    run = agent.execute(store, Config(), run.id, transport=ScriptedTransport(immediate))
    selected_id = run.goal_evidence["a"].result_id
    request = store.load_request(run)
    newer = execute_call(store, run, "evidence.value", request.goals[0].conditions["query"])
    selected = current_goal_evidence(store, run, request, request.goals[0])
    assert selected["result"].id == selected_id != newer.id
    run.goal_evidence = {}
    selected = current_goal_evidence(store, run, request, request.goals[0])
    assert selected["result"] is None and selected["gaps"] == ["goal_has_no_evidence_binding"]


@pytest.mark.parametrize("mutation", [
    {"port": "energy"}, {"rule_version": "obsolete"}, {"attempt_id": "different_attempt"},
    {"run_id": "unpermitted_run"}, {"result_id": "unbound_result"}, {"sha256": "0" * 64},
])
def test_invalid_direct_binding_cannot_hide_valid_explicit_plan_reference(tmp_path, mutation):
    store, run, _ = make_run(tmp_path)
    run = agent.execute(store, Config(), run.id, transport=ScriptedTransport(immediate))
    result = store.load_result(run.id, run.goal_evidence["a"].result_id)
    request = store.load_request(run)
    plan = Plan(request_id=request.id, steps=[Step(id="unused", logical_id="unused",
        tool="evidence.value", parameters=request.goals[0].conditions["query"])],
        goal_map={"a": OutputBinding(
        port="value_observation", evidence=run.goal_evidence["a"].model_copy(deep=True))})
    run.goal_evidence["a"] = run.goal_evidence["a"].model_copy(update=mutation)
    selection = current_goal_evidence(store, run, request, request.goals[0], plan)
    assert selection["result"].id == result.id and selection["assessment"]["status"] == "passed"
    assert selection["binding"] == plan.goal_map["a"] and not selection["gaps"]


@pytest.mark.parametrize("name,expected", [("water", "satisfied"), ("methane", "insufficient_evidence")])
def test_named_target_update_rechecks_actual_geometry_before_retaining_result(tmp_path, name, expected):
    store, _, _ = make_run(tmp_path)
    run, result = archived_energy(store)
    old_plan = store.load_plan(run)
    before, usage, permission = snapshot_evidence(store, run), run.usage.model_dump(), run.permission
    goal = store.load_request(run).goals[0].model_copy(deep=True)
    goal.identity = {"canonical_names": [name], "support_status": "supported"}
    goal.original_text = f"Change the named target to {name}"
    message = store.enqueue_message(run.id, goal.original_text)
    run = apply_user_update(store, run.id, message, {"goals": [goal.model_dump()]})
    assert run.goal_status == {"energy": expected}
    assert bool(run.goal_evidence) == (expected == "satisfied")
    assert run.usage.model_dump() == usage and run.permission == permission
    assert snapshot_evidence(store, run) == before
    if name == "methane":
        assert not build_report(store, run)["user_goal_complete"]
        plan = old_plan.model_copy(update={"version": 2, "request_version": run.request_version})
        with pytest.raises(StoreError, match="named_target_geometry_identity_mismatch"):
            store.commit_revision(run, plan, decision_id="wrong_named_target", basis=basis(store, run))
        assert store.load_run(run.id).usage.model_dump() == usage
        assert not store.path(f"runs/{run.id}/decisions/wrong_named_target.json").exists()
    else:
        assert run.goal_evidence["energy"].result_id == result.id


def test_named_geometry_mismatch_rejects_initial_plan_before_reservation(tmp_path):
    from orca_agent.models import InputRef, PermissionSnapshot, Request, SystemInput

    store, _, _ = make_run(tmp_path)
    path = tmp_path / "methane.xyz"
    path.write_text("5\nSynthetic methane mislabeled water\nC 0 0 0\nH .63 .63 .63\n"
                    "H -.63 -.63 .63\nH -.63 .63 -.63\nH .63 -.63 -.63\n")
    geometry = store.import_artifact(path, "initial_geometry")
    request = Request(geometry_artifact_id=geometry.id,
        systems=[SystemInput(id="water", geometry_artifact_id=geometry.id)],
        goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2",
                    system_ids=["water"], identity={"canonical_names": ["water"]})])
    step = Step(id="sp", logical_id="sp", tool="orca.sp", system_id="water",
                geometry=InputRef(artifact_id=geometry.id))
    plan = Plan(request_id=request.id, steps=[step], goal_map={
        "energy": OutputBinding(step_id=step.id, port="energy")})
    permission = PermissionSnapshot(scientific_execution=True, artifact_ids=[geometry.id])
    existing = {p.name for p in store.path("runs").iterdir()}
    with pytest.raises(StoreError, match="named_target_geometry_identity_mismatch"):
        store.create_run(request, plan, permission)
    assert {p.name for p in store.path("runs").iterdir()} == existing
    assert store.environment_lease() is None


def test_future_geometry_rechecks_revised_named_identity_before_consumption(tmp_path):
    from tests.unit.test_current_applicability import _optimization_run, _synthetic_result

    store, request, plan, permission, initial = _optimization_run(tmp_path)
    run = store.create_run(request, plan, permission)
    result = _synthetic_result(store, run, plan.steps[0], initial.id)
    artifact = result.qualified_outputs["optimized_geometry"].artifact_id
    usage = run.usage.model_dump()
    goal = request.goals[0].model_copy(deep=True)
    goal.identity = {"canonical_names": ["methane"]}
    message = store.enqueue_message(run.id, "Change the requested target to methane")
    run = apply_user_update(store, run.id, message, {"goals": [goal.model_dump()]})
    with pytest.raises(ValueError, match="named_target_geometry_identity_mismatch"):
        validate_geometry_consumption(store, run, plan.steps[1], artifact)
    assert store.load_run(run.id).usage.model_dump() == usage
    assert run.usage.orca_starts_actual == 0


def test_purpose_identity_excludes_text_provenance_and_preserves_legacy_shape(tmp_path):
    store, run, _ = make_run(tmp_path)
    request = store.load_request(run)
    goal = request.goals[0]
    legacy = purpose_snapshot(request, goal)
    assert "goal_identity" not in legacy
    goal.identity = {"requested_names": ["water"], "support_status": "unknown"}
    goal.text_evidence = {"message_id": "message_new", "text_basis": "water"}
    assert purpose_snapshot(request, goal) == legacy
    goal.identity["canonical_names"] = ["water"]
    named = purpose_snapshot(request, goal)
    assert named["goal_identity"] == {"rule_version": "named-target-1", "canonical_names": ["water"]}
    goal.text_evidence["message_id"] = "message_another"
    goal.identity["requested_names"] = ["H2O"]
    assert purpose_snapshot(request, goal) == named
