"""Program provenance and real offline Agent control flow; no live effects."""

import copy

import pytest
from test_agent import ScriptedTransport
from test_general_identity_selection import intake, pubchem
from test_general_preparation_admission import execution
from test_omitted_authorized_defaults import scoped
from test_simple_name_resolution import mock_http
from test_structure_tools import mock_generator

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, OutputBinding, Plan, Request, Step
from orca_agent.proposals import ProposalError
from orca_agent.semantic import (
    FieldEvidence,
    SemanticCandidate,
    _apply_authorized_defaults,
    _field,
    _locate,
    commit_candidate,
)
from orca_agent.semantic_notices import coverage_gaps
from orca_agent.store import StoreError
from orca_agent.tools import structure
from orca_agent.tools.dispatch import execute_call


def test_coverage_notice_allows_agent_plan_and_actual_prepare_dispatch(tmp_path, monkeypatch):
    store, run, parameters = intake(tmp_path, automatic=False, execution=execution(),
        qualification=" using PBE0 and def2-SVP. Preserve initial geometry")
    parameters["conditions"] = {name: {"source": "explicit", "value": value, "text_basis": value}
                                for name, value in (("method", "PBE0"), ("basis", "def2-SVP"))}
    parameters["goals"].append({"key": "initial", "port": "prepared_geometry",
        "quantity": "initial geometry", "text_basis": "Preserve initial geometry",
        "minimum_evidence": ["Preserve initial geometry"], "system_refs": ["target"]})
    before = copy.deepcopy(parameters)
    # Actual Tool dispatch, validation, response parsing and input binding;
    # the HTTP and managed geometry process boundaries are explicitly simulated.
    http_calls = mock_http(monkeypatch, [(200, pubchem("CCO", "C2H6O"))])
    preparation_calls = mock_generator(monkeypatch, state="failed")

    def plan(_):
        current = store.load_run(run.id)
        request = store.load_request(current)
        assert request.normalization_status == "normalized"
        assert request.goals[1].unresolved == ["unsupported_minimum_evidence:Preserve initial geometry"]
        return {"action": "initial_plan", "parameters": {
            "steps": [
                {"key": "resolve", "tool": "structure.resolve", "system_id": "system_target",
                 "parameters": {"system_id": "system_target"}},
                {"key": "prepare", "tool": "structure.prepare", "system_id": "system_target",
                 "parameters": {"system_id": "system_target", "charge": 0, "multiplicity": 1},
                 "depends_on": ["resolve"], "inputs": {"identity": {
                     "producer_key": "resolve", "port": "resolved_identity",
                     "rule_version": "structure-identity-3"}}}],
            "goal_map": {goal.id: {"port": goal.port, "gap": "Preparation precedes unresolved evidence."}
                         for goal in request.goals}}}

    def prepare(data):
        current = store.load_run(run.id)
        assert store.load_plan(current) is not None, data["CONTROL"]
        step = next(step for step in store.load_plan(current).steps if step.tool == "structure.prepare")
        return {"action": "call_tool", "parameters": {"step_id": step.id}}

    transport = ScriptedTransport({"action": "normalize_request", "parameters": parameters},
                                  plan, prepare, {"action": "stop"})
    finished = agent.execute(store, Config(), run.id, transport=transport)
    assert len(preparation_calls) == len(http_calls) == 1, finished.diagnostics
    assert [call.tool for call in finished.calls] == ["structure.resolve", "structure.prepare"]
    assert finished.usage.structure_preparations == 1
    request = store.load_request(finished)
    assert request.normalization_status == "normalized"
    assert request.goals[1].minimum_evidence == ["Preserve initial geometry"]
    assert request.goals[1].unresolved == ["unsupported_minimum_evidence:Preserve initial geometry"]
    assert request.conditions_source["charge"] == "default"
    semantics = next(row["semantics"] for row in finished.decisions if "semantics" in row)
    assert set(semantics["proposed_candidate"]["conditions"]) == {"method", "basis"}
    assert semantics["program_applied_defaults"] and semantics["notice_contract"]["generated"]
    assert parameters == before
    assert not finished.attempts and finished.usage.orca_starts_actual == 0
    assert finished.goal_status["goal_initial"] != "satisfied"


