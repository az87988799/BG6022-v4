"""Offline feedback and recovery checks; no model or scientific process is called."""

import json

import pytest

from orca_agent import agent, runner
from orca_agent.config import Config
from orca_agent.context import _task_progress
from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    InternalCall,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
    utc_now,
)
from orca_agent.tools import managed
from tests.unit.test_agent import ScriptedTransport


def checkpoint_case(tmp_path, monkeypatch, *, needs_more=False, state="completed"):
    from orca_agent.store import Store

    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda name: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    code_file = tmp_path / "task.py"
    code_file.write_text("# Inert offline feedback fixture; never executed.\n", encoding="utf-8")
    code = store.import_artifact(code_file, "code")
    reference = CodeRef(artifact_id=code.id, sha256=code.sha256)
    request = Request(original_text="Deliver the requested file with its evidence and limitations.",
        conditions={"explain_results": True}, conditions_source={name: "default" for name in
            ("method", "basis", "charge", "multiplicity", "geometry")},
        goals=[Goal(id="report", port="artifacts", minimum_check_version="generic-observation-1",
            evidence_contract_version="generic-observation-1", verification="reported_with_limitations",
            minimum_evidence=["purpose_preserved@1"])])
    step = Step(id="first_batch", logical_id="first_batch", tool="python.execute",
                parameters={"code": reference, "observation_names": ["artifacts"]})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"report": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(code_execution=True,
        artifact_writes=True, model_execution=True, artifact_ids=[code.id], allowed_tools=["python.execute"]),
        BudgetLimits(model_calls=4, model_tokens=100000, input_tokens=24000, output_tokens=2000,
            decision_rounds=4, plan_revisions=2, code_retries=2, corrections_per_proposal=0),
        configuration=Config().run_configuration())
    run.agent_enabled = True
    store.save_run(run)
    attempt = store.reserve_attempt(run, step)
    checkpoint = store.save_task_checkpoint(run, attempt, "batch_one",
        {"uncertainty": 0.8 if needs_more else 0.01, "candidate_count": 2},
        requires_decision=True, reason="Assess observations before choosing another batch.")
    directory = store.path(attempt.directory) / "task"
    directory.mkdir(exist_ok=True)
    (directory / "report.txt").write_text("synthetic observed file", encoding="utf-8")
    (directory / "observations.json").write_text(json.dumps({"version": 1, "observations": [
        {"name": "artifacts", "value": None if needs_more else "synthetic observed file",
         "sources": [{"filename": "report.txt"}]}]}), encoding="utf-8")
    execution = {"state": state, "exit_code": 0 if state == "completed" else 1,
                 "resource_usage": {}, "reason": "offline fixture; no process launched"}
    store._write_json(f"{attempt.directory}/execution.json", execution, immutable=True)
    result = managed.collect_result(store, run, step, attempt, execution)
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state=state, result_id=result.id,
                         started=False, termination_confirmed=True)
    return store, store.load_run(run.id), step, result, checkpoint


@pytest.mark.parametrize("needs_more,expected", [(False, "stop"), (True, "plan")])
def test_paired_observations_choose_stop_or_a_new_continuation_step(
        tmp_path, monkeypatch, needs_more, expected):
    store, run, step, result, checkpoint = checkpoint_case(tmp_path, monkeypatch, needs_more=needs_more)
    runner._goals(store, run, store.load_plan(run), {step.id: result})
    store.save_run(run)

    def assess(data):
        rows = data["DATA"]["task_checkpoints"]["rows"]
        assert rows[0]["sha256"] == checkpoint.sha256
        assert rows[0]["source_attempt_id"] == result.attempt_id
        assert data["AUTHORITY"]["related_results"] == [result.id]
        assert data["DATA"]["trust"] == "untrusted"
        if rows[0]["state"]["uncertainty"] <= 0.05:
            assert store.load_run(run.id).goal_status["report"] == "satisfied"
            return {"action": "stop", "parameters": {"reason": "Evidence suffices; no additional work."}}
        assert data["AUTHORITY"]["goal_status"]["report"] == "insufficient_evidence"
        return {"action": "revise_plan", "parameters": {
            "steps": [{"key": step.id}, {"key": "next_batch", "tool": "python.execute",
                "parameters": {"code": step.parameters.code.model_dump(), "observation_names": ["artifacts"]},
                "depends_on": [step.id]}],
            "goal_map": {"report": {"step_key": "next_batch", "port": "artifacts"}}}}

    transport = ScriptedTransport(assess)
    run, action, _ = agent._decision(store, run, store.load_plan(run), {step.id: result},
                                      transport, None, None)
    assert action == expected
    assert run.processed_feedback == [result.id]
    assert run.usage.model_calls == len(transport.sent) == 1
    assert len(run.attempts) == 1 and run.usage.orca_starts_reserved == 0
    assert run.task_checkpoints == [checkpoint]
    plan = store.load_plan(run)
    assert plan.version == (2 if needs_more else 1)
    assert len(plan.steps) == (2 if needs_more else 1)
    assert not result.qualified_outputs


@pytest.mark.parametrize("state", ["failed", "completed"])
def test_explicit_resume_never_replays_a_settled_python_attempt(tmp_path, monkeypatch, state):
    store, run, _, _, checkpoint = checkpoint_case(tmp_path, monkeypatch, state=state)
    run.agent_enabled = False
    store.save_run(run)
    before = run.usage.model_dump()
    monkeypatch.setattr(managed, "execute", lambda *a, **k: pytest.fail("resume replayed task Python"))
    for _ in range(2):
        recovered = runner.execute(store, Config(), run.id, resume=True)
        assert len(recovered.attempts) == 1
        assert recovered.task_checkpoints == [checkpoint]
        assert recovered.usage.model_dump() == before
        assert store.environment_lease() is None


