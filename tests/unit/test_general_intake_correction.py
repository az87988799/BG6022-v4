"""Replay retained real replies offline; corrected candidates are test-authored."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_semantic_control import candidate
from test_text_entry import defaults, text_environment

from orca_agent.condition_selection import selectable_conditions
from orca_agent.model_usage import current_basis
from orca_agent.natural import _missing_information, initialize_text
from orca_agent.proposals import ProposalError
from orca_agent.semantic import commit_candidate
from orca_agent.store import StoreError

FIXTURE = Path(__file__).parents[1] / "fixtures/code_intake_live001.json"
FIXTURE_SHA256 = "1dd1c4a4259d54947688a66ef5d12823307110bb83fdb231ee6909c932715c41"
TEXT = "请为乙醇生成结构，选择合适的方法和基组，优化结构并分析振动频率，说明计算条件和结果验证情况。"


def environment(tmp_path, text=TEXT, *, code=True):
    store, config = text_environment(tmp_path,
        defaults={"charge": 0, "multiplicity": 1, "electronic_state": "RKS", "environment": "gas_phase"},
        permission={"model_execution": True, "artifact_writes": True, "code_execution": code,
                    "external_identity_queries": True, "geometry_preparation": True,
                    "allowed_tools": ["structure.resolve", "structure.prepare", "knowledge.search"]
                                     + (["python.execute"] if code else [])})
    return store, config, initialize_text(store, config, text)


def proposal(store, config, run, *, text=TEXT, relation="optimized", quantity="振动频率"):
    message = store.read_control(run.id)["messages"][0]
    return candidate(store, run, kind="normalize", conditions=defaults(config),
        identities=[{"key": "target", "name": "乙醇", "query_name": "ethanol",
                     "text_basis": text, "message_id": message["id"]}],
        goals=[{"key": "frequency", "port": "vibrational_frequencies", "quantity": quantity,
                "geometry_relation": relation, "text_basis": text, "system_refs": ["target"],
                "verification": "reported_with_limitations", "message_id": message["id"]}],
        automatic_selection={field: {"text_basis": "选择合适的方法和基组", "message_id": message["id"]}
                             for field in ("method", "basis")} if "选择合适的方法和基组" in text else {})


def commit(store, run, parameters):
    return commit_candidate(store, run, parameters, decision_id="candidate", basis=current_basis(store, run))


@pytest.mark.parametrize("index", [0, 1])
def test_real_live001_replies_are_rejected_without_rewriting_historical_semantics(tmp_path, index):
    before = FIXTURE.read_bytes()
    assert hashlib.sha256(before).hexdigest() == FIXTURE_SHA256
    fixture = json.loads(before)
    assert fixture["user_text"] == TEXT
    assert [reply["historical_status"] for reply in fixture["replies"]] == ["rejected", "accepted_waiting_user"]
    assert fixture["replies"][0]["model_id"].startswith("model_f766")
    retained = fixture["replies"][index]
    assert json.loads(retained["raw_content"]) == retained["proposal"]
    store, _, run = environment(tmp_path)
    parameters = copy.deepcopy(retained["proposal"]["parameters"])
    replacement = store.read_control(run.id)["messages"][0]["id"]
    def rebind(value):
        # Only authenticate this replay's local message identity. All semantic
        # values, including the historical errors, remain literal fixture data.
        if isinstance(value, dict):
            return {key: replacement if key == "message_id" else [replacement] if key == "message_ids"
                    else rebind(item) for key, item in value.items()}
        return [rebind(item) for item in value] if isinstance(value, list) else value
    with pytest.raises(ProposalError) as rejected:
        commit(store, run, rebind(parameters))
    reasons = {entry["reason"] for entry in rejected.value.detail["corrections"]}
    assert "delegated_choice_is_not_unknown" in reasons
    assert "general_quantity_requires_exact_user_phrase" in reasons
    assert "legacy_qualified_scope_is_not_a_general_molecule_whitelist" in reasons
    if index == 0:
        assert "no_user_demand_for_specialized_qualification" in reasons
    else:
        assert "no_registered_checker_does_not_make_quantity_unknown" in reasons
    assert store.load_request(run).normalization_status == "pending"
    assert run.request_version == 1 and not run.calls and not run.attempts
    assert not run.processed_messages and not run.decisions
    assert FIXTURE.read_bytes() == before


def test_test_authored_correction_preserves_both_goals_and_authorization_without_selecting_values(tmp_path):
    store, config, run = environment(tmp_path)
    parameters = proposal(store, config, run)
    geometry = copy.deepcopy(parameters["goals"][0])
    geometry.update(key="geometry", port="optimized_geometry", quantity="优化结构")
    parameters["goals"].insert(0, geometry)
    run = commit(store, run, parameters)
    request = store.load_request(run)
    assert [goal.quantity for goal in request.goals] == ["优化结构", "振动频率"]
    assert all(goal.conditions == {"geometry_relation": "optimized"} for goal in request.goals)
    assert all(goal.evidence_contract_version == "generic-observation-1" for goal in request.goals)
    assert all(goal.verification == "reported_with_limitations" and not goal.unresolved for goal in request.goals)
    assert request.method is request.basis is None and not request.unresolved
    assert selectable_conditions(request) == ["method", "basis"]
    assert request.original_text == TEXT and not run.calls and not run.attempts


@pytest.mark.parametrize(("text", "relation"), [
    ("请优化乙醇结构并分析振动频率。", "optimized"),
    ("请报告乙醇初始几何的极化率。", "fixed_initial"),
])
def test_explicit_geometry_relation_cannot_be_omitted_or_reversed(tmp_path, text, relation):
    store, config, run = environment(tmp_path, text)
    quantity = "振动频率" if "振动频率" in text else "极化率"
    for value in (None, "fixed_initial" if relation == "optimized" else "optimized"):
        parameters = proposal(store, config, run, text=text, relation=value, quantity=quantity)
        with pytest.raises(ProposalError) as rejected:
            commit(store, run, parameters)
        assert any(item["reason"] == "preserve_explicit_geometry_relation"
                   for item in rejected.value.detail["corrections"])


@pytest.mark.parametrize("text", ["请分析乙醇的极化率。", "乙醇采用初始几何还是优化几何未知，分析极化率。"])
def test_unknown_geometry_relation_is_not_invented(tmp_path, text):
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, quantity="极化率")
    with pytest.raises(ProposalError) as rejected:
        commit(store, run, parameters)
    assert any(item["reason"] == "geometry_relation_is_unknown_or_ambiguous_in_user_text"
               for item in rejected.value.detail["corrections"])


def test_explicit_unit_is_bound_by_program_and_remains_unverified(tmp_path):
    text = "请分析乙醇振动频率，单位为 cm^-1。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None)
    before = copy.deepcopy(parameters)
    assert parameters["goals"][0].get("minimum_evidence") is None
    run = commit(store, run, parameters)
    goal = store.load_request(run).goals[0]
    assert goal.minimum_evidence == ["单位为 cm^-1"]
    assert goal.minimum_evidence_source == {"单位为 cm^-1": "explicit"}
    assert goal.text_evidence["shared_constraints"][0]["text_basis"] == "单位为 cm^-1"
    assert parameters == before and not run.result_ids and not run.goal_evidence


def test_explicit_qualified_requirement_is_not_reclassified_as_an_available_observation(tmp_path):
    text = "请分析乙醇振动频率，必须科学合格。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None)
    parameters["goals"][0].update(verification="qualified", unresolved=["missing:registered_frequency_checker"])
    parameters.update(unresolved=["frequency_checker_unregistered"], notices=["现有检查器不能满足科学合格要求。"])
    run = commit(store, run, parameters)
    request = store.load_request(run)
    assert request.goals[0].verification == "qualified"
    assert "missing:registered_frequency_checker" in request.goals[0].unresolved
    assert "frequency_checker_unregistered" in request.unresolved


def test_real_unknown_conditions_and_goal_gaps_are_preserved(tmp_path):
    text = "乙醇的计算方法未知，振动频率的温度未知，请登记需求。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None)
    parameters["conditions"].update(method={"source": "unknown"}, temperature_K={"source": "unknown"})
    parameters["goals"][0]["unresolved"] = ["missing:temperature_definition"]
    parameters["notices"] = ["方法与温度仍然未知，等待用户明确。"]
    run = commit(store, run, parameters)
    request = store.load_request(run)
    assert request.conditions_source["method"] == "unknown" and not request.automatic_selection
    assert "unconfirmed:method" in request.unresolved
    assert "missing:temperature_definition" in request.goals[0].unresolved
    assert "missing:temperature_definition" in _missing_information(request).goals[0].unresolved


def test_explicit_stronger_requirement_cannot_be_downgraded(tmp_path):
    text = "请分析乙醇振动频率，必须科学合格。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None)
    with pytest.raises(StoreError, match="cannot be downgraded"):
        commit(store, run, parameters)
