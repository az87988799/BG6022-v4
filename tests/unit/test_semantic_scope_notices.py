"""Visible capability facts and notices do not grant execution or repair model text."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import store_at
from test_semantic_control import candidate

from orca_agent.applicability import PROFILE, canonical_condition
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.models import PermissionSnapshot
from orca_agent.proposals import ProposalError
from orca_agent.semantic import SemanticCandidate, action_parameters, commit_candidate
from orca_agent.tools.registry import SCIENCE_COMPOSITIONS, catalog
from tests.helpers.phase_b_model_cases import create_request
from tests.helpers.semantic_replay import current_candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-scope-notices-v7.json"
PROPOSALS = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposals"]
NOTICES = {
    "N-05/raw-unsupported-solvent": "已登记并保留水溶剂要求。当前能力仅支持气相，无法执行该环境；本轮不计算，目标保持未满足。",
    "N-06/raw-unsupported-system": "已登记乙醇优化及优化后电子能需求。当前分子范围仅H2O/CH4，乙醇不受支持；乙醇几何也未登记。两项缺口均保留，本轮不计算。",
}


def raw_run(tmp_path, variant):
    store = store_at(tmp_path)
    run, _ = create_request(store, variant, 1, category="development", freeze_label="offline-scope-notices")
    return store, run


def fresh_parameters(store, run, entry):
    assert hashlib.sha256(entry["raw_content"].encode("utf-8")).hexdigest() == entry["raw_content_sha256"]
    parameters = copy.deepcopy(json.loads(entry["raw_content"])["parameters"])
    # Explicit offline derivation: transport identity and schema change; source bytes remain intact.
    parameters["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
    parameters = current_candidate(parameters)
    return parameters


@pytest.mark.parametrize("variant", list(NOTICES))
def test_visible_scope_is_registry_profile_data_not_permission_or_registered_systems(tmp_path, variant):
    store, run = raw_run(tmp_path, variant)
    request = store.load_request(run)
    before = request.model_dump_json(), run.model_dump_json()
    contract = action_parameters(run.permission.allowed_tools, request=request)
    scope = contract["normalize_request"]["science_scope"]
    assert scope == {
        "systems": list(SCIENCE_COMPOSITIONS), "conditions": PROFILE,
        "names": {"H2O": ["水", "water"], "CH4": ["甲烷", "methane"]},
        "ports": sorted({port for tool in catalog() if "execute_orca" in tool["effects"]
                         for port in tool["output_ports"]}),
    }
    prepared = build_context(request, run, relevant_tools=[],
        user_messages=store.read_control(run.id)["messages"], action_parameters=contract)
    data = payload(prepared)
    visible = data["ACTION_PARAMETERS"]["normalize_request"]
    assert visible["science_scope"] == scope
    assert "not permission/defaults" in visible["instruction"]
    assert "notices" in prepared.body()["messages"][0]["content"]
    assert "critical gaps blocking the requested scope" in prepared.body()["messages"][0]["content"]
    # Intake uses a hash/count reference for access IDs, never new authority.
    visible_permission = copy.deepcopy(data["AUTHORITY"]["permission"])
    reference = visible_permission.pop("access_bindings")
    bindings = {key: getattr(run.permission, key) for key in ("artifact_ids", "source_ids", "result_ids")}
    assert reference == {"reference": "Run.permission", "counts": {k: len(v) for k, v in bindings.items()},
        "sha256": hashlib.sha256(json.dumps(bindings, ensure_ascii=False, sort_keys=True,
                                              separators=(",", ":")).encode()).hexdigest()}
    assert PermissionSnapshot.model_validate({**visible_permission, **bindings}) == run.permission
    assert data["AUTHORITY"]["permission"]["allowed_tools"] == []
    assert data["TOOL_CATALOG"] == []
    assert [s["id"] for s in data["AUTHORITY"]["request"]["systems"]] == [s.id for s in request.systems]
    assert prepared.input_token_bound <= 12000
    assert (request.model_dump_json(), run.model_dump_json()) == before
    scope["conditions"]["environment"] = "changed only in the local projection"
    scope["systems"].append("C2H6O")
    assert PROFILE["environment"] == "gas_phase" and "C2H6O" not in SCIENCE_COMPOSITIONS


@pytest.mark.parametrize("entry", PROPOSALS, ids=[p["model_record_id"] for p in PROPOSALS])
def test_v7_original_runtime_dispositions_and_bad_notice_text_are_not_silently_changed(tmp_path, entry):
    store, run = raw_run(tmp_path, entry["variant_id"])
    parameters = fresh_parameters(store, run, entry)
    before = store.load_run(run.id).model_dump_json()
    original = json.loads(entry["raw_content"])["parameters"]
    historical = SemanticCandidate.model_validate(original)
    assert historical.schema_version == "request-semantics-1"
    assert historical.questions == original["questions"]
    # Historical dispositions remain source facts; v1 is now read-only and
    # cannot be activated under the changed v2 interaction contract.
    assert entry["runtime_disposition"] in {"accepted", "rejected"}
    if entry["runtime_disposition"] == "accepted":
        assert entry["independent_semantic_passed"] is False
    legacy = copy.deepcopy(original)
    legacy["message_ids"] = parameters["message_ids"]
    with pytest.raises(ProposalError):
        commit_candidate(store, run, legacy, decision_id="legacy_read_only", basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before



@pytest.mark.parametrize("entry", PROPOSALS, ids=[p["model_record_id"] for p in PROPOSALS])
def test_developer_scope_notices_keep_requested_targets_and_all_unmet_facts(tmp_path, entry):
    store, run = raw_run(tmp_path, entry["variant_id"])
    parameters = fresh_parameters(store, run, entry)
    parameters["questions"] = []
    parameters["notices"] = [NOTICES[entry["variant_id"]]]
    if entry["variant_id"] == "N-06/raw-unsupported-system":
        parameters["unresolved"].append("unsupported_system:ethanol")
    updated = commit_candidate(store, run, parameters, decision_id="developer_notice", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "clarification"
    assert request.original_text == parameters["text_basis"]
    assert updated.decisions[-1]["semantics"]["notices"] == parameters["notices"]
    assert not updated.decisions[-1]["semantics"]["questions"]
    assert all("?" not in text and "？" not in text for text in parameters["notices"])
    if entry["variant_id"] == "N-05/raw-unsupported-solvent":
        assert request.conditions["environment"] == "water_solvent"
        assert request.conditions_source["environment"] == "explicit"
        assert request.goals[0].conditions["environment"] == "water_solvent"
        assert "applicability:unsupported_condition:environment" in request.goals[0].unresolved
    else:
        assert not request.systems and request.geometry_artifact_id is None
        assert "unsupported_system:ethanol" in request.unresolved
        assert all("missing:geometry" in goal.unresolved for goal in request.goals)
        assert {goal.port for goal in request.goals} == {"energy", "optimized_geometry"}
        assert all(goal.conditions["geometry_relation"] == "optimized" for goal in request.goals)
    assert not updated.permission.scientific_execution and updated.permission.allowed_tools == []
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert updated.usage.model_calls == updated.usage.orca_starts_actual == 0


@pytest.mark.parametrize("environment", ["gas", "gas_phase"])
def test_scope_canonical_environment_keeps_lexical_gas_aliases_and_explicit_origins(tmp_path, environment):
    store, run = raw_run(tmp_path, "N-01/raw-water-sp")
    fields = {"method": ("HF", "RHF"), "basis": ("STO-3G", "STO-3G"), "charge": (0, "中性"),
              "multiplicity": (1, "单重态"), "electronic_state": ("RHF", "RHF"), "environment": (environment, "气相")}
    conditions = {key: {"value": value, "source": "explicit", "text_basis": quote}
                  for key, (value, quote) in fields.items()}
    parameters = candidate(store, run, kind="normalize", conditions=conditions, goals=[{
        "key": "energy", "port": "energy", "text_basis": "水的单点电子能",
        "geometry_relation": "fixed_initial", "system_refs": ["water"]}])
    updated = commit_candidate(store, run, parameters, decision_id="explicit_gas", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and request.conditions["environment"] == environment
    assert all(request.conditions_source[key] == "explicit" for key in PROFILE)
    assert {key: canonical_condition(key, request.conditions[key]) for key in PROFILE} == PROFILE
    assert not request.semantic_defaults


def test_critical_unknown_conditions_still_allow_visible_questions(tmp_path):
    store, run = raw_run(tmp_path, "N-03/raw-electron-state-clarification")
    parameters = candidate(store, run, kind="normalize", goals=[{
        "key": "energy", "port": "energy", "text_basis": "单点电子能",
        "geometry_relation": "fixed_initial", "system_refs": ["water"]}],
        conditions={"charge": {"source": "unknown", "value": None}},
        questions=["请确认水的电荷与多重度。"])
    updated = commit_candidate(store, run, parameters, decision_id="actual_unknown", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "clarification"
    assert request.charge is None and request.conditions_source["charge"] == "unknown"
    assert "unconfirmed:charge" in request.unresolved
    assert updated.decisions[-1]["semantics"]["questions"] == parameters["questions"]
