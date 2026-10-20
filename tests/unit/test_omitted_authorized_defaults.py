"""Only configured, authorized and uncontradicted omissions receive defaults."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_general_intake_correction import environment

from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, Request, SystemInput
from orca_agent.semantic import (
    FieldEvidence,
    SemanticCandidate,
    _field,
    _omitted_default_corrections,
    action_parameters,
    commit_candidate,
)
from orca_agent.store import StoreError

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live011-omitted-defaults.json"


def test_original_live011_candidate_receives_program_defaults_and_preserves_model_proposal(tmp_path):
    raw = FIXTURE.read_bytes()
    retained = json.loads(raw)
    assert retained["response_record_sha256"] == "ef37badab59d25624739dfa29f25cb9d3ee72a2fd3ad92b0f63769ba9bfabde2"
    original = retained["proposal"]["parameters"]
    assert "conditions" not in original
    store, config, run = environment(tmp_path, original["text_basis"])
    message_id = store.read_control(run.id)["messages"][0]["id"]

    def rebind(value):
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value

    proposed = rebind(original)
    before = copy.deepcopy(proposed)
    updated = commit_candidate(store, run, proposed, decision_id="normalized", basis=current_basis(store, run))
    rows = updated.decisions[-1]["semantics"]["program_applied_defaults"]
    assert {tuple(row["path"]) for row in rows} == {("conditions", name)
        for name in ("charge", "multiplicity", "electronic_state", "environment")}
    assert all(row["reason"] == "authorized_default_applied" for row in rows)
    assert {row["path"][-1]: row["authorized_default"]["value"] for row in rows} == config.text.defaults
    assert proposed == before
    assert updated.decisions[-1]["semantics"]["proposed_candidate"]["conditions"] == {}
    request = store.load_request(updated)
    assert {name: request.conditions[name] for name in config.text.defaults} == config.text.defaults
    assert all(request.conditions_source[name] == "default" for name in config.text.defaults)
    assert request.method is request.basis is None  # Delegation stays separate from defaults.
    assert not request.unresolved and not updated.calls and not updated.attempts
    assert hashlib.sha256(FIXTURE.read_bytes()).digest() == hashlib.sha256(raw).digest()


def scoped(text="Optimize water and methane.", *, defaults=None):
    request = Request(original_text=text, charge=None, multiplicity=None, method=None, basis=None,
        conditions_source={},
        goals=[Goal(id="raw", port="unresolved", minimum_check_version="unresolved-1")],
        semantic_defaults={"charge": 0} if defaults is None else defaults,
        systems=[SystemInput(id=name, label=name, geometry_source="prepare", identity={"canonical_names": [name]})
                 for name in ("water", "methane")])
    candidate = SemanticCandidate(schema_version="request-semantics-3", message_ids=["message1"],
        kind="normalize", text_basis=text, goals=[{"key": name, "port": "optimized_geometry",
            "quantity": "Optimize", "geometry_relation": "optimized", "text_basis": text, "system_refs": [name]}
            for name in ("water", "methane")])
    return request, candidate, [{"id": "message1", "text": text}]


@pytest.mark.parametrize("text", ["Water charge is unknown; optimize water and methane.",
                                 "Water charge is 1; optimize water and methane.",
                                 "Water charge is 0 or 1; optimize water and methane."])
def test_user_field_declaration_blocks_default_only_for_its_actual_scope(text):
    request, candidate, messages = scoped(text)
    before = copy.deepcopy((request, candidate))
    rows = _omitted_default_corrections(candidate, request, messages)
    assert [row["path"] for row in rows] == [["system_conditions", "methane", "charge"]]
    with pytest.raises(StoreError):
        _field("charge", FieldEvidence(source="default", default_rule="local-hf-1", value=0),
               request, messages, system=request.systems[0])
    assert (request, candidate) == before


@pytest.mark.parametrize("source", ["unknown", "inferred", "explicit", "inherited"])
def test_recorded_scope_provenance_is_not_replaced_by_default(source):
    request, candidate, messages = scoped()
    request.systems[0].conditions["charge"] = None
    request.systems[0].conditions_source["charge"] = source
    rows = _omitted_default_corrections(candidate, request, messages)
    assert [row["path"] for row in rows] == [["system_conditions", "methane", "charge"]]


def test_existing_candidate_scopes_goal_conditions_and_global_unknown_are_respected():
    request, candidate, messages = scoped()
    candidate.system_conditions["water"] = {"charge": FieldEvidence(source="unknown")}
    candidate.goals[1].conditions["charge"] = FieldEvidence(source="explicit", value=1, text_basis="charge is 1")
    assert _omitted_default_corrections(candidate, request, messages) == []
    candidate.system_conditions.clear()
    candidate.goals[1].conditions.clear()
    candidate.conditions["charge"] = FieldEvidence(source="unknown")
    assert _omitted_default_corrections(candidate, request, messages) == []


@pytest.mark.parametrize(("schema_version", "port"), [
    ("request-semantics-3", "knowledge_answer"),
    ("request-semantics-3", "value_observation"),
    ("request-semantics-3", "prepared_geometry"),
    ("request-semantics-3", "unresolved"),
    # These unregistered historical labels are not current read-only ports.
    ("request-semantics-2", "artifacts"),
    ("request-semantics-2", "observations"),
])
def test_non_scientific_goal_does_not_require_adoption(schema_version, port):
    text = "Read the existing report for water and methane."
    request, candidate, messages = scoped(text)
    candidate.schema_version = schema_version
    for goal in candidate.goals:
        goal.port = port
        goal.quantity = None
        goal.geometry_relation = None
        if port == "value_observation":
            goal.query = {"artifact_id": "existing_report"}
    before = copy.deepcopy((request, candidate))
    assert _omitted_default_corrections(candidate, request, messages) == []
    assert (request, candidate) == before


@pytest.mark.parametrize("port", ["value_observation", "unresolved", "artifacts", "observations"])
def test_general_scientific_quantity_still_requires_explicit_adoption_despite_port_label(port):
    # Keep the former fixture as a negative case: changing the label cannot
    # turn an Optimize quantity into a read-only or non-scientific request.
    request, candidate, messages = scoped()
    for goal in candidate.goals:
        goal.port = port
    before = copy.deepcopy((request, candidate))
    rows = _omitted_default_corrections(candidate, request, messages)
    assert [row["path"] for row in rows] == [
        ["system_conditions", "water", "charge"],
        ["system_conditions", "methane", "charge"],
    ]
    assert all(row["reason"] == "authorized_default_applied" for row in rows)
    assert (request, candidate) == before
    adopted = FieldEvidence(source="default", default_rule="local-hf-1", value=0)
    assert _field("charge", adopted, request, messages)[:2] == (0, "default")
    candidate.conditions["charge"] = adopted
    assert _omitted_default_corrections(candidate, request, messages) == []


def test_no_default_and_existing_confirmed_values_do_not_create_new_requirement():
    request, candidate, messages = scoped(defaults={})
    assert _omitted_default_corrections(candidate, request, messages) == []
    request.semantic_defaults = {"charge": 0}
    request.charge = 1
    request.conditions_source["charge"] = "explicit"
    assert _omitted_default_corrections(candidate, request, messages) == []


def test_contract_discloses_program_defaults_and_their_limits(tmp_path):
    store, _, run = environment(tmp_path)
    request = store.load_request(run)
    contract = action_parameters(run.permission.allowed_tools, request=request, text_input=True,
                                 code_authorized=True)["normalize_request"]
    assert "program applies omitted applicable" in contract["default_adoption"]
    assert "source=default" in contract["default_adoption"]
    assert contract["authorized_defaults"]["values"] == request.semantic_defaults
