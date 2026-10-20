"""Retained Goal contracts against inert archives, never real scientific evidence."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from orca_agent.config import Config
from orca_agent.models import Goal, RunConfiguration, SystemInput
from orca_agent.task_observations import assess
from tests.unit.test_general_observation_admission import general_case, recollect
from tests.unit.test_task_observation_conditions import pure_file_case

POLICY = "reported-observation-1"
RETAINED = Path(__file__).parents[1] / "fixtures/code_contracts/live-t2-a005-reported-goals.json"


def enable(run):
    run.configuration = run.configuration.model_copy(update={"observation_delivery_policy": POLICY})


def text_goal(goal, text):
    goal.original_text = goal.quantity = text
    goal.minimum_evidence = [text]
    goal.minimum_evidence_source = {text: "explicit"}
    goal.text_evidence = {"message_id": "original", "text_basis": text}


def retained_goal(port, target):
    raw = next(item for item in json.loads(RETAINED.read_text(encoding="utf-8"))["goals"]
               if item["port"] == port)
    # Only test fixture bindings change. All retained source/quantity/minimum
    # text is unmodified and the provenance file is not execution evidence.
    return Goal.model_validate(raw).model_copy(update={
        "id": target.id, "port": target.port, "system_ids": ["molecule"]})


@pytest.mark.parametrize("model", [Config, RunConfiguration])
def test_delivery_policy_is_explicit_frozen_and_absent_from_old_serialization(model):
    old = model().model_dump(mode="json")
    assert "observation_delivery_policy" not in old
    restored = model.model_validate(old)
    assert restored.model_dump(mode="json") == old
    assert model(observation_delivery_policy=None).model_dump(mode="json") == old
    chosen = model(observation_delivery_policy=POLICY)
    assert chosen.model_dump(mode="json")["observation_delivery_policy"] == POLICY
    if model is Config:
        assert chosen.run_configuration().observation_delivery_policy == POLICY
    else:
        with pytest.raises(ValidationError):
            chosen.observation_delivery_policy = None


@pytest.mark.parametrize("bad", [True, 1, "true", "reported-observation-unknown", {}])
def test_delivery_policy_is_not_a_truthy_feature_flag(bad):
    with pytest.raises(ValidationError):
        Config(observation_delivery_policy=bad)
    with pytest.raises(ValidationError):
        RunConfiguration(observation_delivery_policy=bad)


@pytest.mark.parametrize("port", ["search_filter_program", "selected_conformers", "conformer_duplicate_check",
                                  "search_provenance_report", "random_seed", "candidate_structures"])
def test_retained_task_goals_keep_raw_obligations_without_global_quantum_conditions(tmp_path, monkeypatch, port):
    store, run, request, old_goal, result = pure_file_case(tmp_path, monkeypatch)
    goal = retained_goal(port, old_goal)
    request.goals = [goal]
    request.systems = [SystemInput(id="molecule", conditions={"charge": 0, "environment": "gas_phase"})]
    request.conditions = {"method": "r2SCAN-3c", "basis": "built-in", "electronic_state": "RKS"}
    request.automatic_selection = ["method", "basis"]
    before = goal.model_dump(mode="json")
    legacy = assess(store, run, request, goal, result)
    assert legacy["status"] == "unresolved"
    enable(run)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["verification_basis"] == "reported_delivery"
    assert answer["scientific_qualification"] is False and not result.qualified_outputs
    assert answer["current_use"]["source_conditions"] == {}
    assert all(row["status"] == "unverified" and row["blocking"] is False
               for row in answer["minimum_evidence"])
    assert [row["requested"] for row in answer["minimum_evidence"]] == goal.minimum_evidence
    assert goal.model_dump(mode="json") == before


def test_retained_native_ranking_reports_unknown_obligations_but_rechecks_actual_conditions(tmp_path):
    store, run, request, old_goal, result = general_case(tmp_path)
    goal = retained_goal("energy_ranking", old_goal)
    request.goals = [goal]
    enable(run)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False
    assert all(row["status"] == "unverified" for row in answer["minimum_evidence"])
    run.internal_calls[0].actual_conditions["environment"] = "CPCM(water)"
    recollect(store, run, result)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "native_source_condition_missing_or_mismatch:environment" in answer["reasons"]


@pytest.mark.parametrize("text", ["真实ORCA单点能量比较", "请运行ORCA计算。", "ORCA能量输出结果",
    "Calculate the electronic energy using ORCA.", "Report actual ORCA results."])
def test_task_json_cannot_supply_explicit_native_execution(tmp_path, monkeypatch, text):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    text_goal(goal, text)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "explicit_native_execution_requires_native_source" in answer["reasons"]
    assert answer["scientific_qualification"] is False


def test_native_minimum_is_not_lost_when_quantity_label_is_renamed(tmp_path, monkeypatch):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    goal.quantity = "comparison"
    goal.minimum_evidence = ["在同一方法、基组和统一计算类型下完成真实ORCA单点能量比较"]
    answer = assess(store, run, request, goal, result)
    assert "explicit_native_execution_requires_native_source" in answer["reasons"]


@pytest.mark.parametrize("text", ["说明ORCA是否支持固定随机种子。", "不需要运行ORCA，请记录种子1701。",
    "Do not run ORCA; report the random seed."])
def test_native_mention_in_metadata_does_not_invent_a_computation(tmp_path, monkeypatch, text):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    text_goal(goal, text)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer


@pytest.mark.parametrize("condition,value", [("method", "HF"), ("basis", "STO-3G"),
    ("charge", 0), ("multiplicity", 1), ("electronic_state", "RHF"), ("environment", "gas_phase"),
    ("geometry_relation", "fixed_initial")])
def test_goal_physics_is_not_verified_by_task_output(tmp_path, monkeypatch, condition, value):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    goal.conditions[condition] = value
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    expected = ("geometry_relation_requires_native_evidence" if condition == "geometry_relation"
                else "scientific_condition_requires_native_evidence:" + condition)
    assert expected in answer["reasons"]


@pytest.mark.parametrize("change", ["wrong_run", "wrong_attempt", "wrong_hash", "wrong_system", "missing_value"])
def test_reported_delivery_keeps_actual_provenance_failures_hard(tmp_path, monkeypatch, change):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    text_goal(goal, "请实际保存至少8份候选，并说明局限。")
    if change == "wrong_run":
        result.run_id = "other"
    elif change == "wrong_attempt":
        result.attempt_id = "other"
    elif change == "wrong_hash":
        result.source["code_sha256"] = "a" * 64
    elif change == "wrong_system":
        request.systems = [SystemInput(id="one"), SystemInput(id="two")]
        goal.system_ids = ["one"]
    else:
        result.observation_records[0].value = None
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved", answer
    assert answer["scientific_qualification"] is False


@pytest.mark.parametrize("contract,verification", [("legacy-1", "reported_with_limitations"),
                                                 ("generic-observation-1", "qualified")])
def test_new_policy_does_not_change_other_evidence_contracts(tmp_path, monkeypatch, contract, verification):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    goal.evidence_contract_version, goal.verification = contract, verification
    text_goal(goal, "用户原文最低要求")
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "verification_basis" not in answer


def test_registered_failed_requirement_still_blocks_reported_delivery(tmp_path, monkeypatch):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    enable(run)
    goal.minimum_evidence = ["purpose_preserved@1", "converged_scf@1", "至少8份候选结构"]
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved", answer
    rows = {row["requested"]: row for row in answer["minimum_evidence"]}
    assert rows["至少8份候选结构"]["status"] == "unverified"
    assert rows["converged_scf@1"]["blocking"] is True
