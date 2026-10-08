"""Raw model declarations are selected and checked, never supplied by a renderer."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request, SystemInput
from orca_agent.natural import _missing_information, initialize_agent
from orca_agent.proposals import ProposalError
from orca_agent.report import build_report
from orca_agent.semantic import SemanticCandidate, action_parameters, commit_candidate
from orca_agent.semantic_notices import (
    NOTICE_CONTRACT_VERSION,
    notice_choices,
    required_notice_kinds,
    validate_notices,
)
from orca_agent.store import Store
from orca_agent.tools.registry import SCIENCE_IDENTITIES
from tests.unit.test_agent import ScriptedTransport
from tests.unit.test_semantic_control import candidate
from tests.unit.test_semantic_energy_coverage import goal, synthetic_intake

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/repair-cycle-v23-rejections.json"


def actual_intake(tmp_path):
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"][0]
    assert recorded["variant_id"] == "N-06/raw-unsupported-system"
    response = recorded["responses"][1]
    assert response["accepted"]
    assert hashlib.sha256(response["raw_content"].encode()).hexdigest() == response["raw_content_sha256"]
    assert json.loads(response["raw_content"]) == response["proposal"]
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    request = Request.model_validate(recorded["initial_request"])
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True),
        BudgetLimits(model_calls=4, model_tokens=32000, decision_rounds=4,
                     input_tokens=12000, output_tokens=2000, corrections_per_proposal=1))
    message_id = store.enqueue_message(run.id, request.original_text)
    parameters = copy.deepcopy(response["proposal"]["parameters"])
    parameters["message_ids"] = [message_id]

    def rebind(value):
        if isinstance(value, dict):
            if "message_id" in value:
                value["message_id"] = message_id
            for child in value.values():
                rebind(child)
        elif isinstance(value, list):
            for child in value:
                rebind(child)

    rebind(parameters)
    return store, run, parameters, response


def state(store, run):
    loaded = store.load_run(run.id)
    return loaded.model_dump_json(), store.load_request(loaded).model_dump_json()


def commit(store, run, parameters):
    return commit_candidate(store, run, parameters, decision_id="new_notice_contract",
                            basis=current_basis(store, run))


def test_actual_d2_accepted_notice_is_readable_but_cannot_pass_new_declaration_coverage(tmp_path):
    store, run, parameters, response = actual_intake(tmp_path)
    assert SemanticCandidate.model_validate(response["proposal"]["parameters"]).notices == parameters["notices"]
    before = state(store, run)
    with pytest.raises(ProposalError, match="complete notice_choices") as rejected:
        commit(store, run, parameters)
    assert rejected.value.detail["missing_notice_choices"] == ["missing_geometry", "unsupported_system"]
    assert state(store, run) == before
    assert not list(store.path(f"runs/{run.id}/decisions").glob("*.json"))


@pytest.mark.parametrize("omitted", ["unsupported_system", "missing_geometry"])
def test_one_declaration_cannot_cover_two_independent_actual_gaps(tmp_path, omitted):
    store, run, parameters, _ = actual_intake(tmp_path)
    parameters["notices"] = [text for kind, text in notice_choices().items() if kind != omitted]
    before = state(store, run)
    with pytest.raises(ProposalError) as rejected:
        commit(store, run, parameters)
    assert rejected.value.detail["missing_notice_choices"] == [omitted]
    assert state(store, run) == before


def test_explicit_synthetic_choices_register_without_rewriting_model_text_or_extra_question(tmp_path):
    store, run, parameters, response = actual_intake(tmp_path)
    # Synthetic change only: the actual old notices are retained in the fixture.
    parameters["notices"] = list(notice_choices().values())
    raw_parameters = copy.deepcopy(parameters)
    proposal = {"action": "normalize_request", "parameters": parameters,
                "reason": response["proposal"]["reason"]}
    transport = ScriptedTransport(proposal)
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "paused", stopped.diagnostics
    semantic = stopped.decisions[-1]["semantics"]
    assert semantic["candidate"] == SemanticCandidate.model_validate(raw_parameters).model_dump(mode="json")
    assert semantic["notices"] == raw_parameters["notices"]
    assert semantic["notice_contract"]["version"] == NOTICE_CONTRACT_VERSION
    assert semantic["notice_contract"]["selected"] == ["missing_geometry", "unsupported_system"]
    assert semantic["questions"] == [] and not semantic["awaiting_reply"]
    report = build_report(store, stopped)
    assert report["communication"]["registration_complete"] and not report["user_goal_complete"]
    assert not stopped.calls and not stopped.attempts
    for _ in range(2):
        replayed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
        assert replayed.usage == stopped.usage and replayed.decisions == stopped.decisions


def test_invalid_declarations_use_only_the_existing_single_correction(tmp_path):
    store, run, parameters, _ = actual_intake(tmp_path)
    corrected = copy.deepcopy(parameters)
    corrected["notices"] = list(notice_choices().values())
    transport = ScriptedTransport({"action": "normalize_request", "parameters": parameters},
                                  {"action": "normalize_request", "parameters": corrected})
    stopped = agent.execute(store, Config(), run.id, transport=transport)
    assert stopped.state == "paused", stopped.diagnostics
    assert len(transport.sent) == stopped.usage.model_calls == 2
    assert stopped.usage.model_tokens_used == 150
    assert not stopped.calls and not stopped.attempts
    assert stopped.budget == run.budget and stopped.permission == run.permission


@pytest.mark.parametrize("name", ["water", "methane"])
def test_supported_target_without_geometry_only_selects_input_limit(tmp_path, name):
    text = f"Report {name} single-point electronic energy. Registration only."
    store, run = synthetic_intake(tmp_path, text)
    parameters = candidate(store, run, kind="normalize", goals=[goal(text)],
                           notices=[notice_choices()["missing_geometry"]])
    updated = commit(store, run, parameters)
    assert updated.decisions[-1]["semantics"]["notice_contract"]["required"] == {"missing_geometry": ["goal_energy"]}
    assert not updated.decisions[-1]["semantics"]["awaiting_reply"]


def test_capability_selection_cannot_be_asserted_for_supported_target(tmp_path):
    text = "Report water single-point electronic energy. Registration only."
    store, run = synthetic_intake(tmp_path, text)
    parameters = candidate(store, run, kind="normalize", goals=[goal(text)],
                           notices=list(notice_choices().values()))
    with pytest.raises(ProposalError) as rejected:
        commit(store, run, parameters)
    assert rejected.value.detail["inapplicable_notice_choices"] == ["unsupported_system"]


def test_multiple_targets_and_solvent_do_not_broaden_affected_identity(tmp_path):
    text = ("Report water single-point electronic energy. "
            "Report ethanol single-point electronic energy in water solvent. Registration only.")
    store, run = synthetic_intake(tmp_path, text)
    parameters = candidate(store, run, kind="normalize", goals=[
        goal("water single-point electronic energy", key="water_energy"),
        goal("ethanol single-point electronic energy", key="ethanol_energy")],
        notices=list(notice_choices().values()))
    updated = commit(store, run, parameters)
    required = updated.decisions[-1]["semantics"]["notice_contract"]["required"]
    assert required == {"missing_geometry": ["goal_ethanol_energy", "goal_water_energy"],
                        "unsupported_system": ["goal_ethanol_energy"]}


def test_unknown_identity_is_not_classified_as_unsupported(tmp_path):
    text = "The molecule identity is unknown. Calculate its single-point electronic energy. Registration only."
    store, run = synthetic_intake(tmp_path, text)
    item = goal("Calculate its single-point electronic energy")
    item["unresolved"] = ["ambiguous_system"]
    parameters = candidate(store, run, kind="normalize", goals=[item],
        notices=[notice_choices()["missing_geometry"]], questions=["Which molecule?"],
        question_gaps={"Which molecule?": ["ambiguous_system"]})
    updated = commit(store, run, parameters)
    assert updated.decisions[-1]["semantics"]["notice_contract"]["selected"] == ["missing_geometry"]
    assert updated.decisions[-1]["semantics"]["awaiting_reply"]


def test_registered_geometry_does_not_expand_named_scientific_scope():
    request = _missing_information(Request(geometry_artifact_id="registered_ethanol_xyz",
        goals=[Goal(id="ethanol", port="energy", identity={"canonical_names": ["ethanol"]})]))
    assert required_notice_kinds(request) == {"unsupported_system": ["ethanol"]}
    assert validate_notices(request, [notice_choices()["unsupported_system"]])["selected"] == ["unsupported_system"]


def test_geometry_absence_is_attributed_only_to_affected_goals():
    request = _missing_information(Request(systems=[
        SystemInput(id="water", geometry_artifact_id="water_xyz"), SystemInput(id="methane")], goals=[
            Goal(id="water_energy", port="energy", system_ids=["water"], identity={"canonical_names": ["water"]}),
            Goal(id="methane_energy", port="energy", system_ids=["methane"], identity={"canonical_names": ["methane"]})]))
    assert required_notice_kinds(request) == {"missing_geometry": ["methane_energy"]}
    assert "At least one" in notice_choices()["missing_geometry"]


def test_authorized_prepare_intent_is_not_a_false_missing_geometry_blocker():
    request = _missing_information(Request(systems=[SystemInput(id="water", geometry_source="prepare")],
        goals=[Goal(id="energy", port="energy", system_ids=["water"], identity={"canonical_names": ["water"]})]))
    assert required_notice_kinds(request) == {}


@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("identity,missing", [
    ({}, False),
    ({"canonical_names": ["water"]}, False),
    ({"canonical_names": ["methane"]}, True),
    ({"canonical_names": ["ethanol"]}, True),
    ({"canonical_names": [], "explicitly_unknown": True}, True),
])
def test_implicit_input_binding_respects_current_identity_but_preserves_legacy_rules(prepared, identity, missing):
    system = SystemInput(id="water", label="water", geometry_artifact_id=None if prepared else "water_xyz",
                         geometry_source="prepare" if prepared else "registered")
    request = Request(systems=[system], geometry_artifact_id="unrelated_global_xyz",
                      goals=[Goal(id="energy", port="energy", identity=identity)])
    before = request.model_dump_json()
    refreshed = _missing_information(request)
    assert ("missing:geometry" in refreshed.goals[0].unresolved) == missing
    assert request.model_dump_json() == before
    assert required_notice_kinds(refreshed).get("missing_geometry", []) == (["energy"] if missing else [])


def test_multiple_matching_systems_require_explicit_goal_binding_before_geometry_is_available():
    systems = [SystemInput(id="water_left", label="water", geometry_artifact_id="left_xyz"),
               SystemInput(id="water_right", label="water", geometry_artifact_id="right_xyz")]
    request = _missing_information(Request(systems=systems, goals=[
        Goal(id="ambiguous", port="energy", identity={"canonical_names": ["water"]}),
        Goal(id="bound", port="energy", system_ids=["water_left"], identity={"canonical_names": ["water"]})]))
    assert required_notice_kinds(request) == {"missing_geometry": ["ambiguous"]}


def test_catalog_is_same_as_validation_scope_without_default_or_permission_authority():
    choices = notice_choices()
    visible = action_parameters()["normalize_request"]
    assert visible["notice_choices"] == choices
    assert visible["notice_contract_version"] == NOTICE_CONTRACT_VERSION
    assert all(f"{name}({formula})" in choices["unsupported_system"] for name, formula in SCIENCE_IDENTITIES.items())
    assert "unsupported after geometry registration" in choices["unsupported_system"]
    # The returned projection cannot change subsequent validation or registry.
    visible["notice_choices"]["unsupported_system"] = "everything supported"
    assert notice_choices() == choices


def test_contract_keeps_extra_model_prose_without_certifying_its_semantics():
    request = _missing_information(Request(goals=[Goal(id="energy", port="energy",
        identity={"canonical_names": ["ethanol"]})]))
    notices = [*notice_choices().values(), "Additional model prose still requires independent review."]
    before = copy.deepcopy(notices)
    checked = validate_notices(request, notices)
    assert notices == before
    assert set(checked) == {"version", "required", "selected"}
    assert "facts_passed" not in checked and "semantics_passed" not in checked


def test_negated_other_target_is_not_an_unsupported_current_goal(tmp_path):
    text = "Do not calculate ethanol. Report water single-point electronic energy. Registration only."
    store, run = synthetic_intake(tmp_path, text)
    parameters = candidate(store, run, kind="normalize",
        goals=[goal("Report water single-point electronic energy")], notice_kinds=["missing_geometry"])
    updated = commit(store, run, parameters)
    assert updated.decisions[-1]["semantics"]["notice_contract"]["selected"] == ["missing_geometry"]


def test_explicitly_unknown_identity_and_free_unresolved_labels_cannot_invent_capability_limit():
    request = _missing_information(Request(goals=[
        Goal(id="uncertain", port="energy", identity={"canonical_names": ["ethanol"], "explicitly_unknown": True}),
        Goal(id="water", port="energy", identity={"canonical_names": ["water"]},
             unresolved=["unsupported_system:invented_by_candidate"])]))
    assert required_notice_kinds(request) == {"missing_geometry": ["uncertain", "water"]}


def test_repeated_exact_choice_is_not_a_second_fact():
    request = _missing_information(Request(goals=[Goal(id="energy", port="energy",
        identity={"canonical_names": ["water"]})]))
    with pytest.raises(ProposalError) as rejected:
        validate_notices(request, [notice_choices()["missing_geometry"]] * 2)
    assert rejected.value.detail["duplicate_notice_choices"] == ["missing_geometry"]


def test_persisted_legacy_registration_without_contract_is_not_revalidated_on_resume(tmp_path, monkeypatch):
    from orca_agent import semantic

    store, run, parameters, _ = actual_intake(tmp_path)
    # Create a separate local Store record from the historical accepted shape;
    # neither active history nor the source fixture/real Run is rewritten.
    recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"][0]
    record = copy.deepcopy(recorded["responses"][1]["decision"]["semantics"])
    assert "notice_contract" not in record
    historical_request = Request.model_validate(recorded["current_request"])
    historical_request.messages = store.read_control(run.id)["messages"]
    record["candidate"]["message_ids"] = parameters["message_ids"]
    legacy = store.commit_revision(run, None,
        request=historical_request,
        decision_id="legacy_registration", basis=current_basis(store, run),
        user_message_ids=parameters["message_ids"], semantic_record=record)
    legacy.state = "paused"
    store.save_run(legacy)
    before = copy.deepcopy(legacy.decisions)
    monkeypatch.setattr(semantic, "validate_notices", lambda *_: pytest.fail("old declaration revalidated"))
    replayed = agent.execute(store, Config(), legacy.id, resume=True, transport=ScriptedTransport())
    assert replayed.decisions == before and replayed.usage == legacy.usage
    assert build_report(store, replayed)["communication"]["notices"] == record["notices"]


@pytest.mark.parametrize("alter", [lambda x: x + " ", lambda x: "[" + x + "]", lambda x: x.lower()])
def test_partial_or_reworded_statement_is_not_an_exact_selection(alter):
    request = _missing_information(Request(goals=[Goal(id="energy", port="energy",
        identity={"canonical_names": ["ethanol"]})]))
    with pytest.raises(ProposalError) as rejected:
        validate_notices(request, [alter(notice_choices()["unsupported_system"]), notice_choices()["missing_geometry"]])
    assert rejected.value.detail["missing_notice_choices"] == ["unsupported_system"]