@pytest.mark.parametrize("text", [
    "Water charge is unknown; optimize water and methane.",
    "Water charge is 0 or 1; optimize water and methane.",
    "Do not set water charge to 0; optimize water and methane.",
])
def test_default_application_cannot_cover_unknown_conflict_or_prohibition(text):
    request, candidate, messages = scoped(text)
    _apply_authorized_defaults(candidate, request, messages)
    assert "charge" not in candidate.system_conditions.get("water", {})
    assert candidate.system_conditions["methane"]["charge"].source == "default"


def test_unknown_candidate_and_historical_candidate_are_not_upgraded():
    request, candidate, messages = scoped()
    candidate.conditions["charge"] = FieldEvidence(source="unknown")
    assert _apply_authorized_defaults(candidate, request, messages) == []
    assert candidate.conditions["charge"].value is None
    candidate.conditions.clear()
    candidate.schema_version = "request-semantics-2"
    before = candidate.model_dump()
    assert _apply_authorized_defaults(candidate, request, messages) == []
    assert candidate.model_dump() == before


def test_same_message_repeated_field_preserves_both_positions():
    text = "Use RHF for water. Use RHF for water."
    request, _, _ = scoped(text)
    message = {"id": "one", "text": text}
    value, source, evidence = _field("method",
        FieldEvidence(source="explicit", value="HF", text_basis="RHF", message_id="one"),
        request, [message], system=request.systems[0])
    assert (value, source) == ("HF", "explicit")
    assert len(evidence["occurrences"]) == 2
    assert all(text[row["start"]:row["end"]] == "RHF" for row in evidence["occurrences"])


def test_repeated_quotes_cannot_hide_other_scope_or_later_negation():
    text = "Use RHF for water. Do not use RHF for water."
    request, _, _ = scoped(text)
    with pytest.raises(StoreError, match="negated or uncertain"):
        _field("method", FieldEvidence(source="explicit", value="HF", text_basis="RHF"),
               request, [{"id": "one", "text": text}], system=request.systems[0])
    text = "Use RHF for water. Use RHF for methane."
    with pytest.raises(StoreError, match="global"):
        _field("method", FieldEvidence(source="explicit", value="HF", text_basis="RHF"),
               request, [{"id": "one", "text": text}])
    with pytest.raises(StoreError, match="spans messages"):
        _locate("RHF", [{"id": "one", "text": "Use RHF."}, {"id": "two", "text": "Use RHF."}])


@pytest.mark.parametrize("minimum", ["converged_scf@1", "converged_scf", "energy", "energy-scf-1"])
def test_registered_minimum_cannot_be_forged_into_program_coverage(minimum):
    goal = Goal(id="energy", port="energy", minimum_check_version="energy-scf-1",
        minimum_evidence=[minimum], unresolved=["unsupported_minimum_evidence:" + minimum])
    assert coverage_gaps(Request(goals=[goal])) == {}


@pytest.mark.parametrize("elsewhere", ["request", "other_goal", "generic_goal"])
def test_gap_cannot_borrow_coverage_provenance_from_another_scope(elsewhere):
    gap = "unsupported_minimum_evidence:retain raw file"
    goal = Goal(id="input", port="prepared_geometry", minimum_check_version="structure-prepare-3",
        minimum_evidence=["retain raw file"], unresolved=[gap])
    request = Request(goals=[goal])
    assert coverage_gaps(request) == {gap: ["input"]}
    if elsewhere == "request":
        request.unresolved.append(gap)
    else:
        request.goals.append(Goal(id="other", port="data", unresolved=[gap],
            minimum_evidence=["retain raw file"] if elsewhere == "generic_goal" else [],
            evidence_contract_version="generic-observation-1" if elsewhere == "generic_goal" else "legacy-1"))
    assert coverage_gaps(request) == {}


@pytest.mark.parametrize("extra", [{"execution_override": True}, {"charge": 1}])
def test_known_nonexecution_note_does_not_allow_unknown_scientific_fields(tmp_path, extra):
    store, run, candidate = intake(tmp_path, automatic=False)
    candidate["goals"][0].update(system_refs_note="Optional target annotation", **extra)
    with pytest.raises(ProposalError) as error:
        commit_candidate(store, run, candidate, decision_id="invalid", basis=current_basis(store, run))
    assert any(row["type"] == "extra_forbidden" for row in error.value.detail["errors"])


