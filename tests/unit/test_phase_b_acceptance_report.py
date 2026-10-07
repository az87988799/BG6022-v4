"""Aggregation tests use offline records; no model/ORCA work is performed."""

import json
from types import SimpleNamespace

import pytest

from orca_agent.store import sha256_file
from tests.helpers import phase_b_acceptance_report as report
from tests.unit.test_phase_b_budget import bind_model, budget, make_run, record, settle_model


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path, monkeypatch):
    entries = []
    for name, kind in (("V-01/model", "real_model_with_frozen_evidence"),
                       ("V-03/joint", "joint_real_model_orca"),
                       ("V-10/offline", "offline_fault_injection")):
        entries.append({"variant_id": name, "evidence_requirement": kind, "formal_slots": [
            {"repetition": n, "pytest_nodeids": ["tests/unit/test_example.py::test_bound"],
             **({"joint_case": "sampling_left"} if kind == "joint_real_model_orca" else {})}
            for n in (1, 2, 3)]})
    coverage = write(tmp_path / "coverage.json", {"entries": entries})
    frozen = write(tmp_path / "freeze.json", {"freeze_label": "formal-v1", "code_commit": "offline-test",
                                               "files": {"coverage.json": sha256_file(coverage)}})
    monkeypatch.setattr(report, "_cost", lambda _: {"verified": True, "offline_fixture_only": True})
    return {"project": tmp_path, "coverage_path": coverage, "freeze_path": frozen, "budget": object()}


def add_model(project, monkeypatch, rows):
    root = project["project"] / "model"
    project["model_root"] = root
    for n, row in enumerate(rows):
        write(root / f"{row['category']}/batch/variant/{n}/metadata.json", row)
    monkeypatch.setattr(report, "_model_record", lambda path, _: json.loads(path.read_text()))


def model_row(project, *, category="formal", repetition=1, status="passed", corrected=False, label="formal-v1"):
    return {"category": category, "variant_id": "V-01/model", "repetition": repetition,
            "status": status, "first_success": status == "passed" and not corrected,
            "correction_or_transport_failure": corrected, "freeze_label": label,
            "freeze_sha256": sha256_file(project["freeze_path"]),
            "evidence_type": "real_model_with_frozen_evidence", "run_id": f"offline-{category}-{repetition}"}


def test_development_pass_and_old_freeze_cannot_fill_formal_slot(project, monkeypatch):
    add_model(project, monkeypatch, [model_row(project, category="development"),
                                    model_row(project, label="prior-batch")])
    result = report.build_report(**project)
    assert result["formal"]["status_counts"] == {"passed": 0, "failed": 0, "unverified": 0, "not_run": 9}
    assert len(result["development_records"]) == len(result["other_formal_batches"]) == 1
    assert not result["passed"] and len(result["unverified_or_unrun_slots"]) == 9


def test_failure_and_unreviewed_slots_remain_in_success_denominator(project, monkeypatch):
    add_model(project, monkeypatch, [model_row(project, repetition=1, corrected=True),
        model_row(project, repetition=2, status="failed"), model_row(project, repetition=3, status="unverified")])
    result = report.build_report(**project)["by_evidence_type"]["real_model_with_frozen_evidence"]
    assert result["rate_denominator"] == 3
    assert result["first_success_rate"] == 0
    assert result["success_after_allowed_correction_rate"] == pytest.approx(1 / 3)
    assert result["corrected_subset_success_rate"] == 1


def test_same_run_cannot_fill_three_independent_formal_repetitions(project, monkeypatch):
    rows = [model_row(project, repetition=n) for n in (1, 2, 3)]
    for row in rows:
        row["run_id"] = "same-offline-run"
    add_model(project, monkeypatch, rows)
    result = report.build_report(**project)
    assert result["variants"][0]["status_counts"]["unverified"] == 3
    assert result["formal"]["final_successes"] == 0


@pytest.mark.parametrize("fault", ["freeze_missing", "freeze_file_changed", "wrong_digest", "duplicate_slot", "coverage_unfrozen"])
def test_formal_success_needs_unique_run_and_matching_frozen_files(project, monkeypatch, fault):
    rows = [model_row(project)]
    if fault == "freeze_missing":
        project["freeze_path"].unlink()
    elif fault == "freeze_file_changed":
        project["coverage_path"].write_text(project["coverage_path"].read_text() + " ")
    elif fault == "wrong_digest":
        rows[0]["freeze_sha256"] = "0" * 64
    elif fault == "duplicate_slot":
        rows.append({**rows[0], "run_id": "different-run"})
    else:
        extra = write(project["project"] / "other.json", {})
        write(project["freeze_path"], {"freeze_label": "formal-v1", "code_commit": "offline-test",
                                      "files": {extra.name: sha256_file(extra)}})
    add_model(project, monkeypatch, rows)
    result = report.build_report(**project)
    assert result["formal"]["final_successes"] == 0 and not result["passed"]


