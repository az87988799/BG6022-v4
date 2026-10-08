"""Rejected real proposal shapes replay offline; no live-understanding claim."""

import copy
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.minimum_evidence import LEGACY_NAMES, REQUIREMENTS, RULE_VERSION
from orca_agent.model_usage import current_basis
from orca_agent.natural import initialize_bundle
from orca_agent.proposals import ProposalError
from orca_agent.semantic import CONDITIONS, action_parameters, commit_candidate
from tests.helpers.phase_b_model_cases import create_request
from tests.helpers.semantic_replay import current_candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-rejections-v1.json"
REJECTED = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposals"]


def condition_key_sets(schema):
    parameters = schema["properties"]["parameters"]
    return [set(parameters["properties"]["conditions"]["propertyNames"]["enum"]),
            set(schema["$defs"]["semantic_SemanticGoal"]["properties"]["conditions"]["propertyNames"]["enum"]),
            set(next(iter(parameters["properties"]["system_conditions"]["patternProperties"].values()))[
                "propertyNames"]["enum"])]


@pytest.mark.parametrize("entry", REJECTED, ids=[entry["model_record_id"] for entry in REJECTED])
def test_real_rejected_shapes_get_specific_key_errors_and_bounded_correction_context(tmp_path, entry):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-replay")
    proposed = copy.deepcopy(entry["parameters"])
    proposed["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
    proposed = current_candidate(proposed)
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError) as caught:
        commit_candidate(store, run, proposed, decision_id="rejected_shape", basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before
    correction = caught.value.detail
    assert set(correction["allowed_condition_keys"]) == CONDITIONS
    locations = [error["loc"] for error in correction["errors"]]
    assert all(path in [location[:-1] for location in locations] for path in [
        ["parameters", "conditions", "phase"], ["parameters", "conditions", "explain_results"],
        ["parameters", "system_conditions", "water", "geometry"],
        ["parameters", "goals", 0, "conditions", "phase"]])
    context = build_context(store.load_request(run), run, relevant_tools=[],
        user_messages=store.read_control(run.id)["messages"], action_parameters=action_parameters(),
        feedback={"validation_error": {"category": "ProposalError", "requirement": correction}})
    assert context.input_token_bound <= 12000
    data = payload(context)
    contract = data["ACTION_PARAMETERS"]["normalize_request"]
    assert "schema" not in contract
    assert all(keys == CONDITIONS for keys in condition_key_sets(data["PROPOSAL_SCHEMA"]))
    assert "environment=gas/solvent" in contract["instruction"]
    assert "No geometry conditions" in contract["instruction"]
    assert "Keep unknown/unsupported and explain_results" in contract["instruction"]
    assert "[] keeps basic checks" in contract["instruction"]
    assert contract["minimum_evidence_rules"]["version"] == RULE_VERSION
    assert contract["minimum_evidence_rules"]["registered"] == json.loads(json.dumps(REQUIREMENTS))
    assert contract["minimum_evidence_rules"]["legacy_aliases"] == LEGACY_NAMES
    assert data["CONTROL"]["validation_error"]["requirement"] == correction
    # A developer-authored correction verifies the program path only. It is
    # deliberately not fed to a real model or recorded as live understanding.
    proposed["conditions"]["environment"] = proposed["conditions"].pop("phase")
    proposed["conditions"].pop("explain_results")
    proposed["system_conditions"] = {}
    proposed["unresolved"] = []
    for goal in proposed["goals"]:
        goal["conditions"]["environment"] = goal["conditions"].pop("phase")
        goal["unresolved"] = []
    with pytest.raises(ProposalError, match="never invent rule names") as minimum_error:
        commit_candidate(store, run, proposed, decision_id="unsupported_rule", basis=current_basis(store, run))
    assert minimum_error.value.detail["path"] == ["parameters", "goals", 0, "minimum_evidence", 0]
    assert store.load_run(run.id).model_dump_json() == before
    proposed["goals"][0]["minimum_evidence"] = []
    updated = commit_candidate(store, run, proposed, decision_id="offline_corrected", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "normalized"
    assert request.conditions["environment"] == "gas"
    assert request.goals[0].port == "energy"
    assert request.goals[0].conditions["geometry_relation"] == "fixed_initial"
    assert not request.goals[0].minimum_evidence
    assert not updated.attempts and not updated.calls and not updated.model_records


def test_user_unknown_minimum_requirement_preserved_as_unresolved(tmp_path):
    store = store_at(tmp_path)
    text = "读取原始文件，并要求实验独立复测"
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text))
    proposed = candidate(store, run, kind="normalize", goals=[{
        "key": "read", "port": "text_window", "text_basis": text,
        "minimum_evidence": ["实验独立复测"]}], questions=["实验独立复测不受当前工具支持，是否保留为未满足要求？"])
    updated = commit_candidate(store, run, proposed, decision_id="user_requirement", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.goals[0].minimum_evidence == ["实验独立复测"]
    assert request.goals[0].unresolved == ["unsupported_minimum_evidence:实验独立复测"]
    assert request.normalization_status == "clarification"


@pytest.mark.parametrize("requirement", ["energy", "orca-hf-2", "converged_scf@1", "converged SCF"])
def test_registered_minimum_requirements_are_not_mistaken_for_unknown_user_text(tmp_path, requirement):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-rules")
    text = store.load_request(run).original_text
    proposed = candidate(store, run, kind="normalize", goals=[{
        "key": "energy", "port": "energy", "text_basis": text, "system_refs": ["water"],
        "geometry_relation": "fixed_initial", "minimum_evidence": [requirement]}],
        questions=["请确认方法、基组、电荷与多重度，当前脚本尚未填写这些条件。"])
    updated = commit_candidate(store, run, proposed, decision_id="registered_rule", basis=current_basis(store, run))
    goal = store.load_request(updated).goals[0]
    assert goal.minimum_evidence == [requirement]
    assert not any(gap.startswith("unsupported_minimum_evidence:") for gap in goal.unresolved)
