"""Independent report boundaries using archived files, without any execution."""

import json

import pytest

from orca_agent.delivery import collect_goal_selections, delivery_snapshot
from orca_agent.models import (
    Attempt,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    RunConfiguration,
    Step,
)
from orca_agent.report import build_report, render_report
from orca_agent.store import Store
from orca_agent.task_observations import assess, collect


def archived_report(tmp_path, monkeypatch, *, reported, unknown):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda name: None)
    configuration = RunConfiguration(
        observation_delivery_policy="reported-observation-1" if reported else None)
    store = Store(tmp_path / "data", configuration=configuration)
    code_path = tmp_path / "task.py"
    code_path.write_text("# Never executed: archived report fixture.\n", encoding="utf-8")
    code = store.import_artifact(code_path, "code")
    requirement = "至少保存8份候选结构" if unknown else "purpose_preserved@1"
    goal = Goal(id="candidates", port="candidate_structures", original_text=requirement,
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations", minimum_evidence=[requirement],
        minimum_evidence_source={requirement: "explicit"})
    request = Request(original_text=requirement, method=None, basis=None, charge=None, multiplicity=None,
                      conditions_source={}, goals=[goal])
    step = Step(id="report", logical_id="report", tool="python.execute", parameters={
        "code": {"artifact_id": code.id, "sha256": code.sha256},
        "observation_names": [goal.port]})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)})
    run = store.create_run(request, plan, PermissionSnapshot(
        code_execution=True, artifact_writes=True, allowed_tools=["python.execute"], artifact_ids=[code.id]))
    attempt = Attempt(step_id=step.id, logical_id=step.logical_id, number=1, tool=step.tool,
        execution_kind="python", input_fingerprint="c" * 64, directory="offline-report-fixture",
        request_version=request.version, frozen_step=step, state="completed")
    run.attempts.append(attempt)
    files = {}
    # Deliberately no candidate is present. Reporting this fact must never
    # claim that the user's explicit count was verified or the goal achieved.
    for name, content in {
        "summary.json": json.dumps({"candidate_count": 0}),
        "observations.json": json.dumps({"version": 1, "observations": [{
            "name": goal.port, "value": {"candidate_count": 0},
            "sources": [{"filename": "summary.json"}], "artifacts": ["summary.json"]}]}),
    }.items():
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        artifact = store.import_artifact(path, "task_output", run_id=run.id, attempt_id=attempt.id)
        files["task/" + name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="completed",
        artifact_ids=[item["artifact_id"] for item in files.values()],
        observations={"evidence_collection_complete": True},
        source={"files": files, "code_sha256": code.sha256},
        observation_records=collect(store, run, step, attempt, files))
    # This is an archived fixture, not a fabricated execution lifecycle.
    store._write_json(f"runs/{run.id}/results/{result.id}.json", result, immutable=True)
    attempt.result_id = result.id
    run.result_ids.append(result.id)
    run.goal_status[goal.id] = "satisfied"
    run.state, run.delivery_status = "completed", "complete"
    return store, run, request, goal, result


def test_reported_delivery_never_claims_unknown_explicit_count_was_achieved(tmp_path, monkeypatch):
    store, run, request, goal, result = archived_report(tmp_path, monkeypatch, reported=True, unknown=True)
    report = build_report(store, run)
    assert report["report_delivery_status"] == "complete"
    assert report["reported_delivery_complete"] is True
    assert report["user_goal_complete"] is False
    assert report["goals"][0]["unverified_user_obligations"][0]["requested"] == "至少保存8份候选结构"
    rendered = render_report(report)
    assert "用户目标完成：是" not in rendered
    assert "未验证" in rendered.splitlines()[2]
    assert "至少保存8份候选结构" in rendered
    selections = collect_goal_selections(store, run, request, store.load_plan(run))
    snapshot = delivery_snapshot(request, run, selections)
    row, = snapshot["goals"]
    assert row["goal_complete"] is True  # Report completion is still permitted.
    assert row["user_obligations_verified"] is False
    assert row["goal_completion_basis"] == "reported_delivery"
    assert snapshot["explanations"][0]["relation"] == "reported_observation_delivered"
    assert not result.qualified_outputs


def test_legacy_report_with_verified_contract_keeps_existing_completion(tmp_path, monkeypatch):
    store, run, _, _, _ = archived_report(tmp_path, monkeypatch, reported=False, unknown=False)
    report = build_report(store, run)
    assert report["report_delivery_status"] == "complete"
    assert report["user_goal_complete"] is True
    assert "用户目标完成：是" in render_report(report)
    assert "observation_delivery_policy" not in run.configuration.model_dump(mode="json")
    assert "verification_basis" not in report["goals"][0]


def test_legacy_run_without_configuration_never_opts_into_unknown_delivery(tmp_path, monkeypatch):
    store, run, request, goal, result = archived_report(tmp_path, monkeypatch, reported=False, unknown=True)
    run.configuration = None
    before = run.model_dump(mode="json")
    assessment = assess(store, run, request, goal, result)
    assert assessment["status"] == "unresolved"
    assert assessment["minimum_evidence"][0]["status"] == "unresolved"
    assert "verification_basis" not in assessment
    report = build_report(store, run)
    assert report["report_delivery_status"] == "partial" and report["user_goal_complete"] is False
    assert run.model_dump(mode="json") == before


def test_changed_source_cannot_be_described_as_completed_observation_delivery(tmp_path, monkeypatch):
    store, run, _, _, result = archived_report(tmp_path, monkeypatch, reported=True, unknown=True)
    source = result.source["files"]["task/summary.json"]["artifact_id"]
    store.artifact_path(source).write_text("changed source bytes", encoding="utf-8")
    report = build_report(store, run)
    assert report["report_delivery_status"] == "partial"
    assert report["reported_delivery_complete"] is False and report["user_goal_complete"] is False
    assert report["goals"][0]["report_status"] == "insufficient_evidence"
    assert "仅完成有来源观察的交付" not in render_report(report)


@pytest.mark.parametrize("requirement", [
    "不要运行ORCA优化，但必须完成真实ORCA单点能量计算。",
    "Do not run ORCA optimization, but perform a real ORCA single-point calculation.",
])
def test_prohibited_extra_calculation_does_not_waive_required_real_calculation(
        tmp_path, monkeypatch, requirement):
    store, run, request, goal, result = archived_report(tmp_path, monkeypatch, reported=True, unknown=True)
    goal.original_text = requirement
    goal.minimum_evidence = [requirement]
    assessment = assess(store, run, request, goal, result)
    assert not run.internal_calls
    assert assessment["status"] == "unresolved", assessment
    assert "explicit_native_execution_requires_native_source" in assessment["reasons"]