def test_coverage_requires_three_distinct_slots_and_unique_variants(project):
    coverage = json.loads(project["coverage_path"].read_text())
    coverage["entries"][0]["formal_slots"].pop()
    write(project["coverage_path"], coverage)
    with pytest.raises(ValueError, match="exactly three"):
        report.build_report(**project)


def test_coverage_cannot_omit_a_frozen_variant_before_computing_rates(project):
    cases = write(project["project"] / "cases.json", {"cases": [{"id": "V-01", "variants": [
        {"id": "model", "evidence_requirement": "real_model_with_frozen_evidence"},
        {"id": "missing", "evidence_requirement": "real_model_with_frozen_evidence"}]}]})
    coverage = json.loads(project["coverage_path"].read_text())
    coverage["frozen_cases"] = {"path": cases.name, "sha256": sha256_file(cases)}
    write(project["coverage_path"], coverage)
    with pytest.raises(ValueError, match="omits or relabels"):
        report.build_report(**project)


def offline_receipt(project, repetition, *, outcome="", invocation=None):
    xml = project["project"] / f"offline-{repetition}.xml"
    xml.write_text(f'<testsuite><testcase classname="tests.unit.test_example" name="test_bound">{outcome}</testcase></testsuite>')
    return write(project["project"] / f"offline-{repetition}.json", {
        "category": "formal_offline_fault_injection", "freeze_label": "formal-v1",
        "freeze_sha256": sha256_file(project["freeze_path"]), "repetition": repetition,
        "invocation_id": invocation or f"independent-{repetition}", "junit_path": xml.name,
        "junit_sha256": sha256_file(xml)})


def test_offline_junit_missing_skipped_and_failed_are_not_automatic_green(project):
    paths = [offline_receipt(project, 1), offline_receipt(project, 2, outcome='<skipped message="no environment"/>'),
             offline_receipt(project, 3, outcome='<failure message="counterexample"/>')]
    result = report.build_report(**project, offline_receipts=paths)
    slots = result["variants"][2]["slots"]
    assert [s["status"] for s in slots] == ["passed", "unverified", "failed"]
    assert report.build_report(**project)["variants"][2]["status_counts"]["not_run"] == 3


@pytest.mark.parametrize("fault", ["hash", "reuse_invocation", "missing_node"])
def test_offline_receipt_integrity_and_repetition_are_required(project, fault):
    one = offline_receipt(project, 1)
    two = offline_receipt(project, 2, invocation="independent-1" if fault == "reuse_invocation" else None)
    if fault == "hash":
        (project["project"] / "offline-2.xml").write_text("changed")
    elif fault == "missing_node":
        xml = project["project"] / "offline-2.xml"
        xml.write_text('<testsuite/>')
        value = json.loads(two.read_text())
        value["junit_sha256"] = sha256_file(xml)
        write(two, value)
    result = report.build_report(**project, offline_receipts=[one, two])
    assert result["variants"][2]["slots"][1]["status"] == "unverified"


@pytest.mark.parametrize("failed_axis", [None, "limits", "missing"])
def test_six_axes_never_inferred_from_mechanical_success(failed_axis):
    review = {"explanation": {name: {"passed": name != failed_axis, "quote": "observed", "rationale": "offline review"}
                              for name in report.AXES}}
    if failed_axis == "missing":
        review["explanation"].pop("source")
    assert report._axes(review, "observed") == ("passed" if failed_axis is None else "unverified" if failed_axis == "missing" else "failed")


