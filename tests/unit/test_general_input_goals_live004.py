"""Actual intake replies plus a synthetic input chain; no live HTTP or calculation."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_general_identity_selection import pubchem, selections, source
from test_general_intake_correction import environment
from test_structure_tools import mock_generator

from orca_agent.condition_selection import commit_selections
from orca_agent.goals import goal_evidence_assessment
from orca_agent.model_usage import current_basis
from orca_agent.models import EvidenceRef, OutputBinding, Plan, Step
from orca_agent.proposals import ProposalError
from orca_agent.semantic import commit_candidate
from orca_agent.tools import structure
from orca_agent.tools.dispatch import execute_call

FIXTURE = Path(__file__).parents[1] / "fixtures/code_intake_live004.json"
DIGEST = "6950ada2708b20820d11c7eb1e728831ed428cb249e911095514275dfa9625e0"
XYZ = "9\nsynthetic initial, not optimized\nC 0 0 0\nC 1.5 0 0\nO 2.9 0 0\nH -.5 .9 0\nH -.5 -.9 0\nH 0 0 1\nH 1.5 1 0\nH 1.5 -1 0\nH 3.3 .85 0\n"


def replay(tmp_path, index):
    raw = FIXTURE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == DIGEST
    fixture = json.loads(raw)
    store, _, run = environment(tmp_path, fixture["user_text"])
    response = fixture["replies"][index]["response"]
    assert json.loads(response["raw_content"]) == response["proposal"]
    message_id = store.read_control(run.id)["messages"][0]["id"]

    def rebind(value):
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value

    return store, run, rebind(response["proposal"]["parameters"])


def submit(store, run, parameters):
    return commit_candidate(store, run, parameters, decision_id="offline_normalize", basis=current_basis(store, run))


def input_plan(store, run):
    request = store.load_request(run)
    system = request.systems[0].id
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id=system,
                   parameters={"system_id": system})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id=system,
        parameters={"system_id": system}, depends_on=["resolve"],
        inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity",
                                       rule_version="structure-identity-3")})
    goals = {goal.id: OutputBinding(port=goal.port, **(
        {"step_id": "prepare" if goal.port == "prepared_geometry" else "resolve"}
        if goal.port in {"prepared_geometry", "resolved_identity"} else {"gap": "Scientific observation pending."}))
        for goal in request.goals}
    return Plan(request_id=request.id, request_version=request.version, steps=[resolve, prepare], goal_map=goals)


def test_first_reply_missing_identity_message_stays_rejected_and_missing_quantities_are_not_filled(tmp_path):
    store, run, proposed = replay(tmp_path, 0)
    with pytest.raises(ProposalError) as error:
        submit(store, run, proposed)
    assert any(item["loc"] == ["parameters", "identities", 0, "message_id"]
               and item["type"] == "missing" for item in error.value.detail["errors"])
    proposed["identities"][0]["message_id"] = proposed["message_ids"][0]
    with pytest.raises(ProposalError) as error:
        submit(store, run, proposed)
    assert {tuple(item["path"]) for item in error.value.detail["corrections"]} == {
        ("parameters", "goals", 1, "quantity"), ("parameters", "goals", 2, "quantity")}
    assert not run.processed_messages and not run.calls and not run.attempts


@pytest.mark.parametrize("index", [1, 2])
def test_real_replies_preserve_goals_after_explicit_default_adoption_if_missing(tmp_path, index):
    store, run, proposed = replay(tmp_path, index)
    if index == 1:
        # This historical reply omitted two configured conditions. v42 requires
        # an explicit decision; the retained real fixture is never rewritten.
        with pytest.raises(ProposalError) as rejected:
            submit(store, run, proposed)
        rows = rejected.value.detail["corrections"]
        assert all(row["reason"] == "authorized_default_requires_explicit_adoption" for row in rows)
        for row in rows:
            proposed.setdefault("conditions", {})[row["path"][-1]] = row["authorized_default"]
    before = copy.deepcopy(proposed)
    permission = run.permission.model_dump()
    run = submit(store, run, proposed)
    request = store.load_request(run)
    assert request.normalization_status == "normalized" and not request.unresolved
    assert proposed == before and len(request.goals) == len(proposed["goals"]) == 4
    for goal, declared in zip(request.goals, proposed["goals"]):
        assert goal.original_text == declared["text_basis"]
        if goal.port == "prepared_geometry":
            assert goal.quantity == declared["quantity"]
            assert goal.minimum_check_version == "structure-prepare-3"
            assert goal.evidence_contract_version == "legacy-1"
            assert goal.verification == declared["verification"]
            assert "geometry_relation" not in goal.conditions
        elif goal.port != "knowledge_answer":
            assert goal.quantity == declared["quantity"]
            assert goal.minimum_check_version == "generic-observation-1"
            assert goal.conditions["geometry_relation"] == declared["geometry_relation"] == "optimized"
    plan = input_plan(store, run)
    run = store.commit_revision(run, plan, decision_id="offline_plan", basis=current_basis(store, run))
    assert run.permission.model_dump() == permission
    assert not run.calls and not run.attempts and not run.model_records
    assert FIXTURE.read_bytes() and hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == DIGEST


def completed_input_chain(tmp_path, monkeypatch):
    store, run, proposed = replay(tmp_path, 2)
    run = submit(store, run, proposed)
    reference = source(store, run)
    run = commit_selections(store, run, selections(store, run, reference), decision_id="offline_selection",
                            basis=current_basis(store, run))
    plan = input_plan(store, run)
    run = store.commit_revision(run, plan, decision_id="offline_plan", basis=current_basis(store, run))

    def query(_, url, **kwargs):
        data = (json.dumps({"InformationList": {"Information": [{"CID": 123,
            "Synonym": ["ethanol", "乙醇"]}]}}).encode() if "/synonyms/" in url else pubchem("CCO", "C2H6O"))
        return 200, data, True

    monkeypatch.setattr(structure, "_paced_query", query)
    generated = mock_generator(monkeypatch, xyz=XYZ)
    identity = execute_call(store, run, "structure.resolve", plan.steps[0].parameters.model_dump(), step=plan.steps[0])
    assert identity.operation_status == "completed", identity.diagnostics
    result = execute_call(store, run, "structure.prepare", plan.steps[1].parameters.model_dump(), step=plan.steps[1],
                          results={"resolve": identity})
    assert result.operation_status == "completed", result.diagnostics
    assert len(generated) == 1 and not run.attempts and run.usage.orca_starts_actual == 0
    request = store.load_request(run)
    goal = next(goal for goal in request.goals if goal.port == "prepared_geometry")
    return store, run, request, goal, result, identity


def test_registered_input_goal_uses_actual_bound_v3_checks_without_scientific_promotion(tmp_path, monkeypatch):
    store, run, request, goal, result, identity = completed_input_chain(tmp_path, monkeypatch)
    assert goal_evidence_assessment(store, run, request, goal, result)["status"] == "passed"
    identity_goal = goal.model_copy(update={"port": "resolved_identity", "minimum_check_version": "structure-identity-3"})
    assert goal_evidence_assessment(store, run, request, identity_goal, identity)["status"] == "passed"
    for scientific in request.goals:
        if scientific.port in {"optimized_geometry", "vibrational_frequencies"}:
            assert goal_evidence_assessment(store, run, request, scientific, result)["status"] == "unresolved"
    assert set(result.qualified_outputs) == {"prepared_geometry"}


@pytest.mark.parametrize("change", ["wrong_system", "stale_request", "hash", "wrong_rule", "optimized",
                                    "extra_condition", "different_method", "scientific_minimum"])
def test_input_goal_rejects_wrong_scope_stale_evidence_and_scientific_claims(tmp_path, monkeypatch, change):
    store, run, request, goal, result, _ = completed_input_chain(tmp_path, monkeypatch)
    goal = goal.model_copy(deep=True)
    if change == "wrong_system":
        goal.system_ids = ["different"]
    elif change == "stale_request":
        request = request.model_copy(update={"version": request.version + 1})
    elif change == "hash":
        store.artifact_path(result.qualified_outputs[goal.port].artifact_id).write_text("changed", encoding="utf-8")
    elif change == "wrong_rule":
        goal.minimum_check_version = "structure-prepare-1"
    elif change == "optimized":
        goal.conditions["geometry_relation"] = "optimized"
    elif change == "extra_condition":
        goal.conditions["temperature_K"] = 298.15
    elif change == "different_method":
        goal.conditions["method"] = "HF"
    else:
        goal.minimum_evidence = ["converged_scf@1"]
    assert goal_evidence_assessment(store, run, request, goal, result)["status"] == "unresolved"


@pytest.mark.parametrize("field,value", [("geometry_relation", "optimized"), ("quantity", "优化结构")])
def test_input_intake_rejects_optimized_claim_without_rewriting_the_goal(tmp_path, field, value):
    store, run, proposed = replay(tmp_path, 2)
    index = next(i for i, goal in enumerate(proposed["goals"]) if goal["port"] == "prepared_geometry")
    proposed["goals"][index][field] = value
    with pytest.raises(ProposalError) as error:
        submit(store, run, proposed)
    if field == "geometry_relation":
        assert [item["path"] for item in error.value.detail["corrections"]] == [["parameters", "goals", index, field]]
    else:
        assert error.value.detail["path"] == ["parameters", "goals", index, field]
    assert proposed["goals"][index][field] == value and not run.decisions


def test_registered_input_goal_keeps_actual_unknowns_and_immutable_prior_request(tmp_path):
    store, run, proposed = replay(tmp_path, 2)
    original = store.path(f"runs/{run.id}/request-revisions/1.json").read_bytes()
    goal = next(goal for goal in proposed["goals"] if goal["port"] == "prepared_geometry")
    goal["unresolved"] = ["missing:stereochemistry_confirmation"]
    proposed["notices"] = ["Unresolved stereochemistry confirmation is retained; preparation has not completed."]
    run = submit(store, run, proposed)
    request = store.load_request(run)
    goal = next(goal for goal in request.goals if goal.port == "prepared_geometry")
    assert goal.unresolved == ["missing:stereochemistry_confirmation"]
    assert request.normalization_status == "clarification"
    assert store.path(f"runs/{run.id}/request-revisions/1.json").read_bytes() == original
    assert not run.calls and not run.attempts and not run.goal_evidence


def test_legacy_input_rule_is_not_upgraded_by_missing_information_projection():
    from orca_agent.models import Goal, Request
    from orca_agent.natural import _missing_information

    request = Request(goals=[Goal(id="prepared", port="prepared_geometry",
        minimum_check_version="structure-prepare-1", unresolved=["missing:stereochemistry_confirmation"])])
    original = request.model_dump_json()
    projected = _missing_information(request)
    assert projected.goals[0].minimum_check_version == "structure-prepare-1"
    assert projected.goals[0].unresolved == request.goals[0].unresolved
    assert request.model_dump_json() == original
