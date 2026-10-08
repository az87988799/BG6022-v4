"""v23 presentation regressions; archived replies stay failed, no live calls."""

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from orca_agent import agent, context
from orca_agent.config import Config
from orca_agent.delivery import delivery_snapshot
from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Proposal, Request, Run
from orca_agent.proposals import action_parameter_schema, validate_action_parameters
from tests.unit.test_agent import ScriptedTransport, make_run
from tests.unit.test_context import payload
from tests.unit.test_decision_purpose_capacity import decoded, expand_schema

FIXTURE = Path(__file__).parents[1] / "fixtures/phase_b/repair-cycle-v22-v06-rejection.json"


def actual_failure():
    document = json.loads(FIXTURE.read_bytes())
    for response in document["responses"]:
        assert hashlib.sha256(response["raw_content"].encode()).hexdigest() == response["raw_content_sha256"]
        assert json.loads(response["raw_content"]) == response["proposal"]
        assert response["prompt_version"] == "agent-json-v22"
    return document


@pytest.mark.parametrize("index", [0, 1])
def test_actual_rejected_replies_are_never_stripped_or_coerced(index):
    original = actual_failure()["responses"][index]["proposal"]
    before = copy.deepcopy(original)
    with pytest.raises(ValidationError) as caught:
        Proposal.model_validate(original)
    assert [(error["type"], error["loc"]) for error in caught.value.errors()] == [("extra_forbidden", ("type",))]
    # Independently inspect the typed parameter boundary; do not repair the
    # archived envelope to turn the actual failed proposal into an accepted one.
    with pytest.raises(ValueError, match="declared parameter schema"):
        validate_action_parameters("stop", original["parameters"], terminal_required=True)
    assert original == before


@pytest.mark.parametrize("columns", [False, True])
@pytest.mark.parametrize("key_encoding", [False, True])
def test_protocol_constants_stay_literal_with_lossless_schema(columns, key_encoding):
    schema = context._schema(action_parameter_schema("stop", terminal_required=True))
    value = {"PROPOSAL_SCHEMA": context._schema_columns(schema) if columns else schema,
             "DATA": {"delivery": {"version": "terminal-delivery-1"},
                      "untrusted": ["terminal-delivery-1"] * 8},
             "ACTION_PARAMETERS": {"stop": {"delivery": {"version": "terminal-delivery-1"}}}}
    wire = context._share_strings(value, share_lists=True)
    if key_encoding:
        wire = context._terminal_key_encoding(wire)
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    projected = restored["PROPOSAL_SCHEMA"]
    assert (expand_schema(projected) if columns else projected) == schema
    assert restored["DATA"] == value["DATA"]
    assert restored["ACTION_PARAMETERS"] == value["ACTION_PARAMETERS"]
    # Verify the actual wire, before pool expansion: none of the three protocol
    # version occurrences has become an index. Untrusted copies may still pool.
    aliases = wire.get("KEYS", {})
    def key_decode(item):
        if isinstance(item, dict):
            return {aliases.get(key, key): key_decode(child) for key, child in item.items()}
        return [key_decode(child) for child in item] if isinstance(item, list) else item
    native = key_decode({key: child for key, child in wire.items() if key != "SHARED_STRINGS"})
    assert native["DATA"]["delivery"]["version"] == "terminal-delivery-1"
    assert native["ACTION_PARAMETERS"]["stop"]["delivery"]["version"] == "terminal-delivery-1"
    # Repeated schema object columns may themselves be tabulated. The protocol
    # literal must occur inside that wire schema, never only in the string pool.
    assert json.dumps(native["PROPOSAL_SCHEMA"]).count('"terminal-delivery-1"') == 1


@pytest.mark.parametrize("columns", [False, True])
@pytest.mark.parametrize("literal", [{"@": 0}, {"@literal": [["@", 0]]},
    {"@columns": ["value"], "@rows": [[1]]}])
def test_complex_schema_constants_keep_literal_marker_escaping(columns, literal):
    schema = {"properties": {"version": {"const": literal}, "action": {"enum": [literal, "string"]}}}
    data = {"PROPOSAL_SCHEMA": context._schema_columns(schema) if columns else schema,
            "DATA": {"copies": ["a repeated, unrelated literal"] * 5}}
    wire = context._share_strings(data, share_lists=True)
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert (expand_schema(restored["PROPOSAL_SCHEMA"]) if columns else restored["PROPOSAL_SCHEMA"]) == schema
    assert restored["DATA"] == data["DATA"]