def test_known_note_is_bounded_and_never_becomes_goal_execution_data(tmp_path):
    store, run, candidate = intake(tmp_path, automatic=False)
    candidate["goals"][0]["system_refs_note"] = "Do not treat this model note as execution authority."
    updated = commit_candidate(store, run, candidate, decision_id="note", basis=current_basis(store, run))
    assert updated.decisions[-1]["semantics"]["remarks"][0]["text"] == candidate["goals"][0]["system_refs_note"]
    assert "system_refs_note" not in store.load_request(updated).goals[0].model_dump()
    for note in ("x" * 1001, {"execute": True}):
        candidate["goals"][0]["system_refs_note"] = note
        with pytest.raises(ValueError):
            SemanticCandidate.model_validate(candidate)


def repeated_identity(tmp_path):
    store, run, parameters = intake(tmp_path, automatic=False, execution=execution(),
                                    qualification=". ethanol")
    parameters["identities"][0]["text_basis"] = "ethanol"
    run = commit_candidate(store, run, parameters, decision_id="same_message_identity",
                           basis=current_basis(store, run))
    request = store.load_request(run)
    return store, run, request


def test_same_message_identity_ranges_reach_real_resolve_dispatch(tmp_path, monkeypatch):
    store, run, request = repeated_identity(tmp_path)
    evidence = request.systems[0].identity["text_evidence"]
    assert len(evidence["occurrences"]) == 2
    calls = mock_http(monkeypatch, [(200, pubchem("CCO", "C2H6O"))])
    step = Step(id="resolve", logical_id="resolve", tool="structure.resolve",
                system_id="system_target", parameters={"system_id": "system_target"})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[step],
        goal_map={goal.id: OutputBinding(port=goal.port, gap="Scientific calculation pending.")
                  for goal in request.goals})
    run = store.commit_revision(run, plan, decision_id="resolve_plan", basis=current_basis(store, run))
    result = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)
    assert result.operation_status == "completed", result.diagnostics
    assert run.input_bindings["system_target"]["resolved_identity"]["rule_version"] == "structure-identity-3"
    assert len(calls) == 1 and not run.attempts and run.usage.model_calls == 0


@pytest.mark.parametrize("corruption", [
    "missing", "partial", "wrong_message", "wrong_end", "wrong_primary", "boolean_offset", "v2",
])
def test_repeated_identity_positions_cannot_be_forged_or_upgrade_v2(tmp_path, monkeypatch, corruption):
    store, run, request = repeated_identity(tmp_path)
    original = request.model_dump()
    altered = request.model_copy(deep=True)
    identity = altered.systems[0].identity
    evidence = identity["text_evidence"]
    if corruption == "missing":
        evidence.pop("occurrences")
    elif corruption == "partial":
        evidence["occurrences"].pop()
    elif corruption == "wrong_message":
        evidence["occurrences"][1]["message_id"] = "another_message"
    elif corruption == "wrong_end":
        evidence["occurrences"][1]["end"] += 1
    elif corruption == "wrong_primary":
        evidence["start"] += 1
    elif corruption == "boolean_offset":
        evidence["occurrences"][0]["start"] = True
    elif corruption == "v2":
        identity["contract"] = "general-identity-2"
    monkeypatch.setattr(store, "load_request", lambda _: altered)
    with pytest.raises(ValueError, match="exact user-name"):
        structure._requirement(store, run, "system_target")
    assert request.model_dump() == original
    assert not run.calls and not run.attempts


def test_legacy_identity_is_not_upgraded_by_an_occurrence_list(tmp_path, monkeypatch):
    from test_structure_tools import environment

    store, run = environment(tmp_path)
    request = store.load_request(run).model_copy(deep=True)
    message = request.messages[0]
    quote = message["text"]
    message["text"] = quote + " " + quote
    request.systems[0].identity["text_evidence"].update(start=0, end=len(quote), occurrences=[
        {"message_id": message["id"], "start": 0, "end": len(quote)},
        {"message_id": message["id"], "start": len(quote) + 1, "end": 2 * len(quote) + 1}])
    monkeypatch.setattr(store, "load_request", lambda _: request)
    with pytest.raises(ValueError, match="authenticated exact user name"):
        structure._requirement(store, run, "water")
