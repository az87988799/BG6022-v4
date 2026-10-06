"""Offline grader fixtures reuse archived bytes; no model or process is started."""

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.models import CalculationParameters, Check, QualifiedOutput, Result, fingerprint
from orca_agent.store import sha256_file
from tests.helpers import phase_b_grade_joint as grade

PROJECT = Path(__file__).resolve().parents[2]
RAW = PROJECT / "tests/fixtures/phase_a/real_water_sp"


class OfflineStore:
    def __init__(self, root):
        self.root = root
        self.results, self.artifacts = {}, {}
        self.run = SimpleNamespace(id="run_offline_grader", attempts=[], result_ids=[], selected_results={},
            goal_status={"result_goal": "satisfied"}, state="completed", batch_category="formal",
            usage=SimpleNamespace(orca_starts_reserved=0, orca_starts_actual=0, postprocess_starts=0),
            model_records=[], decisions=[], calls=[])
        self.plan = SimpleNamespace(goal_map={"sampling_goal": SimpleNamespace(step_id="analysis", port="sampling")})

    def path(self, relative):
        return self.root / relative

    def load_run(self, identity):
        assert identity == self.run.id
        return self.run

    def load_result(self, identity, result_id):
        assert identity == self.run.id
        return self.results[result_id]

    def load_artifact(self, artifact_id):
        return self.artifacts[artifact_id]

    def artifact_path(self, artifact_id):
        artifact = self.artifacts[artifact_id]
        if sha256_file(artifact.path) != artifact.sha256:
            raise ValueError("offline fixture hash changed")
        return artifact.path

    def load_plan(self, _):
        return self.plan

    def add_artifact(self, path, attempt_id=None, role="raw_evidence"):
        identity = "artifact_" + str(len(self.artifacts))
        artifact = SimpleNamespace(id=identity, sha256=sha256_file(path), path=path,
                                   run_id=self.run.id, attempt_id=attempt_id, role=role)
        self.artifacts[identity] = artifact
        return artifact


def checks(names, version="orca-hf-2"):
    return [Check(name=name, status="passed", rule_version=version) for name in sorted(names)]


def add_attempt(store, fixture, *, scf_maxiter=100, logical_id=None, number=1):
    index = len(store.run.attempts)
    identity = f"attempt_{index}"
    directory = store.root / identity
    directory.mkdir(parents=True)
    names = ["stdout.out", "job.inp", "geometry.xyz"]
    for name in names:
        shutil.copyfile(fixture / name, directory / name)
    execution = {"state": "completed", "postprocess_starts_detected": 0,
                 "handle": {"pid": 123, "create_time": 123.0, "atomic_job_assignment": True},
                 "resource_usage": {"active_processes": 0, "cores": 4,
                     "job_commit_limit_bytes": 1073741824, "peak_job_commit_bytes": 1000},
                 "offline_fixture_only": True}
    (directory / "execution.json").write_text(json.dumps(execution), encoding="utf-8")
    names.append("execution.json")
    initial = store.add_artifact(directory / "geometry.xyz")
    params = CalculationParameters(scf_maxiter=scf_maxiter)
    input_fingerprint = fingerprint({"tool": "orca.sp", "parameters": params.model_dump(),
                                     "geometry_hash": initial.sha256})
    attempt = SimpleNamespace(id=identity, result_id=f"result_{index}", step_id=f"step_{index}",
        logical_id=logical_id or f"logical_{index}", number=number, started=True, state="completed",
        finished_at="offline-completion", geometry_artifact_id=initial.id, tool="orca.sp",
        input_fingerprint=input_fingerprint, frozen_step=SimpleNamespace(parameters=params), plan_version=1)
    files = {name: store.add_artifact(directory / name, identity) for name in names}
    raw = grade.independent_output(directory / "stdout.out")
    qualified = {}
    raw_checks = checks(grade.ENERGY_CHECKS)
    if raw["status"] == "converged":
        qualified["energy"] = QualifiedOutput(value=raw["energy_eh"], unit="Eh", checks=raw_checks)
    else:
        attempt.state = "failed"
    result = Result(id=attempt.result_id, run_id=store.run.id, attempt_id=identity, step_id=attempt.step_id,
        operation_status="completed" if qualified else "failed", checks={"energy": raw_checks},
        qualified_outputs=qualified, artifact_ids=[a.id for a in files.values()],
        source={"input_fingerprint": input_fingerprint, "geometry_artifact_id": initial.id,
                "files": {name: {"artifact_id": a.id, "sha256": a.sha256} for name, a in files.items()}})
    store.results[result.id] = result
    store.run.result_ids.append(result.id)
    store.run.attempts.append(attempt)
    store.run.usage.orca_starts_reserved += 1
    store.run.usage.orca_starts_actual += 1
    return attempt, result


