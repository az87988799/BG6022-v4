"""Replay the saved web failure offline; derived replies are not live success."""

import copy
import json
from pathlib import Path

import pytest
from test_agent import ScriptedTransport
from test_text_entry import text_environment

from orca_agent import agent
from orca_agent.context import _correction_instruction, _output_instruction
from orca_agent.models import Proposal
from orca_agent.natural import initialize_text
from orca_agent.semantic import action_parameters

EVIDENCE = Path(__file__).resolve().parents[2] / "docs/acceptance/local-web/live-demo.json"


def test_saved_web_failure_corrects_without_user_xyz_or_execution(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, evidence["request"]["original_text"])
    # Only message identity and the explicitly tested notice correction differ
    # from the first real response. Original receipt remains immutable.
    original = copy.deepcopy(evidence["responses"][0]["reply"]["proposal"]["parameters"])
    message_id = store.read_control(run.id)["messages"][0]["id"]
    original["message_ids"] = [message_id]
    for goal in original["goals"]:
        goal["message_id"] = message_id
    corrected = copy.deepcopy(original)
    corrected["notices"] = []
    transport = ScriptedTransport(
        {"action": "normalize_request", "parameters": original},
        {"action": "normalize_request", "parameters": corrected},
    )
    updated, _, _ = agent._decision(store, run, None, {}, transport, None, None)
    request = store.load_request(updated)
    rejection = transport.sent[1]["CONTROL"]["validation_error"]
    assert rejection["requirement"]["inapplicable_notice_choices"] == ["missing_geometry"]
    assert "remove the missing_geometry sentence" in _correction_instruction({"validation_error": rejection})
    assert request.normalization_status == "normalized"
    assert request.systems[0].geometry_source == "prepare"
    assert request.systems[0].geometry_artifact_id is None
    assert request.goals[0].conditions["geometry_relation"] == "fixed_initial"
    assert not request.goals[0].unresolved
    assert updated.usage.model_calls == 2
    assert updated.usage.identity_queries == updated.usage.structure_preparations == 0
    assert updated.usage.orca_starts_actual == 0 and not updated.attempts


@pytest.mark.parametrize("name", ["water", "methane"])
def test_prepare_contract_names_the_opi_acquisition_path(tmp_path, name):
    store, config = text_environment(tmp_path)
    run = initialize_text(store, config, f"Calculate initial geometry single-point energy of {name}.")
    contract = action_parameters(request=store.load_request(run))["normalize_request"]
    guidance = contract["input_acquisition"]
    assert "structure.prepare via OPI" in guidance
    assert "NOT missing:geometry" in guidance
    assert "Permission still gates execution" in guidance


def test_output_envelope_lists_native_fields_without_transport_wrapper():
    instruction = _output_instruction(intake=True)
    assert all(name in instruction for name in Proposal.model_fields)
    assert "never response fields" in instruction
