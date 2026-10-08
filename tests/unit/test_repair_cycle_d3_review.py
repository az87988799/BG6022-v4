"""Independent offline D3 regressions; no production or historical edits."""

import copy

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, PermissionSnapshot, Request, SystemInput
from orca_agent.natural import initialize_agent
from orca_agent.planning import validate_revision
from orca_agent.proposals import ProposalError, materialize_plan
from orca_agent.semantic import commit_candidate
from orca_agent.semantic_notices import notice_choices, required_notice_kinds
from orca_agent.store import Store
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_proposals import single, source
from tests.unit.test_semantic_control import candidate
from tests.unit.test_semantic_energy_coverage import goal


@pytest.mark.parametrize("symbol", ["read source", "电子能", "energy/point.1"])
def test_symbolic_step_labels_keep_existing_materialization_contract(tmp_path, symbol):
    store, run, request, geometry, _ = source(tmp_path)
    proposal = single()
    proposal["steps"][0]["key"] = symbol
    proposal["goal_map"]["energy"]["step_key"] = symbol
    before = copy.deepcopy(proposal)

    plan = materialize_plan(store, run, proposal)

    validate_revision(request, None, request, plan, run)
    assert plan.steps[0].id != symbol and plan.steps[0].id.startswith("step_")
    assert plan.goal_map["energy"].step_id == plan.steps[0].id
    assert plan.steps[0].geometry.artifact_id == geometry.id
    assert proposal == before
    assert store.load_plan(run) is None and not run.calls and not run.attempts


def mixed_intake(tmp_path, other_target, input_kind="registered"):
    """One genuine water input cannot be used as another target's geometry."""
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    xyz = tmp_path / "water.xyz"
    xyz.write_text("3\nSynthetic only\nO 0 0 0\nH 0 0.757 0.587\nH 0 -0.757 0.587\n",
                   encoding="utf-8")
    artifact = store.import_artifact(xyz, "initial_geometry")
    text = ("Report water single-point electronic energy. "
            f"Report {other_target} single-point electronic energy. Registration only.")
    water = (SystemInput(id="water", label="water", geometry_artifact_id=artifact.id)
             if input_kind == "registered" else
             SystemInput(id="water", label="water", geometry_source="prepare"))
    request = Request(original_text=text, normalization_status="pending",
        conditions={"environment": "gas_phase", "electronic_state": "RHF"},
        systems=[water],
        goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1",
                    original_text=text, unresolved=["missing:goal_definition"])])
    run = initialize_agent(store, Config(), request,
        PermissionSnapshot(model_execution=True, artifact_ids=[artifact.id],
            allowed_tools=["structure.resolve", "structure.prepare"],
            external_identity_queries=True, geometry_preparation=True))
    store.enqueue_message(run.id, text)
    water = goal("Report water single-point electronic energy", key="water_energy")
    water["system_refs"] = ["water"]
    other = goal(f"Report {other_target} single-point electronic energy", key="other_energy")
    return store, run, [water, other]


@pytest.mark.parametrize("target", ["ethanol", "methane"])
@pytest.mark.parametrize("input_kind", ["registered", "prepare"])
def test_other_registered_geometry_cannot_hide_unbound_target_input_gap(tmp_path, target, input_kind):
    store, run, goals = mixed_intake(tmp_path, target, input_kind)
    notices = [notice_choices()["missing_geometry"]]
    if target == "ethanol":
        notices.append(notice_choices()["unsupported_system"])
    parameters = candidate(store, run, kind="normalize", goals=goals, notices=notices)
    original = copy.deepcopy(parameters)

    updated = commit_candidate(store, run, parameters,
        decision_id="independent_mixed_notice", basis=current_basis(store, run))

    current = store.load_request(updated)
    expected = {"missing_geometry": ["goal_other_energy"]}
    if target == "ethanol":
        expected["unsupported_system"] = ["goal_other_energy"]
    assert required_notice_kinds(current) == expected
    assert updated.decisions[-1]["semantics"]["notice_contract"]["required"] == expected
    assert updated.decisions[-1]["semantics"]["notices"] == notices
    assert parameters == original
    assert not updated.calls and not updated.attempts
    assert updated.usage.model_calls == updated.usage.orca_starts_actual == 0


def test_unbound_target_missing_notice_rejects_without_rewriting_candidate(tmp_path):
    store, run, goals = mixed_intake(tmp_path, "ethanol")
    parameters = candidate(store, run, kind="normalize", goals=goals,
                           notices=[notice_choices()["unsupported_system"]])
    original = copy.deepcopy(parameters)
    persisted = store.path(f"runs/{run.id}/run.json").read_bytes()

    with pytest.raises(ProposalError) as caught:
        commit_candidate(store, run, parameters,
            decision_id="independent_missing_notice", basis=current_basis(store, run))

    assert caught.value.detail["missing_notice_choices"] == ["missing_geometry"]
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == persisted
    assert parameters == original


def test_semantic_clarification_preserves_unknown_charge_and_selected_notice(tmp_path):
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    request = Request(original_text="Report water single-point electronic energy.", charge=None,
        goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2",
                    identity={"canonical_names": ["water"]},
                    conditions={"geometry_relation": "fixed_initial"})])
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True))
    store.enqueue_message(run.id, "The molecular charge is unknown.")
    reply = {"action": "normalize_request", "parameters": candidate(store, run,
        kind="clarify", questions=["What is the molecular charge?"],
        unresolved=["missing:charge"], notices=[notice_choices()["missing_geometry"]])}
    original = copy.deepcopy(reply)
    transport = ScriptedTransport(reply)

    updated = agent.execute(store, Config(), run.id, transport=transport)

    assert updated.state == "waiting_user", updated.diagnostics
    assert len(transport.sent) == updated.usage.model_calls == 1
    assert not updated.calls and not updated.attempts
    assert store.load_request(updated).charge is None
    assert updated.decisions[-1]["semantics"]["questions"] == reply["parameters"]["questions"]
    assert updated.decisions[-1]["semantics"]["notices"] == reply["parameters"]["notices"]
    assert reply == original
