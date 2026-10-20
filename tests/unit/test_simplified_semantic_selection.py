"""Source/selection protocol regressions; no live model, HTTP or ORCA."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from test_general_identity_selection import source
from test_general_preparation_admission import execution, resolved
from test_semantic_control import candidate
from test_structure_tools import mock_generator, tool_call
from test_text_entry import defaults, text_environment

from orca_agent.condition_selection import commit_selections, selection_action_parameters
from orca_agent.input_bindings import prepared_geometry, validate_input_result
from orca_agent.model_usage import current_basis
from orca_agent.models import Result
from orca_agent.natural import initialize_text
from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters, commit_candidate, source_fragments
from orca_agent.store import StoreError
from orca_agent.tools import structure

PACK = Path(__file__).parents[2] / "docs/acceptance/code-as-tools-step7-refreeze-2"


def intake(tmp_path, text, *, policy="simplified-1"):
    store, config = text_environment(tmp_path,
        defaults={"charge": 0, "multiplicity": 1, "electronic_state": "RKS", "environment": "gas_phase"},
        permission={"model_execution": True, "artifact_writes": True, "code_execution": True,
                    "external_identity_queries": True, "geometry_preparation": True,
                    "allowed_tools": ["structure.resolve", "structure.prepare", "knowledge.search", "python.execute"]})
    config.workflow_policy = policy
    config.execution = execution()
    run = initialize_text(store, config, text)
    return store, config, run


def submit(store, run, parameters, name="normalize"):
    return commit_candidate(store, run, parameters, decision_id=name, basis=current_basis(store, run))


def normal(store, config, run, *, quantity="极化率"):
    message = store.read_control(run.id)["messages"][0]
    text = message["text"]
    return candidate(store, run, kind="normalize", conditions=defaults(config),
        identities=[{"key": "target", "name": "乙醇", "query_name": "ethanol",
                     "text_basis": "乙醇", "message_id": message["id"]}],
        goals=[{"key": "property", "port": "property", "quantity": quantity,
                "text_basis": text, "message_id": message["id"], "system_refs": ["target"]}],
        automatic_selection=({name: {"text_basis": "选择方法和基组", "message_id": message["id"]}
                              for name in ("method", "basis")} if "选择方法和基组" in text else {}))


def retained(task, index=-1):
    """Use committed historical bytes; only message identities are rebound below."""
    review = json.loads((PACK / "review" / f"{task}-review.json").read_bytes())
    records = review["candidate_evolution"] if task == "T4" else review["model_integrity"]["records"]
    model_id = records[index]["model_id"] if task == "T4" else records[index]["id"]
    manifest = json.loads((PACK / "evidence/index.json").read_bytes())
    entry = next(item for row in manifest["tasks"] if row["task"] == task
                 for item in row["files"] if item["path"].endswith(model_id + ".response.json"))
    path = PACK / entry["published_path"]
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
    response = json.loads(raw)
    return review.get("original_text") or review["original_task_preservation"]["text"], response["proposal"]["parameters"], path, raw


def rebind(value, message_id):
    if isinstance(value, dict):
        return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                else rebind(item, message_id) for key, item in value.items()}
    return [rebind(item, message_id) for item in value] if isinstance(value, list) else value


@pytest.mark.parametrize("task,index", [("T4", -1), ("T5", 0), ("T5", 1), ("T5", 2)])
def test_retained_protocol_failures_activate_without_scientific_execution(tmp_path, task, index):
    text, proposal, path, raw = retained(task, index)
    store, _, run = intake(tmp_path, text)
    parameters = rebind(proposal, store.read_control(run.id)["messages"][0]["id"])
    before = copy.deepcopy(parameters)
    updated = submit(store, run, parameters)
    request = store.load_request(updated)
    assert request.normalization_status == "normalized"
    assert request.original_text == text and parameters == before and path.read_bytes() == raw
    assert not updated.calls and not updated.attempts and updated.usage.model_calls == 0
    if task == "T4":
        assert all(goal.evidence_contract_version == "generic-observation-1"
                   for goal in request.goals if goal.port != "prepared_geometry")
        fields = updated.decisions[-1]["semantics"]["program_fields"]
        assert any(row["path"][-1] == "analysis_goal_ref" for row in fields)
        assert "HOMO-LUMO" in request.original_text
    else:
        energies = [goal for goal in request.goals if goal.port == "energy"]
        assert {goal.conditions["environment"] for goal in energies} == {"gas_phase", "CPCM(water)"}
        assert all(goal.conditions["geometry_relation"] == "fixed_initial" for goal in energies)
        assert "不得称为完整水合自由能" in request.original_text
        assert not any(value == "satisfied" for value in updated.goal_status.values())


@pytest.mark.parametrize("task", ["T4", "T5"])
def test_historical_policy_does_not_acquire_new_intake_behavior(tmp_path, task):
    text, proposal, _, _ = retained(task)
    store, _, run = intake(tmp_path, text, policy=None)
    parameters = rebind(proposal, store.read_control(run.id)["messages"][0]["id"])
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).version == 1


def test_program_sources_preserve_intervening_constraints_and_original_proposal(tmp_path):
    text = "请为乙醇交付全部原始文件。保持固定几何，不改变坐标。计算极化率。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    fragments = source_fragments(store.read_control(run.id)["messages"])
    parameters["goals"][0].pop("text_basis")
    parameters["goals"][0].pop("message_id")
    parameters["goals"][0]["source_refs"] = [fragments[0]["id"], fragments[-1]["id"]]
    original = copy.deepcopy(parameters)
    updated = submit(store, run, parameters)
    goal = store.load_request(updated).goals[0]
    assert goal.original_text == text
    assert goal.conditions["geometry_relation"] == "fixed_initial"
    semantics = updated.decisions[-1]["semantics"]
    assert semantics["program_source_references"][0]["end"] == len(text)
    assert semantics["proposed_candidate"]["goals"][0]["text_basis"] == ""
    assert parameters == original


@pytest.mark.parametrize("change", ["foreign_ref", "conflicting_text", "contradictory_relation", "execution_field"])
def test_source_and_mechanical_fields_do_not_authorize_changed_requirements(tmp_path, change):
    text = "请为乙醇保持固定几何。计算极化率。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    goal = parameters["goals"][0]
    if change == "foreign_ref":
        goal["source_refs"] = ["source_not_from_user"]
    elif change == "conflicting_text":
        goal["source_refs"] = [source_fragments(store.read_control(run.id)["messages"])[-1]["id"]]
    elif change == "contradictory_relation":
        goal["geometry_relation"] = "optimized"
    else:
        goal["execute_orca"] = True
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).version == 1 and not run.calls and not run.attempts


@pytest.mark.parametrize(("text", "value", "accept"), [
    ("在ORCA C-PCM水环境下", "water_solvent", True),
    ("采用CPCM的水溶剂模型", "CPCM(water)", True),
    ("in CPCM(methanol)", "CPCM(methanol)", True),
    ("in SMD(acetone)", "SMD(acetone)", True),
    ("在SMD水环境下", "CPCM(water)", False),
    ("在C-PCM甲醇环境下", "CPCM(water)", False),
    ("不要在C-PCM水环境下", "CPCM(water)", False),
    ("在CPCM(water)或者SMD(water)下", "CPCM(water)", False),
])
def test_environment_model_and_solvent_components_must_match(tmp_path, text, value, accept):
    full = f"请为乙醇计算极化率，{text}。"
    store, config, run = intake(tmp_path, full)
    parameters = normal(store, config, run)
    parameters["conditions"].pop("environment")
    parameters["goals"][0]["conditions"] = {"environment": {
        "source": "explicit", "value": value, "text_basis": text,
        "message_id": store.read_control(run.id)["messages"][0]["id"]}}
    if not accept:
        with pytest.raises((ProposalError, StoreError)):
            submit(store, run, parameters)
        return
    updated = submit(store, run, parameters)
    goal = store.load_request(updated).goals[0]
    assert goal.conditions["environment"] == ("CPCM(water)" if value == "water_solvent" else value)
    proof = goal.text_evidence["conditions"]["environment"]
    assert proof["normalization"]["raw_phrase"] and proof["text_basis"] == text


def selected_fixture(tmp_path, *, policy="simplified-1"):
    text = "请为乙醇计算极化率。选择方法和基组。"
    store, config, run = intake(tmp_path, text, policy=policy)
    run = submit(store, run, normal(store, config, run))
    return store, run


def choices(run, source_ref=None):
    return {"request_version": run.request_version, "choices": {
        name: {"value": value, "reason": "Offline fixture choice, not scientific validation.",
               **({"sources": [source_ref]} if source_ref is not None else {})}
        for name, value in (("method", "PBE0"), ("basis", "def2-SVP"))}}


def test_optional_retrieval_preserves_reason_authority_and_cumulative_budget(tmp_path):
    store, run = selected_fixture(tmp_path)
    before = copy.deepcopy((store.load_request(run), run.permission, run.budget, run.usage))
    updated = commit_selections(store, run, choices(run), decision_id="choose", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert (request.method, request.basis) == ("PBE0", "def2-SVP")
    assert request.goals == before[0].goals and request.messages == before[0].messages
    assert (updated.permission, updated.budget, updated.usage) == before[1:]
    assert request.condition_evidence["request.method"]["basis_kind"] == "model_reason"
    assert request.condition_evidence["request.method"]["sources"] == []
    assert updated.decisions[-1]["condition_selection"]["schema"] == "condition-selection-3"


def test_source_reference_omits_copying_hash_but_checks_stored_evidence(tmp_path):
    store, run = selected_fixture(tmp_path)
    reference = source(store, run, kind="project_guide")
    updated = commit_selections(store, run, choices(run, {"result_id": reference["result_id"]}),
                                decision_id="choose", basis=current_basis(store, run))
    proof = store.load_request(updated).condition_evidence["request.method"]["sources"][0]
    assert proof["document_sha256"] == reference["document_sha256"]
    assert proof["quote"] == reference["quote"] and proof["kind"] == "project_guide"


@pytest.mark.parametrize("mutation", ["hash", "quote", "unknown_result", "unauthorized", "missing_reason"])
def test_optional_citation_cannot_forge_sources_or_expand_selection(tmp_path, mutation):
    store, run = selected_fixture(tmp_path)
    reference = source(store, run)
    proposal = choices(run, reference)
    if mutation == "hash":
        proposal["choices"]["method"]["sources"][0]["document_sha256"] = "0" * 64
    elif mutation == "quote":
        proposal["choices"]["method"]["sources"][0]["quote"] = "Invented support"
    elif mutation == "unknown_result":
        proposal["choices"]["method"]["sources"][0]["result_id"] = "result_not_retrieved"
    elif mutation == "unauthorized":
        proposal["choices"]["environment"] = copy.deepcopy(proposal["choices"]["method"])
    else:
        proposal["choices"]["method"].pop("reason")
    before = store.load_request(run)
    with pytest.raises((ValueError, StoreError)):
        commit_selections(store, run, proposal, decision_id="bad", basis=current_basis(store, run))
    assert store.load_request(run) == before


def test_old_selection_still_requires_retrieval_and_legacy_schema(tmp_path):
    store, run = selected_fixture(tmp_path, policy=None)
    schema = selection_action_parameters(store.load_request(run))
    assert "SelectionSource" in schema["$defs"]
    with pytest.raises(ValueError):
        commit_selections(store, run, choices(run), decision_id="bad", basis=current_basis(store, run))
    assert "workflow_policy" not in store.load_request(run).condition_evidence


def test_prepared_geometry_survives_only_authenticated_non_geometry_choices(tmp_path, monkeypatch):
    store, run = selected_fixture(tmp_path)
    _, identity, consumption = resolved(store, run, monkeypatch)
    xyz = "9\nfixture only\nC 0 0 0\nC 1.5 0 0\nO 2.9 0 0\nH -.5 .9 0\nH -.5 -.9 0\nH 0 0 1\nH 1.5 1 0\nH 1.5 -1 0\nH 3.3 .85 0\n"
    mock_generator(monkeypatch, xyz=xyz)
    call = tool_call(store, run, "structure.prepare",
                     {"system_id": "system_target", "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id,
        operation_status=data["operation_status"], qualified_outputs=data["qualified_outputs"],
        checks={"prepared_geometry": data["qualified_outputs"]["prepared_geometry"]["checks"]},
        artifact_ids=data["artifact_ids"], source={"tool": call.tool, **data["source"]})
    store.finish_call(run, call, result)
    before_request = store.load_request(run)
    artifact_id = prepared_geometry(store, run, before_request, "system_target")
    raw_result = store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes()
    updated = commit_selections(store, run, choices(run), decision_id="choose", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert prepared_geometry(store, updated, request, "system_target") == artifact_id
    assert request.systems == before_request.systems
    assert updated.input_bindings["system_target"]["prepared_geometry"]["source_request_version"] == before_request.version
    assert store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes() == raw_result
    changed = request.model_copy(deep=True)
    changed.systems[0].label = "different identity"
    with pytest.raises(ValueError, match="identity_geometry|activated_request"):
        validate_input_result(store, updated, changed, result, "prepared_geometry")
    assert store.load_result(run.id, identity.id).source["request_version"] == before_request.version


def test_schema_exposes_program_references_only_for_new_policy(tmp_path):
    store, _, run = intake(tmp_path, "请为乙醇计算极化率。")
    request = store.load_request(run)
    new = action_parameters(request=request, code_authorized=True,
        messages=store.read_control(run.id)["messages"], workflow_simplified=True)["normalize_request"]
    old = action_parameters(request=request, code_authorized=True)["normalize_request"]
    assert new["source_fragments"] and "source_refs" in new["schema"]["$defs"]["SemanticGoal"]["properties"]
    assert "source_fragments" not in old
    assert "source_refs" not in old["schema"]["$defs"]["SemanticGoal"]["properties"]


def test_invalid_quote_does_not_mask_independent_target_and_minimum_errors(tmp_path):
    store, config, run = intake(tmp_path, "请为乙醇计算极化率，交付全部分量。")
    parameters = normal(store, config, run)
    goal = parameters["goals"][0]
    goal.update(text_basis="乙醇不存在的原文", system_refs=["missing_system"],
                minimum_evidence=["虚构通过标准"])
    with pytest.raises(ProposalError) as rejected:
        submit(store, run, parameters)
    reasons = {row["reason"] for row in rejected.value.detail["corrections"]}
    assert {"semantic_quote_not_found", "goal_requires_exact_registered_targets",
            "minimum_evidence_requires_exact_user_wording", "general_quantity_requires_exact_user_phrase"} <= reasons
    assert not run.decisions and store.load_request(run).version == 1


def test_explicit_scientific_minimum_survives_program_source_binding(tmp_path):
    text = "请为乙醇计算极化率。要求科学验证通过并交付全部分量。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    goal = parameters["goals"][0]
    goal.pop("text_basis")
    goal.pop("message_id")
    goal["source_refs"] = [row["id"] for row in source_fragments(store.read_control(run.id)["messages"])]
    updated = submit(store, run, parameters)
    actual = store.load_request(updated).goals[0]
    assert actual.verification == "qualified"
    assert any("科学验证通过" in item for item in actual.minimum_evidence)
    assert updated.goal_status[actual.id] != "satisfied"
