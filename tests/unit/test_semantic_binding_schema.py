"""One semantic parameter schema and request-scoped references, without live calls."""

import copy
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import query_run, store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.context import _compose_semantic_parameters, build_context
from orca_agent.llm import _strict_json
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, PermissionSnapshot, Request, SystemInput
from orca_agent.natural import initialize_agent
from orca_agent.proposals import ProposalError
from orca_agent.semantic import _goal_binding_contract, action_parameters, commit_candidate
from orca_agent.store import StoreError
from tests.helpers.phase_b_model_cases import create_request

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-proposals-v4.json"
V4 = json.loads(FIXTURE.read_text(encoding="utf-8"))["proposals"]


def semantic_context(store, run):
    request = store.load_request(run)
    prepared = build_context(request, run, relevant_tools=[],
        user_messages=store.read_control(run.id)["messages"],
        action_parameters=action_parameters(run.permission.allowed_tools, request=request))
    assert prepared.input_token_bound <= 12000
    return payload(prepared)


def test_parameter_schema_is_direct_single_and_every_definition_reference_resolves(tmp_path):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-v9-schema")
    contract = action_parameters(request=store.load_request(run))
    before = copy.deepcopy(contract)
    prepared = build_context(store.load_request(run), run, relevant_tools=[], action_parameters=contract)
    schema = payload(prepared)["PROPOSAL_SCHEMA"]
    parameters = schema["properties"]["parameters"]
    assert parameters["required"] == ["schema_version", "message_ids", "kind", "text_basis"]
    assert parameters["additionalProperties"] is False and "request_semantics" not in parameters["properties"]
    assert parameters["properties"]["goal_bindings"]["maxProperties"] == 0
    assert "$defs" not in parameters
    assert "schema" not in payload(prepared)["ACTION_PARAMETERS"]["normalize_request"]
    assert set(schema["$defs"]) == {"semantic_FieldEvidence", "semantic_SemanticGoal"}
    pending = [schema]
    references = []
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if "$ref" in value:
                reference = value["$ref"]
                target = schema
                for part in reference.removeprefix("#/").split("/"):
                    target = target[part]
                assert isinstance(target, dict)
                references.append(reference)
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    assert references and all(ref.startswith("#/$defs/semantic_") for ref in references)
    assert contract == before  # Context assembly must not consume the reusable source schema.


