"""Offline first a023 proposal replay; no model, ORCA or live Run mutation."""

import copy
import json
from pathlib import Path

import pytest
from test_simplified_semantic_selection import intake, normal, submit

from orca_agent.proposals import ProposalError
from orca_agent.semantic import source_fragments
from orca_agent.store import StoreError

FIXTURE = Path(__file__).parents[1] / "fixtures/a023-scoped-energy-rejection.json"


def actual(tmp_path):
    record = json.loads(FIXTURE.read_bytes())
    store, _, run = intake(tmp_path, record["original_text"])
    message = store.read_control(run.id)["messages"][0]
    old = {"id": record["parameters"]["message_ids"][0], "text": record["original_text"]}
    refs = {a["id"]: b["id"] for a, b in zip(source_fragments([old]), source_fragments([message]), strict=True)}

    def rebind(value):
        if isinstance(value, dict):
            return {key: message["id"] if key == "message_id" else [message["id"]] if key == "message_ids"
                    else [refs[ref] for ref in child] if key == "source_refs" else rebind(child)
                    for key, child in value.items()}
        return [rebind(item) for item in value] if isinstance(value, list) else value

    return store, run, rebind(record["parameters"])


def test_actual_a023_first_proposal_retains_both_scopes_without_label_rewrite(tmp_path):
    store, run, parameters = actual(tmp_path)
    proposed = copy.deepcopy(parameters)
    updated = submit(store, run, parameters)
    request = store.load_request(updated)
    assert request.normalization_status == "normalized"
    energies = [goal for goal in request.goals if goal.port == "energy"]
    assert len(energies) == 2 and all(goal.required for goal in energies)
    assert {goal.conditions["environment"] for goal in energies} == {"gas_phase", "CPCM(water)"}
    assert all(goal.conditions["geometry_relation"] == "fixed_initial" for goal in energies)
    assert {goal.quantity for goal in energies} == {"分别计算气相与ORCA C-PCM水环境下的单点电子能量"}
    assert all(goal.text_evidence["quantity_canonicalization"]["rule"] == "source-quantity-label-1"
               for goal in energies)
    assert len(request.goals) == len(parameters["goals"]) == 5
    assert parameters == proposed and not updated.calls and not updated.attempts
    assert updated.usage.model_calls == 0


@pytest.mark.parametrize("change", ["remove_gas", "remove_water", "both_water", "both_gas", "remove_difference",
                                  "gas_quote_only", "foreign_source"])
def test_scoped_source_coverage_does_not_invent_missing_goal_or_condition(tmp_path, change):
    store, run, parameters = actual(tmp_path)
    goals = parameters["goals"]
    by_key = {goal["key"]: goal for goal in goals}
    if change.startswith("remove_"):
        key = {"remove_gas": "energy_gas", "remove_water": "energy_cpcm",
               "remove_difference": "energy_difference"}[change]
        goals.remove(by_key[key])
    elif change == "both_water":
        by_key["energy_gas"]["conditions"] = copy.deepcopy(by_key["energy_cpcm"]["conditions"])
    elif change == "both_gas":
        by_key["energy_cpcm"]["conditions"] = copy.deepcopy(by_key["energy_gas"]["conditions"])
    elif change == "gas_quote_only":
        by_key["energy_gas"]["text_basis"] = "气相"
    else:
        by_key["energy_gas"]["message_id"] = "foreign_message"
    before = store.load_request(run).model_dump_json()
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).model_dump_json() == before
    assert not run.calls and not run.attempts


@pytest.mark.parametrize("metric", ["归一化电子能", "电子能阈值", "电子能计算次数"])
def test_source_derived_metric_does_not_become_raw_energy_from_display_canonicalization(tmp_path, metric):
    text = f"请为乙醇报告电子能。再报告{metric}。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="摘要标签")
    parameters["goals"][0]["text_basis"] = metric
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"
