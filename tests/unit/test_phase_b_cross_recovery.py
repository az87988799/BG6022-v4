"""Cross-boundary acceptance regressions, entirely offline with isolated ledgers."""

import shutil
from pathlib import Path

import pytest
from test_agent import ScriptedTransport, initial_proposal, make_run
from test_dispatch import REAL_SP, archived_energy
from test_natural import scientific_run
from test_phase_b_reliability import ScriptedHTTP, query_case

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.models import (
    BudgetLimits,
    EvidenceRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Run,
    Step,
    SystemInput,
)
from orca_agent.natural import apply_user_update
from orca_agent.planning import PlanningError, validate_revision
from orca_agent.report import build_report, render_report
from orca_agent.store import Store, StoreError, sha256_file
from orca_agent.tools import calculation, electronic, evidence
from orca_agent.tools.dispatch import execute_call
from tests.helpers import phase_b_budget as budget


@pytest.mark.parametrize("repetition", (1, 2, 3))
@pytest.mark.parametrize("window", ("after_model_response_saved", "after_revision_saved", "after_result_saved"))
def test_global_model_receipt_and_query_recover_twice_without_new_spend(tmp_path, monkeypatch, repetition, window):
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "isolated-batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "absent-delivered.json")
    store, run, _ = make_run(tmp_path)
    run.batch_category = "formal"
    store._write_json(f"runs/{run.id}/run.json", run)
    book = budget.AcceptanceBudget(store)
    transport = ScriptedTransport(initial_proposal)

    def crash(point):
        if point == window:
            raise KeyboardInterrupt

    interrupted = agent.execute(store, Config(), run.id, transport=transport, batch=book, fault=crash)
    assert interrupted.state == "unknown"
    initial_snapshot = book.snapshot()
    settled_snapshot = None
    for _ in range(2):
        completed = agent.execute(store, Config(), run.id, transport=transport, batch=book, resume=True)
        assert completed.state == "completed" and completed.goal_status == {"a": "satisfied"}
        assert completed.deadline == run.deadline
        assert completed.usage.model_calls == completed.usage.evidence_reads == 1
        assert completed.usage.orca_starts_reserved == completed.usage.orca_starts_actual == 0
        snapshot = book.snapshot()
        assert set(snapshot["model_records"]) == set(initial_snapshot["model_records"])
        assert snapshot["model_usage"]["http_requests"] == initial_snapshot["model_usage"]["http_requests"]
        # The first window can crash before the known receipt is linked. Its
        # first recovery settles that reservation; the second changes nothing.
        if settled_snapshot is not None:
            assert snapshot == settled_snapshot
        settled_snapshot = snapshot
    assert len(transport.sent) == 1
    assert book.snapshot()["model_usage"]["http_requests"] == 1
    assert book.snapshot()["model_usage"]["known_tokens"] == 75
    assert not completed.attempts and not store.environment_lease()


@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_initial_sampling_plan_cannot_preplan_an_optional_fourth_point(repetition):
    request = Request(systems=[SystemInput(id=f"candidate{i}", geometry_artifact_id=f"geometry{i}") for i in range(4)],
        conditions={"initial_system_ids": [f"candidate{i}" for i in range(3)]},
        goals=[Goal(id=f"energy{i}", port="energy", system_ids=[f"candidate{i}"],
                    minimum_check_version="orca-hf-2") for i in range(3)])
    steps = [Step(id=f"sp{i}", logical_id=f"logical{i}", system_id=f"candidate{i}", tool="orca.sp",
                  geometry=InputRef(artifact_id=f"geometry{i}")) for i in range(4)]
    plan = Plan(request_id=request.id, steps=steps[:3], goal_map={
        f"energy{i}": OutputBinding(step_id=f"sp{i}", port="energy") for i in range(3)})
    run = Run(request_id=request.id, request_version=1, permission=PermissionSnapshot(scientific_execution=True,
        allow_additional_science=True, artifact_ids=[f"geometry{i}" for i in range(4)]))
    validate_revision(request, None, request, plan, run)
    before = run.model_dump()
    preplanned = plan.model_copy(deep=True, update={"steps": steps})
    with pytest.raises(PlanningError, match="exactly the required initial"):
        validate_revision(request, None, request, preplanned, run)
    assert run.model_dump() == before and not run.attempts


