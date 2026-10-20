"""Actual a024 intake replay, without model/Tool execution or live data writes."""

import copy
import json
from pathlib import Path

import pytest
from test_simplified_semantic_selection import intake, rebind, submit

from orca_agent.proposals import ProposalError
from orca_agent.semantic import _explicit_energy_requests
from orca_agent.store import StoreError

FIXTURE = Path(__file__).parents[1] / "fixtures/a024-energy-delivery-reference.json"


def actual(tmp_path, *, change_text=None, policy="simplified-1"):
    record = json.loads(FIXTURE.read_bytes())
    text = record["original_text"]
    if change_text:
        text = change_text(text)
    store, _, run = intake(tmp_path, text, policy=policy)
    parameters = rebind(record["parameters"], store.read_control(run.id)["messages"][0]["id"])
    parameters["text_basis"] = text
    return store, run, parameters


def test_actual_a024_delivery_binds_existing_calculations_and_preserves_eh(tmp_path):
    store, run, parameters = actual(tmp_path)
    original = copy.deepcopy(parameters)
    updated = submit(store, run, parameters)
    request = store.load_request(updated)
    energies = [goal for goal in request.goals if goal.port == "energy"]
    assert request.normalization_status == "normalized"
    assert len(request.goals) == len(parameters["goals"]) == 5
    assert {goal.conditions["environment"] for goal in energies} == {"gas_phase", "CPCM(water)"}
    for goal in energies:
        assert goal.required and goal.conditions["geometry_relation"] == "fixed_initial"
        assert "绝对能量（Eh）" in goal.minimum_evidence
        assert goal.minimum_evidence_source["绝对能量（Eh）"] == "explicit"
        shared = next(item for item in goal.text_evidence["shared_constraints"]
                      if item["text_basis"] == "交付两份原始输入输出、共用XYZ及hash、绝对能量（Eh）")
        assert request.original_text[shared["start"]:shared["end"]] == shared["text_basis"]
        audit, = goal.text_evidence["energy_delivery_references"]
        assert audit["rule"] == "energy-delivery-reference-1"
        assert audit["minimum_evidence"] == "绝对能量（Eh）"
        assert audit["delivery"]["message_id"] == audit["calculation"]["message_id"]
        assert audit["calculation"]["end"] < audit["delivery"]["start"]
        assert audit["calculation"]["target"] == "ethanol"
    difference = next(goal for goal in request.goals if goal.port == "energy_difference")
    minimum = "并按ΔE=E(C-PCM水)-E(气相)给出kJ/mol差值及符号解释"
    assert minimum in difference.minimum_evidence
    assert difference.minimum_evidence_source[minimum] == "explicit"
    assert any(item["text_basis"] == minimum for item in difference.text_evidence["shared_constraints"])
    assert "environment" not in difference.conditions  # Operands are references, not conflicting execution settings.
    assert parameters == original and not updated.calls and not updated.attempts
    assert updated.usage.model_calls == 0
    assert not any(status == "satisfied" for status in updated.goal_status.values())


@pytest.mark.parametrize("change", ["remove_gas", "remove_water", "both_water", "remove_difference",
                                  "optional_difference", "different_source"])
def test_delivery_never_fills_a_missing_calculation_scope(tmp_path, change):
    store, run, parameters = actual(tmp_path)
    gas, water = parameters["goals"][:2]
    if change == "both_water":
        gas["conditions"] = copy.deepcopy(water["conditions"])
    elif change == "remove_difference":
        parameters["goals"].pop(2)
    elif change == "optional_difference":
        parameters["goals"][2]["required"] = False
    elif change == "different_source":
        parameters["goals"][2].update(quantity="电子能量差", text_basis="这个量是指定模型下的电子能量差")
    else:
        parameters["goals"].remove(gas if change == "remove_gas" else water)
    before = store.load_request(run).model_dump_json()
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    assert store.load_request(run).model_dump_json() == before


@pytest.mark.parametrize("before,after", [
    ("交付两份", "在甲醇溶剂中交付两份"),
    ("交付两份", "在另一套几何上交付两份"),
    ("交付两份", "改用另一套坐标。交付两份"),
    ("交付两份", "另外计算一个点。交付两份"),
    ("交付两份", "为甲烷交付两份"),
    ("绝对能量（Eh）", "未知变换的绝对能量（Eh）"),
    ("绝对能量（Eh）", "绝对能量差（Eh）"),
])
def test_new_scope_operation_or_quantity_is_not_a_delivery_reference(tmp_path, before, after):
    store, run, parameters = actual(tmp_path, change_text=lambda text: text.replace(before, after))
    # Artifact labels only lose their exact quote in variants changing the list
    # prefix; preserve their original authentic short file phrase for this probe.
    parameters["goals"][3]["text_basis"] = "两份原始输入输出"
    parameters["goals"][3]["quantity"] = "两份原始输入输出"
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"
    assert not run.calls and not run.attempts


def test_legacy_workflow_does_not_gain_delivery_association(tmp_path):
    store, run, parameters = actual(tmp_path, policy=None)
    obligations = _explicit_energy_requests(store.load_request(run), store.read_control(run.id)["messages"])
    assert not any(item.get("energy_reference_formula") for item in obligations)
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).version == 1


def test_qualified_formula_does_not_replace_the_original_difference(tmp_path):
    phrase = "归一化的 ΔE=E(C-PCM水)-E(气相)"
    store, run, parameters = actual(tmp_path, change_text=lambda text: text + "\n请报告" + phrase + "。")
    parameters["goals"][2].update(text_basis=phrase, quantity="变换差量摘要")
    with pytest.raises(ProposalError, match="electronic-energy") as caught:
        submit(store, run, parameters)
    missing = caught.value.detail["missing_energy_requests"]
    assert any(item.get("energy_kinds") == ["difference"]
               and item.get("source_quantity") == "ΔE=E(C-PCM水)-E(气相)" for item in missing)
