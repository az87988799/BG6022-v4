"""Real intake replay and source boundaries; no model or scientific execution."""

import copy
import json
from pathlib import Path

import pytest
from test_simplified_semantic_selection import intake, normal, rebind, submit

from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters
from orca_agent.store import StoreError

FIXTURE = Path(__file__).parents[1] / "fixtures/a021-quantity-rejection.json"


def actual(tmp_path, *, policy="simplified-1"):
    record = json.loads(FIXTURE.read_bytes())
    store, _, run = intake(tmp_path, record["original_text"], policy=policy)
    message_id = store.read_control(run.id)["messages"][0]["id"]
    return store, run, rebind(record["parameters"], message_id)


def test_actual_a021_nonliteral_seed_label_uses_authenticated_source(tmp_path):
    store, run, parameters = actual(tmp_path)
    original = copy.deepcopy(parameters)
    before = store.load_request(run)
    updated = submit(store, run, parameters)
    request = store.load_request(updated)
    assert request.normalization_status == "normalized"
    assert request.original_text == before.original_text
    # JSON decoding gives the same real newline characters in both copies.
    assert "\n\n" in request.original_text
    assert original["text_basis"] == request.original_text
    assert parameters == original
    assert len(request.goals) == len(original["goals"]) == 7
    for goal, proposed in zip(request.goals, original["goals"], strict=True):
        assert goal.original_text == proposed["text_basis"]
        assert goal.required == proposed["required"]
        assert set(proposed["minimum_evidence"]) <= set(goal.minimum_evidence)
        assert goal.verification == proposed["verification"]
    goal = request.goals[6]
    assert goal.quantity == original["goals"][6]["text_basis"]
    assert goal.text_evidence["quantity_canonicalization"] == {
        "proposed_value": "随机数种子 1701 记录", "value": goal.quantity,
        "source": "authenticated_goal_text_basis", "rule": "source-quantity-label-1",
        "complete_source_field": "original_text"}
    assert [g.quantity for g in request.goals[:6]] == [g["quantity"] for g in original["goals"][:6]]
    semantics = updated.decisions[-1]["semantics"]
    assert semantics["proposed_candidate"]["goals"][6]["quantity"] == "随机数种子 1701 记录"
    assert semantics["candidate"]["goals"][6]["quantity"] == goal.quantity
    assert not updated.attempts and not updated.calls and not updated.result_ids
    assert updated.usage.model_calls == 0


@pytest.mark.parametrize("change", ["quote", "message", "minimum", "condition", "geometry", "qualification"])
def test_quantity_canonicalization_does_not_approve_other_proposed_claims(tmp_path, change):
    store, run, parameters = actual(tmp_path)
    if change == "quote":
        parameters["goals"][6]["text_basis"] = "不存在的原文：种子 999"
    elif change == "message":
        parameters["goals"][6]["message_id"] = "foreign_message"
    elif change == "minimum":
        parameters["goals"][6]["minimum_evidence"] = ["未提出的科学保证"]
    elif change == "condition":
        parameters["conditions"]["charge"]["value"] = 1
    elif change == "geometry":
        parameters["goals"][1]["geometry_relation"] = "optimized"
    else:
        parameters["goals"][1]["verification"] = "qualified"
    before = store.load_request(run).model_dump_json()
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).model_dump_json() == before
    assert not run.attempts and not run.calls


def test_historical_workflow_keeps_exact_quantity_rule(tmp_path):
    store, run, parameters = actual(tmp_path, policy=None)
    with pytest.raises(ProposalError) as error:
        submit(store, run, parameters)
    assert any(row["reason"] == "general_quantity_requires_exact_user_phrase"
               for row in error.value.detail["corrections"])
    assert store.load_request(run).normalization_status == "pending"


def test_simplified_prompt_does_not_require_duplicate_exact_label():
    old = action_parameters(code_authorized=True)["normalize_request"]
    new = action_parameters(code_authorized=True, workflow_simplified=True)["normalize_request"]
    assert "quantity as an exact user phrase" in old["generated_task_intent"]
    assert "quantity as an exact user phrase" not in new["generated_task_intent"]
    assert "nonliteral label becomes an authenticated" in new["generated_task_intent"]
    assert "General quantity quotes user" not in new["instruction"]
    assert "citation alone is insufficient" in new["source_reference_rules"]


def test_input_display_label_keeps_registered_input_guarantees(tmp_path):
    store, run, parameters = actual(tmp_path)
    parameters["goals"][0]["quantity"] = "HCN初始几何"
    updated = submit(store, run, parameters)
    goal = store.load_request(updated).goals[0]
    assert goal.quantity == "生成并保存一份合理结构"
    assert goal.port == "prepared_geometry"
    assert goal.evidence_contract_version == "legacy-1"
    assert goal.minimum_check_version != "generic-observation-1"


def test_omitted_quantity_uses_the_same_authenticated_source(tmp_path):
    store, run, parameters = actual(tmp_path)
    parameters["goals"][6]["quantity"] = None
    updated = submit(store, run, parameters)
    goal = store.load_request(updated).goals[6]
    assert goal.quantity == parameters["goals"][6]["text_basis"]
    assert goal.text_evidence["quantity_canonicalization"]["proposed_value"] is None


def test_long_source_cannot_hide_optimized_geometry_behind_input_excerpt(tmp_path):
    text = "请为乙醇" + "保留原文来源" * 35 + "计算优化结构。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="初始输入结构")
    parameters["goals"][0]["port"] = "prepared_geometry"
    with pytest.raises(ProposalError, match="optimized quantity"):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"


@pytest.mark.parametrize("text", [
    "请为乙醇报告" + "原文约束" * 70 + "；原文尾部义务必须保留。",
    "请为乙醇报告以下原文\n完整报告与限制。",
])
def test_bounded_display_excerpt_keeps_complete_authenticated_obligation(tmp_path, text):
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="非逐字的显示名称")
    updated = submit(store, run, parameters)
    goal = store.load_request(updated).goals[0]
    assert goal.quantity in text and len(goal.quantity) <= 160
    assert not any(ord(char) < 32 for char in goal.quantity)
    assert goal.original_text == text
    assert goal.text_evidence["text_basis"] == text
    assert goal.text_evidence["quantity_canonicalization"]["proposed_value"] == "非逐字的显示名称"


def test_paraphrased_label_cannot_replace_the_missing_energy_source(tmp_path):
    store, config, run = intake(tmp_path, "请为乙醇报告电子能。另报告结构。")
    parameters = normal(store, config, run, quantity="电子能结果")
    parameters["goals"][0]["text_basis"] = "结构"
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"
