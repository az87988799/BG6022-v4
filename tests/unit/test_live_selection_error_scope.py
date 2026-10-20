"""Retained a007 syntax-valid response; parameter errors keep their envelope path."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent.condition_selection import commit_selections
from orca_agent.model_usage import current_basis
from orca_agent.proposals import DecisionIntent, ProposalError, validation_error_details
from tests.unit.test_simplified_semantic_selection import choices, selected_fixture

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live-t2-a007-selection-envelope.json"


def test_actual_duplicate_reason_error_points_to_parameters_without_changing_provider_response(tmp_path):
    evidence = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert json.loads(evidence["raw_content"]) == evidence["proposal"]
    original = copy.deepcopy(evidence)
    proposal = DecisionIntent.model_validate(evidence["proposal"])
    assert proposal.reason and proposal.parameters["reason"]
    store, run = selected_fixture(tmp_path)
    before = store.load_request(run).model_dump_json(), store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError) as rejected:
        commit_selections(store, run, proposal.parameters, decision_id="rejected_selection",
                          basis=current_basis(store, run))
    assert rejected.value.detail["errors"] == [
        {"type": "extra_forbidden", "loc": ["parameters", "reason"]}]
    assert evidence == original
    assert (store.load_request(run).model_dump_json(), store.load_run(run.id).model_dump_json()) == before


@pytest.mark.parametrize("policy", [None, "simplified-1"])
def test_choice_schema_error_keeps_full_parameter_scope(tmp_path, policy):
    store, run = selected_fixture(tmp_path, policy=policy)
    params = choices(run)
    for choice in params["choices"].values():
        choice["sources"] = [{"result_id": "result_unused", "document_sha256": "a" * 64, "quote": "unused"}]
    params["choices"]["method"].pop("reason")
    with pytest.raises(ProposalError) as rejected:
        commit_selections(store, run, params, decision_id="rejected_selection", basis=current_basis(store, run))
    assert {"type": "missing", "loc": ["parameters", "choices", "method", "reason"]} in rejected.value.detail["errors"]


def test_decision_envelope_errors_remain_at_root():
    with pytest.raises(ValidationError) as rejected:
        DecisionIntent.model_validate({"action": "select_conditions", "parameters": {}})
    assert validation_error_details(rejected.value) == [{"type": "missing", "loc": ["reason"]}]