@pytest.fixture
def store(tmp_path):
    source = OfflineStore(tmp_path)
    directory = source.path(f"runs/{source.run.id}/model")
    directory.mkdir(parents=True)
    request = directory / "model_offline.request.json"
    response = directory / "model_offline.response.json"
    request.write_text('{"offline_fixture_only":true}', encoding="utf-8")
    response.write_text('{"proposal":{"action":"initial_plan"},"offline_fixture_only":true}', encoding="utf-8")
    source.run.model_records.append({"id": "model_offline", "request_hash": sha256_file(request),
        "response_record_sha256": sha256_file(response), "status": "known", "http_status": 200,
        "error_category": None})
    source.run.decisions.append({"id": "model_offline", "action": "initial_plan"})
    request = directory / "model_stop.request.json"
    response = directory / "model_stop.response.json"
    request.write_text('{"offline_fixture_only":true}', encoding="utf-8")
    response.write_text('{"proposal":{"action":"stop","reason":"offline fixture final explanation"}}', encoding="utf-8")
    source.run.model_records.append({"id": "model_stop", "request_hash": sha256_file(request),
        "response_record_sha256": sha256_file(response), "status": "known", "http_status": 200,
        "error_category": None})
    source.run.decisions.append({"id": "model_stop", "action": "stop", "reason": "offline fixture final explanation"})
    return source


def failed_checks(report):
    return [c["name"] for c in report["checks"] if not c["passed"]]


def test_rejected_actual_sampling_proposals_without_plan_report_failure_and_cost(store):
    archive = json.loads((PROJECT / "tests/fixtures/phase_b/model_rejections/sampling_no_plan.json").read_text(encoding="utf-8"))
    store.run.model_records = archive["records"]
    store.run.decisions = archive["decisions"]
    store.run.goal_status = {"sampling_goal": "insufficient_evidence"}
    store.run.state = "failed"
    store.plan = None
    # Only the request body is a labelled offline placeholder. The two actual
    # rejected model proposals and their cost records remain unchanged.
    directory = store.path(f"runs/{store.run.id}/model")
    for record, response in zip(store.run.model_records, archive["responses"], strict=True):
        request_path = directory / f"{record['id']}.request.json"
        response_path = directory / f"{record['id']}.response.json"
        request_path.write_text('{"offline_fixture_only":true}', encoding="utf-8")
        response_path.write_text(json.dumps(response), encoding="utf-8")
        record["request_hash"] = sha256_file(request_path)
        record["response_record_sha256"] = sha256_file(response_path)
    assert archive["responses"][0]["proposal"]["parameters"]["steps"][0].get("system_id") is None
    assert isinstance(archive["responses"][1]["proposal"]["parameters"]["steps"][3]["inputs"]["qualified_energy"], list)
    report = grade.grade_joint(store, store.run, "sampling_left")
    assert not report["passed"] and not report["scientific_success"]
    assert not report["trajectory_passed"] and not report["protocol_passed"]
    assert {"real_model_initial_plan_receipt", "initial_sampling_plan_exactly_three_then_analysis",
            "sampling_analysis_result_available", "terminal_delivery"} <= set(failed_checks(report))
    assert report["recorded_cost"]["model_http_records"] == 2
    assert report["recorded_cost"]["known_tokens"] == 7142
    assert report["recorded_cost"]["cost_upper_usd"] == "0.0034386"
    assert report["recorded_cost"]["orca_starts_actual"] == 0


@pytest.mark.parametrize("case", grade.CASES)
def test_no_attempt_cannot_publish_scientific_success_in_any_joint_case(store, case):
    store.plan = None
    report = grade.grade_joint(store, store.run, case)
    assert not report["passed"] and not report["scientific_success"]
    assert not report["scientific_evidence_passed"]