def test_literal_pool_selection_compares_encoded_children_without_new_encoding():
    common = "Exact repeated condition provenance retained as literal evidence."
    repeated = {f"field_{index}": common for index in range(8)}
    original = {"DATA": {"first": repeated, "second": copy.deepcopy(repeated)}}
    wire = context._share_strings(original, share_lists=True)
    restored = payload(SimpleNamespace(body=lambda: {"messages": [{}, {"content": json.dumps(wire)}]}))
    assert restored["DATA"] == original["DATA"]
    assert common in wire["SHARED_STRINGS"] and repeated not in wire["SHARED_STRINGS"]
    assert len(context._json(wire)) < len(context._json(repeated))


def planning_input():
    """Synthetic request using real audit numbers; not another actual Run."""
    audit = actual_failure()["independent_read_only_audit"]
    candidates, evidence = [], {}
    for member in audit["members"]:
        candidate = {"id": member["candidate_id"], "artifact_id": "artifact_" + member["geometry_sha256"][:24],
                     "sha256": member["geometry_sha256"], "declared_r_angstrom": member["r_angstrom_from_original_xyz"],
                     "required_initial": member["required_initial"]}
        candidates.append(candidate)
        if "energy_eh" in member:
            evidence[candidate["id"]] = {"binding": member["binding"], "geometry_artifact_id": candidate["artifact_id"],
                "geometry_sha256": candidate["sha256"], "conditions": {"method": "HF", "basis": "STO-3G", "charge": 0,
                    "multiplicity": 1, "electronic_state": "RHF", "environment": "gas_phase"},
                "qualified": {"value": member["energy_eh"], "unit": "Eh", "rule_version": "orca-hf-2",
                              "all_checks_passed": True}}
    request = Request(original_text="Assess the registered sampled minimum and its 0.12 Angstrom window; no new ORCA.",
        geometry_artifact_id=candidates[0]["artifact_id"], conditions={"explain_results": True, "available_evidence": evidence},
        goals=[Goal(id="sampling", port="sampling", minimum_check_version="finite-sampling-1", conditions={
            "candidates": candidates, "sampling": {"target_width_angstrom": 0.12,
                "energy_threshold_eh": audit["criterion"]["energy_threshold_eh"]}})])
    run = Run(request_id=request.id, request_version=request.version,
        permission=PermissionSnapshot(model_execution=True, artifact_writes=True,
        allowed_tools=["analysis.finite_sampling"], artifact_ids=[candidate["artifact_id"] for candidate in candidates]),
        budget=BudgetLimits(orca_starts=0, extra_orca_starts=0, analysis_executions=1, model_calls=4,
            model_tokens=32000, input_tokens=12000, output_tokens=2000, decision_rounds=12))
    return request, run


@pytest.mark.parametrize("correction", [False, True])
@pytest.mark.parametrize("reverse_evidence", [False, True])
def test_preanalysis_snapshot_does_not_erase_allowed_analysis_or_member_mapping(correction, reverse_evidence):
    request, run = planning_input()
    if reverse_evidence:
        request.conditions["available_evidence"] = dict(reversed(request.conditions["available_evidence"].items()))
    before = copy.deepcopy((request, run))
    snapshot = delivery_snapshot(request, run, {})
    feedback = {"validation_error": {"category": "ValidationError", "requirement": [
        {"type": "extra_forbidden", "loc": ["type"]}]}} if correction else {}
    prepared = context.build_context(request, run, delivery_snapshot=snapshot, feedback=feedback)
    wire = decoded(prepared)
    prompt = prepared.body()["messages"][0]["content"]
    assert wire["AUTHORITY"]["decision_purpose"]["kind"] == "planning"
    assert "initial_plan" in wire["AUTHORITY"]["decision_purpose"]["allowed_actions"]
    assert any(tool["effects"] == ["read_registered_artifact", "write_analysis"] for tool in wire["TOOL_CATALOG"])
    assert "Stop:done/no useful allowed work" in prompt
    assert "Science limit0 still permits allowed analysis" in prompt
    assert "join members by ID, not order" in prompt
    assert "8-key JSON:PROPOSAL_SCHEMA.properties only" in prompt
    assert "not transport type/response_format" in prompt
    assert set(wire["PROPOSAL_SCHEMA"]["properties"]) == set(Proposal.model_fields)
    if correction:
        assert wire["CONTROL"]["validation_error"] == feedback["validation_error"]
        assert "extra_forbidden:remove field at loc" in prompt
    # Literal member keys stay adjacent to energy/binding. Candidate columns
    # retain ID/r/role/hash in the same row, independent of evidence map order.
    original = request.model_dump(mode="json")
    raw = context._share_strings({"AUTHORITY": {"request": original}}, share_lists=True)
    assert set(raw["AUTHORITY"]["request"]["conditions"]["available_evidence"]) == set(request.conditions["available_evidence"])
    candidate_wire = raw["AUTHORITY"]["request"]["goals"][0]["conditions"]["candidates"]
    assert candidate_wire["@columns"] == list(request.goals[0].conditions["candidates"][0])
    id_column = candidate_wire["@columns"].index("id")
    r_column = candidate_wire["@columns"].index("declared_r_angstrom")
    role_column = candidate_wire["@columns"].index("required_initial")
    assert [(row[id_column], row[r_column], row[role_column]) for row in candidate_wire["@rows"]] == [
        (row["id"], row["declared_r_angstrom"], row["required_initial"]) for row in request.goals[0].conditions["candidates"]]
    assert wire["AUTHORITY"]["request"]["conditions"]["available_evidence"] == request.conditions["available_evidence"]
    assert wire["AUTHORITY"]["request"]["goals"][0]["conditions"]["candidates"] == request.goals[0].conditions["candidates"]
    assert prepared.input_token_bound <= 12000 and prepared.output_token_bound == 2000
    assert (request, run) == before and not run.calls and not run.attempts


