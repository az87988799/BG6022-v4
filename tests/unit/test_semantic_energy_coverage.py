"""Bounded physical-quantity coverage; real failures and synthetic derivatives stay distinct."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request, SystemInput
from orca_agent.natural import initialize_agent
from orca_agent.proposals import ProposalError
from orca_agent.report import build_report
from orca_agent.semantic import commit_candidate
from orca_agent.store import Store, StoreError
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_semantic_control import candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/n06-goal-coverage"


def recorded():
    raw = (FIXTURE / "input.json").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_bytes())
    assert provenance["kind"] == "real_derived"
    assert hashlib.sha256(raw).hexdigest() == provenance["input_file"]["sha256"]
    return json.loads(raw)


def rebind(parameters, message_id):
    parameters = copy.deepcopy(parameters)
    parameters["message_ids"] = [message_id]

    def fields(value):
        if isinstance(value, dict):
            if "message_id" in value:
                value["message_id"] = message_id
            for child in value.values():
                fields(child)
        elif isinstance(value, list):
            for child in value:
                fields(child)
    fields(parameters)
    return parameters


def actual_intake(tmp_path):
    source = recorded()
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    run = initialize_agent(store, Config(), Request.model_validate(source["initial_request"]),
                           PermissionSnapshot.model_validate(source["permission"]),
                           BudgetLimits.model_validate(source["budget"]))
    message_id = store.enqueue_message(run.id, source["control"]["messages"][0]["text"])
    return store, run, rebind(source["proposal"]["parameters"], message_id)


def snapshot(store, run):
    current = store.load_run(run.id)
    return current.model_dump_json(), store.load_request(current).model_dump_json()


def commit(store, run, parameters, identifier="offline_semantics"):
    return commit_candidate(store, run, parameters, decision_id=identifier, basis=current_basis(store, run))


def goal(text, *, port="energy", relation="fixed_initial", key="energy"):
    return {"key": key, "port": port, "text_basis": text, "system_refs": [],
            "geometry_relation": relation, "minimum_evidence": []}


def synthetic_intake(tmp_path, text):
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    request = Request(original_text=text, normalization_status="pending",
        conditions={"environment": "gas_phase", "electronic_state": "RHF"},
        goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1",
                    original_text=text, unresolved=["missing:goal_definition"])])
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True),
                           BudgetLimits(model_calls=4, model_tokens=32000, decision_rounds=4,
                                        input_tokens=12000, output_tokens=2000))
    store.enqueue_message(run.id, text)
    return store, run


def proposed(store, run, goals, **values):
    return candidate(store, run, kind="normalize", goals=goals,
                     notices=["Registered requested scope; capability/geometry gaps remain."], **values)


def test_real_n06_candidate_is_rejected_without_activation_or_budget_mutation(tmp_path):
    source = recorded()
    assert source["historical_state"] == "waiting_user"
    assert source["accepted_communication"]["awaiting_reply"] is True
    assert source["accepted_communication"]["questions"] == []
    assert [g["port"] for g in source["accepted_request"]["goals"]] == ["optimized_geometry"]
    store, run, parameters = actual_intake(tmp_path)
    before = snapshot(store, run)
    with pytest.raises(ProposalError, match="electronic-energy") as rejected:
        commit(store, run, parameters)
    assert rejected.value.detail["path"] == ["parameters", "goals"]
    assert rejected.value.detail["missing_energy_requests"][0]["target"] == "ethanol"
    assert rejected.value.detail["missing_energy_requests"][0]["geometry_relation"] == "optimized"
    assert snapshot(store, run) == before
    assert not list(store.path(f"runs/{run.id}/decisions").glob("*.json"))


def test_real_candidate_shape_cannot_activate_through_agent_or_reset_correction_cost(tmp_path):
    store, run, parameters = actual_intake(tmp_path)
    original = store.load_request(run).model_dump_json()
    scripted = {"action": "normalize_request", "parameters": parameters}
    transport = ScriptedTransport(scripted, scripted)
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "failed" and len(transport.sent) == 2
    assert stopped.usage.model_calls == 2 and stopped.usage.model_tokens_used == 150
    assert store.load_request(stopped).model_dump_json() == original
    assert stopped.permission == run.permission and stopped.budget == run.budget
    assert not stopped.calls and not stopped.attempts and not stopped.processed_messages
    for _ in range(2):
        resumed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert resumed.usage == stopped.usage and resumed.state == "failed"


@pytest.mark.parametrize("include_geometry", [False, True])
def test_synthetic_derivative_registers_energy_and_notices_then_resumes_without_cost(tmp_path, include_geometry):
    store, run, parameters = actual_intake(tmp_path)
    # Synthetic derivative: the real candidate is kept verbatim in input.json.
    # Correct only the physical port; optionally retain a separate structure goal.
    derivative = {"kind": "synthetic_derivative", "source": "input.json",
                  "changes": ["goals[0].port = energy"]}
    structure = copy.deepcopy(parameters["goals"][0])
    parameters["goals"][0]["port"] = "energy"
    if include_geometry:
        structure["key"] = "requested_structure"
        parameters["goals"].append(structure)
        derivative["changes"].append("retain an additional optimized_geometry goal")
    transport = ScriptedTransport({"action": "normalize_request", "parameters": parameters})
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert derivative["kind"] == "synthetic_derivative"
    assert stopped.state == "paused", stopped.diagnostics
    request = store.load_request(stopped)
    assert "energy" in {g.port for g in request.goals}
    assert all(g.conditions["geometry_relation"] == "optimized" for g in request.goals)
    assert {"system:ethanol not in registered science_scope (H2O, CH4)", "energy unit not stated"} <= set(request.unresolved)
    report = build_report(store, stopped)
    assert report["communication"]["registration_complete"]
    assert not report["communication"]["awaiting_reply"] and not report["user_goal_complete"]
    assert not stopped.calls and not stopped.attempts
    assert stopped.usage.model_calls == 1 and stopped.usage.orca_starts_actual == 0
    for _ in range(2):
        resumed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert resumed.state == "paused" and resumed.usage == stopped.usage
        assert resumed.permission == stopped.permission and resumed.budget == stopped.budget


@pytest.mark.parametrize("text", [
    "给出水的单点电子能。", "优化乙醇，给出优化后的电子能。",
    "Report water electronic energy and optimize methane.",
    "Calculate methane electronic energy in water solvent.",
    "以水为溶剂计算甲烷电子能。",
    "Report water electronic energy without executing calculations.",
    "Report water electronic energy but do not execute calculations.",
    "给出水的电子能但方法未知。",
    "Report water electronic energy with unknown charge.",
])
def test_an_energy_named_key_or_geometry_goal_does_not_cover_explicit_energy(tmp_path, text):
    store, run = synthetic_intake(tmp_path, text + "本轮只登记需求，不启动计算。")
    parameters = proposed(store, run, [goal(text, port="optimized_geometry", relation="optimized")])
    before = snapshot(store, run)
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, run, parameters)
    assert snapshot(store, run) == before


@pytest.mark.parametrize("text", [
    "优化乙醇结构。不要给出电子能。", "不再报告电子能，只优化乙醇结构。",
    "是否需要计算电子能？仅登记乙醇结构。", "优化乙醇结构。",
    "Optimize ethanol; do not report electronic energy.",
    "Should I calculate electronic energy? Register ethanol geometry only.",
    "Should I report electronic energy and optimize ethanol?",
])
def test_negated_questions_and_pure_optimization_do_not_create_energy_obligations(tmp_path, text):
    store, run = synthetic_intake(tmp_path, text + "本轮只登记需求，不启动计算。")
    parameters = proposed(store, run, [goal(text, port="optimized_geometry", relation="optimized")])
    updated = commit(store, run, parameters)
    assert [g.port for g in store.load_request(updated).goals] == ["optimized_geometry"]


@pytest.mark.parametrize("wrong_target,wrong_relation,optional", [(True, False, False), (False, True, False), (False, False, True)])
def test_energy_coverage_keeps_the_requested_target_relation_and_required_status(tmp_path, wrong_target, wrong_relation, optional):
    text = "给出水的单点电子能。优化甲烷结构。本轮只登记需求，不启动计算。"
    store, run = synthetic_intake(tmp_path, text)
    item = goal("优化甲烷结构" if wrong_target else "给出水的单点电子能",
                relation="optimized" if wrong_relation else "fixed_initial")
    item["required"] = not optional
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, run, proposed(store, run, [item]))


@pytest.mark.parametrize("text,quote,target", [
    ("Report water electronic energy and optimize methane.", "Report water electronic energy", "water"),
    ("Calculate methane electronic energy in water solvent.", "methane electronic energy", "methane"),
    ("以水为溶剂计算甲烷电子能。", "甲烷电子能", "methane"),
])
def test_energy_target_is_not_the_other_task_or_solvent(tmp_path, text, quote, target):
    store, run = synthetic_intake(tmp_path, text + "本轮只登记需求，不启动计算。")
    updated = commit(store, run, proposed(store, run, [goal(quote)]))
    assert store.load_request(updated).goals[0].identity["canonical_names"] == [target]


def test_coordinated_other_quantity_does_not_create_a_second_energy_requirement(tmp_path):
    text = "Report water electronic energy and methane geometry. Registration only."
    store, run = synthetic_intake(tmp_path, text)
    parameters = proposed(store, run, [goal("water electronic energy"),
        goal("methane geometry", port="optimized_geometry", relation="optimized", key="structure")])
    updated = commit(store, run, parameters)
    assert {g.port for g in store.load_request(updated).goals} == {"energy", "optimized_geometry"}


def test_initial_later_explicit_energy_cancellation_does_not_force_a_retired_goal(tmp_path):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。本轮只登记需求。")
    store.enqueue_message(run.id, "取消电子能目标，只登记水优化结构。")
    updated = commit(store, run, proposed(store, run, [
        goal("水优化结构", port="optimized_geometry", relation="optimized")]))
    assert [g.port for g in store.load_request(updated).goals] == ["optimized_geometry"]


@pytest.mark.parametrize("withdrawal", ["不取消", "不撤回", "不要取消", "不得撤回"])
def test_negated_cancellation_keeps_the_energy_requirement(tmp_path, withdrawal):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。本轮只登记需求。")
    store.enqueue_message(run.id, withdrawal + "电子能目标。同时登记水优化结构。")
    parameters = proposed(store, run, [goal("水优化结构", port="optimized_geometry", relation="optimized")])
    before = snapshot(store, run)
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, run, parameters)
    assert snapshot(store, run) == before


@pytest.mark.parametrize("withdrawal", ["不取消", "不撤回"])
def test_negated_replacement_cannot_remove_registered_energy(tmp_path, withdrawal):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。本轮只登记需求。")
    current = commit(store, run, proposed(store, run, [goal("水的单点电子能")]))
    store.enqueue_message(current.id, withdrawal + "电子能目标。同时登记水优化结构。")
    parameters = candidate(store, current, kind="replace_goals",
        replaces=[g.id for g in store.load_request(current).goals],
        goals=[goal("水优化结构", port="optimized_geometry", relation="optimized")],
        notices=["Registered structure requirement."])
    before = snapshot(store, current)
    with pytest.raises(StoreError, match="explicit user replacement"):
        commit(store, current, parameters, "negated_replacement")
    assert snapshot(store, current) == before


@pytest.mark.parametrize("already_normalized", [False, True])
@pytest.mark.parametrize("amendment", ["方法改成HF。", "Change water method to HF."])
def test_condition_change_does_not_authorize_replacing_energy_goals(tmp_path, already_normalized, amendment):
    store, run = synthetic_intake(tmp_path, "优化水结构，给出水的单点电子能。本轮只登记需求。")
    if already_normalized:
        run = commit(store, run, proposed(store, run, [goal("水的单点电子能")]))
    store.enqueue_message(run.id, amendment + "同时登记水优化结构。")
    parameters = candidate(store, run, kind="replace_goals",
        replaces=[g.id for g in store.load_request(run).goals],
        goals=[goal("水优化结构", port="optimized_geometry", relation="optimized")],
        notices=["Registered structure requirement."])
    before = snapshot(store, run)
    with pytest.raises(StoreError, match="explicit user replacement"):
        commit(store, run, parameters, "condition_is_not_replacement")
    assert snapshot(store, run) == before


@pytest.mark.parametrize("already_normalized", [False, True])
def test_explicit_n09_quantity_replacement_remains_legal(tmp_path, already_normalized):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。本轮只登记需求。")
    if already_normalized:
        run = commit(store, run, proposed(store, run, [goal("水的单点电子能")]))
    text = "不再需要单点电子能，把目标明确替换为优化后的几何结构，沿用原科学条件；本轮不启动计算。"
    store.enqueue_message(run.id, text)
    values = dict(goals=[goal("优化后的几何结构", port="optimized_geometry", relation="optimized")],
                  notices=["Registered the explicitly replaced quantity."])
    parameters = candidate(store, run, kind="replace_goals" if already_normalized else "normalize",
        replaces=[g.id for g in store.load_request(run).goals] if already_normalized else [], **values)
    updated = commit(store, run, parameters, "explicit_quantity_replacement")
    assert [g.port for g in store.load_request(updated).goals] == ["optimized_geometry"]


def test_targeted_cancellation_does_not_retire_the_other_systems_energy(tmp_path):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。给出甲烷的单点电子能。本轮只登记需求。")
    store.enqueue_message(run.id, "取消水的电子能目标，只登记甲烷目标。")
    parameters = proposed(store, run, [goal("甲烷的单点电子能")])
    updated = commit(store, run, parameters)
    assert store.load_request(updated).goals[0].identity["canonical_names"] == ["methane"]


@pytest.mark.parametrize("withdrawal,accepted", [
    ("取消它的电子能目标", False), ("取消全部电子能目标", True),
])
def test_unnamed_cancellation_requires_a_unique_target_or_explicit_all(tmp_path, withdrawal, accepted):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。给出甲烷的单点电子能。本轮只登记需求。")
    store.enqueue_message(run.id, withdrawal + "，只登记乙醇优化结构。")
    parameters = proposed(store, run, [goal("乙醇优化结构", port="optimized_geometry", relation="optimized")])
    if accepted:
        assert [g.port for g in store.load_request(commit(store, run, parameters)).goals] == ["optimized_geometry"]
    else:
        before = snapshot(store, run)
        with pytest.raises(ProposalError, match="electronic-energy"):
            commit(store, run, parameters)
        assert snapshot(store, run) == before


def test_cross_message_pronoun_uses_a_previously_named_target_and_validated_binding(tmp_path):
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    text = "Optimize water. Registration only."
    request = Request(original_text=text, normalization_status="pending",
        systems=[SystemInput(id="water", geometry_source="prepare")],
        conditions={"environment": "gas_phase", "electronic_state": "RHF"},
        goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1", original_text=text)])
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True))
    store.enqueue_message(run.id, text)
    identifier = store.enqueue_message(run.id, "Report its electronic energy.")
    item = goal("Report its electronic energy.", relation="optimized")
    item.update(system_refs=["water"], message_id=identifier)
    updated = commit(store, run, proposed(store, run, [item]))
    assert store.load_request(updated).goals[0].system_ids == ["water"]


def test_all_initial_messages_are_covered_but_explicit_replacement_retires_old_energy(tmp_path):
    store, run = synthetic_intake(tmp_path, "给出水的单点电子能。本轮只登记需求，不启动计算。")
    store.enqueue_message(run.id, "还需要甲烷的单点电子能。")
    parameters = proposed(store, run, [goal("水的单点电子能")])
    before = snapshot(store, run)
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, run, parameters)
    assert snapshot(store, run) == before
    parameters["goals"].append(goal("甲烷的单点电子能", key="methane_energy"))
    current = commit(store, run, parameters)
    store.enqueue_message(current.id, "替换目标为优化乙醇结构，本轮只登记需求，不启动计算。")
    replacement = candidate(store, current, kind="replace_goals",
        replaces=[g.id for g in store.load_request(current).goals],
        goals=[goal("优化乙醇结构", port="optimized_geometry", relation="optimized")],
        notices=["Registered the explicitly replaced structure requirement."])
    updated = commit(store, current, replacement, "replacement")
    assert [g.port for g in store.load_request(updated).goals] == ["optimized_geometry"]


def test_replacement_message_cannot_drop_its_new_explicit_energy(tmp_path):
    store, run = synthetic_intake(tmp_path, "优化水结构。本轮只登记需求。")
    current = commit(store, run, proposed(store, run, [goal("优化水结构", port="optimized_geometry")]))
    store.enqueue_message(current.id, "替换目标为给出乙醇优化后的电子能。本轮只登记需求。")
    parameters = candidate(store, current, kind="replace_goals",
        replaces=[g.id for g in store.load_request(current).goals],
        goals=[goal("给出乙醇优化后的电子能", port="optimized_geometry", relation="optimized")],
        notices=["Registered ethanol requirement."])
    before = snapshot(store, current)
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, current, parameters, "bad_replacement")
    assert snapshot(store, current) == before


@pytest.mark.parametrize("text,gap,question", [
    ("那个分子的单点电子能。", "ambiguous_system", "那个分子具体指哪个体系？"),
    ("水的单点电子能，电荷未知。", "field:charge", "水的总电荷是多少？"),
    ("水的某个性质。", "unknown:quantity", "需要水的哪个物理量？"),
    ("水的单点电子能，方法未知。", "field:method", "需要采用哪个方法？"),
])
def test_registration_critical_unknown_requires_a_real_associated_question(tmp_path, text, gap, question):
    store, run = synthetic_intake(tmp_path, text + "本轮只登记需求，不启动计算。")
    parameters = candidate(store, run, kind="clarify", unresolved=[gap],
        notices=["The requested scope still has an unresolved critical fact."])
    if gap in {"field:charge", "field:method"}:
        parameters["conditions"] = {gap.split(":")[1]: {"source": "unknown", "value": None}}
    before = snapshot(store, run)
    with pytest.raises(ProposalError, match="actual question"):
        commit(store, run, parameters)
    assert snapshot(store, run) == before
    parameters.update(questions=[question], question_gaps={question: [gap]})
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport(
        {"action": "normalize_request", "parameters": parameters}))
    assert stopped.state == "waiting_user", stopped.diagnostics
    communication = stopped.decisions[-1]["semantics"]
    assert communication["awaiting_reply"] and communication["question_gaps"] == {question: [gap]}
    assert not stopped.calls and not stopped.attempts and stopped.usage.orca_starts_actual == 0