@pytest.mark.parametrize("missing", ["binding", "selected_result"])
def test_incomplete_sampling_plan_without_selected_analysis_is_a_failed_grade(store, missing):
    sampling_fixture(store, "stop")
    if missing == "binding":
        store.plan.goal_map.clear()
    else:
        store.run.selected_results.clear()
    report = grade.grade_joint(store, store.run, "sampling_stop")
    assert not report["passed"]
    assert "sampling_analysis_result_available" in failed_checks(report)


def test_frozen_inputs_and_reference_candidates_are_verified():
    review, cases, manifest = grade.frozen_references()
    assert review["uses_product_science_parser_for_expected"] is False
    assert len(grade.verify_candidates(PROJECT, manifest)) == 15
    assert cases["common"]["science"]["rule"] == "orca-hf-2"


def test_water_sp_grade_reads_real_archived_bytes_but_fixture_never_launches(store):
    add_attempt(store, RAW)
    report = grade.grade_joint(store, store.run, "water_sp")
    assert report["passed"], failed_checks(report)
    assert not report["uses_product_scientific_parser"]
    assert not report["uses_goal_status_as_science_oracle"]


@pytest.mark.parametrize("change", ["number", "extra_start", "raw_hash", "model_hash"])
def test_goal_status_cannot_hide_bad_numbers_hashes_or_extra_execution(store, change):
    _, result = add_attempt(store, RAW)
    if change == "number":
        result.qualified_outputs["energy"].value += 0.1
    elif change == "extra_start":
        store.run.usage.orca_starts_actual += 1
    elif change == "raw_hash":
        store.artifacts[result.artifact_ids[0]].path.write_text("changed", encoding="utf-8")
    else:
        store.path(f"runs/{store.run.id}/model/model_offline.response.json").write_text("changed", encoding="utf-8")
    report = grade.grade_joint(store, store.run, "water_sp")
    assert not report["passed"] and failed_checks(report)
    assert report["recorded_goal_status"]["result_goal"] == "satisfied"


@pytest.mark.parametrize("case,second,fixture", [
    ("repair_success", 100, RAW),
    ("repair_exhaustion", 2, PROJECT / "tests/fixtures/phase_b/independent/scf-maxiter-2"),
])
def test_exact_two_attempt_repair_and_exhaustion_trajectories(store, case, second, fixture):
    add_attempt(store, PROJECT / "tests/fixtures/phase_a/real_water_scf_limit", scf_maxiter=1,
                logical_id="repair", number=1)
    add_attempt(store, fixture, scf_maxiter=second, logical_id="repair", number=2)
    if case == "repair_exhaustion":
        store.run.state = "failed"
    report = grade.grade_joint(store, store.run, case)
    assert report["passed"], failed_checks(report)
    add_attempt(store, fixture, scf_maxiter=second, logical_id="repair", number=3)
    report = grade.grade_joint(store, store.run, case)
    assert not report["passed"]
    assert "exact_attempt_and_launch_count" in failed_checks(report)


def sampling_fixture(store, window_id, *, wrong_half=False):
    review, _, manifest = grade.frozen_references()
    window = next(w for w in manifest["windows"] if w["id"] == window_id)
    reviewed = next(w for w in review["sampling"]["windows"] if w["window_id"] == window_id)
    selected = reviewed["selected_candidate_id"]
    if wrong_half:
        selected = next(c for c in window["optional_candidate_ids"] if c != selected)
    chosen = window["initial_candidate_ids"] + ([selected] if selected else [])
    sources = []
    for candidate_id in chosen:
        candidate = next(c for c in window["candidates"] if c["id"] == candidate_id)
        _, source = add_attempt(store, PROJECT / "tests/fixtures/phase_b/independent" / candidate["reference_id"])
        sources.append(source)
    rows = [{"member_id": c["model_candidate_id"], "status": "qualified" if c["id"] in chosen else "missing",
             "energy_eh": 0.0 if c["id"] in chosen else None} for c in window["candidates"]]
    expected_minimum = next(c["model_candidate_id"] for c in window["candidates"]
                            if c["id"] == reviewed["after_action"]["minimum_candidate_id"])
    published = checks({"finite_discrete_sampling"}, "finite-sampling-1")
    derived = Result(id="result_analysis", run_id=store.run.id, call_id="call_analysis", step_id="analysis",
        operation_status="completed", checks={"sampling": published},
        qualified_outputs={"sampling": QualifiedOutput(value=reviewed["after_action"]["neighbor_span_angstrom"],
            unit="angstrom", checks=published, source={"minimum_candidate_id": expected_minimum,
                "sampled": [{"result_id": r.id, "run_id": r.run_id, "attempt_id": r.attempt_id,
                    "energy_eh": r.qualified_outputs["energy"].value,
                    "artifact_hashes": {a: store.artifacts[a].sha256 for a in r.artifact_ids}}
                    for r in sources]})}, observations={"analysis": {"members": rows}})
    analysis_path = store.root / "analysis.json"
    analysis_path.write_text(json.dumps({"members": rows}), encoding="utf-8")
    derived.artifact_ids = [store.add_artifact(analysis_path, role="analysis").id]
    store.results[derived.id] = derived
    store.run.result_ids.append(derived.id)
    store.run.selected_results["analysis"] = derived.id
    _sampling_decisions(store, sources)