@pytest.mark.parametrize("sequence", [0, 1])
def test_actual_v4_raw_shapes_reject_before_developer_authored_correction(tmp_path, sequence):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-v4-replay")
    entry = V4[sequence]
    before = store.load_run(run.id).model_dump_json()
    if sequence == 0:
        with pytest.raises(json.JSONDecodeError, match="Extra data") as invalid_json:
            _strict_json(entry["raw_content"])
        assert entry["raw_content"][invalid_json.value.pos:] == "}"
        # These two changes are explicit test corrections, never parser fallback.
        parsed, _ = json.JSONDecoder().raw_decode(entry["raw_content"])
        with pytest.raises(ProposalError) as wrapper:
            commit_candidate(store, run, parsed["parameters"], decision_id="offline_wrapper",
                             basis=current_basis(store, run))
        assert {tuple(error["loc"]) for error in wrapper.value.detail["errors"]} >= {
            ("parameters", "request_semantics"), ("parameters", "schema_version")}
        proposed = copy.deepcopy(parsed["parameters"]["request_semantics"])
    else:
        parsed = _strict_json(entry["raw_content"])
        assert parsed == entry["proposal"]
        proposed = copy.deepcopy(parsed["parameters"])
        proposed["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
        with pytest.raises(ProposalError) as bad_reference:
            commit_candidate(store, run, proposed, decision_id="offline_new_key_binding",
                             basis=current_basis(store, run))
        assert bad_reference.value.detail["path"] == ["parameters", "goal_bindings", "energy"]
        assert bad_reference.value.detail["allowed_goal_ids"] == []
        proposed.pop("goal_bindings")
    assert store.load_run(run.id).model_dump_json() == before
    proposed["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
    updated = commit_candidate(store, run, proposed, decision_id="offline_corrected_shape",
                               basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and not request.unresolved
    assert [goal.port for goal in request.goals] == ["energy"]
    assert request.goals[0].system_ids == ["water"] and not request.goals[0].unresolved
    assert not updated.attempts and not updated.calls and not updated.model_records


def binding_run(tmp_path):
    store = store_at(tmp_path)
    request = Request(original_text="读取指定体系的已有字段", systems=[SystemInput(id="water"), SystemInput(id="methane")],
        goals=[Goal(id="goal_observe", port="value_observation", minimum_check_version="evidence-read-1",
                    unresolved=["system:goal_observe"])])
    run = initialize_agent(store, Config(), request, PermissionSnapshot(model_execution=True))
    store.enqueue_message(run.id, "指定水体系。")
    return store, run


def test_existing_goal_binding_uses_the_same_visible_ids_and_runtime_scope(tmp_path):
    store, run = binding_run(tmp_path)
    schema = semantic_context(store, run)["PROPOSAL_SCHEMA"]["properties"]["parameters"]
    assert schema["properties"]["goal_bindings"]["propertyNames"] == {"enum": ["goal_observe"]}
    proposed = candidate(store, run, kind="amend", goal_bindings={"goal_observe": ["water"]},
                         resolves=["system:goal_observe"])
    updated = commit_candidate(store, run, proposed, decision_id="existing_goal_binding", basis=current_basis(store, run))
    goal = store.load_request(updated).goals[0]
    assert goal.id == "goal_observe" and goal.system_ids == ["water"] and not goal.unresolved
    assert not updated.calls and not updated.attempts and not updated.model_records


@pytest.mark.parametrize("failure", ["guessed_key", "new_goals_and_bindings", "unknown_system", "wrong_user_scope"])
def test_binding_key_modes_and_original_system_guards_are_all_enforced(tmp_path, failure):
    store, run = binding_run(tmp_path)
    proposed = candidate(store, run, kind="amend", goal_bindings={"goal_observe": ["water"]})
    if failure == "guessed_key":
        proposed["goal_bindings"] = {"observe": ["water"]}
    elif failure == "new_goals_and_bindings":
        proposed.update(kind="replace_goals", goals=[{"key": "observe", "port": "value_observation",
                                                       "text_basis": "指定水体系。", "system_refs": ["water"]}])
    else:
        proposed["goal_bindings"] = {"goal_observe": ["unregistered" if failure == "unknown_system" else "methane"]}
    before = store.load_run(run.id).model_dump_json()
    if failure in {"guessed_key", "new_goals_and_bindings"}:
        with pytest.raises(ProposalError) as error:
            commit_candidate(store, run, proposed, decision_id=failure, basis=current_basis(store, run))
        allowed, mode_schema = _goal_binding_contract(store.load_request(run), defines_goals="goals" in proposed)
        assert error.value.detail["allowed_goal_ids"] == allowed
        if failure == "new_goals_and_bindings":
            schema = semantic_context(store, run)["PROPOSAL_SCHEMA"]["properties"]["parameters"]
            assert schema["anyOf"] == [{"properties": {"goals": {"type": "null"}}},
                                       {"properties": {"goal_bindings": mode_schema}}]
            assert mode_schema == {"maxProperties": 0}
    else:
        with pytest.raises(StoreError, match="registered systems"):
            commit_candidate(store, run, proposed, decision_id=failure, basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before


def test_schema_composition_rejects_dangling_refs_and_namespace_collisions():
    with pytest.raises(ValueError, match="undeclared definition"):
        _compose_semantic_parameters({"properties": {}}, {"$ref": "#/$defs/Missing"})
    with pytest.raises(ValueError, match="namespace collides"):
        _compose_semantic_parameters({"properties": {}, "$defs": {"semantic_Field": {}}},
                                     {"$defs": {"Field": {"type": "string"}}})


def test_read_query_schema_remains_available_and_tool_catalog_returns_after_semantic_update(tmp_path):
    store, run, _, plan = query_run(tmp_path)
    store.enqueue_message(run.id, "仍然读取已登记的a字段。")
    request = store.load_request(run)
    intake = payload(build_context(request, run, plan, relevant_tools=run.permission.allowed_tools,
        user_messages=store.read_control(run.id)["messages"],
        action_parameters=action_parameters(run.permission.allowed_tools, request=request)))
    assert not intake["TOOL_CATALOG"] and not intake["PARAMETER_SCHEMAS"]
    assert "value_observation" in intake["ACTION_PARAMETERS"]["normalize_request"]["query_schemas"]
    updated = commit_candidate(store, run, candidate(store, run, kind="amend"),
                               decision_id="read_query_unchanged", basis=current_basis(store, run))
    assert updated.plan_id is None
    planning = payload(build_context(store.load_request(updated), updated,
                                     relevant_tools=updated.permission.allowed_tools))
    assert [tool["name"] for tool in planning["TOOL_CATALOG"]] == ["evidence.value"]
    assert planning["PARAMETER_SCHEMAS"]
    assert not updated.calls and not updated.attempts and not updated.model_records
