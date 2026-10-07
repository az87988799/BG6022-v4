"""Semantic schemas describe the same bounded contract used at activation."""

import pytest
from test_context import payload
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.context import _schema, build_context
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, Request, SystemInput
from orca_agent.natural import apply_user_update, initialize_bundle
from orca_agent.semantic import (
    RULES,
    FieldEvidence,
    SemanticCandidate,
    _field,
    _goals,
    _resolution_matches,
    action_parameters,
    commit_candidate,
)
from orca_agent.store import StoreError
from orca_agent.tools.registry import get_tool
from tests.helpers.phase_b_model_cases import (
    create_request,
    evaluate_response,
    evaluation_variant_ids,
)


def test_query_schemas_come_from_registry_and_only_permitted_read_tools():
    contract = action_parameters(["evidence.text", "orca.sp"])["normalize_request"]
    assert contract["query_schemas"] == {"text_window": _schema(get_tool("evidence.text").parameter_schema)}
    assert "Ports:minimum_evidence_rules.port_rules" in contract["instruction"]
    assert set(contract["minimum_evidence_rules"]["port_rules"]) == set(RULES) - {"unresolved"}
    assert contract["minimum_evidence_rules"]["port_rules"] == {
        port: rule for port, rule in RULES.items() if port != "unresolved"}
    assert not action_parameters()["normalize_request"]["query_schemas"]
    query = contract["query_schemas"]["text_window"]
    assert set(query["properties"]) == {"artifact_id", "start_line", "lines"}


@pytest.mark.parametrize("variant", [variant for variant in evaluation_variant_ids() if variant.startswith("N-")])
def test_raw_driver_intake_context_fits_and_exposes_the_parameter_contract(tmp_path, variant):
    store = store_at(tmp_path)
    run, _ = create_request(store, variant, 1, category="development", freeze_label="offline-schema-contract")
    prepared = build_context(store.load_request(run), run, relevant_tools=[],
        user_messages=store.read_control(run.id)["messages"],
        action_parameters=action_parameters(run.permission.allowed_tools, request=store.load_request(run)),
        feedback={"new_result_ids": [], "pending_step_ids": [], "allowed_repairs": run.permission.allowed_repairs})
    data = payload(prepared)
    assert prepared.input_token_bound <= run.budget.input_tokens == 12000
    assert data["PROPOSAL_SCHEMA"]["properties"]["action"]["enum"] == ["normalize_request"]
    contract = data["ACTION_PARAMETERS"]["normalize_request"]
    assert "Energy:geometry_relation=fixed_initial" in contract["instruction"]
    assert "query:query_schemas[port]" in contract["instruction"]
    assert set(contract["query_schemas"]) == ({"artifact_metadata", "text_window"}
        if variant == "N-07/raw-read-only-window" else set())
    assert data["TOOL_CATALOG"] == [] and data["PARAMETER_SCHEMAS"] == {}
    assert data["AUTHORITY"]["permission"]["allowed_tools"] == run.permission.allowed_tools
    assert data["AUTHORITY"]["pending_user_message_ids"]


def inherited_request(system=None, **changes):
    return Request(method="HF", conditions={"method": "HF"}, conditions_source={"method": "explicit"},
                   systems=[system or SystemInput(id="water")],
                   goals=[Goal(id="q", port="value_observation", minimum_check_version="evidence-read-1")],
                   **changes)


def inherit_method(request, value="HF", version=None, system=None):
    return _field("method", FieldEvidence(value=value, source="inherited", system_ref="water",
        request_version=version or request.version), request, [{"text": "沿用水的科学条件"}], system=system)


def test_system_inheritance_uses_confirmed_request_when_local_field_absent():
    value, source, evidence = inherit_method(inherited_request())
    assert (value, source) == ("HF", "inherited")
    assert evidence["system_ref"] == "water" and evidence["request_version"] == 1


@pytest.mark.parametrize("provenance", ["unknown", "inferred", "not_applicable", None])
def test_system_inheritance_cannot_confirm_unknown_or_missing_global_origin(provenance):
    request = inherited_request()
    request.conditions_source = {"method": provenance} if provenance else {}
    with pytest.raises(StoreError, match="unique recorded source"):
        inherit_method(request)