def _plan_receipt(store, identity, plan, related_results=(), *, initial=False):
    directory = store.path(f"runs/{store.run.id}/model")
    request, response = directory / f"{identity}.request.json", directory / f"{identity}.response.json"
    request.write_text('{"offline_fixture_only":true}', encoding="utf-8")
    action = "initial_plan" if initial else "revise_plan"
    response.write_text(json.dumps({"proposal": {"action": action}}), encoding="utf-8")
    model = {"id": identity, "request_hash": sha256_file(request), "response_record_sha256": sha256_file(response),
             "status": "known", "http_status": 200, "error_category": None}
    store.run.model_records = [r for r in store.run.model_records if r["id"] != identity] + [model]
    record = {"id": identity, "plan": plan, "basis": {"plan_version": None if initial else 1},
              "related_results": list(related_results)}
    path = store.path(f"runs/{store.run.id}/decisions/{identity}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    decision = {"id": identity, "prior_plan_id": None if initial else plan["id"], "plan_id": plan["id"],
                "basis": record["basis"], "record_sha256": fingerprint(record)}
    store.run.decisions = [d for d in store.run.decisions if d["id"] != identity] + [decision]
    return record


def _sampling_decisions(store, sources):
    steps = [{"id": r.step_id, "tool": "orca.sp"} for r in sources[:3]]
    analysis = {"id": "initial_analysis", "tool": "analysis.finite_sampling",
                "inputs": {str(i): {"producer_step_id": r.step_id} for i, r in enumerate(sources[:3])}}
    plan = {"id": "plan_offline", "version": 1, "steps": steps + [analysis]}
    _plan_receipt(store, "model_offline", plan, initial=True)
    if len(sources) == 3:
        return
    facts = {r.id: grade.geometry_facts(store.artifact_path(r.source["files"]["geometry.xyz"]["artifact_id"]))
             for r in sources[:3]}
    ordered = sorted(sources[:3], key=lambda r: facts[r.id]["r01_angstrom"])
    values = [r.qualified_outputs["energy"].value for r in ordered]
    observation = {"reason": "span_too_wide", "goal_satisfied": False, "scientific_status": "insufficient_evidence",
        "neighbor_span_angstrom": facts[ordered[2].id]["r01_angstrom"] - facts[ordered[0].id]["r01_angstrom"],
        "left_gap_eh": values[0] - values[1], "right_gap_eh": values[2] - values[1]}
    consumption = {str(i): {"run_id": r.run_id, "result_id": r.id, "attempt_id": r.attempt_id,
        "artifact_hashes": {a: store.artifacts[a].sha256 for a in r.artifact_ids}, "status": "qualified",
        "result_fingerprint": fingerprint(r), "rule_version": "orca-hf-2"} for i, r in enumerate(sources[:3])}
    path = store.root / "initial-analysis.json"
    path.write_text(json.dumps(observation), encoding="utf-8")
    previous = Result(id="initial_analysis_result", run_id=store.run.id, call_id="initial_analysis_call",
        step_id=analysis["id"], operation_status="completed", observations={"analysis": observation},
        source={"consumption": consumption}, artifact_ids=[store.add_artifact(path, role="analysis").id])
    store.results[previous.id] = previous
    store.run.result_ids.append(previous.id)
    store.run.calls.append(SimpleNamespace(id=previous.call_id, result_id=previous.id, consumption=consumption))
    revised = {"id": plan["id"], "version": 2, "steps": plan["steps"] + [
        {"id": sources[3].step_id, "tool": "orca.sp"}, {"id": "analysis", "tool": "analysis.finite_sampling"}]}
    _plan_receipt(store, "model_append", revised, [previous.id])
    store.run.attempts[3].plan_version = 2


