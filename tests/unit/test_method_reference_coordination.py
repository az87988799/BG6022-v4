"""Offline method/reference checks; no model, structure generation or ORCA."""

import hashlib
import json
import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from orca_agent import applicability, semantic
from orca_agent.condition_selection import commit_selections
from orca_agent.input_bindings import prepared_geometry, validate_input_result
from orca_agent.model_usage import current_basis
from orca_agent.proposals import ProposalError
from orca_agent.semantic import commit_candidate
from orca_agent.store import Store
from tests.unit.test_semantic_control import candidate
from tests.unit.test_simplified_semantic_selection import intake
from tests.unit.test_text_entry import defaults

RUN_ID = "run_620199b211174f2e910574089d224e23"
SELECTION_ID = "model_8bf0f440766448b6b629e6d3a13dd9dd"
XYZ_ID = "artifact_df31f57f3e9b45c0a93c6f2602bdf1f5"
XYZ_SHA256 = "1d4a7b75058f9f07aab29c88aa1fca7a5b0f33ce46d1b402f787468e066ddca7"


def default_reference(method="RHF"):
    return {"method": method, "conditions": {"method": method, "electronic_state": "RKS"},
        "conditions_source": {"method": "auto_selected", "electronic_state": "default"},
        "semantic_defaults": {"electronic_state": "RKS"}, "systems": [], "goals": [],
        "condition_evidence": {
            "request.method": {"value": method, "source": "auto_selected", "reason": "Offline permitted choice."},
            "request.electronic_state": {"value": "RKS", "source": "default", "default_rule": "local-hf-1"}}}


@pytest.mark.parametrize("method", ["RHF", " rhf "])
def test_default_reference_follows_explicit_rhf_meaning_without_mutation(method):
    values = default_reference(method)
    original = deepcopy(values)
    coordinated, conflicts = applicability.coordinate_method_reference(values)
    assert not conflicts and values == original
    assert coordinated["conditions"]["electronic_state"] == "RHF"
    assert coordinated["conditions_source"]["electronic_state"] == "inherited"
    assert coordinated["semantic_defaults"] == original["semantic_defaults"]
    assert coordinated["condition_evidence"]["request.method"] == original["condition_evidence"]["request.method"]
    proof = coordinated["condition_evidence"]["request.electronic_state"]
    assert "method-reference-1" in json.dumps(proof)
    assert "RKS" in json.dumps(proof["previous_default"])
    assert proof["method_source"]


@pytest.mark.parametrize("barrier", ["explicit", "unknown", "wrong_default", "missing_evidence"])
def test_reference_coordination_does_not_overwrite_conflict_or_unproven_default(barrier):
    values = default_reference()
    if barrier in {"explicit", "unknown"}:
        values["conditions_source"]["electronic_state"] = barrier
        values["condition_evidence"]["request.electronic_state"]["source"] = barrier
    elif barrier == "wrong_default":
        values["semantic_defaults"]["electronic_state"] = "UHF"
    else:
        values["condition_evidence"].pop("request.electronic_state")
    original = deepcopy(values)
    coordinated, conflicts = applicability.coordinate_method_reference(values)
    assert conflicts and coordinated == original and values == original


def test_non_rhf_method_does_not_guess_a_reference():
    values = default_reference("PBE0")
    original = deepcopy(values)
    coordinated, conflicts = applicability.coordinate_method_reference(values)
    assert not conflicts and coordinated == original and values == original


@pytest.mark.parametrize("scope", ["system", "goal"])
def test_reference_coordination_preserves_independent_scoped_rks(scope):
    values = default_reference()
    values["systems"] = [{"id": "h2", "conditions": {}, "conditions_source": {}}]
    if scope == "system":
        values["systems"][0]["conditions"] = {"electronic_state": "RKS"}
        values["systems"][0]["conditions_source"] = {"electronic_state": "explicit"}
    else:
        values["goals"] = [{"id": "energy", "system_ids": ["h2"],
                            "conditions": {"electronic_state": "RKS"}}]
    original = deepcopy(values)
    coordinated, conflicts = applicability.coordinate_method_reference(values)
    assert conflicts and coordinated == original and values == original


