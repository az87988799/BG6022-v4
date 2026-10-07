"""Raw placeholders are replaced; real user uncertainty remains unresolved."""

import copy
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.natural import initialize_bundle
from orca_agent.proposals import ProposalError
from orca_agent.semantic import RULES, action_parameters, commit_candidate
from tests.helpers.phase_b_model_cases import create_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-proposal-v3.json"
V3 = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_actual_v3_placeholder_shape_still_rejects_and_only_test_authored_correction_normalizes(tmp_path):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-v3-replay")
    proposed = copy.deepcopy(V3["proposal"]["parameters"])
    messages = store.read_control(run.id)["messages"]
    proposed["message_ids"] = [messages[0]["id"]]
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError, match="visible question") as caught:
        commit_candidate(store, run, proposed, decision_id="offline_v3_placeholder", basis=current_basis(store, run))
    assert caught.value.detail["new_unresolved"] == ["unsupported_quantity:unresolved"]
    assert store.load_run(run.id).model_dump_json() == before
    prepared = build_context(store.load_request(run), run, relevant_tools=[], user_messages=messages,
        action_parameters=action_parameters(),
        feedback={"validation_error": {"category": "ProposalError", "requirement": caught.value.detail}})
    data = payload(prepared)
    contract = data["ACTION_PARAMETERS"]["normalize_request"]
    assert prepared.input_token_bound <= 12000
    assert "PLAN_RULES" not in data and "PLAN_REFERENCES" not in data
    assert "normalize replaces initial raw_request/missing:goal_definition with actual requested Goals" in contract[
        "instruction"]
    assert "Registration-only/no-execution limits are not extra Goals" in contract["instruction"]
    assert "only actual user unsupported/unknown requirements" in contract["instruction"]
    assert "preserve goals.unresolved" not in contract["instruction"]
    assert "normalize defines raw_request" not in contract["instruction"]
    ports = contract["instruction"].split("Goal ports:", 1)[1].split(".", 1)[0].split(",")
    assert set(ports) == set(contract["minimum_evidence_rules"]["port_rules"]) == set(RULES) - {"unresolved"}
    # Removing the unjustified extra Goal is a developer-authored correction;
    # production must neither silently remove it nor count this as a live pass.
    proposed["goals"] = proposed["goals"][:1]
    updated = commit_candidate(store, run, proposed, decision_id="offline_v3_corrected", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and not request.unresolved
    assert [goal.port for goal in request.goals] == ["energy"]
    assert request.goals[0].conditions["geometry_relation"] == "fixed_initial"
    assert not request.goals[0].unresolved and request.goals[0].minimum_evidence == ["converged_scf@1"]
    assert not updated.attempts and not updated.calls and not updated.model_records
    assert updated.usage.orca_starts_actual == updated.usage.model_calls == 0


@pytest.mark.parametrize("uncertainty", ["missing_condition", "unsupported_quantity", "unsupported_minimum"])
def test_actual_user_gaps_remain_goals_and_clarification_after_placeholder_replacement(tmp_path, uncertainty):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nsynthetic geometry\nO 0 0 0\nH 0 0.8 0.6\nH 0 -0.8 0.6\n")
    text = "登记水的单点电子能。采用气相 RHF/STO-3G，中性单重态。本轮只登记，不启动计算。"
    if uncertainty == "missing_condition":
        text = text.replace("中性单重态", "单重态，电荷待确认")
    elif uncertainty == "unsupported_quantity":
        text += "还需要水的偶极矩。"
    else:
        text += "还要求实验独立复测。"
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        geometries=[{"id": "water", "file": "water.xyz"}], allowed_tools=[]))
    fields = {"method": ("HF", "RHF"), "basis": ("STO-3G", "STO-3G"),
              "charge": (0, "中性"), "multiplicity": (1, "单重态"),
              "environment": ("gas", "气相"), "electronic_state": ("RHF", "RHF")}
    conditions = {name: {"value": value, "source": "explicit", "text_basis": quote}
                  for name, (value, quote) in fields.items()}
    goals = [{"key": "energy", "port": "energy", "text_basis": "水的单点电子能", "system_refs": ["water"],
              "geometry_relation": "fixed_initial"}]
    if uncertainty == "missing_condition":
        conditions["charge"] = {"source": "unknown", "value": None}
        expected_gap = "missing:charge:water"
    elif uncertainty == "unsupported_quantity":
        goals.append({"key": "dipole", "port": "dipole_moment", "text_basis": "水的偶极矩", "system_refs": ["water"]})
        expected_gap = "unsupported_quantity:dipole_moment"
    else:
        goals[0]["minimum_evidence"] = ["实验独立复测"]
        expected_gap = "unsupported_minimum_evidence:实验独立复测"
    proposed = candidate(store, run, kind="normalize", goals=goals, conditions=conditions,
        questions=["请确认待定电荷或保留未支持的用户要求。"])
    updated = commit_candidate(store, run, proposed, decision_id="offline_actual_gap", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "clarification"
    assert expected_gap in {gap for goal in request.goals for gap in goal.unresolved}
    assert all(goal.id != "raw_request" and goal.id != "goal_raw_request" for goal in request.goals)
    if uncertainty == "missing_condition":
        assert request.charge is None and request.conditions_source["charge"] == "unknown"
    elif uncertainty == "unsupported_quantity":
        assert request.goals[1].port == "unresolved" and request.goals[1].required
        assert request.goals[1].original_text == "水的偶极矩"
    else:
        assert request.goals[0].minimum_evidence == ["实验独立复测"]
    assert not updated.attempts and not updated.calls and not updated.model_records