@pytest.mark.parametrize("window", ["left", "right", "stop"])
def test_sampling_checks_correct_half_or_stop_and_raw_independent_goal(store, window):
    sampling_fixture(store, window)
    report = grade.grade_joint(store, store.run, "sampling_" + window)
    assert report["passed"], failed_checks(report)
    assert report["sampling_independent_judgment"]["satisfied"]


def test_wrong_half_is_rejected_even_if_published_sampling_claims_success(store):
    sampling_fixture(store, "left", wrong_half=True)
    report = grade.grade_joint(store, store.run, "sampling_left")
    assert not report["passed"]
    assert "initial_three_then_correct_half_or_stop" in failed_checks(report)


def test_sampling_result_cannot_bind_different_source_ids(store):
    sampling_fixture(store, "stop")
    store.results["result_analysis"].qualified_outputs["sampling"].source["sampled"] = [{"result_id": "other"}]
    report = grade.grade_joint(store, store.run, "sampling_stop")
    assert not report["passed"]
    assert "qualified_sampling_bound_to_actual_results" in failed_checks(report)


def test_sampling_source_uses_exact_qualified_precision_and_separate_stdout_rounding_tolerance(store):
    sampling_fixture(store, "stop")
    source = store.results["result_0"]
    source.qualified_outputs["energy"].value += 3e-13
    derived = store.results["result_analysis"].qualified_outputs["sampling"]
    item = next(s for s in derived.source["sampled"] if s["result_id"] == source.id)
    item["energy_eh"] = source.qualified_outputs["energy"].value
    report = grade.grade_joint(store, store.run, "sampling_stop")
    assert report["passed"], failed_checks(report)
    item["energy_eh"] += 1e-13
    report = grade.grade_joint(store, store.run, "sampling_stop")
    assert not report["passed"] and "qualified_sampling_bound_to_actual_results" in failed_checks(report)


