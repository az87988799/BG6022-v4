"""Independent user-message/commit regressions for proposition grounding.

All requests and replies here are synthetic offline evidence; no model or ORCA
is invoked. The production Store proves rejected candidates have no effects.
"""

import pytest

from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.natural import apply_user_update, initialize_bundle
from orca_agent.semantic import commit_candidate
from tests.unit.test_natural import bundle_at, query_run, store_at
from tests.unit.test_semantic_control import candidate


def scoped_query(tmp_path, *, two_systems=False):
    store, run, _, _ = query_run(tmp_path)
    message = store.enqueue_message(run.id, "Register water and its independent query")
    systems = [{"id": "water"}]
    if two_systems:
        systems.append({"id": "methane"})
    run = apply_user_update(store, run.id, message, {"systems": systems})
    return store, run


def amend_field(store, run, name, value, quote, *, scoped=True, message_id=None):
    field = {"value": value, "source": "explicit", "text_basis": quote}
    if message_id:
        field["message_id"] = message_id
    fields = {"system_conditions": {"water": {name: field}}} if scoped else {"conditions": {name: field}}
    parameters = candidate(store, run, kind="amend", **fields)
    return commit_candidate(store, run, parameters, decision_id="field_commit", basis=current_basis(store, run))


@pytest.mark.parametrize("text,quote,name,value", [
    ("Do not use RHF for water.", "RHF", "method", "HF"),
    ("Is RHF suitable for water?", "RHF", "method", "HF"),
    ("Use RHF and B3LYP for water.", "RHF", "method", "HF"),
    ("Use RHF but use B3LYP for water.", "RHF", "method", "HF"),
    ("Water charge 0 and charge 1.", "charge 0", "charge", 0),
    ("Water charge 0, charge 1.", "charge 0", "charge", 0),
    ("Use RHF for water. Do not use RHF for water.", "RHF", "method", "HF"),
])
def test_clipped_uncertainty_conflict_or_duplicate_cannot_commit(tmp_path, text, quote, name, value):
    store, run = scoped_query(tmp_path)
    store.enqueue_message(run.id, text)
    before = store.load_run(run.id).model_dump_json()
    request = store.load_request(run).model_dump_json()
    with pytest.raises(ValueError):
        amend_field(store, run, name, value, quote)
    restored = store.load_run(run.id)
    assert restored.model_dump_json() == before
    assert store.load_request(restored).model_dump_json() == request
    assert not restored.calls and not restored.attempts
    assert restored.usage.orca_starts_actual == restored.usage.orca_starts_reserved == 0


@pytest.mark.parametrize("text,quote", [
    ("Water uses RHF and methane does not use RHF.", "Water uses RHF"),
    ("Methane does not use RHF but water uses RHF.", "water uses RHF"),
    ("Water uses RHF, methane does not use RHF.", "Water uses RHF"),
])
def test_one_system_assertion_survives_independent_other_system_negation(tmp_path, text, quote):
    store, run = scoped_query(tmp_path, two_systems=True)
    store.enqueue_message(run.id, text)
    updated = amend_field(store, run, "method", "HF", quote)
    request = store.load_request(updated)
    assert request.systems[0].conditions["method"] == "HF"
    assert request.systems[1].conditions == {}
    assert updated.permission == run.permission and updated.usage == run.usage
    assert not updated.calls and not updated.attempts


def test_other_system_assertion_cannot_become_global_or_water_condition(tmp_path):
    store, run = scoped_query(tmp_path, two_systems=True)
    store.enqueue_message(run.id, "Methane uses RHF, water has an unknown method.")
    before = store.load_run(run.id).model_dump_json()
    for scoped in (True, False):
        with pytest.raises(ValueError):
            amend_field(store, run, "method", "HF", "Methane uses RHF", scoped=scoped)
    assert store.load_run(run.id).model_dump_json() == before


def test_unique_current_confirmation_can_answer_an_older_pending_question(tmp_path):
    store, run = scoped_query(tmp_path)
    store.enqueue_message(run.id, "Should water use RHF?")
    confirmation = store.enqueue_message(run.id, "I confirm that water uses RHF.")
    updated = amend_field(store, run, "method", "HF", "water uses RHF", message_id=confirmation)
    request = store.load_request(updated)
    assert request.systems[0].conditions["method"] == "HF"
    evidence = request.condition_evidence["water.method"]
    assert evidence["message_id"] == confirmation
    assert evidence["text_basis"] == "water uses RHF"
    assert len(updated.processed_messages) == len(run.processed_messages) + 2
    assert updated.permission == run.permission and updated.usage == run.usage


def test_old_affirmation_cannot_override_later_explicit_unknown(tmp_path):
    store, run = scoped_query(tmp_path)
    old_message = store.enqueue_message(run.id, "Use RHF for water.")
    store.enqueue_message(run.id, "The method for water is now unknown.")
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ValueError):
        amend_field(store, run, "method", "HF", "Use RHF for water", message_id=old_message)
    assert store.load_run(run.id).model_dump_json() == before


def test_message_id_cannot_disambiguate_two_short_quotes_in_the_same_message(tmp_path):
    store, run = scoped_query(tmp_path)
    message = store.enqueue_message(run.id, "RHF? Use RHF for water.")
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ValueError, match="repeated"):
        amend_field(store, run, "method", "HF", "RHF", message_id=message)
    assert store.load_run(run.id).model_dump_json() == before


def test_natural_zero_phrase_proposes_integer_without_structured_coercion(tmp_path):
    store, run = scoped_query(tmp_path)
    store.enqueue_message(run.id, "水的电荷为零。")
    updated = amend_field(store, run, "charge", 0, "电荷为零")
    request = store.load_request(updated)
    assert type(request.systems[0].conditions["charge"]) is int
    assert request.condition_evidence["water.charge"]["text_basis"] == "电荷为零"


def raw_named_request(tmp_path, text):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nSynthetic input\nO 0 0 0\nH 0 .7 .5\nH 0 -.7 .5\n")
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        geometries=[{"id": "water", "file": "water.xyz"}]))
    return store, run


def test_pronoun_cannot_forget_explicit_unregistered_target_in_preceding_clause(tmp_path):
    text = "Ethanol is the target, report its electronic energy. Registration only."
    store, run = raw_named_request(tmp_path, text)
    parameters = candidate(store, run, kind="normalize", goals=[{
        "key": "energy", "port": "energy", "text_basis": "report its electronic energy",
        "system_refs": ["water"], "geometry_relation": "fixed_initial"}],
        notices=["Registered the requirements; scientific conditions remain incomplete."])
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ValueError, match="binding|identity|target|scope"):
        commit_candidate(store, run, parameters, decision_id="wrong_pronoun", basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before