def test_system_override_and_exact_source_version_are_not_bypassed_by_fallback():
    override = inherited_request(SystemInput(id="water", conditions={"method": "UHF"},
                                            conditions_source={"method": "explicit"}))
    with pytest.raises(StoreError, match="unique recorded source"):
        inherit_method(override)
    with pytest.raises(StoreError, match="target scope"):
        inherit_method(override, value="UHF")
    assert inherit_method(override, value="UHF", system=override.systems[0])[:2] == ("UHF", "inherited")
    override.systems[0].conditions["method"] = None
    override.systems[0].conditions_source["method"] = "unknown"
    with pytest.raises(StoreError, match="unique recorded source"):
        inherit_method(override)
    with pytest.raises(StoreError, match="current Request version"):
        inherit_method(inherited_request(), version=2)


def test_energy_relation_is_correctable_before_any_request_activation(tmp_path):
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text="读取水的单点电子能"))
    parameters = candidate(store, run, kind="normalize", goals=[
        {"key": "energy", "port": "energy", "text_basis": "读取水的单点电子能"}])
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(StoreError, match="geometry_relation: fixed_initial or optimized"):
        commit_candidate(store, run, parameters, decision_id="missing_relation", basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before
    assert not store.load_run(run.id).processed_messages
    parameters["goals"][0]["geometry_relation"] = "fixed_initial"
    parameters["questions"] = ["请提供几何、方法、基组、电荷与多重度。"]
    updated = commit_candidate(store, run, parameters, decision_id="corrected_relation", basis=current_basis(store, run))
    goal = store.load_request(updated).goals[0]
    assert goal.conditions["geometry_relation"] == "fixed_initial"
    assert "semantic:geometry_relation_missing" not in goal.unresolved


@pytest.mark.parametrize("source,value", [("unknown", None), ("inferred", 0)])
def test_uncertain_goal_condition_rejected_then_scoped_answer_can_resolve(tmp_path, source, value):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-03/raw-electron-state-clarification", 1,
                            category="development", freeze_label="offline-goal-contract")
    text = store.load_request(run).original_text
    unknown = {"value": value, "source": source, "text_basis": text}
    parameters = candidate(store, run, kind="normalize", goals=[{
        "key": "energy", "port": "energy", "text_basis": text, "system_refs": ["water"],
        "geometry_relation": "fixed_initial", "conditions": {"charge": unknown},
        "unresolved": ["missing:charge:water", "missing:multiplicity:water"]}],
        conditions={name: {"value": value, "source": "explicit", "text_basis": text}
                    for name, value in {"method": "HF", "basis": "STO-3G",
                                        "environment": "gas", "electronic_state": "RHF"}.items()})
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(StoreError, match="Candidate.conditions/system_conditions.*preserve goals.unresolved"):
        commit_candidate(store, run, parameters, decision_id="uncertain_goal", basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before
    parameters["goals"][0]["conditions"] = {}
    parameters["system_conditions"] = {"water": {"charge": unknown,
        "multiplicity": {"value": None, "source": "unknown"}}}
    parameters["questions"] = ["请确认水的电荷与多重度。"]
    initial = commit_candidate(store, run, parameters, decision_id="scoped_uncertainty", basis=current_basis(store, run))
    request = store.load_request(initial)
    assert request.systems[0].conditions_source["charge"] == source
    assert "unconfirmed:water:charge" in request.unresolved
    assert request.normalization_status == "clarification"
    store.enqueue_message(run.id, "水的电荷0、多重度1，其他条件沿用。")
    supplied = candidate(store, initial, kind="amend", system_conditions={"water": {
        "charge": {"value": 0, "source": "explicit", "text_basis": "电荷0"},
        "multiplicity": {"value": 1, "source": "explicit", "text_basis": "多重度1"}}})
    updated = commit_candidate(store, initial, supplied, decision_id="confirmed_scoped_answer",
                               basis=current_basis(store, initial))
    answered = store.load_request(updated)
    assert answered.goals[0].id == "goal_energy" and answered.goals[0].system_ids == ["water"]
    assert answered.systems[0].conditions_source["charge"] == "explicit"
    assert answered.systems[0].conditions["charge"] == 0
    assert answered.normalization_status == "normalized" and not answered.unresolved


def test_confirmed_goal_constraints_and_other_unresolved_facts_are_preserved():
    candidate = SemanticCandidate(schema_version="request-semantics-1", message_ids=["m"],
        kind="normalize", text_basis="电荷0", goals=[{"key": "energy", "port": "energy",
            "text_basis": "电荷0", "geometry_relation": "fixed_initial",
            "conditions": {"charge": {"value": 0, "source": "explicit", "text_basis": "电荷0"}},
            "unresolved": ["unsupported_quantity:dipole"]}])
    goal = _goals(candidate, inherited_request(), [{"text": "电荷0"}])[0]
    assert goal.conditions["charge"] == 0
    assert goal.unresolved == ["unsupported_quantity:dipole"]


@pytest.mark.parametrize("other_gaps", [[], ["unsupported_quantity:dipole"]])
def test_explicit_answer_resolves_goal_field_marker_without_rebinding(tmp_path, other_gaps):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nsynthetic geometry\nO 0 0 0\nH 0 0.8 0.6\nH 0 -0.8 0.6\n")
    text = "登记水的单点电子能，气相 RHF/STO-3G，多重度1，电荷未知。"
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        geometries=[{"id": "water", "file": "water.xyz"}]))
    explicit = {name: {"value": value, "source": "explicit", "text_basis": text}
                for name, value in {"method": "HF", "basis": "STO-3G", "multiplicity": 1,
                                    "environment": "gas", "electronic_state": "RHF"}.items()}
    parameters = candidate(store, run, kind="normalize", goals=[{
        "key": "energy", "port": "energy", "text_basis": text, "system_refs": ["water"],
        "geometry_relation": "fixed_initial", "conditions": {"basis": explicit["basis"]},
        "unresolved": ["field:charge", *other_gaps]}],
        conditions={**explicit, "charge": {"value": None, "source": "unknown"}},
        questions=["请确认电荷；其他未支持要求继续保留。"])
    initial = commit_candidate(store, run, parameters, decision_id="question", basis=current_basis(store, run))
    assert "field:charge" in store.load_request(initial).goals[0].unresolved
    store.enqueue_message(run.id, "电荷0，其他条件沿用。")
    response = candidate(store, initial, kind="amend", resolves=["field:charge"],
        conditions={"charge": {"value": 0, "source": "explicit", "text_basis": "电荷0"}})
    assert not response.get("goal_bindings")
    updated = commit_candidate(store, initial, response, decision_id="answer", basis=current_basis(store, initial))
    request = store.load_request(updated)
    assert request.goals[0].unresolved == other_gaps
    assert request.goals[0].conditions == {"basis": "STO-3G", "geometry_relation": "fixed_initial"}
    assert request.goals[0].system_ids == ["water"] and request.charge == 0
    assert not request.unresolved
    assert request.normalization_status == ("clarification" if other_gaps else "normalized")