@pytest.mark.parametrize("fault", ["preplanned_fourth", "no_analysis_feedback", "wrong_consumption", "changed_gap"])
def test_right_final_sampling_count_cannot_hide_nonadaptive_or_unbound_plan(store, fault):
    sampling_fixture(store, "left")
    if fault == "preplanned_fourth":
        path = store.path(f"runs/{store.run.id}/decisions/model_offline.json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["plan"]["steps"].insert(3, {"id": store.run.attempts[3].step_id, "tool": "orca.sp"})
        _plan_receipt(store, "model_offline", record["plan"], initial=True)
    elif fault == "no_analysis_feedback":
        path = store.path(f"runs/{store.run.id}/decisions/model_append.json")
        record = json.loads(path.read_text(encoding="utf-8"))
        _plan_receipt(store, "model_append", record["plan"], ["result_2"])
    elif fault == "wrong_consumption":
        store.run.calls[0].consumption["0"]["artifact_hashes"] = {}
    else:
        store.results["initial_analysis_result"].observations["analysis"]["left_gap_eh"] += .1
    report = grade.grade_joint(store, store.run, "sampling_left")
    assert not report["passed"]
    assert "sampling_append_follows_bound_analysis_feedback" in failed_checks(report)


def test_handwritten_plan_followed_only_by_model_explanation_is_not_model_generated_plan(store):
    add_attempt(store, RAW)
    store.run.model_records = [r for r in store.run.model_records if r["id"] == "model_stop"]
    report = grade.grade_joint(store, store.run, "water_sp")
    assert report["scientific_success"] and not report["protocol_passed"] and not report["passed"]
    assert failed_checks(report) == ["real_model_initial_plan_receipt"]


def test_strict_opt_requires_all_five_thresholds_even_with_converged_banner(tmp_path):
    path = tmp_path / "stdout.out"
    text = "Geometry convergence\n" + "\n".join([
        "Energy change -0.0000000032 0.0000010000 YES", "RMS gradient 0.0000000719 0.0000300000 YES",
        "MAX gradient 0.0000001137 0.0001000000 YES", "RMS step 0.0000001516 0.0006000000 YES",
        "MAX step 0.0000002397 0.0010000000 YES", "THE OPTIMIZATION HAS CONVERGED"])
    path.write_text(text, encoding="utf-8")
    assert grade._strict_opt(path)
    path.write_text(text.replace("MAX step 0.0000002397", "MAX step 0.01"), encoding="utf-8")
    assert not grade._strict_opt(path)


def test_frozen_reference_hash_change_is_a_failed_grade_not_new_expected_value(store, monkeypatch):
    add_attempt(store, RAW)
    monkeypatch.setitem(grade.FROZEN, "docs/acceptance/phase-b/reference-review.json", "0" * 64)
    report = grade.grade_joint(store, store.run, "water_sp")
    assert not report["passed"] and failed_checks(report) == ["evidence_available_and_unchanged"]


def test_scientific_success_is_separate_from_missing_final_explanation_and_failed_protocol(store):
    add_attempt(store, RAW)
    store.run.decisions = [d for d in store.run.decisions if d["action"] != "stop"]
    store.run.state = "failed"
    report = grade.grade_joint(store, store.run, "water_sp")
    assert report["scientific_success"] and report["scientific_evidence_passed"] and report["trajectory_passed"]
    assert not report["protocol_passed"] and not report["passed"]
    assert set(failed_checks(report)) == {"accepted_final_explanation", "terminal_delivery"}
    assert report["explanation"]["quality_passed"] is None


def test_acceptance_initial_plan_receipt_uses_frozen_decision_fingerprint(store):
    add_attempt(store, RAW)
    record = {"id": "model_offline", "plan": {"id": "plan_offline"}, "basis": {"plan_version": None}}
    path = store.path(f"runs/{store.run.id}/decisions/model_offline.json")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    store.run.decisions[0] = {"id": "model_offline", "prior_plan_id": None, "plan_id": "plan_offline",
                              "basis": record["basis"], "record_sha256": fingerprint(record)}
    assert grade.grade_joint(store, store.run, "water_sp")["passed"]
    path.write_text('{"changed":true}', encoding="utf-8")
    assert not grade.grade_joint(store, store.run, "water_sp")["passed"]


def test_methane_control_compares_strict_opt_energy_and_all_pair_distances(store):
    review, _, _ = grade.frozen_references()
    record = review["source_index"]["phase-a/methane_opt"]
    source = Path(next(e["path"] for e in record["files"] if e["path"].endswith("job.xyz")))
    if not source.exists():
        pytest.skip("complete historical methane raw archive unavailable; no new calculation is launched")
    attempt, result = add_attempt(store, source.parent)
    attempt.tool = "orca.opt"
    attempt.input_fingerprint = fingerprint({"tool": attempt.tool,
        "parameters": attempt.frozen_step.parameters.model_dump(),
        "geometry_hash": store.artifacts[attempt.geometry_artifact_id].sha256})
    result.source["input_fingerprint"] = attempt.input_fingerprint
    final_path = store.root / attempt.id / "job.xyz"
    shutil.copyfile(source, final_path)
    artifact = store.add_artifact(final_path, attempt.id)
    result.artifact_ids.append(artifact.id)
    result.source["files"]["job.xyz"] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    geometry_checks = checks(grade.GEOMETRY_CHECKS)
    result.checks["optimized_geometry"] = geometry_checks
    result.qualified_outputs["optimized_geometry"] = QualifiedOutput(artifact_id=artifact.id, checks=geometry_checks)
    report = grade.grade_joint(store, store.run, "methane_opt_control")
    assert report["passed"], failed_checks(report)
    result.qualified_outputs["optimized_geometry"].artifact_id = attempt.geometry_artifact_id
    report = grade.grade_joint(store, store.run, "methane_opt_control")
    assert not report["passed"] and "strict_optimization_and_bound_final_geometry" in failed_checks(report)
