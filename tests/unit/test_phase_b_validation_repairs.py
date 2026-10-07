"""Portable references, unified grades and receipt identity; all evidence is offline."""

import copy
import json
import shutil
from pathlib import Path

import pytest

from orca_agent.llm import ModelReply, ModelUsage, prepare_request
from orca_agent.model_usage import current_basis, send_model
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import Store
from tests.helpers import phase_b_grade_joint as joint
from tests.helpers import phase_b_model_cases as cases
from tests.helpers.phase_b_grading import classify_grade, model_response_evidence
from tests.unit.test_model_usage import ScriptedTransport


def passing_grade():
    return {"status": "passed", "real_model_evidence_present": True,
            "safety_invariants_passed": True, "fixture_gaps": [],
            "assertions": [{"status": "passed"}],
            "explanation": {"source": {"status": "passed"}},
            "proposal_review": {"status": "passed"},
            "required_final_response_accepted": {"required": True, "passed": True}}


@pytest.mark.parametrize("fault,expected", [
    (None, "passed"), ("axis", "failed"), ("proposal", "failed"), ("delivery", "failed"),
    ("review", "unverified"), ("not_run", "not_run"), ("axis_and_gap", "failed"),
    ("delivery_and_review", "failed"), ("safety_and_gap", "failed"),
])
def test_shared_classification_prioritizes_known_failures(fault, expected):
    grade = passing_grade()
    if fault and "axis" in fault:
        grade["explanation"]["source"]["status"] = "failed"
    if fault == "proposal":
        grade["proposal_review"]["status"] = "failed"
    if fault and "delivery" in fault:
        grade["required_final_response_accepted"]["passed"] = False
    if fault and "review" in fault:
        grade["proposal_review"]["status"] = "not_verified"
    if fault and "gap" in fault:
        grade["fixture_gaps"] = ["missing source"]
    if fault == "safety_and_gap":
        grade["safety_invariants_passed"] = False
    if fault == "not_run":
        grade["status"] = "not_run"
    assert classify_grade(grade) == expected


def test_recorded_v10_failures_remain_failed_without_any_model_call():
    root = Path(__file__).resolve().parents[2] / "docs/acceptance/phase-b/b06-b07-live"
    grades = [json.loads(path.read_text(encoding="utf-8")) for path in root.rglob("*grade*.json")]
    selected = [grade for grade in grades if grade.get("variant_id") in {
        "V-08/missing-conditions", "V-09/different-method"}
        and (grade.get("proposal_review", {}).get("status") == "failed"
             or any(axis.get("status") == "failed" for axis in grade.get("explanation", {}).values()))]
    assert selected, "historical known failures must remain available"
    assert all(classify_grade(grade) == "failed" for grade in selected)


@pytest.fixture
def portable(tmp_path, monkeypatch):
    original = joint.PROJECT
    manifest = "tests/fixtures/phase_b/independent/reference-copies.json"
    mapping = json.loads((original / manifest).read_text(encoding="utf-8"))
    for name in [manifest, *joint.FROZEN, *(item["repository_path"] for item in mapping["copies"])]:
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original / name, destination)
    monkeypatch.setattr(joint, "PROJECT", tmp_path)
    return tmp_path, mapping


def test_independent_references_use_only_explicit_portable_copies(portable):
    root, mapping = portable
    joint.frozen_references()
    for item in mapping["copies"]:
        path = joint._verified({"path": item["historical_path"], "sha256": item["sha256"]})
        assert path == root / item["repository_path"]
        assert path.is_relative_to(root)


@pytest.mark.parametrize("fault", ["hash", "unmapped", "escape", "duplicate"])
def test_reference_copy_rejects_changed_missing_ambiguous_or_escaping_identity(portable, fault):
    root, mapping = portable
    item = mapping["copies"][0]
    entry = {"path": item["historical_path"], "sha256": item["sha256"]}
    if fault == "hash":
        (root / item["repository_path"]).write_bytes(b"changed")
    elif fault == "unmapped":
        entry["path"] = "Z:/unmapped/stdout.out"
    elif fault == "escape":
        item["repository_path"] = "../outside.out"
    else:
        mapping["copies"].append(copy.deepcopy(item))
    (root / "tests/fixtures/phase_b/independent/reference-copies.json").write_text(json.dumps(mapping))
    with pytest.raises(ValueError):
        joint._verified(entry)


