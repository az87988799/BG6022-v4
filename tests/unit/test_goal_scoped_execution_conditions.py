"""Replay recorded T5 conditions; admission fixtures perform no scientific work."""

import copy
import hashlib
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import model_usage
from orca_agent.applicability import effective_conditions, preparation_conditions
from orca_agent.context import current_decision_purpose
from orca_agent.decision_purpose import terminal_actions
from orca_agent.models import (
    EvidenceRef,
    NativeInputPolicy,
    OutputBinding,
    Plan,
    Request,
    ResourceLimits,
    Run,
    Step,
    SystemInput,
)
from orca_agent.orca.native import validate_native_input
from orca_agent.proposals import materialize_plan
from orca_agent.tools import managed

EVIDENCE = Path(__file__).parents[2] / "docs/acceptance/code-as-tools-step7-refreeze-3/evidence"
T5 = EVIDENCE / "T5/data/runs/run_4c98667882294f5d86fd0d0424a0174b"
DECISION = T5 / "decisions/model_32198b590eca4bd2bc837b6e7496c4ce.json"
XYZ = b"2\ninert admission fixture\nH 0 0 0\nH 0 0 0.74\n"


def t5_request():
    return Request.model_validate(json.loads(DECISION.read_bytes())["request"])


def selected_request():
    request = t5_request()
    # This fixture starts after authorized method selection. No model or
    # calculation ran, and it does not claim that hydrogen is the ethanol input.
    request.method, request.basis = "PBE0", "def2-SVP"
    request.conditions_source.update(method="auto_selected", basis="auto_selected")
    return request


def test_real_t5_goal_conditions_allow_planning_before_method_selection(monkeypatch):
    request = t5_request()
    run = Run.model_validate_json((T5 / "run.json").read_bytes())
    recorded_at = datetime.fromisoformat(next(record["created_at"] for record in run.model_records
                                             if record["id"] == DECISION.stem))
    monkeypatch.setattr(model_usage, "utc_now", lambda: recorded_at)
    before = copy.deepcopy((request, run))
    assert request.charge is request.multiplicity is None
    assert request.systems[0].conditions == {}
    assert terminal_actions(request, run) == ("stop",)
    purpose = current_decision_purpose(request, run)
    assert purpose.kind == "planning"
    assert "select_conditions" in purpose.allowed_actions
    assert "initial_plan" in purpose.allowed_actions
    for identifier, environment in (("goal_gas_energy", "gas_phase"),
                                    ("goal_solvent_energy", "CPCM(water)")):
        goal = next(goal for goal in request.goals if goal.id == identifier)
        assessment = effective_conditions(request, goal, "system_ethanol")
        assert assessment["conditions"]["charge"] == 0
        assert assessment["conditions"]["multiplicity"] == 1
        assert assessment["conditions"]["environment"] == environment
        assert assessment["sources"]["charge"] == "goal:" + identifier
        assert set(assessment["reasons"]) == {"unknown_condition:method", "unknown_condition:basis"}
    assert (request, run) == before


def test_real_t5_geometry_preparation_consumes_only_agreeing_electronic_state():
    request = t5_request()
    state = preparation_conditions(request, "system_ethanol")
    assert state["status"] == "passed"
    assert state["conditions"] == {"charge": 0, "multiplicity": 1}
    assert "environment" not in state["conditions"]
    run = Run.model_validate_json((T5 / "run.json").read_bytes())
    run.input_bindings = {}
    store = SimpleNamespace(load_request=lambda _: request, load_plan=lambda _: None)
    plan = materialize_plan(store, run, {"steps": [
        {"key": "identity", "tool": "structure.resolve", "system_id": "system_ethanol"},
        {"key": "prepare", "tool": "structure.prepare", "system_id": "system_ethanol",
         "inputs": {"identity": {"producer_key": "identity", "port": "resolved_identity"}}}]})
    prepared = next(step for step in plan.steps if step.tool == "structure.prepare")
    assert prepared.parameters.charge == 0 and prepared.parameters.multiplicity == 1
    assert set(plan.goal_map) == {goal.id for goal in request.goals}


@pytest.mark.parametrize("scope", ["request", "system"])
@pytest.mark.parametrize("value,source", [(None, "explicit"), (0, "unknown"), (0, "inferred"), (1, "explicit")])
def test_scoped_goal_cannot_override_explicit_unknown_or_conflicting_charge(scope, value, source):
    request = t5_request()
    target = request if scope == "request" else request.systems[0]
    target.conditions["charge"] = value
    target.conditions_source["charge"] = source
    result = preparation_conditions(request, "system_ethanol")
    assert result["status"] == "unresolved" and result["conditions"]["charge"] is None
    assert any(reason.endswith(":charge") for reason in result["reasons"])


def test_preparation_does_not_choose_between_disagreeing_goal_states():
    request = t5_request()
    request.goals[2].conditions["multiplicity"] = 3
    assert preparation_conditions(request, "system_ethanol")["status"] == "unresolved"