@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_old_attempt_collection_preserves_frozen_versions_after_new_request(tmp_path, monkeypatch, repetition):
    store, run, plan, geometries = scientific_run(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, plan.steps[0], geometries[0].id)
    intent = store.path(f"{attempt.directory}/intent.json").read_bytes()
    store._write_json(f"{attempt.directory}/execution.json", {
        "state": "failed", "exit_code": 1, "handle": None, "resource_usage": {}}, immutable=True)
    message = store.enqueue_message(run.id, "Use the second registered geometry")
    changed = apply_user_update(store, run.id, message, {"geometry_artifact_id": geometries[1].id})
    assert changed.request_version == 2
    for _ in range(2):
        assert runner._recover(store, None, changed, None)
    assert len(changed.result_ids) == 1
    result = store.load_result(run.id, changed.result_ids[0])
    assert {key: result.source[key] for key in ("request_version", "plan_version", "permission_version")} == {
        "request_version": 1, "plan_version": 1, "permission_version": 1}
    assert result.source["geometry_artifact_id"] == geometries[0].id
    assert store.load_request(changed).geometry_artifact_id == geometries[1].id
    assert not result.qualified_outputs
    assert changed.usage.orca_starts_reserved == 1 and changed.usage.orca_starts_actual == 0
    assert store.path(f"{attempt.directory}/intent.json").read_bytes() == intent


def test_legacy_unknown_source_versions_are_not_inferred_from_current_run(tmp_path, monkeypatch):
    store, run, plan, geometries = scientific_run(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, plan.steps[0], geometries[0].id)
    attempt.request_version = attempt.plan_version = attempt.permission_version = None
    result = calculation.collect_result(store, run, plan.steps[0], attempt, {"state": "unknown"})
    assert all(result.source[name] is None for name in ("request_version", "plan_version", "permission_version"))
    assert result.operation_status == "unknown" and not result.qualified_outputs


@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_unknown_attempt_cannot_be_misclassified_as_scf_failure(tmp_path, monkeypatch, repetition):
    store, run, plan, geometries = scientific_run(tmp_path, monkeypatch)
    attempt = store.reserve_attempt(run, plan.steps[0], geometries[0].id)
    uncertain = Result(run_id=run.id, step_id=attempt.step_id, attempt_id=attempt.id,
        operation_status="unknown", diagnostics=[{"category": "scf_not_converged"}])
    store.save_result(uncertain)
    attempt.result_id, attempt.state = uncertain.id, "unknown"
    run.result_ids.append(uncertain.id)
    store.save_run(run)
    repair = plan.model_copy(deep=True, update={"version": 2})
    repair.steps[0].id = "retry_sp"
    repair.steps[0].parameters.scf_maxiter = 100
    repair.goal_map["energy"].step_id = "retry_sp"
    with pytest.raises(StoreError, match="reconciled failure"):
        agent._repair_evidence(store, run, repair)
    assert run.usage.orca_starts_reserved == 1 and len(run.attempts) == 1
    assert store.environment_lease()["attempt_id"] == attempt.id


@pytest.mark.parametrize("repetition", (1, 2, 3))
@pytest.mark.parametrize("entry", ("orca._native", "orca_2json", "python.exec"))
def test_readonly_proposal_cannot_reach_hidden_native_entry(tmp_path, repetition, entry):
    store, run, _ = query_case(tmp_path)
    attack = {"action": "call_tool", "parameters": {"tool": entry, "parameters": {}}}
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedHTTP(attack, attack))
    assert stopped.state != "completed" and not stopped.calls and not stopped.attempts
    assert stopped.usage.model_calls == 2
    assert stopped.usage.orca_starts_reserved == stopped.usage.postprocess_starts == stopped.usage.evidence_reads == 0
    assert stopped.permission == run.permission and not store.environment_lease()


