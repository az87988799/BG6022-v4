"""Offline sampling counterexamples joined to the production deterministic report.

XYZ/stdout fixtures are read without modification. Source bindings, ToolCall and
Result are explicitly synthetic test records; no scientific Attempt, model
request, historical Store mutation, or process execution is created.
"""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_analysis import MANIFEST, PROJECT, sampling_case

from orca_agent import runner
from orca_agent.models import (
    Artifact,
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    Run,
    Step,
    ToolCall,
    fingerprint,
)
from orca_agent.report import build_report, render_report
from orca_agent.tools.analysis import finite_sampling


def _sampling_report_case(variant, repetition):
    candidates, members, geometry_bytes, parameters = sampling_case("stop")
    window = next(window for window in MANIFEST["windows"] if window["id"] == "stop")
    artifacts, source_hashes = {}, {}

    def register(identity, path, role):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        source_hashes[path] = digest
        artifacts[identity] = Artifact(id=identity, path=str(path), sha256=digest,
            size=path.stat().st_size, role=role, source={"test_usage": "read_only_archived_fixture"})
        return digest

    for candidate, item, member in zip(candidates, window["candidates"], members, strict=True):
        geometry_hash = register(candidate.artifact_id, PROJECT / item["path"], "sampling_candidate")
        assert geometry_hash == candidate.sha256
        if member.evidence:
            member.evidence.run_id = "offline_source_run_" + candidate.id
            member.evidence.attempt_id = "offline_source_attempt_" + candidate.id
            member.evidence.result_id = "offline_source_result_" + candidate.id
            member.evidence.artifact_hashes = {candidate.artifact_id: geometry_hash}
            if variant == "missing-required-point":
                path = PROJECT / "tests/fixtures/phase_b/independent" / item["reference_id"] / "stdout.out"
                raw_hash = register("stdout_" + candidate.id, path, "archived_reference_stdout")
                numbers = re.findall(rb"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", path.read_bytes())
                assert numbers and member.evidence.energy_eh == pytest.approx(float(numbers[-1]), abs=1e-12)
                member.evidence.artifact_hashes["stdout_" + candidate.id] = raw_hash

    required = [member for member in members if member.required]
    if variant == "boundary-minimum":
        # The frozen counterexample explicitly injects monotone energies; these
        # numbers are not presented as the archived ORCA fixture's energies.
        for index, member in enumerate(required):
            member.evidence.energy_eh = -75.0 + 0.1 * index
        absent = None
        expected_reason = "boundary_minimum"
    else:
        absent = next(member for member in required if member.id.endswith("-center"))
        absent.evidence = None
        absent.missing_reason = "required_center_energy_not_available"
        expected_reason = "missing_required_energy"
    summary = finite_sampling(candidates, members, geometry_bytes, parameters)
    assert summary["reason"] == expected_reason and not summary["qualified_outputs"]
    goal = Goal(id="finite_sampling", port="sampling", minimum_check_version="finite-sampling-1",
        original_text="An interior minimum in the sampled discrete geometries with qualified neighbors.",
        conditions={"sampling": parameters.model_dump(), "candidates": [c.model_dump() for c in candidates]})
    request = Request(id=f"offline_request_{repetition}", goals=[goal],
        original_text="Report finite sampling and any missing required candidate without a continuous-minimum claim.",
        conditions={"test_evidence_kind": "offline_fault_injection",
                    "test_energy_mode": "injected_monotone" if absent is None else "replayed_archived_values"})
    step = Step(id="offline_analysis_step", logical_id="offline_analysis", tool="analysis.finite_sampling",
                parameters={"goal_id": goal.id})
    plan = Plan(request_id=request.id, steps=[step], goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)})
    run = Run(id=f"offline_sampling_run_{repetition}", request_id=request.id, request_version=1,
        plan_id=plan.id, plan_version=plan.version, state="failed",
        permission=PermissionSnapshot(scientific_execution=False), budget=BudgetLimits(orca_starts=0, model_calls=0))
    result = Result(id="offline_sampling_result", run_id=run.id, step_id=step.id,
        call_id="offline_analysis_call", operation_status="completed", checks={"sampling": summary["checks"]},
        observations={"analysis": summary}, artifact_ids=list(artifacts),
        source={"evidence_kind": "offline_fault_injection", "conditions": {
            "method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1},
            "artifact_hashes": {key: artifact.sha256 for key, artifact in artifacts.items()}})
    run.calls = [ToolCall(id=result.call_id, tool=step.tool, parameters={"goal_id": goal.id},
        step_id=step.id, state="completed", result_id=result.id, request_version=1,
        plan_version=plan.version, frozen_step=step)]
    run.result_ids = [result.id]
    run.selected_results = {step.id: result.id}

    def artifact_path(identity):
        artifact = artifacts[identity]
        path = Path(artifact.path)
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256
        return path

    store = SimpleNamespace(load_run=lambda _: run, load_request=lambda _: request, load_plan=lambda _: plan,
        load_result=lambda *_: result, load_artifact=artifacts.__getitem__, artifact_path=artifact_path)
    assert not runner._goals(store, run, plan, {step.id: result})
    return SimpleNamespace(store=store, run=run, summary=summary, result=result, goal=goal,
                           absent=absent.id if absent else None, source_hashes=source_hashes)


def _assert_partial(case):
    before = fingerprint(case.result)
    report = build_report(case.store, case.run)
    assert report == build_report(case.store, case.run.id)
    assert not report["user_goal_complete"] and report["report_delivery_status"] == "partial"
    assert report["goals"][0]["report_status"] == "insufficient_evidence"
    recorded = report["results"][0]
    assert recorded["operation_status"] == "completed" and not recorded["qualified_outputs"]
    assert not recorded["gaps"]  # Partial science, rather than a missing/hash-broken fixture.
    assert recorded["observations"]["scientific_qualification"] is False
    rows = recorded["observations"]["data"]["analysis"]["members"]
    assert len(rows) == 5
    assert [row["member_id"] for row in rows] == [row["member_id"] for row in case.summary["members"]]
    text = render_report(report)
    for row in rows:
        assert row["member_id"] in text
        if row["energy_eh"] is not None:
            assert row["source"]["unit"] == "Eh"
            assert f"{row['energy_eh']:.15g} Eh" in text
    assert "no continuous or global minimum" in text
    assert "Only the sampled discrete range" in text
    assert "offline_fault_injection" in text and "已验证 sampling" not in text
    assert not case.run.attempts and case.run.usage.orca_starts_actual == case.run.usage.model_calls == 0
    assert fingerprint(case.result) == before
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in case.source_hashes.items())
    return report, text, rows