def test_h2_normalization_coordinates_rhf_without_executing_tools(tmp_path):
    text = "请为氢气使用 RHF/STO-3G 计算电子能。"
    store, config, run = intake(tmp_path, text)
    message = store.read_control(run.id)["messages"][0]
    conditions = defaults(config)
    for field, value in (("method", "RHF"), ("basis", "STO-3G")):
        conditions[field] = {"value": value, "source": "explicit", "text_basis": value,
                             "message_id": message["id"]}
    parameters = candidate(store, run, kind="normalize", conditions=conditions,
        identities=[{"key": "h2", "name": "氢气", "query_name": "hydrogen",
                     "text_basis": "氢气", "message_id": message["id"]}],
        goals=[{"key": "energy", "port": "energy", "text_basis": "计算电子能",
                "quantity": "电子能", "message_id": message["id"], "system_refs": ["h2"]}])
    original = deepcopy(parameters)
    updated = commit_candidate(store, run, parameters, decision_id="offline_h2_reference",
                               basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.method == "RHF" and request.conditions["electronic_state"] == "RHF"
    assert request.conditions_source["electronic_state"] == "inherited"
    assert request.semantic_defaults["electronic_state"] == "RKS"
    assert not updated.attempts and not updated.calls
    assert updated.usage.orca_starts_actual == updated.usage.model_calls == 0
    assert parameters == original


def test_h2_explicit_rks_token_cannot_be_hidden_by_rhf_method_reference(tmp_path):
    text = "请为氢气使用 RHF/STO-3G 计算电子能，使用RKS。"
    store, config, run = intake(tmp_path, text)
    message = store.read_control(run.id)["messages"][0]
    conditions = defaults(config)
    for field, value in (("method", "RHF"), ("basis", "STO-3G")):
        conditions[field] = {"value": value, "source": "explicit", "text_basis": value,
                             "message_id": message["id"]}
    parameters = candidate(store, run, kind="normalize", conditions=conditions,
        identities=[{"key": "h2", "name": "氢气", "query_name": "hydrogen",
                     "text_basis": "氢气", "message_id": message["id"]}],
        goals=[{"key": "energy", "port": "energy", "text_basis": "计算电子能",
                "quantity": "电子能", "message_id": message["id"], "system_refs": ["h2"]}])
    with pytest.raises(ProposalError) as caught:
        commit_candidate(store, run, parameters, decision_id="offline_explicit_rks",
                         basis=current_basis(store, run))
    assert any(item["path"] == ["parameters", "conditions", "electronic_state"]
               for item in caught.value.detail["corrections"])
    assert store.load_run(run.id) == run
    assert store.load_request(run).version == 1


@pytest.fixture(scope="module")
def a013_sources():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a013/data"
    if not (root / "runs" / RUN_ID / "run.json").is_file():
        pytest.skip("local a013 prepared-input archive is not available")
    store = Store(root)
    run = store.load_run(RUN_ID)
    prior = store.load_request_revision(run, 2)
    assert prior.conditions["electronic_state"] == "RKS"
    selection = store._read_json(f"runs/{run.id}/decisions/{SELECTION_ID}.json")["condition_selection"]
    assert selection["parameters"]["choices"]["method"]["value"] == "RHF"
    # Keep the historical activated Request 2 and input Plan declarations; only
    # this offline view loses subsequent activity. Cumulative usage is retained.
    run.decisions = run.decisions[:next(i for i, item in enumerate(run.decisions) if item["id"] == SELECTION_ID)]
    run.request_version, run.plan_id, run.plan_version = 2, None, None
    run.calls = [item for item in run.calls if item.tool in {"structure.resolve", "structure.prepare"}]
    assert len(run.calls) == 2 and all(item.state == "completed" for item in run.calls)
    run.attempts = []
    assert not run.internal_calls and run.usage.orca_starts_actual == 0
    run.result_ids = [item.result_id for item in run.calls]
    run.selected_results = {item.step_id: item.result_id for item in run.calls}
    run.processed_feedback = [identifier for identifier in run.processed_feedback if identifier in run.result_ids]
    run.input_bindings = {}
    artifacts = set()
    for call in run.calls:
        result = store.load_result(run.id, call.result_id)
        artifacts.update(result.artifact_ids)
        for port in result.qualified_outputs:
            run.input_bindings.setdefault("system_hcn", {})[port] = validate_input_result(
                store, run, prior, result, port)
    assert run.input_bindings["system_hcn"]["prepared_geometry"]["artifact_id"] == XYZ_ID
    assert store.load_artifact(XYZ_ID).sha256 == XYZ_SHA256
    source_paths = {root / "runs" / run.id / name for name in (
        "run.json", "configuration.json", "permission.json", "budget.json", "control.json",
        "environment.json", "sources.json")}
    source_paths.update((root / "runs" / run.id / "request-revisions").glob("*.json"))
    source_paths.update(root / "runs" / run.id / "results" / f"{identifier}.json" for identifier in run.result_ids)
    source_paths.update(path for item in run.decisions
        if (path := root / "runs" / run.id / "decisions" / f"{item['id']}.json").is_file())
    source_paths.add(root / "runs" / run.id / "decisions" / f"{SELECTION_ID}.json")
    for identifier in artifacts:
        source_paths.update(path for path in (root / "artifacts" / identifier).rglob("*") if path.is_file())
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths}
    yield SimpleNamespace(root=root, run=run, prior=prior, selection=selection, source_paths=source_paths)
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths} == hashes


