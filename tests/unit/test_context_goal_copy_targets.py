"""Current planning copy targets and actual rejection context, without HTTP."""

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orca_agent import context
from orca_agent.delivery import delivery_snapshot
from orca_agent.models import Goal, InputRef, OutputBinding, Plan, Request, Run, Step
from orca_agent.semantic import action_parameters
from orca_agent.tools.registry import get_tool
from tests.unit.test_context import objects, payload
from tests.unit.test_decision_purpose_capacity import decoded

FIXTURE = Path(__file__).parents[1] / "fixtures/phase_b/repair-cycle-v23-rejections.json"


def _actual_sampling_objects():
    archived = next(row for row in json.loads(FIXTURE.read_bytes())["runs"]
                    if row["variant_id"] == "V-06/insufficient-additional-budget")
    return Request.model_validate(archived["current_request"]), Run.model_validate(archived["run_state"])


@pytest.mark.parametrize("count", [1, 2])
def test_request_goal_ids_remain_native_even_when_repeated_in_untrusted_data(count):
    goals = [{"id": "finite_sample_internal_minimum_" + str(index), "port": "sampling"}
             for index in range(count)]
    original = {"AUTHORITY": {"request": {"goals": goals}},
                "DATA": {"untrusted_repetitions": [copy.deepcopy(goals)] * 8}}
    before = copy.deepcopy(original)
    wire = context._share_strings(original, share_lists=True)
    visible = wire["AUTHORITY"]["request"]["goals"]
    if isinstance(visible, dict):
        visible = [dict(zip(visible["@columns"], row)) for row in visible["@rows"]]
    # Do not expand SHARED_STRINGS here: the ID itself must be copyable.
    assert [goal["id"] for goal in visible] == [goal["id"] for goal in goals]
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert restored["AUTHORITY"] == original["AUTHORITY"]
    assert restored["DATA"] == original["DATA"] and original == before


def test_extra_retired_status_id_does_not_hide_current_native_request_id():
    goal_id = "finite_sample_internal_minimum"
    original = {
        "AUTHORITY": {"request": {"goals": [{"id": goal_id, "port": "sampling"}]},
                      "goal_status": {goal_id: "insufficient_evidence", "retired_goal": "satisfied"}},
        "DATA": {"untrusted_repetitions": [goal_id] * 8},
    }
    before = copy.deepcopy(original)
    wire = context._share_strings(original, share_lists=True)
    visible = wire["AUTHORITY"]["request"]["goals"]
    if isinstance(visible, dict):
        visible = [dict(zip(visible["@columns"], row)) for row in visible["@rows"]]
    # A historical cache may contain retired IDs. It cannot replace the
    # current Request as the exact native set of IDs a new Plan must bind.
    assert visible[0]["id"] == goal_id
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert restored["AUTHORITY"] == original["AUTHORITY"]
    assert restored["DATA"] == original["DATA"] and original == before


def test_planning_prompt_uses_request_ids_when_status_contains_retired_goal():
    request, run = _actual_sampling_objects()
    # Synthetic historical cache inconsistency; the original archived Run is
    # unchanged and is not claimed to have had an extra retired Goal.
    run.goal_status["retired_goal"] = "satisfied"
    before = copy.deepcopy((request, run))
    prepared = context.build_context(request, run,
        delivery_snapshot=delivery_snapshot(request, run, {}), now=run.created_at)
    prompt = prepared.body()["messages"][0]["content"]
    wire = decoded(prepared)
    if wire["AUTHORITY"].get("response_contract") == "decision-intent-2":
        assert "Map every required Goal ID/port." in prompt
        assert set(wire["RESPONSE_ENVELOPE"]) == {"action", "parameters", "reason"}
        assert "initial_plan" in wire["ACTION_SCHEMAS"]
    else:
        assert "Goal IDs=AUTHORITY.request.goals[].id." in prompt
    assert "Goal IDs=AUTHORITY.goal_status keys" not in prompt and "Goal IDs=goal_status keys" not in prompt
    current = wire["AUTHORITY"]
    assert {goal["id"] for goal in current["request"]["goals"]} == {goal.id for goal in request.goals}
    assert all(json.dumps(goal.id) in prepared.body()["messages"][1]["content"] for goal in request.goals)
    assert current["goal_status"]["retired_goal"] == "satisfied"
    assert (request, run) == before


def test_terminal_key_encoding_preserves_long_native_goal_status_id():
    goal_id = "goal_" + "copyable_current_goal_" * 4
    Goal(id=goal_id, port="energy")  # This 93-character ID is legal in the production model.
    original = {
        "AUTHORITY": {"goal_status": {goal_id: "insufficient_evidence"}},
        "DATA": {"repeated_goal_maps": [{goal_id: {"step_id": "step_1", "port": "energy"}}] * 8},
    }
    before = copy.deepcopy(original)
    wire = context._terminal_key_encoding(original)
    assert "KEYS" in wire  # Exercise actual alias compression, not its identity fallback.
    assert wire["AUTHORITY"]["goal_status"] == {goal_id: "insufficient_evidence"}
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert restored["AUTHORITY"] == original["AUTHORITY"]
    assert restored["DATA"] == original["DATA"] and original == before


