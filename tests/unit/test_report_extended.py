import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent.models import (
    Artifact,
    Attempt,
    BudgetLimits,
    Check,
    EvidenceRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    QualifiedOutput,
    Request,
    Result,
    Run,
    Step,
    ToolCall,
)
from orca_agent.orca.checks import check_outputs
from orca_agent.report import build_report, render_report


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "stdout.out"
    path.write_text("Synthetic report fixture; not scientific evidence", encoding="utf-8")
    artifact = Artifact(id="artifact_stdout", path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        size=path.stat().st_size, role="stdout", run_id="run_one", attempt_id="attempt_one")
    goal = Goal(id="g_energy", port="energy", minimum_check_version="orca-hf-2")
    request = Request(id="request_one", geometry_artifact_id="geometry", goals=[goal])
    checks = check_outputs({}, "orca.sp")["energy"]
    for check in checks:
        check.status = "passed"
        check.detail = "Synthetic report fixture; not scientific evidence"
    artifacts = {artifact.id: artifact}
    files = {"stdout.out": {"artifact_id": artifact.id, "sha256": artifact.sha256}}
    for name, identifier, content in [
        ("job.inp", "artifact_input", "! RHF STO-3G\n# Synthetic report fixture"),
        ("geometry.xyz", "artifact_geometry", "3\nSynthetic\nO 0 0 0\nH 0 .7 .6\nH 0 -.7 .6\n"),
    ]:
        extra_path = tmp_path / name
        extra_path.write_text(content, encoding="utf-8")
        extra = Artifact(id=identifier, path=str(extra_path), sha256=hashlib.sha256(extra_path.read_bytes()).hexdigest(),
                         size=extra_path.stat().st_size, role="raw_evidence", run_id="run_one", attempt_id="attempt_one")
        artifacts[identifier] = extra
        files[name] = {"artifact_id": identifier, "sha256": extra.sha256}
    artifacts["geometry"] = artifacts["artifact_geometry"].model_copy(update={"id": "geometry", "run_id": None, "attempt_id": None})
    result = Result(id="result_one", run_id="run_one", step_id="step_one", attempt_id="attempt_one",
                    operation_status="completed", artifact_ids=[entry["artifact_id"] for entry in files.values()], checks={"energy": checks},
                    qualified_outputs={"energy": QualifiedOutput(value=-74.96299, unit="Eh", checks=checks)},
                    observations={"energy_eh": -99, "unregistered": {"value": 1.234, "unit": None}},
                    source={"conditions": {"method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1},
                            "input_fingerprint": "input1", "geometry_artifact_id": "geometry", "files": files})
    attempt = Attempt(id="attempt_one", step_id="step_one", logical_id="logical_one", number=1,
                      tool="orca.sp", state="completed", started=True, result_id=result.id,
                      geometry_artifact_id="geometry", input_fingerprint="input1", directory="attempt-001")
    run = Run(id="run_one", request_id=request.id, request_version=1, plan_id="plan_one", plan_version=1,
              permission=PermissionSnapshot(), budget=BudgetLimits(), attempts=[attempt],
              result_ids=[result.id], state="completed", goal_status={goal.id: "satisfied"}, delivery_status="complete")
    run.usage.orca_starts_reserved = 1
    run.usage.orca_starts_actual = 1
    plan = SimpleNamespace(goal_map={goal.id: OutputBinding(step_id="step_one", port="energy")})
    results = {(run.id, result.id): result}
    read_artifacts = []

    def artifact_path(artifact_id):
        read_artifacts.append(artifact_id)
        item = artifacts[artifact_id]
        if hashlib.sha256(Path(item.path).read_bytes()).hexdigest() != item.sha256:
            raise ValueError("hash changed")
        return Path(item.path)

    def load_result(run_id, result_id):
        try:
            return results[(run_id, result_id)]
        except KeyError:
            raise ValueError("unavailable source result") from None

    store = SimpleNamespace(load_run=lambda _: run, load_request=lambda _: request,
                            load_plan=lambda _: plan, load_result=load_result,
                            load_artifact=artifacts.__getitem__, artifact_path=artifact_path,
                            load_request_revision=lambda *_: request)
    return SimpleNamespace(store=store, run=run, request=request, plan=plan,
                           result=result, results=results, artifact=artifact, path=path,
                           artifacts=artifacts, read_artifacts=read_artifacts)


def test_report_is_readonly_deterministic_and_preserves_three_distinct_statuses(source):
    before = source.path.read_bytes()
    first = build_report(source.store, source.run)
    second = build_report(source.store, source.run.id)
    assert first == second
    assert source.path.read_bytes() == before
    assert first["user_goal_complete"]
    result = first["results"][0]
    assert result["operation_status"] == "completed"
    assert result["scientific_status"] == "passed"
    assert result["qualified_outputs"]["energy"]["value"] == -74.96299
    assert result["observations"]["data"]["energy_eh"] == -99
    assert result["observations"]["scientific_qualification"] is False
    assert first["goals"][0]["evidence_status"] == "scientific_output_verified"
    assert set(source.result.artifact_ids).issubset(source.read_artifacts)
    text = render_report(first)
    assert "已验证 energy：-74.96299 Eh" in text
    assert "原始证据" in text and source.artifact.sha256 in text


def test_operation_success_and_observations_do_not_satisfy_scientific_goal(source):
    source.result.qualified_outputs = {}
    report = build_report(source.store, source.run)
    assert report["results"][0]["operation_status"] == "completed"
    assert not report["user_goal_complete"]
    assert report["recorded_delivery_status"] == "complete"
    assert report["report_delivery_status"] == "partial"
    assert "已验证 energy" not in render_report(report)


def test_hash_change_suppresses_scientific_numbers_without_rewriting_old_goal_record(source):
    source.path.write_bytes(b"tampered")
    report = build_report(source.store, source.run)
    assert source.run.goal_status == {"g_energy": "satisfied"}
    assert not report["user_goal_complete"]
    assert report["results"][0]["qualified_outputs"] == {}
    assert "source_unverified:artifact_stdout" in report["results"][0]["gaps"]
    assert "-74.96299" not in render_report(report)


def test_changed_upstream_hash_withholds_derived_output_even_if_derived_artifact_is_unchanged(source, tmp_path):
    path = tmp_path / "other.out"
    path.write_bytes(b"upstream")
    artifact = Artifact(id="upstream", path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        size=8, role="stdout", run_id="run_other", attempt_id="attempt_other")
    source.artifacts[artifact.id] = artifact
    source.result.qualified_outputs["energy"].source = {"A": {"artifact_hashes": {artifact.id: artifact.sha256}}}
    path.write_bytes(b"changed upstream")
    report = build_report(source.store, source.run)
    assert not report["user_goal_complete"] and not report["results"][0]["qualified_outputs"]


def test_two_results_require_explicit_binding_and_bad_selection_never_falls_back(source):
    other = source.result.model_copy(deep=True, update={"id": "result_two"})
    other.qualified_outputs["energy"].value = -74.5
    source.results[(source.run.id, other.id)] = other
    source.run.result_ids.append(other.id)
    ambiguous = build_report(source.store, source.run)
    assert not ambiguous["user_goal_complete"]
    assert "ambiguous_result_binding" in ambiguous["goals"][0]["gaps"]
    source.run.selected_results = {"step_one": "result_two"}
    source.run.attempts[0].result_id = other.id
    selected = build_report(source.store, source.run)
    assert selected["goals"][0]["result_id"] == "result_two"
    assert selected["user_goal_complete"]
    source.run.selected_results = {"step_one": "result_unbound"}
    rejected = build_report(source.store, source.run)
    assert rejected["goals"][0]["result_id"] is None
    assert not rejected["user_goal_complete"]


def test_missing_required_goal_remains_missing_with_other_goal_complete(source):
    source.request.goals.append(Goal(id="free_energy", port="free_energy", minimum_check_version="unresolved-1",
                                     original_text="Compare free energies", unresolved=["frequency_not_supported"]))
    report = build_report(source.store, source.run)
    assert not report["user_goal_complete"]
    assert report["goals"][0]["report_status"] == "satisfied"
    assert report["goals"][1]["port"] == "free_energy"
    assert "frequency_not_supported" in report["goals"][1]["gaps"]


def test_optional_gap_does_not_turn_complete_required_evidence_into_incomplete(source):
    source.request.goals.append(Goal(id="optional", port="dipole", required=False,
                                     minimum_check_version="unresolved-1"))
    report = build_report(source.store, source.run)
    assert report["user_goal_complete"]
    assert report["goals"][1]["report_status"] == "insufficient_evidence"


def test_query_can_complete_reading_goal_without_publishing_a_scientific_port(source):
    source.request.goals = [Goal(id="g_energy", port="field_observation", minimum_check_version="evidence-read-1")]
    source.plan.goal_map = {"g_energy": OutputBinding(step_id="step_one", port="field_observation")}
    source.result.qualified_outputs = {}
    source.result.checks = {"field_observation": [Check(name="read", status="passed", rule_version="evidence-read-1")]}
    source.result.observations = {"field_observation": {"status": "observed", "value": 1.234}}
    call = ToolCall(tool="evidence.value", parameters={"artifact_id": source.artifact.id}, request_version=1)
    source.run.calls.append(call)
    source.result.call_id = call.id
    report = build_report(source.store, source.run)
    assert report["user_goal_complete"]
    assert report["goals"][0]["evidence_status"] == "query_evidence_read_verified"
    assert not report["results"][0]["qualified_outputs"]


def test_model_exhaustion_retains_actual_unknown_and_reserved_costs_without_prompts(source):
    source.run.state = "budget_exhausted"
    source.run.usage.model_calls = 2
    source.run.usage.model_tokens_used = 100
    source.run.usage.model_tokens_unknown = 4000
    source.run.model_records = [
        {"id": "model_known", "status": "known", "cost_reserved_usd": .02, "cost_known_usd": .001,
         "total_tokens": 100, "prompt": "private content", "api_key": "sensitive"},
        {"id": "model_timeout", "status": "unknown", "cost_reserved_usd": .03,
         "error_category": "timeout", "input_reserved": 3000, "output_reserved": 1000},
    ]
    report = build_report(source.store, source.run)
    cost = report["budget"]["model_cost"]
    assert cost["known_cost"] == "0.001"
    assert cost["unsettled_reservation"] == "0.03"
    assert cost["unsettled_call_count"] == 1
    assert report["budget"]["usage"]["model_tokens_unknown"] == 4000
    assert "private content" not in json.dumps(report)
    assert "api_key" not in json.dumps(cost)
    assert report["results"][0]["qualified_outputs"]["energy"]["value"] == -74.96299


def test_unpriced_record_is_unknown_and_does_not_become_zero_dollars(source):
    source.run.model_records = [{"id": "unknown", "status": "unknown"}]
    cost = build_report(source.store, source.run)["budget"]["model_cost"]
    assert cost["known_cost"] is None and cost["unsettled_reservation"] is None
    assert not cost["cost_records_complete"]


def test_credentials_are_redacted_from_untrusted_descriptions_and_observations(source):
    key = "sk-" + "abc123" * 6
    source.request.original_text = "Inspect " + key
    source.result.observations["api_key"] = key
    source.result.observations["text"] = "Authorization Bearer secret-text"
    source.run.diagnostics = [{"message": key}]
    report = build_report(source.store, source.run)
    assert key not in json.dumps(report)
    assert "secret-text" not in render_report(report)
    assert "[redacted]" in json.dumps(report)


def test_energy_difference_render_preserves_formula_unit_exact_source_and_member_gaps(source):
    check = Check(name="comparison", status="passed", rule_version="energy-compare-1")
    output = QualifiedOutput(value=-.0012, unit="Eh", checks=[check], source={
        "formula": "E(B) - E(A)", "A": {"run_id": "run_a", "attempt_id": "attempt_a", "result_id": "result_a"},
        "B": {"run_id": "run_b", "attempt_id": "attempt_b", "result_id": "result_b"},
    })
    source.request.goals = [Goal(id="g_energy", port="energy_difference", minimum_check_version="energy-compare-1")]
    source.plan.goal_map = {"g_energy": OutputBinding(step_id="step_one", port="energy_difference")}
    source.result.qualified_outputs = {"energy_difference": output}
    source.result.checks = {"energy_difference": [check]}
    call = ToolCall(tool="analysis.energy_compare", parameters={"goal_id": "g_energy"}, request_version=1)
    source.run.calls.append(call)
    source.result.call_id = call.id
    source.result.observations = {"members": [
        {"member_id": "A", "required": True, "status": "qualified"},
        {"member_id": "optional", "required": False, "status": "missing", "missing_reason": "SCF_failed"},
    ]}
    text = render_report(build_report(source.store, source.run))
    assert "ΔE = E(B) − E(A)：-0.0012 Eh" in text
    assert "run_a" in text and "attempt_b" in text and "result_b" in text
    assert "optional" in text and "SCF_failed" in text


def test_attempt_conditions_and_repairs_preserve_execution_versions(source):
    first = source.run.attempts[0]
    first.request_version, first.plan_version = 1, 1
    first.frozen_step = Step(id="step_one", logical_id="logical_one", tool="orca.sp",
                             geometry=InputRef(artifact_id="geometry"), parameters={"scf_maxiter": 1})
    second = first.model_copy(deep=True, update={"id": "attempt_two", "number": 2, "request_version": 2,
                                                "state": "unknown", "result_id": None})
    second.frozen_step.parameters.scf_maxiter = 100
    source.run.attempts.append(second)
    report = build_report(source.store, source.run)
    assert report["attempts"][0]["request_version"] == 1
    assert report["attempts"][1]["parameter_changes"]["scf_maxiter"] == {"before": 1, "after": 100}
    assert report["budget"]["unresolved_scientific_attempt_ids"] == ["attempt_two"]


def test_no_plan_and_missing_request_produce_partial_report_without_execution(source):
    source.run.plan_id = None
    source.run.plan_version = None
    report = build_report(source.store, source.run)
    assert not report["user_goal_complete"]
    assert "goal_has_no_evidence_binding" in report["goals"][0]["gaps"]

    def unavailable(_):
        raise OSError("private filesystem diagnostic")

    source.store.load_request = unavailable
    report = build_report(source.store, source.run)
    assert report["request"] is None and "request_unavailable" in report["gaps"]
    assert "private filesystem diagnostic" not in json.dumps(report)


def test_check_version_mismatch_does_not_upgrade_historical_success(source):
    source.request.goals[0].minimum_check_version = "orca-hf-1"
    report = build_report(source.store, source.run)
    assert not report["user_goal_complete"]
    assert report["results"][0]["scientific_status"] == "passed"
    assert report["goals"][0]["evidence_status"] == "insufficient_evidence"


def test_output_check_failure_does_not_hide_another_independently_qualified_output(source):
    good = source.result.qualified_outputs["energy"]
    source.result.qualified_outputs = {"other": good.model_copy(deep=True), "energy": good}
    source.result.checks["other"] = []
    report = build_report(source.store, source.run)
    assert "other" not in report["results"][0]["qualified_outputs"]
    assert "energy" in report["results"][0]["qualified_outputs"]
    assert report["user_goal_complete"]


def test_immediate_query_evidence_binding_works_without_a_plan(source):
    source.run.plan_id = source.run.plan_version = None
    source.request.goals = [Goal(id="g_energy", port="field_observation", minimum_check_version="evidence-read-1")]
    source.result.qualified_outputs = {}
    source.result.checks = {"field_observation": [Check(name="read", status="passed", rule_version="evidence-read-1")]}
    source.result.observations = {"field_observation": {"status": "observed", "value": 1.234}}
    call = ToolCall(tool="evidence.value", parameters={"artifact_id": source.artifact.id}, request_version=1)
    source.run.calls.append(call)
    source.result.call_id = call.id
    source.run.goal_evidence = {"g_energy": EvidenceRef(run_id=source.run.id, result_id=source.result.id,
                                                       port="field_observation")}
    report = build_report(source.store, source.run)
    assert report["user_goal_complete"]
    assert report["goals"][0]["evidence_status"] == "query_evidence_read_verified"


def test_old_recorded_success_cannot_complete_a_changed_current_scientific_condition(source):
    source.request.basis = "different_basis"
    report = build_report(source.store, source.run)
    assert report["goals"][0]["recorded_status"] == "satisfied"
    assert not report["user_goal_complete"]
    assert "source_not_applicable_to_current_goal" in report["goals"][0]["gaps"]


def test_decimal_string_costs_are_exact_upper_bounds_not_provider_charges(source):
    source.run.model_records = [
        {"id": "first", "status": "known", "cost_known_usd": "0.1"},
        {"id": "second", "status": "known", "cost_known_usd": "0.2"},
        {"id": "unknown", "status": "unknown", "cost_reserved_usd": "0.000006"},
    ]
    report = build_report(source.store, source.run)
    cost = report["budget"]["model_cost"]
    assert cost["known_cost"] == "0.3"
    assert cost["unsettled_reservation"] == "0.000006"
    assert cost["provider_charged_amount"] is None and not cost["provider_billing_queried"]
    assert cost["cost_basis"] == "frozen_peak_uncached_upper_bound_from_known_tokens"
    text = render_report(report)
    assert "按冻结最高价估算费用上界" in text and "实际扣费金额未查询" in text


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1", True, "not-money"])
def test_invalid_decimal_money_stays_unknown(source, amount):
    source.run.model_records = [{"id": "bad", "status": "known", "cost_known_usd": amount}]
    cost = build_report(source.store, source.run)["budget"]["model_cost"]
    assert cost["known_cost"] is None and not cost["cost_records_complete"]