@pytest.mark.parametrize("repetition", [1, 2, 3])
def test_boundary_minimum_keeps_partial_discrete_sampling_report(repetition):
    case = _sampling_report_case("boundary-minimum", repetition)
    assert case.summary["reason"] == "boundary_minimum" and not case.summary["goal_satisfied"]
    assert case.summary["observed_lowest_candidate_id"] == case.summary["sampled_candidate_ids"][0]
    assert not case.summary.get("left_neighbor_id") and not case.summary.get("right_neighbor_id")
    _, text, _ = _assert_partial(case)
    assert "boundary_minimum" in text


@pytest.mark.parametrize("repetition", [1, 2, 3])
def test_missing_required_center_is_named_without_complete_sampling_claim(repetition):
    case = _sampling_report_case("missing-required-point", repetition)
    assert case.absent == "stop-center" and not case.summary["goal_satisfied"]
    assert case.absent in case.summary["unsampled_candidate_ids"]
    _, text, rows = _assert_partial(case)
    missing = next(row for row in rows if row["member_id"] == case.absent)
    assert missing["required"] and missing["status"] == "missing" and missing["energy_eh"] is None
    assert missing["missing_reason"] == "required_center_energy_not_available"
    assert case.absent in text and missing["missing_reason"] in text
    assert "missing_required_energy" in text


def test_sampling_report_hides_member_numbers_when_an_actual_source_is_unverifiable():
    case = _sampling_report_case("boundary-minimum", 1)
    actual_read = case.store.artifact_path
    first = case.result.artifact_ids[0]

    def unreadable(identity):
        if identity == first:
            raise ValueError("offline injected verification failure; original bytes are untouched")
        return actual_read(identity)

    case.store.artifact_path = unreadable
    report = build_report(case.store, case.run)
    text = render_report(report)
    assert not report["user_goal_complete"]
    assert report["results"][0]["gaps"] == ["source_unverified:" + first]
    for row in case.summary["members"]:
        assert row["member_id"] in text
        if row["energy_eh"] is not None:
            assert f"{row['energy_eh']:.15g} Eh" not in text
    assert "source_unverified" in text and "status=qualified" not in text
    assert "有限采样目标检查：未核验" in text
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in case.source_hashes.items())
