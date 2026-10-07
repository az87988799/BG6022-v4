"""Offline grading regressions; no HTTP receipt or scientific evidence is fabricated."""

import copy
import json

import pytest

from orca_agent.model_usage import current_basis
from orca_agent.models import Goal
from orca_agent.store import Store, sha256_file
from tests.helpers import phase_b_model_cases as cases
from tests.helpers.phase_b_grading import classify_grade

NORMALIZATION_STATUS = {
    "N-01/raw-water-sp": "normalized",
    "N-02/raw-optimized-energy": "normalized",
    # Both frozen follow-up messages supply the previously missing answer.
    "N-03/raw-electron-state-clarification": "normalized",
    "N-03/raw-ambiguous-reference": "normalized",
    "N-04/raw-authorized-defaults": "normalized",
    "N-04/raw-unique-inheritance": "normalized",
    "N-04/raw-unconfirmed-inference": "clarification",
    "N-05/raw-unsupported-solvent": "clarification",
    "N-06/raw-unsupported-system": "clarification",
    "N-07/raw-read-only-window": "normalized",
    "N-09/raw-user-goal-replacement": "normalized",
    "N-09/raw-preserve-goal": "normalized",
}
METRIC = "raw_request.normalization_status"


@pytest.mark.parametrize("variant,expected", NORMALIZATION_STATUS.items())
def test_raw_final_status_follows_user_text_without_changing_inputs_or_budget(variant, expected):
    current = cases.variant_spec(variant)
    historical = cases.variant_spec(variant, recorded_sha256=cases.RAW_CASES_V1_SHA256)
    assertion = {"metric": METRIC, "operator": "eq", "value": expected}
    assert assertion in current["variant"]["expected"]
    assert [a for a in current["variant"]["expected"] if a["metric"] != METRIC] == historical["variant"]["expected"]
    assert current["input"] == historical["input"]
    assert current["budget"] == historical["budget"]
    assert current["expected_ref"] == historical["expected_ref"]
    assert current["spec_sha256"] != historical["spec_sha256"]


def test_every_raw_variant_has_an_explicit_final_normalization_assertion():
    assert set(NORMALIZATION_STATUS) == {v for v in cases.evaluation_variant_ids() if v.startswith("N-")}
    assert sha256_file(cases.RAW_CASES_V1) == cases.RAW_CASES_V1_SHA256
    assert all(not any(a["metric"] == METRIC for a in cases.variant_spec(v)["variant"]["expected"])
               for v in cases.fixed_variant_ids())


def test_real_v2_unconfirmed_ground_state_cannot_receive_a_mechanical_pass(tmp_path, monkeypatch):
    """Replay the persisted shape of repair-raw-dev-v2 N01, not a new model claim.

    That accepted proposal retained energy/fixed_initial but invented an inferred
    electronic_state='ground'. Its saved Request was clarification, with
    unconfirmed:electronic_state, despite the empty candidate questions list.
    """
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run, metadata = cases.create_request(store, "N-01/raw-water-sp", 1,
                                         category="development", freeze_label="offline-v2-replay")
    initial = store.load_request(run)
    request = initial.model_copy(deep=True)
    request.version += 1
    message = store.read_control(run.id)["messages"][0]
    request.messages.append(message)
    request.method, request.basis, request.charge, request.multiplicity = "RHF", "STO-3G", 0, 1
    request.conditions.update(method="RHF", basis="STO-3G", charge=0, multiplicity=1,
                              environment="gas", electronic_state="ground")
    request.conditions_source.update({k: "explicit" for k in ("method", "basis", "charge", "multiplicity",
                                                              "environment")})
    request.conditions_source["electronic_state"] = "inferred"
    request.normalization_status = "clarification"
    request.unresolved = ["unconfirmed:electronic_state"]
    request.goals = [Goal(id="goal_single_point_energy", port="energy", minimum_check_version="orca-hf-2",
        system_ids=["water"], conditions={"geometry_relation": "fixed_initial"},
        minimum_evidence=["converged_scf@1"],
        unresolved=["applicability:conflicting_condition:method/electronic_state"])]
    run = store.commit_revision(run, None, decision_id="offline_v2_state", basis=current_basis(store, run),
        request=request, user_message_ids=[message["id"]], semantic_record={"kind": "normalize"})
    # Supply the historical action shape only to the deterministic extractor.
    # Absence of actual model receipts must still prevent real-model evidence.
    monkeypatch.setattr(cases, "_actions", lambda *_: [{"action": "normalize_request",
        "parameters": {"kind": "normalize", "questions": []}, "reason": "offline persisted-state replay"}])
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    grade = cases.evaluate_response(store, run, metadata)
    assert grade["safety_invariants_passed"]
    assert not grade["real_model_evidence_present"]
    assert all(a["status"] == "passed" for a in grade["assertions"] if a["metric"] != METRIC)
    assert next(a for a in grade["assertions"] if a["metric"] == METRIC)["observed"] == "clarification"
    assert classify_grade(grade) == "failed"

    historical = cases.variant_spec(metadata["variant_id"], recorded_sha256=cases.RAW_CASES_V1_SHA256)
    old_metadata = copy.deepcopy(metadata)
    old_metadata.update(spec_sha256=historical["spec_sha256"], expected_ref=historical["expected_ref"],
                        expected=historical["variant"]["expected"])
    original_bytes = json.dumps(old_metadata, sort_keys=True)
    old_grade = cases.evaluate_response(store, run, old_metadata)
    assert all(a["status"] == "passed" for a in old_grade["assertions"])
    assert not old_grade["real_model_evidence_present"]
    assert classify_grade(old_grade) == "unverified"
    assert json.dumps(old_metadata, sort_keys=True) == original_bytes
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before


def test_historical_spec_selection_cannot_accept_arbitrary_or_changed_bytes(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="frozen expected assertions changed"):
        cases.variant_spec("N-01/raw-water-sp", recorded_sha256="0" * 64)
    with pytest.raises(ValueError, match="frozen expected assertions changed"):
        cases.variant_spec(cases.fixed_variant_ids()[0], recorded_sha256=cases.RAW_CASES_V1_SHA256)
    changed = tmp_path / "raw-v1.json"
    changed.write_bytes(cases.RAW_CASES_V1.read_bytes() + b" ")
    monkeypatch.setattr(cases, "RAW_CASES_V1", changed)
    with pytest.raises(ValueError, match="historical raw specification hash differs"):
        cases.variant_spec("N-01/raw-water-sp", recorded_sha256=cases.RAW_CASES_V1_SHA256)


def test_new_run_metadata_cannot_mix_old_expected_with_current_hash(tmp_path):
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run, metadata = cases.create_request(store, "N-01/raw-water-sp", 1, category="development")
    metadata["expected"] = cases.variant_spec(metadata["variant_id"],
        recorded_sha256=cases.RAW_CASES_V1_SHA256)["variant"]["expected"]
    with pytest.raises(ValueError, match="frozen expected assertions changed"):
        cases.evaluate_response(store, run, metadata)