@pytest.mark.parametrize("rejection", [None, 0, 1])
def test_v23_actual_goal_and_both_correction_requirements_fit_current_context(rejection):
    # Rebuild a bounded current projection from persisted facts. This is a
    # derived offline input, not another live response or a regraded old Run.
    archived = next(row for row in json.loads(FIXTURE.read_bytes())["runs"]
                    if row["variant_id"] == "V-06/insufficient-additional-budget")
    request = Request.model_validate(archived["current_request"])
    run = Run.model_validate(archived["run_state"])
    snapshot = delivery_snapshot(request, run, {})
    feedback = {}
    if rejection is not None:
        decision = [row for row in run.decisions if row.get("action") == "rejected"][rejection]
        feedback = {"validation_error": {"category": decision["parameters"]["error_category"],
                    "requirement": decision["parameters"]["requirement"]}}
    prepared = context.build_context(request, run, delivery_snapshot=snapshot,
                                     feedback=feedback, now=run.created_at)
    wire = decoded(prepared)
    assert prepared.input_token_bound <= 12000 and prepared.output_token_bound == 2000
    assert wire["AUTHORITY"]["request"]["goals"][0]["id"] == "finite_sample_internal_minimum"
    assert wire["AUTHORITY"]["request"]["conditions"]["available_evidence"] == (
        request.conditions["available_evidence"])
    actions = {"initial_plan", "clarify", "stop"}
    assert set(wire["AUTHORITY"]["decision_purpose"]["allowed_actions"]) == actions
    if wire["AUTHORITY"].get("response_contract") == "decision-intent-2":
        assert set(wire["ACTION_SCHEMAS"]) == actions
        assert set(wire["RESPONSE_ENVELOPE"]) == {"action", "parameters", "reason"}
        assert set(wire["RESPONSE_ENVELOPE"]["action"].split(" | ")) == actions
    else:
        assert set(wire["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"]) == actions
    if rejection is not None:
        assert wire["CONTROL"]["validation_error"] == feedback["validation_error"]
    assert wire["AUTHORITY"]["permission"]["scientific_execution"] is False
    assert wire["AUTHORITY"]["budget_limits"]["orca_starts"] == 0


@pytest.mark.parametrize("revision_available", [False, True])
def test_ready_step_schema_remains_visible_when_revision_can_change_parameters(revision_available):
    request, run = objects()
    run.permission.allowed_tools = ["orca.sp"]
    run.budget.orca_starts = 1
    step = Step(id="ready", logical_id="ready", tool="orca.sp", geometry=InputRef(artifact_id="geometry1"))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={request.goals[0].id: OutputBinding(step_id=step.id, port="energy")})
    run.plan_id, run.plan_version = plan.id, plan.version
    run.initial_science_steps = [step.logical_id]
    if not revision_available:
        run.usage.plan_revisions = run.budget.plan_revisions
    before = copy.deepcopy((request, run, plan))
    prepared = context.build_context(request, run, plan,
        feedback={"pending_step_ids": [step.id]}, delivery_snapshot=delivery_snapshot(request, run, {}))
    wire = decoded(prepared)
    actions = wire["AUTHORITY"]["decision_purpose"]["allowed_actions"]
    assert ("revise_plan" in actions) is revision_available and "call_tool" in actions
    assert wire["CONTROL"]["pending_step_ids"] == [step.id]
    if revision_available:
        if "TOOLS" in wire:
            tool, = [tool for tool in wire["TOOLS"] if tool["name"] == step.tool]
            schema = tool["parameters"]
        else:
            tool, = [tool for tool in wire["TOOL_CATALOG"] if tool["name"] == step.tool]
            schema = wire["PARAMETER_SCHEMAS"][tool["parameter_schema"]]
        original = get_tool(step.tool).parameter_schema
        assert set(schema["properties"]) == set(original["properties"])
        assert schema["additionalProperties"] is False
        for field in ("cores", "memory_mb", "scf_maxiter"):
            assert schema["properties"][field]["minimum"] == original["properties"][field]["minimum"]
    assert (request, run, plan) == before