@pytest.fixture
def timeout_then_success(tmp_path):
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    request = Request(goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    run = store.create_run(request, None, PermissionSnapshot(model_execution=True),
                           BudgetLimits(model_calls=4, model_tokens=48000,
                                        input_tokens=12000, output_tokens=2000))
    prepared = prepare_request([{"role": "user", "content": "Offline receipt test; return JSON."}], max_output_tokens=100)
    failure = ModelReply(request_hash=prepared.request_hash, error_category="timeout", retryable=True)
    proposal = {"action": "clarify", "reason": "Which geometry?", "parameters": {}}
    success = ModelReply(request_hash=prepared.request_hash, proposal=proposal, usage=ModelUsage(10, 5, 15),
                         response_model="deepseek-flash", http_status=200, response_hash="a" * 64)
    for number, reply in enumerate((failure, success)):
        send_model(store, run, prepared, ScriptedTransport(reply), basis=current_basis(store, run),
                   logical_id=f"offline_{number}")
        run.decisions.append({"id": run.model_records[-1]["id"],
                              **(proposal if number else {"action": "rejected"})})
        store.save_run(run)
    return store, run


def test_timeout_receipt_keeps_unknown_cost_without_erasing_success_identity(timeout_then_success):
    store, run = timeout_then_success
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    evidence = model_response_evidence(store, run)
    assert evidence["present"] and evidence["accepted_proposals"] == 1
    assert evidence["usage"] == {"known": 1, "unknown": 1} and evidence["transport_failures"] == 1
    assert run.usage.model_tokens_unknown > 0
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before


@pytest.mark.parametrize("fault", ["missing_failure", "changed_success", "wrong_decision", "missing_decision_response"])
def test_every_accepted_proposal_and_failed_transport_requires_its_own_receipt(timeout_then_success, fault):
    store, run = timeout_then_success
    ticket = run.model_records[0 if fault == "missing_failure" else 1]["id"]
    response = store.path(f"runs/{run.id}/model/{ticket}.response.json")
    if fault == "missing_failure":
        response.unlink()
    elif fault == "changed_success":
        response.write_bytes(response.read_bytes() + b" ")
    elif fault == "wrong_decision":
        run.decisions[-1]["reason"] = "fabricated accepted proposal"
    else:
        run.decisions.append({"id": "model_missing", "action": "stop"})
    assert not model_response_evidence(store, run)["present"]


@pytest.mark.parametrize("variant", [v for v in cases.evaluation_variant_ids() if v.startswith("N-")])
def test_raw_text_variants_enter_real_intake_without_prepopulated_semantics(tmp_path, variant):
    from orca_agent.context import build_context

    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run, metadata = cases.create_request(store, variant, 1, category="development")
    request = store.load_request(run)
    bundle = json.loads(Path(metadata["raw_intake"]["bundle_path"]).read_text(encoding="utf-8"))
    assert not {"goals", "changes"} & bundle.keys()
    assert bundle["conditions"] == {"explain_results": True}
    assert request.normalization_status == "pending" and request.goals[0].port == "unresolved"
    assert request.method is request.basis is request.charge is request.multiplicity is None
    assert not run.model_records and not run.attempts and not run.permission.scientific_execution
    assert run.budget.orca_starts == run.budget.extra_orca_starts == 0
    assert run.batch_category == "development"
    assert store.read_control(run.id)["messages"][0]["text"] == bundle["text"]
    context = build_context(request, run, relevant_tools=[])
    assert variant not in context.canonical_body and "expected_ref" not in context.canonical_body
    assert "raw_request.accepted_normalization" not in context.canonical_body
    grade = cases.evaluate_response(store, run, metadata)
    assert not grade["real_model_evidence_present"] and grade["safety_invariants_passed"]


def test_raw_followup_only_queues_user_text_and_preserves_request_and_cost(tmp_path):
    from orca_agent.semantic import VERSION, commit_candidate

    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run, metadata = cases.create_request(store, "N-03/raw-electron-state-clarification", 1, category="development")
    message = store.read_control(run.id)["messages"][0]
    candidate = {"schema_version": VERSION, "message_ids": [message["id"]], "kind": "normalize",
                 "text_basis": message["text"], "goals": [{"key": "energy", "port": "energy",
                    "text_basis": "单点电子能", "system_refs": ["water"], "geometry_relation": "fixed_initial"}],
                 "unresolved": ["charge", "multiplicity"], "questions": ["电荷和多重度是什么？"]}
    run = commit_candidate(store, run, candidate, decision_id="offline_semantic", basis=current_basis(store, run))
    request = store.load_request(run)
    usage = run.usage.model_dump()
    assert cases.remaining_user_turns(store, run, metadata) == 1
    updated = cases.advance_user_turn(store, run, metadata)
    queued = store.read_control(run.id)["messages"][-1]
    assert queued["text"] == metadata["continuation_messages"][0]["text"]
    assert "update" not in queued and "changes" not in queued
    assert updated.request_version == run.request_version and store.load_request(updated) == request
    assert updated.usage.model_dump() == usage and cases.remaining_user_turns(store, updated, metadata) == 0
    with pytest.raises(ValueError, match="no frozen raw"):
        cases.advance_user_turn(store, updated, metadata)


def test_joint_methane_control_uses_raw_entry_without_science_or_model_during_preparation(tmp_path, monkeypatch):
    from orca_agent import doctor
    from orca_agent.config import Config
    from tests.helpers.phase_b_joint import prepare_case

    monkeypatch.setattr(doctor, "diagnose", lambda _: pytest.fail("raw intake must defer doctor"))
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run, metadata = prepare_case(store, Config(data_root=store.root), "methane_opt_control", "formal")
    request = store.load_request(run)
    bundle = json.loads(Path(metadata["raw_bundle_path"]).read_text(encoding="utf-8"))
    assert "goals" not in bundle and bundle["conditions"] == {"explain_results": True}
    assert request.normalization_status == "pending" and request.method is None
    assert request.systems[0].id == "methane" and run.permission.allowed_tools == ["orca.opt"]
    assert run.budget.orca_starts == 1 and run.budget.extra_orca_starts == 0
    assert not run.model_records and not run.attempts and metadata["input_form"] == "raw_text"
