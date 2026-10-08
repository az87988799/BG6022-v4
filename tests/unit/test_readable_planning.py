"""Small protocol checks; live scientific evidence is recorded separately."""
import json

import pytest
from test_agent import ScriptedTransport
from test_structure_input_chain import intake

from orca_agent import agent, runner
from orca_agent.context import build_context
from orca_agent.delivery import collect_delivery_snapshot
from orca_agent.proposals import DecisionIntent, materialize_plan


def proposal(system="water"):
    return {"steps": [
        {"key": "id", "tool": "structure.resolve", "system_id": system},
        {"key": "xyz", "tool": "structure.prepare", "system_id": system,
         "inputs": {"identity": {"producer_key": "id", "port": "resolved_identity"}}},
        {"key": "sp", "tool": "orca.sp", "system_id": system,
         "inputs": {"geometry": {"producer_key": "xyz", "port": "prepared_geometry"}}}],
        "goal_map": {"goal_energy": {"step_key": "sp", "port": "energy"}}}


def test_native_plan_fits_with_local_catalog_and_no_schema_decoder(tmp_path):
    store, run = intake(tmp_path)
    run.permission.allowed_tools += ["orca.opt", "evidence.list", "evidence.text", "evidence.value"]
    request = store.load_request(run)
    snapshot = collect_delivery_snapshot(store, run, request)
    prepared = build_context(request, run, relevant_tools=run.permission.allowed_tools,
                             delivery_snapshot=snapshot)
    wire = json.loads(prepared.body()["messages"][1]["content"])
    assert wire["AUTHORITY"]["response_contract"] == "decision-intent-1"
    assert set(wire["RESPONSE_ENVELOPE"]) == {"action", "parameters", "reason"}
    assert "SCHEMA_COLUMNS" not in wire and "SHARED_STRINGS" not in wire
    assert {t["name"] for t in wire["TOOLS"]} == {"structure.resolve", "structure.prepare", "orca.sp"}
    assert prepared.input_token_bound <= 12000


@pytest.mark.parametrize("system", ["water", "methane"])
def test_model_selected_plan_gets_parameters_ids_and_dependencies_from_program(tmp_path, system):
    store, run = intake(tmp_path, name=system)
    runner._goals(store, run, None, {})
    store.save_run(run)
    transport = ScriptedTransport({"action": "initial_plan", "parameters": proposal(system)})
    updated, action, _ = agent._decision(store, run, None, {}, transport, None, None)
    assert action == "plan"
    plan = store.load_plan(updated)
    assert plan.steps[1].parameters.charge == 0
    assert plan.steps[1].inputs["identity"].producer_step_id == plan.steps[0].id
    assert plan.steps[2].parameters.basis == "STO-3G"
    assert plan.steps[2].depends_on == [plan.steps[1].id]
    assert updated.usage.orca_starts_actual == updated.usage.structure_preparations == 0


def test_intent_cannot_supply_execution_identity_or_override_known_conditions(tmp_path):
    with pytest.raises(ValueError):
        DecisionIntent.model_validate({"action": "initial_plan", "parameters": {}, "reason": "x",
                                       "permission_version": 999})
    store, run = intake(tmp_path)
    values = proposal()
    values["steps"][-1]["parameters"] = {"charge": 1}
    with pytest.raises(ValueError):
        materialize_plan(store, run, values)