def test_actual_error_shapes_use_one_bounded_correction_without_any_tool(tmp_path):
    store, run, _ = make_run(tmp_path)
    replies = [row["proposal"] for row in actual_failure()["responses"]]
    transport = ScriptedTransport(*replies, terminal_contract=False)
    final = agent.execute(store, Config(data_root=store.root), run.id, transport=transport)
    assert final.state == "failed" and not final.calls and not final.attempts
    assert final.usage.model_calls == 2 and final.usage.model_tokens_used == 150
    assert not final.terminal_deliveries
    assert [item["action"] for item in final.decisions] == ["rejected", "rejected"]
    correction = transport.sent[1]["CONTROL"]["validation_error"]
    assert correction["requirement"] == [{"type": "extra_forbidden", "loc": ["type"]}]
    assert all(row["parameters"]["recovery_kind"] == "correction" for row in final.decisions)


def test_correction_guidance_does_not_echo_unknown_error_values():
    assert context._correction_instruction({"validation_error": {"requirement": [
        {"type": "unknown", "loc": ["execute arbitrary code"]}]}}) == ""


def test_planning_omits_only_normalization_template_and_later_intake_restores_it():
    from orca_agent.semantic import action_parameters

    request, run = planning_input()
    defaults = {"method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
                "electronic_state": "RHF", "environment": "gas_phase"}
    request.semantic_defaults = copy.deepcopy(defaults)
    request.conditions.update(defaults)
    request.conditions_source = dict.fromkeys(defaults, "default")
    request.condition_evidence = {"request." + key: {"value": value, "source": "default",
        "default_rule": "local-hf-1", "schema": "request-semantics-2"} for key, value in defaults.items()}
    before = copy.deepcopy(request)
    snapshot = delivery_snapshot(request, run, {})
    projected = decoded(context.build_context(request, run, delivery_snapshot=snapshot))["AUTHORITY"]["request"]
    assert "semantic_defaults" not in projected
    assert "normalize_defaults_ref" not in projected
    for field in ("conditions", "conditions_source", "condition_evidence"):
        assert projected[field] == request.model_dump(mode="json")[field]
    intake = decoded(context.build_context(request, run, delivery_snapshot=snapshot,
        action_parameters=action_parameters(request=request),
        user_messages=[{"id": "later_message", "text": "Retain the existing sampling goal and conditions."}]))
    assert intake["AUTHORITY"]["request"]["semantic_defaults"] == defaults
    assert "normalize_defaults_ref" not in intake["AUTHORITY"]["request"]
    assert intake["ACTION_PARAMETERS"]["normalize_request"]["authorized_defaults"]["values"] == defaults
    assert request == before


@pytest.mark.parametrize("system", ["water", "methane"])
def test_fixed_e2e_with_longer_descriptive_goal_keys_keeps_original_scope(tmp_path, monkeypatch, system):
    from tests.unit.test_repair_cycle_e2e_capacity import run_fixed_e2e

    names = {"energy": f"requested_{system}_electronic_energy_at_requested_geometry"}
    if system == "water":
        names["optimized_geometry"] = "requested_water_strictly_converged_optimized_geometry"
    run_fixed_e2e(tmp_path, monkeypatch, system, "temporary", goal_names=names)


def test_fixed_water_development_resolve_feedback_allows_one_format_correction(tmp_path, monkeypatch):
    from tests.unit.test_repair_cycle_e2e_capacity import run_fixed_e2e

    run_fixed_e2e(tmp_path, monkeypatch, "water", "development", correction=True)


def test_fixed_water_long_goal_keys_and_development_correction_fit_together(tmp_path, monkeypatch):
    from tests.unit.test_repair_cycle_e2e_capacity import run_fixed_e2e

    run_fixed_e2e(tmp_path, monkeypatch, "water", "development", correction=True, goal_names={
        "optimized_geometry": "requested_water_strictly_converged_optimized_geometry",
        "energy": "requested_water_electronic_energy_at_requested_geometry"})