@pytest.mark.parametrize("environment,preserved", [("gas", False), ("gas_phase", False), ("water_solvent", True)])
def test_solvent_metric_does_not_count_supported_gas_alias_as_preserved_requirement(tmp_path, environment, preserved):
    store = store_at(tmp_path)
    run, metadata = create_request(store, "N-05/raw-unsupported-solvent", 1,
                                    category="development", freeze_label="offline-grader-contract")
    message = store.enqueue_message(run.id, "Set environment " + environment)
    updated = apply_user_update(store, run.id, message, {"conditions": {"environment": environment}})
    grade = evaluate_response(store, updated, metadata)
    metric = next(a for a in grade["assertions"] if a["metric"] == "raw_request.unsupported_environment_preserved")
    assert metric["observed"] is preserved
    assert not grade["real_model_evidence_present"]


def answer(*, name="charge", system=None, source="explicit"):
    field = {name: {"value": 0, "source": source, "text_basis": "电荷0"}}
    return SemanticCandidate(schema_version="request-semantics-1", message_ids=["m"],
        kind="amend", text_basis="电荷0", conditions=field if system is None else {},
        system_conditions={system: field} if system else {})


@pytest.mark.parametrize("gap", ["field:charge", "unconfirmed:charge", "missing:charge:water"])
def test_global_answer_matches_same_field_for_program_generated_and_prompt_gaps(gap):
    assert _resolution_matches(gap, answer())


def test_scoped_answer_only_resolves_its_system_and_field():
    scoped = answer(system="water")
    assert _resolution_matches("missing:charge:water", scoped)
    assert _resolution_matches("unconfirmed:water:charge", scoped)
    for gap in ("missing:charge:methane", "field:charge", "missing:multiplicity:water",
                "unsupported:charge", "unknown:unit", "missing:unit:water"):
        assert not _resolution_matches(gap, scoped)
    assert not _resolution_matches("missing:multiplicity:water", answer())
    assert not _resolution_matches("missing:charge:water", answer(source="inferred"))
    binding = SemanticCandidate(schema_version="request-semantics-1", message_ids=["m"],
        kind="amend", text_basis="water", goal_bindings={"goal_energy": ["water"]})
    assert _resolution_matches("system:goal_energy", binding)
    assert not _resolution_matches("system:goal_other", binding)