def test_sampling_intake_defers_inputs_but_preserves_specification_and_restores_planning_evidence():
    archived = next(row for row in json.loads(FIXTURE.read_bytes())["runs"]
                    if row["variant_id"] == "V-06/insufficient-additional-budget")
    request = Request.model_validate(archived["current_request"])
    run = Run.model_validate(archived["run_state"])
    before = copy.deepcopy((request, run))
    snapshot = delivery_snapshot(request, run, {})
    intake = decoded(context.build_context(request, run, delivery_snapshot=snapshot, now=run.created_at,
        action_parameters=action_parameters(request=request),
        user_messages=[{"id": "new_message", "text": "只检查既有采样证据，保留原判据、条件和候选。"}]))
    current = intake["AUTHORITY"]["request"]
    inputs = current["conditions"]["available_evidence"]
    assert inputs == {"reference": "Request.conditions.available_evidence",
                      "sha256": context._hash(request.conditions["available_evidence"]), "use": "planning_only"}
    goal = current["goals"][0]
    assert goal["conditions"]["sampling"] == request.goals[0].conditions["sampling"]
    expected_members = [{key: value for key, value in item.items() if key not in {"artifact_id", "sha256"}}
                        for item in request.goals[0].conditions["candidates"]]
    assert goal["conditions"]["candidates"]["members"] == expected_members
    assert goal["conditions"]["candidates"]["sha256"] == context._hash(request.goals[0].conditions["candidates"])
    for field in ("conditions_source", "charge", "multiplicity", "method", "basis"):
        assert current[field] == request.model_dump(mode="json")[field]
    planning = decoded(context.build_context(request, run, delivery_snapshot=snapshot, now=run.created_at))
    assert planning["AUTHORITY"]["request"]["conditions"]["available_evidence"] == (
        request.conditions["available_evidence"])
    assert (request, run) == before


@pytest.mark.parametrize("incomplete_scope", ["mixed_energy", "missing_spec", "empty_candidates"])
def test_intake_does_not_defer_member_conditions_without_all_frozen_sampling_goals(incomplete_scope):
    request, run = _actual_sampling_objects()
    # Alter only an in-memory copy of the actual fixture. The available source
    # conditions must stay visible if even one Goal cannot inherit a complete
    # frozen sampling specification.
    if incomplete_scope == "mixed_energy":
        request.goals.append(Goal(id="extra_energy", port="energy", original_text="existing electronic energy"))
        run.goal_status["extra_energy"] = "insufficient_evidence"
    elif incomplete_scope == "missing_spec":
        request.goals[0].conditions.pop("sampling")
    else:
        request.goals[0].conditions["candidates"] = []
    before = copy.deepcopy((request, run))
    snapshot = delivery_snapshot(request, run, {})
    prepared = context.build_context(request, run, delivery_snapshot=snapshot, now=run.created_at,
        action_parameters=action_parameters(request=request),
        user_messages=[{"id": "new_message", "text": "检查现有目标与条件，不改变证据。"}])
    current = decoded(prepared)["AUTHORITY"]["request"]
    evidence = request.conditions["available_evidence"]
    assert current["conditions"]["available_evidence"] == {
        "reference": "Request.conditions.available_evidence", "sha256": context._hash(evidence),
        "members": {key: {"conditions": value.get("conditions", {})}
                    for key, value in evidence.items() if isinstance(value, dict)},
    }
    if incomplete_scope == "mixed_energy":
        assert {goal["port"] for goal in current["goals"]} == {"sampling", "energy"}
    elif incomplete_scope == "missing_spec":
        assert "sampling" not in current["goals"][0]["conditions"]
    else:
        assert current["goals"][0]["conditions"]["candidates"] == []
    planning = decoded(context.build_context(request, run, delivery_snapshot=snapshot, now=run.created_at))
    assert planning["AUTHORITY"]["request"]["conditions"]["available_evidence"] == evidence
    assert (request, run) == before


@pytest.mark.parametrize("identity_width", [32, 48, 64])
def test_intake_capacity_preserves_conditions_across_bounded_identity_variants(monkeypatch, identity_width):
    # Different legal IDs change only identity bytes, not the old 12k limit or
    # any authority/conditions. Reuse the full original assertions for each
    # scope, including independently decoded model messages and planning data.
    import sys

    original = _actual_sampling_objects
    prepared_requests = []
    build = context.build_context
    def fresh_objects():
        request, run = original()
        suffix = (uuid4().hex + uuid4().hex)[:identity_width]
        request.id = "request_" + suffix
        run.request_id = request.id
        run.id = "run_" + (uuid4().hex + uuid4().hex)[:identity_width]
        return request, run
    def capture(*args, **kwargs):
        prepared = build(*args, **kwargs)
        prepared_requests.append(prepared)
        return prepared
    monkeypatch.setattr(sys.modules[__name__], "_actual_sampling_objects", fresh_objects)
    monkeypatch.setattr(context, "build_context", capture)
    for scope in ("mixed_energy", "missing_spec", "empty_candidates"):
        test_intake_does_not_defer_member_conditions_without_all_frozen_sampling_goals(scope)
    assert len(prepared_requests) == 6
    assert all(prepared.input_token_bound <= 12000 for prepared in prepared_requests)