@pytest.mark.parametrize("barrier", ["started", "unknown"])
def test_semantic_reference_change_requires_no_scientific_or_unknown_execution(a013_sources, barrier):
    store = Store(a013_sources.root)
    run = store.load_run(RUN_ID)
    # Read-only archival fixture with one hypothetical admission barrier; no
    # altered Run or counter is persisted, and no execution is requested.
    if barrier == "started":
        run.usage.orca_starts_actual = 1
    else:
        assert run.attempts
        run.attempts[0].state = "unknown"
        run.attempts[0].finished_at = None
    values = default_reference()
    original = deepcopy(values)
    with pytest.raises(ProposalError):
        semantic._coordinated_method_reference(values, store, run)
    assert values == original


@pytest.fixture
def replay_selection(tmp_path, a013_sources):
    source = a013_sources
    root = tmp_path / "a013_offline"
    for path in source.source_paths:
        target = root / path.relative_to(source.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    store = Store(root, environment_root=tmp_path / "environment")
    run = deepcopy(source.run)
    # Test-only checkpoint in a temporary Store. Never save over the real Run,
    # and never reduce the copied cumulative usage or frozen budget snapshots.
    store._write_json(f"runs/{run.id}/run.json", run)
    before_usage = deepcopy(run.usage)
    before_xyz = store.artifact_path(XYZ_ID).read_bytes()
    updated = commit_selections(store, run, deepcopy(source.selection["parameters"]),
        decision_id="offline_a013_rhf", basis=current_basis(store, run))
    assert updated.usage == before_usage
    return SimpleNamespace(store=store, run=updated, prior=source.prior,
        request=store.load_request(updated), before_xyz=before_xyz)


def test_actual_a013_selection_retains_prepared_geometry_and_source_request(replay_selection):
    case = replay_selection
    request = case.request
    assert request.version == 3 and request.method == "RHF"
    assert request.conditions["electronic_state"] == "RHF"
    assert request.conditions_source["electronic_state"] == "inherited"
    assert request.semantic_defaults == case.prior.semantic_defaults
    assert prepared_geometry(case.store, case.run, request, "system_hcn") == XYZ_ID
    binding = case.run.input_bindings["system_hcn"]["prepared_geometry"]
    assert binding["request_version"] == 3 and binding["source_request_version"] == 2
    assert binding["sha256"] == XYZ_SHA256
    assert case.store.artifact_path(XYZ_ID).read_bytes() == case.before_xyz
    result = case.store.load_result(case.run.id, binding["result_id"])
    assert result.source["request_version"] == 2
    assert request.systems == case.prior.systems and request.goals == case.prior.goals
    assert applicability.effective_conditions(request, system_id="system_hcn")["status"] == "passed"


@pytest.mark.parametrize("field,value", [("charge", 1), ("multiplicity", 3)])
def test_prepared_input_still_rejects_changed_electronic_preparation_state(replay_selection, monkeypatch, field, value):
    case = replay_selection
    changed = case.request.model_copy(deep=True)
    changed.conditions[field] = value
    setattr(changed, field, value)
    original = case.store.load_request_revision

    def changed_revision(run, version):
        revision = original(run, version)
        # Target the preparation-state guard after normal historical lookup;
        # this hypothetical revision is never activated or written anywhere.
        return changed if version == changed.version else revision

    monkeypatch.setattr(case.store, "load_request_revision", changed_revision)
    binding = case.run.input_bindings["system_hcn"]["prepared_geometry"]
    result = case.store.load_result(case.run.id, binding["result_id"])
    with pytest.raises(ValueError, match="prepared_geometry_reuse_cannot_change_electronic_preparation_state"):
        validate_input_result(case.store, case.run, changed, result, "prepared_geometry")
    assert case.store.artifact_path(XYZ_ID).read_bytes() == case.before_xyz