@pytest.fixture
def joint_review(tmp_path, monkeypatch):
    import hashlib

    from tests.helpers import phase_b_grade_joint
    metadata_path = write(tmp_path / "probe.json", {"case": "repair_success", "category": "development",
        "run_id": "offline", "data_root": str(tmp_path), "freeze": None})
    write(tmp_path / "probe.independent-grade.json", {"case": "repair_success", "run_id": "offline", "passed": True})
    response = write(tmp_path / "runs/offline/model/model_stop.response.json", {"proposal": {"reason": "observed"}})
    run = SimpleNamespace(id="offline", batch_category="development", attempts=[],
                          decisions=[{"action": "stop", "id": "model_stop", "reason": "observed"}])
    store = report._store(tmp_path)
    store.load_run = lambda _: run
    monkeypatch.setattr(report, "_store", lambda _: store)
    monkeypatch.setattr(report, "_trajectory", lambda *_: (True, False))
    monkeypatch.setattr(phase_b_grade_joint, "grade_joint", lambda *_args, **_kwargs: {"passed": True})
    review_path = write(tmp_path / "probe.explanation-review.json", {
        "run_id": "offline", "category": "development",
        "model_record_id": "model_stop", "exact_model_reason": "observed",
        "model_response_record_sha256": sha256_file(response),
        "model_reason_sha256": hashlib.sha256(b"observed").hexdigest(),
        "explanation": {axis: {"passed": True, "quote": "observed", "rationale": "offline review"}
                        for axis in report.AXES},
        "all_proposal_facts_passed": True, "semantic_review_passed": True})
    return metadata_path, review_path, run


@pytest.mark.parametrize("review_state,expected", [("missing", "unverified"), ("failed", "failed"), ("passed", "passed")])
def test_joint_record_requires_separate_bound_six_axis_review(joint_review, review_state, expected):
    metadata_path, review_path, _ = joint_review
    if review_state == "missing":
        review_path.unlink()
    elif review_state == "failed":
        review = json.loads(review_path.read_text())
        review["explanation"]["limits"]["passed"] = False
        write(review_path, review)
    value = report._joint_record(metadata_path)
    assert value["mechanical_passed"] and value["status"] == expected
    assert value["first_success"] is (expected == "passed")


@pytest.mark.parametrize("fields,expected", [
    ({"all_proposal_facts_passed": True, "semantic_review_passed": True}, "passed"),
    ({"all_proposal_facts_passed": False, "semantic_review_passed": True}, "failed"),
    ({"all_proposal_facts_passed": True, "semantic_review_passed": False}, "failed"),
    ({"all_proposal_facts_passed": False}, "failed"),
    ({"semantic_review_passed": False}, "failed"),
    ({}, "unverified"),
    ({"all_proposal_facts_passed": True}, "unverified"),
    ({"semantic_review_passed": True}, "unverified"),
    ({"all_proposal_facts_passed": 1, "semantic_review_passed": "true"}, "unverified"),
])
def test_joint_proposal_review_requires_explicit_boolean_passes(joint_review, fields, expected):
    metadata_path, review_path, _ = joint_review
    review = json.loads(review_path.read_text())
    for key in ("all_proposal_facts_passed", "semantic_review_passed"):
        review.pop(key)
    review.update(fields)
    write(review_path, review)
    value = report._joint_record(metadata_path)
    assert value["mechanical_passed"] and value["six_axes_passed"]
    assert value["status"] == value["proposal_review_status"] == expected
    assert value["first_success"] is (expected == "passed")
    for key in ("all_proposal_facts_passed", "semantic_review_passed"):
        assert value[key] is (fields.get(key) if type(fields.get(key)) is bool else None)


def test_joint_final_six_axes_cannot_hide_false_intermediate_claim(joint_review):
    # Offline reproduction of the stop-probe-03 mismatch: two Results existed
    # when the model claimed three completed SPs, although its final stop was correct.
    metadata_path, review_path, run = joint_review
    claim = "Three required initial geometries have been sampled."
    run.decisions.insert(0, {"action": "call_tool", "id": "model_intermediate", "reason": claim})
    review = json.loads(review_path.read_text())
    review.update(all_proposal_facts_passed=False, semantic_review_passed=False,
                  intermediate_mismatch={"visible_result_count": 2, "claimed_result_count": 3},
                  behavior={"every_new_result_correctly_described": {
                      "passed": False, "quote": claim, "quote_model_id": "model_intermediate",
                      "rationale": "Two completed Results cannot support the claim of three."}})
    write(review_path, review)
    value = report._joint_record(metadata_path)
    assert value["mechanical_passed"] and value["six_axes_passed"]
    assert value["all_proposal_facts_passed"] is value["semantic_review_passed"] is False
    assert value["status"] == "failed" and value["first_success"] is False
    rates = report._rates([value])
    assert rates["attempted_slots"] == rates["rate_denominator"] == 1
    assert rates["final_successes"] == rates["first_successes"] == 0