def native_case(monkeypatch, goal_ids, *, step_conditions=None):
    request = selected_request()
    step = Step(id="calculation", logical_id="calculation", tool="python.execute",
        system_id="system_ethanol", parameters={
            "code": {"artifact_id": "script", "sha256": "a" * 64},
            "sdk_orca": True, "observation_names": ["energy"],
            "conditions": step_conditions or {}})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[step],
        goal_map={goal.id: OutputBinding(port=goal.port,
            **({"step_id": step.id} if goal.id in goal_ids else {"gap": "not produced by this fixture"}))
            for goal in request.goals})
    policy = NativeInputPolicy(profile="managed-native-1")
    configuration = SimpleNamespace(workflow_policy="simplified-1",
                                    execution=SimpleNamespace(native_input_policy=policy))
    store = SimpleNamespace(load_request=lambda _: request, load_plan=lambda _: plan,
                            run_configuration=lambda _: configuration)
    monkeypatch.setattr(managed, "_initial_geometry", lambda *args: (
        "geometry", SimpleNamespace(sha256=hashlib.sha256(XYZ).hexdigest()), XYZ))
    return request, plan, step, store, policy


def input_text(solvent=""):
    return (f"! PBE0 RKS def2-SVP NoAutoStart {solvent}\n%maxcore 192\n"
            "%scf MaxIter 64 AutoTRAH false end\n* xyzfile 0 1 initial.xyz\n")


@pytest.mark.parametrize("goal_id,environment,solvent,other", [
    ("goal_gas_energy", "gas_phase", "", "CPCM(water)"),
    ("goal_solvent_energy", "cpcm(water)", "CPCM(water)", ""),
])
def test_each_real_t5_goal_binds_only_its_own_environment(monkeypatch, goal_id, environment, solvent, other):
    request, plan, step, store, policy = native_case(monkeypatch, {goal_id})
    plan.validate_request(request)
    bound = managed.native_input_binding(store, object(), step)
    assert bound["conditions"]["environment"] == environment
    assert bound["conditions"]["charge"] == 0
    assert bound["conditions"]["multiplicity"] == 1
    assert "allowed_condition_values" not in bound
    admitted = validate_native_input(input_text(solvent), {"initial.xyz": XYZ},
                                    ResourceLimits(), policy=policy, binding=bound)
    assert admitted["actual_conditions"]["environment"] == environment
    with pytest.raises(ValueError, match="environment"):
        validate_native_input(input_text(other), {"initial.xyz": XYZ},
                              ResourceLimits(), policy=policy, binding=bound)


def test_single_sdk_step_preserves_both_requested_environment_variants(monkeypatch):
    request, plan, step, store, policy = native_case(
        monkeypatch, {"goal_gas_energy", "goal_solvent_energy"})
    plan.validate_request(request)
    bound = managed.native_input_binding(store, object(), step)
    assert set(bound["allowed_condition_values"]["environment"]) == {"gas_phase", "cpcm(water)"}
    actual = [validate_native_input(input_text(solvent), {"initial.xyz": XYZ},
        ResourceLimits(), policy=policy, binding=bound)["actual_conditions"]["environment"]
        for solvent in ("", "CPCM(water)")]
    assert actual == ["gas_phase", "cpcm(water)"]
    with pytest.raises(ValueError, match="environment"):
        validate_native_input(input_text("CPCM(DMSO)"), {"initial.xyz": XYZ},
                              ResourceLimits(), policy=policy, binding=bound)


def test_step_cannot_borrow_an_unmapped_goals_environment(monkeypatch):
    request, plan, step, store, _ = native_case(monkeypatch, {"goal_gas_energy"},
        step_conditions={"environment": "CPCM(water)"})
    with pytest.raises(ValueError, match="environment"):
        plan.validate_request(request)
    with pytest.raises(ValueError, match="environment"):
        managed.native_input_binding(store, object(), step)


def test_missing_or_conflicting_conditions_still_block_actual_native_execution(monkeypatch):
    request, _, step, store, _ = native_case(monkeypatch, {"goal_gas_energy", "goal_solvent_energy"})
    request.method = None
    with pytest.raises(ValueError, match="unknown_condition:method"):
        managed.native_input_binding(store, object(), step)
    request.method = "PBE0"
    request.goals[2].conditions["charge"] = 1
    with pytest.raises(ValueError, match="differing Goal condition: charge"):
        managed.native_input_binding(store, object(), step)


@pytest.mark.parametrize("evidence", [False, True])
def test_generic_goal_cannot_supply_conditions_to_a_different_system(monkeypatch, evidence):
    request, plan, step, store, _ = native_case(monkeypatch, {"goal_gas_energy"})
    request.systems.append(SystemInput(id="other_system", conditions={"charge": 1}))
    step.system_id = "other_system"
    # Use an unregistered quantity so the fixed energy-port legacy guard cannot
    # accidentally provide the generic system binding guarantee under test.
    request.goals[1].port = "generic_energy"
    step.parameters.observation_names = ["generic_energy"]
    plan.goal_map["goal_gas_energy"] = OutputBinding(port="generic_energy", **(
        {"evidence": EvidenceRef(producer_step_id=step.id, port="generic_energy")}
        if evidence else {"step_id": step.id}))
    for check in (lambda: plan.validate_request(request),
                  lambda: managed.native_input_binding(store, object(), step)):
        with pytest.raises(ValueError, match="different requested system"):
            check()