@pytest.mark.parametrize("repetition", (1, 2, 3))
@pytest.mark.parametrize("limit", ("file_bytes", "total_bytes", "file_count"))
def test_oversized_registered_import_rejects_before_creating_any_partial_snapshot(tmp_path, repetition, limit):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    count, size, message = {"file_bytes": (1, 16 * 1024 * 1024 + 1, "16 MiB per file"),
                            "total_bytes": (5, 13 * 1024 * 1024, "64 MiB in total"),
                            "file_count": (17, 1, "1 to 16 files")}[limit]
    files = []
    for index in range(count):
        source = tmp_path / f"source{index}.out"
        with source.open("wb") as stream:
            stream.seek(size - 1)
            stream.write(b"x")
        files.append({"path": source, "role": "external_evidence", "sha256": sha256_file(source)})
    manifest = {"source1": {"files": files}}
    with pytest.raises(ValueError, match=message):
        evidence.import_source(store, "source1", manifest)
    assert not (store.root / "artifacts").exists()
    assert not store.environment_lease()


def test_analysis_feedback_scientific_append_crash_replays_one_reservation_and_callback(tmp_path, monkeypatch):
    """The ORCA entry is replaced by archived-file collection; no process starts."""
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "isolated-batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "absent-delivered.json")
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    historical, energy = archived_energy(store)
    geometry_id = historical.attempts[0].geometry_artifact_id
    ref = EvidenceRef(run_id=historical.id, result_id=energy.id, attempt_id=energy.attempt_id)
    goal = Goal(id="comparison", port="energy_difference", minimum_check_version="energy-compare-1",
                conditions={"comparison": {"member_a": "A", "member_b": "B"},
                            "members": [{"id": "C", "required": True}]})
    request = Request(geometry_artifact_id=geometry_id, goals=[goal])
    analysis = Step(id="initial_analysis", logical_id="initial_analysis", tool="analysis.energy_compare",
                    parameters={"goal_id": goal.id}, inputs={"A": ref, "B": ref})
    initial = Plan(request_id=request.id, steps=[analysis],
                   goal_map={goal.id: OutputBinding(step_id=analysis.id, port=goal.port)})
    run = store.create_run(request, initial, PermissionSnapshot(model_execution=True, scientific_execution=True,
        artifact_writes=True, allow_additional_science=True, allowed_tools=["analysis.energy_compare", "orca.sp"],
        artifact_ids=[geometry_id], result_ids=[energy.id]), BudgetLimits(orca_starts=1, extra_orca_starts=1,
        model_calls=2, model_tokens=48000, input_tokens=12000, output_tokens=2000, decision_rounds=4,
        analysis_executions=2, plan_revisions=1))
    run.agent_enabled, run.batch_category = True, "formal"
    store._write_json(f"runs/{run.id}/run.json", run)
    store._write_json(f"runs/{run.id}/environment.json", {"orca": {"version": "6.1.1"}, "offline_fixture_only": True})
    feedback = execute_call(store, run, analysis.tool, analysis.parameters.model_dump(), step=analysis)
    assert feedback.observations["analysis"]["reason"] == "missing_required_member"
    book = budget.AcceptanceBudget(store)
    callbacks, reservations = [], []
    original_reserve = book.reserve_science

    def reserve(run, step, geometry, **kwargs):
        ticket = original_reserve(run, step, geometry, **kwargs)
        reservations.append(ticket)
        return ticket

    def offline_science(store, current, step, attempt, config, fault=None):
        callbacks.append(attempt.id)
        directory = store.path(attempt.directory)
        # Read-only reuse of archived ORCA evidence with a synthetic execution
        # receipt. This is recovery/accounting evidence, not a new calculation.
        for path in store.path(historical.attempts[0].directory).iterdir():
            if path.is_file() and path.name != "intent.json":
                shutil.copyfile(path, directory / path.name)
        outcome = {"state": "completed", "exit_code": 0, "handle": {"offline_fixture_only": True},
                   "resource_usage": {"wall_seconds": 1, "user_cpu_seconds": 1}, "offline_fixture_only": True}
        store._write_json(f"{attempt.directory}/execution.json", outcome)
        return calculation.collect_result(store, current, step, attempt, outcome), outcome

    monkeypatch.setattr(book, "reserve_science", reserve)
    monkeypatch.setattr(electronic, "execute", offline_science)

    def append(data):
        assert data["AUTHORITY"]["related_results"] == [feedback.id]
        assert not store.load_run(run.id).attempts
        return {"action": "revise_plan", "parameters": {"steps": [
            {"key": analysis.id}, {"key": "new_science", "tool": "orca.sp", "depends_on": [analysis.id],
             "parameters": historical.attempts[0].frozen_step.parameters.model_dump()},
            {"key": "final_analysis", "tool": "analysis.energy_compare", "parameters": {"goal_id": goal.id},
             "inputs": {"A": ref.model_dump(exclude_none=True), "B": ref.model_dump(exclude_none=True),
                        "C": {"producer_key": "new_science", "port": "energy"}}}],
            "goal_map": {goal.id: {"step_key": "final_analysis", "port": goal.port}}}}

    def analyze(data):
        return {"action": "call_tool", "parameters": {"step_id": data["AUTHORITY"]["plan"]["goal_map"][goal.id]["step_id"]}}

    def crash(point):
        if point == "after_revision_saved":
            raise KeyboardInterrupt

    transport = ScriptedHTTP(append, analyze)
    interrupted = agent.execute(store, Config(), run.id, transport=transport, batch=book, fault=crash)
    assert interrupted.state == "unknown" and interrupted.plan_version == 1, interrupted.diagnostics
    assert not interrupted.attempts and not reservations and not callbacks
    assert interrupted.processed_feedback == []
    snapshot = None
    for _ in range(2):
        completed = agent.execute(store, Config(), run.id, transport=transport, batch=book, resume=True)
        assert completed.state == "completed", completed.diagnostics
        assert completed.goal_status == {goal.id: "satisfied"}
        assert completed.usage.plan_revisions == 1 and completed.plan_version == 2
        assert completed.processed_feedback.count(feedback.id) == 1
        assert completed.usage.orca_starts_reserved == completed.usage.extra_orca_starts_reserved == 1
        assert len(completed.attempts) == len(reservations) == len(callbacks) == 1
        assert completed.usage.model_calls == len(transport.sent) == 2
        current = book.snapshot()
        assert len(current["agent_science"]) == 1
        assert all(item["state"] == "known" and len(item["settlements"]) == 1
                   for item in current["agent_science"].values())
        if snapshot is not None:
            assert current == snapshot
        snapshot = current
    assert completed.deadline == run.deadline and not store.environment_lease()