@pytest.mark.parametrize("semantic,expected", [("failed", "failed"), ("not_verified", "unverified"), ("passed", "passed")])
def test_model_aggregate_preserves_semantic_gate_independently_of_six_axes(tmp_path, monkeypatch, semantic, expected):
    from tests.helpers import phase_b_model_cases

    identity = {"variant_id": "V-01/allowed-default-origin", "repetition": 1,
                "run_id": "offline", "spec_sha256": "offline-spec"}
    metadata_path = write(tmp_path / "metadata.json", {**identity, "category": "development"})
    write(tmp_path / "ready.json", {"run_id": "offline", "metadata_sha256": sha256_file(metadata_path)})
    # An old stored pass cannot override a failed current independent review.
    write(tmp_path / "grade.json", {**identity, "status": "passed", "model_text_sha256": "offline-text"})
    write(tmp_path / "review.json", identity)
    actual = {**identity, "status": "passed" if semantic == "passed" else "incomplete_or_failed",
              "real_model_evidence_present": True,
              "model_text_sha256": "offline-text", "safety_invariants_passed": True,
              "assertions": [{"status": "passed"}],
              "explanation": {axis: {"status": "passed"} for axis in report.AXES},
              "proposal_review": {"status": semantic}}
    monkeypatch.setattr(phase_b_model_cases, "evaluate_response", lambda *_args, **_kwargs: actual)
    monkeypatch.setattr(report, "_trajectory", lambda *_: (True, False))
    store = SimpleNamespace(load_run=lambda _: SimpleNamespace(batch_category="development"))
    row = report._model_record(metadata_path, store)
    assert row["six_axes_passed"] is True
    assert row["proposal_review"]["status"] == semantic
    assert row["status"] == expected
    assert row["first_success"] is (expected == "passed")


def test_first_success_excludes_actual_rejected_proposal_and_transport_receipts(tmp_path):
    store = report._store(tmp_path)
    run = SimpleNamespace(id="offline", decisions=[], model_records=[])
    for n, error in enumerate((None, "timeout")):
        request = write(tmp_path / f"runs/offline/model/m{n}.request.json", {"offline": True})
        response = write(tmp_path / f"runs/offline/model/m{n}.response.json", {"error_category": error})
        run.model_records.append({"id": f"m{n}", "request_hash": sha256_file(request),
            "response_record_sha256": sha256_file(response), "http_status": 200 if not error else None, "error_category": error})
    assert report._trajectory(store, run) == (True, True)
    run.model_records.pop()
    assert report._trajectory(store, run) == (True, False)
    run.decisions.append({"action": "rejected"})
    assert report._trajectory(store, run) == (True, True)


def test_real_budget_snapshot_exact_money_unknown_and_tampered_receipt(tmp_path, monkeypatch):
    from orca_agent.store import Store
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "delivered.json")
    store = Store(tmp_path / "store", environment_root=tmp_path / "env")
    book = budget.AcceptanceBudget(store)
    run, step, geometry = make_run(store, "formal", science=True)
    known, unknown = record(0), record(1)
    for item in (known, unknown):
        book.reserve_model(run, item)
        bind_model(store, run, item)
    settle_model(book, store, run, known)
    settle_model(book, store, run, unknown, known=False)
    book.reserve_science(run, step, geometry.id)
    before = book.ledger.path.read_bytes()
    cost = report._cost(book)
    assert cost["verified"] and book.ledger.path.read_bytes() == before
    assert cost["model"]["known_tokens"] == 50 and cost["model"]["unknown_tokens"] == 150
    assert cost["model"]["known_usd"] == "0.000024" and cost["model"]["unknown_usd"] == "0.00009"
    assert cost["model"]["usd"] == "0.000114"
    assert cost["science"]["formal"] == {"reserved": 1, "known_actual": 0, "unknown_reservations": 1}
    receipt = next((book.ledger.root / "agent-budget/model_records").glob("*/reservation.json"))
    receipt.write_text("{}")
    assert report._cost(book)["verified"] is False


def test_budget_verification_error_never_becomes_zero_cost(tmp_path):
    def bad():
        raise ValueError("receipt changed")
    ledger = write(tmp_path / "ledger.json", {})
    cost = report._cost(SimpleNamespace(snapshot=bad, ledger=SimpleNamespace(path=ledger)))
    assert cost == {"verified": False, "reason": "receipt changed", "totals": None}
