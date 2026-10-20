"""The real a010 label/producer mismatch gets one actionable, non-alias route."""

from copy import deepcopy

import pytest

from orca_agent.models import (
    BudgetLimits,
    Goal,
    PermissionSnapshot,
    Request,
    RunConfiguration,
    SystemInput,
)
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.store import Store


def initial_geometry_route(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    code = tmp_path / "report.py"
    code.write_text("# Offline declaration fixture; never executed.\n", encoding="utf-8")
    artifact = store.import_artifact(code, "python_code")
    # Exact relevant label/quantity/contract from a010 request revision 3.
    goal = Goal(id="goal_initial_geometry", port="initial_geometry", quantity="初始结构",
        original_text="交付初始结构", minimum_evidence=["交付初始结构"],
        minimum_evidence_source={"交付初始结构": "explicit"}, system_ids=["system_ethanol"],
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")
    request = Request(goals=[goal], systems=[SystemInput(id="system_ethanol")])
    run = store.create_run(request, None, PermissionSnapshot(code_execution=True,
        artifact_ids=[artifact.id], artifact_writes=True,
        allowed_tools=["structure.resolve", "structure.prepare", "python.execute"]),
        BudgetLimits(plan_revisions=2, analysis_executions=2),
        configuration=RunConfiguration(workflow_policy="simplified-1", observation_delivery_policy="reported-observation-2"))
    proposal = {"steps": [
        {"key": "resolve", "tool": "structure.resolve", "parameters": {"system_id": "system_ethanol"}},
        {"key": "prepare", "tool": "structure.prepare", "parameters": {"system_id": "system_ethanol"},
         "inputs": {"identity": {"producer_step_id": "resolve", "port": "resolved_identity"}}},
        {"key": "report", "tool": "python.execute", "parameters": {
            "code": {"artifact_id": artifact.id, "sha256": artifact.sha256},
            "sdk_orca": False, "observation_names": ["other_report"]}}],
        "goal_map": {goal.id: {"step_key": "prepare", "port": goal.port}}}
    return store, run, request, proposal


@pytest.mark.parametrize("target_port", ["initial_geometry", "prepared_geometry"])
def test_both_sides_of_real_label_loop_offer_the_same_explicit_adapter(tmp_path, target_port):
    store, run, request, proposal = initial_geometry_route(tmp_path)
    original = request.model_dump_json()
    proposal["goal_map"]["goal_initial_geometry"]["port"] = target_port
    with pytest.raises(ProposalError) as caught:
        materialize_plan(store, run, proposal)
    detail = caught.value.detail
    assert detail["code"] == "goal_output_route_mismatch"
    assert detail["expected"] == "initial_geometry"
    assert detail["path"] == ["parameters", "goal_map", "goal_initial_geometry", "port"]
    route = detail["valid_route"]
    assert route["existing_producers"] == []
    assert "prepared_geometry" in route["selected_producer"]["declared_outputs"]
    assert "initial_geometry" not in route["selected_producer"]["declared_outputs"]
    assert route["observation_adapters"] == [{"step_key": "report", "tool": "python.execute",
                                              "required_observation_name": "initial_geometry"}]
    assert "observation_names" in route["observation_instruction"]
    assert "not new scientific qualification" in route["observation_instruction"]
    assert store.load_request(run).model_dump_json() == original
    assert store.load_plan(run) is None and not run.attempts and not run.calls


def test_explicit_adapter_declares_requested_record_without_relabeling_real_producer(tmp_path):
    store, run, request, proposal = initial_geometry_route(tmp_path)
    original = deepcopy(request.goals[0])
    proposal["steps"][2]["parameters"]["observation_names"].append("initial_geometry")
    proposal["goal_map"][original.id] = {"step_key": "report", "port": original.port}
    plan = materialize_plan(store, run, proposal)
    assert plan.goal_map[original.id].step_id == plan.steps[2].id
    assert plan.goal_map[original.id].port == original.port
    assert store.load_request(run).goals[0] == original
    assert not run.attempts and not run.calls and not run.result_ids
