"""Replay the saved web failure offline; derived replies are not live success."""

import copy
import json
from pathlib import Path

import pytest
from test_agent import ScriptedTransport
from test_text_entry import text_environment

from orca_agent import agent
from orca_agent.context import _correction_instruction, _output_instruction
from orca_agent.natural import initialize_text
from orca_agent.semantic import action_parameters
from orca_agent.tools.registry import get_tool

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
    assert all(isinstance(sent["PROPOSAL_SCHEMA"]["properties"]["parameters"], dict)
               for sent in transport.sent)
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
    assert "native JSON per PROPOSAL_SCHEMA" in instruction
    assert "never response fields" in instruction


def test_container_errors_have_actionable_correction_without_copying_bad_values():
    feedback = {"validation_error": {"requirement": {"errors": [
        {"type": "list_type", "loc": ["parameters", "goals", 0, "minimum_evidence"]},
        {"type": "dict_type", "loc": ["parameters", "question_gaps"]},
    ]}}}
    correction = _correction_instruction(feedback)
    assert "list_type requires a JSON array []" in correction
    assert "dict_type requires a JSON object {}" in correction
    assert "rule IDs as strings" in correction


def test_prepare_catalog_exposes_actual_consumption_key_and_required_arguments(tmp_path):
    from test_structure_input_chain import intake

    from orca_agent.context import _tools

    store, run = intake(tmp_path)
    catalog, schemas = _tools(run, ["structure.prepare"])
    tool = next(tool for tool in catalog if tool["name"] == "structure.prepare")
    assert tool["input_roles"] == ["identity"]
    assert tool["check_contract"]["input_ports"] == {"identity": "resolved_identity"}
    assert schemas[tool["parameter_schema"]]["required"] == get_tool("structure.prepare").parameter_schema["required"]
    assert not run.calls and store.load_request(run).normalization_status == "normalized"


def test_native_input_examples_copy_request_and_tool_contract_without_execution(tmp_path):
    from test_structure_input_chain import intake

    from orca_agent.context import _input_step_examples, _tools

    store, run = intake(tmp_path)
    request = store.load_request(run)
    catalog, _ = _tools(run, run.permission.allowed_tools)
    examples = _input_step_examples(request, catalog)
    prepare = next(item for item in examples if item["tool"] == "structure.prepare")
    assert prepare["parameters"] == {"system_id": "water", "charge": 0, "multiplicity": 1}
    assert prepare["inputs"]["identity"]["port"] == "resolved_identity"
    assert not run.calls and not run.attempts
    assert not _input_step_examples(request.model_copy(update={"systems": []}), catalog)


def test_prepare_defaults_do_not_resolve_unknown_request_conditions(tmp_path):
    from test_structure_input_chain import intake

    from orca_agent.tools.structure import PrepareParameters, validate_call_inputs

    store, run = intake(tmp_path)
    parameters = PrepareParameters(system_id="water").model_dump()
    assert parameters == {"system_id": "water", "charge": 0, "multiplicity": 1}
    request = store.load_request(run)
    request.charge = None
    request.conditions.pop("charge", None)
    request.systems[0].conditions["charge"] = None
    # Read-only test projection: neither the request archive nor permission changes.
    from unittest.mock import patch
    with patch.object(store, "load_request", return_value=request):
        with pytest.raises(ValueError, match="confirmed neutral singlet"):
            validate_call_inputs(store, run, parameters, "structure.prepare")