def test_progress_bounds_declared_state_but_keeps_every_internal_request_identity(tmp_path, monkeypatch):
    _, run, _, _, checkpoint = checkpoint_case(tmp_path, monkeypatch)
    run.task_checkpoints = [checkpoint.model_copy(update={"name": f"batch_{n}",
        "state": {"large": "x" * 5000, "private_path": "E:/private/file"}}) for n in range(12)]
    run.internal_calls = [InternalCall(request_key=f"candidate_{n}", input_fingerprint=f"{n:064x}",
        parent_attempt_id=run.attempts[0].id, state="completed" if n else "unknown",
        finished_at=utc_now() if n else None) for n in range(10)]
    before = run.model_dump_json()
    progress = _task_progress(run, 256)
    checkpoints = progress["task_checkpoints"]
    assert len(checkpoints["rows"]) == 8 and checkpoints["omitted_older"] == 4
    assert len(checkpoints["omitted_older_sha256"]) == 64
    assert all(row["state"]["omitted"] == "bounded query required" for row in checkpoints["rows"])
    assert len(progress["internal_call_summaries"]) == 10
    assert progress["internal_call_summaries"][0]["state"] == "unknown"
    assert "E:/private" not in json.dumps(progress)
    assert run.model_dump_json() == before


@pytest.mark.parametrize("altered", [False, True])
def test_resume_recovers_checkpoint_file_before_run_publication_without_task_replay(
        tmp_path, monkeypatch, altered):
    store, run, _, _, checkpoint = checkpoint_case(tmp_path, monkeypatch)
    run.agent_enabled = False
    store.save_run(run)
    # Exact persistence window: immutable checkpoint exists, while Run still
    # contains its preceding snapshot. This is fixture fault injection only.
    path = store.path(f"runs/{run.id}/run.json")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["task_checkpoints"] = []
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    if altered:
        record = store.path(f"{run.attempts[0].directory}/checkpoints/{checkpoint.name}.json")
        value = json.loads(record.read_text(encoding="utf-8"))
        value["state"]["candidate_count"] = 999
        record.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ValueError, match="checkpoint hash"):
            runner._recover_task_checkpoints(store, store.load_run(run.id), run.attempts[0])
        assert not store.load_run(run.id).task_checkpoints
        return
    monkeypatch.setattr(managed, "execute", lambda *a, **k: pytest.fail("checkpoint recovery replayed Python"))
    for _ in range(2):
        recovered = runner.execute(store, Config(), run.id, resume=True)
        assert recovered.task_checkpoints == [checkpoint]
        assert len(recovered.attempts) == 1 and recovered.usage.model_calls == 0


def reused_source_case(tmp_path):
    from orca_agent.models import Attempt, Result
    from orca_agent.task_observations import collect
    from tests.unit.test_task_observation_conditions import archived_case

    store, run, request, goal, original = archived_case(tmp_path,
        run_type="optimization_and_analytic_frequencies", final_geometry=True)
    goal.conditions["geometry_relation"] = "optimized"
    parent = run.attempts[0]
    parent.finished_at = utc_now()
    receipt = run.internal_calls[0]
    receipt.finished_at = utc_now()
    native_path = tmp_path / "native.inp"
    native_path.write_text("! HF 6-31G* Opt Freq\n", encoding="utf-8")
    native = store.import_artifact(native_path, "native_input", run_id=run.id, attempt_id=parent.id)
    receipt.code = CodeRef(artifact_id=native.id, sha256=native.sha256, language="orca")
    step = parent.frozen_step.model_copy(deep=True, update={"id": "continuation", "logical_id": "continuation"})
    attempt = Attempt(step_id=step.id, logical_id=step.logical_id, number=1, tool=step.tool,
        execution_kind="python", input_fingerprint="e" * 64, directory="offline-continuation",
        request_version=request.version, frozen_step=step, state="completed", finished_at=utc_now())
    run.attempts.append(attempt)
    declaration = store.load_artifact(original.source["files"]["task/observations.json"]["artifact_id"])
    manifest = store.import_artifact(store.artifact_path(declaration.id), "task_output", run_id=run.id,
                                      attempt_id=attempt.id)
    files = {"task/observations.json": {"artifact_id": manifest.id, "sha256": manifest.sha256}}
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="completed",
        artifact_ids=[manifest.id], observations={"evidence_collection_complete": True},
        source={"files": files, "code_sha256": step.parameters.code.sha256},
        observation_records=collect(store, run, step, attempt, files))
    attempt.result_id = result.id
    run.result_ids.append(result.id)
    return store, run, request, goal, result


def test_reused_completed_source_keeps_original_attempt_and_geometry_binding(tmp_path):
    from orca_agent.task_observations import assess

    store, run, request, goal, result = reused_source_case(tmp_path)
    original_id = run.attempts[0].id
    assert result.attempt_id != original_id
    assert result.observation_records[0].sources[0].attempt_id == original_id
    assert store.load_artifact(result.observation_records[0].sources[0].artifact_id).attempt_id == original_id
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False


@pytest.mark.parametrize("change", ["request_version", "parent_unknown", "native_hash", "unfinished_call"])
def test_reused_source_rejects_changed_or_unsettled_original_provenance(tmp_path, change):
    from orca_agent.task_observations import assess

    store, run, request, goal, result = reused_source_case(tmp_path)
    if change == "request_version":
        run.attempts[0].request_version += 1
    elif change == "parent_unknown":
        run.attempts[0].state = "unknown"
    elif change == "native_hash":
        store.artifact_path(run.internal_calls[0].code.artifact_id).write_text("changed", encoding="utf-8")
    else:
        run.internal_calls[0].finished_at = None
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved" and answer["reasons"]
    assert answer["scientific_qualification"] is False