def _offline_scientific_case(tmp_path, *, maxiter=100, model_calls=0, explain=False):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    geometry = store.import_artifact(REAL_SP / "geometry.xyz", "initial_geometry")
    request = Request(geometry_artifact_id=geometry.id,
        conditions={"explain_results": True} if explain else {},
        goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    step = Step(id="initial_sp", logical_id="water_energy", tool="orca.sp",
                geometry=InputRef(artifact_id=geometry.id), parameters={"scf_maxiter": maxiter, "timeout_seconds": 120})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"energy": OutputBinding(step_id=step.id, port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(scientific_execution=True, model_execution=True,
        allowed_repairs={"scf_maxiter": [2, 100]}, artifact_ids=[geometry.id]),
        BudgetLimits(attempts_per_step=2, orca_starts=2, extra_orca_starts=1, model_calls=model_calls,
            input_tokens=12000, output_tokens=2000, model_tokens=48000, decision_rounds=4, plan_revisions=2))
    run.agent_enabled = True
    store.save_run(run)
    store._write_json(f"runs/{run.id}/environment.json", {"orca": {"version": "6.1.1"}, "offline_fixture_only": True})
    return store, run, step, plan, geometry


def _archive_as_offline_attempt(store, run, step, attempt, fixture):
    directory = store.path(attempt.directory)
    for path in fixture.iterdir():
        if path.is_file():
            shutil.copyfile(path, directory / path.name)
    if not (directory / "input-manifest.json").exists():
        store._write_json(f"{attempt.directory}/input-manifest.json", {
            "tool": "orca.sp", "parameters": step.parameters.model_dump(mode="json"),
            "geometry_sha256": sha256_file(directory / "geometry.xyz"),
            "input_sha256": sha256_file(directory / "job.inp"),
            "input_file": "job.inp", "geometry_file": "geometry.xyz"})
    # Synthetic lifecycle receipt is explicitly distinguished from the archived
    # scientific text. Nothing is sent to an ORCA executable or native process.
    expected_failure = fixture.name in {"real_water_scf_limit", "scf-maxiter-2"}
    outcome = {"state": "failed" if expected_failure else "completed", "exit_code": 1 if expected_failure else 0,
               "reason": "nonzero_exit_code" if expected_failure else "process_tree_exited",
               "handle": {"offline_fixture_only": True},
               "resource_usage": {"wall_seconds": 1, "user_cpu_seconds": 1}, "offline_fixture_only": True}
    store._write_json(f"{attempt.directory}/execution.json", outcome)
    result = calculation.collect_result(store, run, step, attempt, outcome)
    assert ("energy" not in result.qualified_outputs) == expected_failure
    return result, outcome


@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_two_failed_attempts_then_new_logical_id_cannot_create_third_start(tmp_path, monkeypatch, repetition):
    store, run, _, _, _ = _offline_scientific_case(tmp_path, maxiter=1, model_calls=2)
    root = Path(__file__).resolve().parents[2]
    callbacks = []

    def offline_science(store, current, step, attempt, config, fault=None):
        callbacks.append(attempt.id)
        fixture = (root / "tests/fixtures/phase_a/real_water_scf_limit" if step.parameters.scf_maxiter == 1
                   else root / "tests/fixtures/phase_b/independent/scf-maxiter-2")
        return _archive_as_offline_attempt(store, current, step, attempt, fixture)

    def propose(data, *, launder=False):
        if launder:
            # After both attempts the transmitted purpose is terminal. The
            # adversarial response still tries a fresh identity; no Plan is
            # fabricated into the terminal projection to manufacture it.
            assert data["AUTHORITY"]["decision_purpose"]["allowed_actions"] == ["stop"]
            logical_key = "laundered_new_intent"
        else:
            logical_key = data["AUTHORITY"]["plan"]["steps"][0]["logical_id"]
        assert len(store.load_run(run.id).attempts) == (2 if launder else 1)
        return {"action": "revise_plan", "parameters": {"steps": [{"key": "renamed_third" if launder else "repair",
            "logical_key": logical_key, "tool": "orca.sp",
            "parameters": {"timeout_seconds": 120, "scf_maxiter": 100 if launder else 2}}],
            "goal_map": {"energy": {"step_key": "renamed_third" if launder else "repair", "port": "energy"}}}}

    monkeypatch.setattr(electronic, "execute", offline_science)
    transport = ScriptedHTTP(propose, lambda data: propose(data, launder=True))
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert len(callbacks) == len(stopped.attempts) == 2, stopped.diagnostics
    assert [a.frozen_step.parameters.scf_maxiter for a in stopped.attempts] == [1, 2]
    assert stopped.usage.orca_starts_reserved == stopped.usage.orca_starts_actual == 2
    assert stopped.usage.logical_attempts == {"water_energy": 2}
    assert stopped.usage.extra_orca_starts_reserved == 1 and stopped.usage.plan_revisions == 1
    assert stopped.plan_version == 2 and any(d.get("action") == "rejected" for d in stopped.decisions)
    assert all(not store.load_result(run.id, a.result_id).qualified_outputs for a in stopped.attempts)
    assert not store.environment_lease()


@pytest.mark.parametrize("repetition", (1, 2, 3))
def test_zero_model_budget_relinks_qualified_orphan_and_reports_without_resending(tmp_path, monkeypatch, repetition):
    store, run, step, _, geometry = _offline_scientific_case(tmp_path, explain=True)
    attempt = store.reserve_attempt(run, step, geometry.id)
    result, _ = _archive_as_offline_attempt(store, run, step, attempt, REAL_SP)
    assert "energy" in result.qualified_outputs
    store.save_result(result)  # Crash boundary: Result is durable; Run/Attempt is not linked.
    original = store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes()
    assert store.load_run(run.id).attempts[0].result_id is None
    assert not store.load_run(run.id).result_ids
    monkeypatch.setattr(calculation, "collect_result", lambda *a, **k: pytest.fail("orphan Result was collected twice"))
    monkeypatch.setattr(electronic, "execute", lambda *a, **k: pytest.fail("unexpected scientific execution"))
    transport = ScriptedHTTP()
    usage = None
    for _ in range(2):
        recovered = agent.execute(store, Config(), run.id, transport=transport, resume=True)
        assert recovered.state == "budget_exhausted"
        assert recovered.goal_status == {"energy": "satisfied"}
        assert recovered.result_ids == [result.id] and recovered.attempts[0].result_id == result.id
        assert recovered.usage.model_calls == len(transport.sent) == 0
        assert recovered.usage.orca_starts_reserved == recovered.usage.orca_starts_actual == 1
        assert recovered.deadline == run.deadline
        if usage is not None:
            assert recovered.usage == usage
        usage = recovered.usage.model_copy(deep=True)
        report = build_report(store, recovered)
        assert report["user_goal_complete"] and report["run_state"] == "budget_exhausted"
        assert report["results"][0]["qualified_outputs"]["energy"]["value"] == result.qualified_outputs["energy"].value
        assert isinstance(render_report(report), str)
    assert store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes() == original
    assert not store.environment_lease()


def test_agent_loads_only_effect_permitted_schemas_without_changing_permission(tmp_path):
    from orca_agent.context import build_context

    store, original, artifact = query_case(tmp_path)
    permission = PermissionSnapshot(model_execution=True, scientific_execution=False, artifact_writes=False,
        allowed_tools=["orca.sp", "analysis.energy_compare", "evidence.import", "evidence.value"],
        artifact_ids=[artifact.id])
    request = store.load_request(original)
    run = store.create_run(request, None, permission, original.budget)
    run.agent_enabled = True
    store.save_run(run)
    for denied in ("orca.sp", "analysis.energy_compare", "evidence.import"):
        with pytest.raises(ValueError, match="outside current permission"):
            build_context(request, run, relevant_tools=[denied])
    transport = ScriptedHTTP(initial_proposal)
    completed = agent.execute(store, Config(), run.id, transport=transport)
    assert completed.state == "completed", completed.diagnostics
    assert len(transport.sent) == completed.usage.model_calls == completed.usage.evidence_reads == 1
    context = transport.sent[0]
    assert [tool["name"] for tool in context["TOOL_CATALOG"]] == ["evidence.value"]
    assert len(context["PARAMETER_SCHEMAS"]) == 1
    assert context["AUTHORITY"]["permission"]["allowed_tools"] == permission.allowed_tools
    assert completed.permission == permission and not completed.attempts and not store.environment_lease()


@pytest.mark.parametrize("fault", ("source_conflict", "memory_limit", "timeout", "cancelled", "postprocess_budget",
                                  "parse_error", "missing_execution", "input_integrity", "parser_consistency",
                                  "changed_manifest", "changed_artifact"))
def test_scf_banner_never_overrides_integrity_resource_or_missing_evidence_failure(tmp_path, fault):
    store, run, step, plan, geometry = _offline_scientific_case(tmp_path, maxiter=1, model_calls=2)
    attempt = store.reserve_attempt(run, step, geometry.id)
    fixture = Path(__file__).resolve().parents[2] / "tests/fixtures/phase_a/real_water_scf_limit"
    result, outcome = _archive_as_offline_attempt(store, run, step, attempt, fixture)
    assert any(d.get("category") == "scf_not_converged" for d in result.diagnostics)
    if fault in {"source_conflict", "parse_error"}:
        result.diagnostics.append({"category": fault, "detail": "explicit offline conflicting fact"})
    elif fault == "memory_limit":
        result.source["execution"]["reason"] = "job_memory_limit_exceeded"
    elif fault in {"timeout", "cancelled"}:
        state = "timed_out" if fault == "timeout" else "cancelled"
        outcome["state"] = result.operation_status = result.source["execution"]["state"] = state
        result.source["execution"]["reason"] = "deadline_exceeded" if fault == "timeout" else "cancel_requested"
    elif fault == "postprocess_budget":
        result.source["execution"]["budget_violations"] = [{"category": "postprocess_budget_exceeded"}]
    elif fault == "missing_execution":
        result.source.pop("execution")
    elif fault in {"input_integrity", "parser_consistency"}:
        next(c for c in result.checks["energy"] if c.name == fault).status = "failed"
    elif fault == "changed_manifest":
        result.source["files"]["stdout.out"]["sha256"] = "0" * 64
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state=outcome["state"], result_id=result.id,
                         started=False, termination_confirmed=True)
    if fault == "changed_artifact":
        store.artifact_path(result.source["files"]["stdout.out"]["artifact_id"]).write_text("changed evidence")
    repair = plan.model_copy(deep=True, update={"version": 2})
    repair.steps[0].id = "repair"
    repair.steps[0].parameters.scf_maxiter = 2
    repair.goal_map["energy"].step_id = "repair"
    before = run.model_dump()
    with pytest.raises(StoreError):
        agent._repair_evidence(store, run, repair)
    assert run.model_dump() == before and len(run.attempts) == 1


def test_authenticated_retarget_is_distinct_from_repair_of_old_resource_failure(tmp_path):
    store, run, step, _, geometry = _offline_scientific_case(tmp_path, maxiter=1, model_calls=2)
    attempt = store.reserve_attempt(run, step, geometry.id)
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="failed",
                    diagnostics=[{"category": "failed", "reason": "job_memory_limit_exceeded"}])
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="failed", result_id=result.id, termination_confirmed=True)
    message = store.enqueue_message(run.id, "Use the explicitly confirmed registered geometry")
    current = apply_user_update(store, run.id, message, {"conditions": {"user_confirmed_geometry": geometry.id}})
    replacement = step.model_copy(deep=True, update={"id": "new_user_step"})
    plan = Plan(request_id=run.request_id, request_version=current.request_version, steps=[replacement],
                goal_map={"energy": OutputBinding(step_id=replacement.id, port="energy")})
    # The user-purpose and Plan validators still run separately; this check only
    # avoids reclassifying a trusted new Request as a model's MaxIter SCF repair.
    agent._repair_evidence(store, current, plan)
    assert not any(d.get("category") == "scf_not_converged" for d in result.diagnostics)
