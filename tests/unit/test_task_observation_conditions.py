"""Offline archived observations; synthetic receipts are not execution evidence."""

import json

import pytest

from orca_agent.applicability import effective_conditions
from orca_agent.models import (
    Attempt,
    ExecutionConfiguration,
    Goal,
    InputManifest,
    InputManifestEntry,
    InternalCall,
    NativeInputPolicy,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Result,
    RunConfiguration,
    Step,
    SystemInput,
)
from orca_agent.store import Store
from orca_agent.task_observations import _native_current_conditions, assess, collect
from orca_agent.tools.managed import native_input_binding


def pure_file_case(tmp_path, monkeypatch):
    from tests.unit.test_generated_delivery import case, execute_fixture, written

    store, run, request, writer = case(tmp_path, monkeypatch)
    _, reference = written(store, run, writer)
    run, _, result = execute_fixture(store, run, reference)
    assert not run.internal_calls and not request.systems and request.geometry_artifact_id is None
    return store, run, request, request.goals[0], result


@pytest.mark.parametrize("scope", ["request", "goal"])
@pytest.mark.parametrize("name,value", [("method", "HF"), ("basis", "STO-3G"), ("charge", 0),
    ("multiplicity", 1), ("electronic_state", "RHF"), ("environment", "gas_phase")])
def test_pure_file_cannot_discharge_any_explicit_core_condition(tmp_path, monkeypatch, scope, name, value):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    (request if scope == "request" else goal).conditions[name] = value
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "scientific_condition_requires_native_evidence:" + name in answer["reasons"]
    assert answer["scientific_qualification"] is False and not result.qualified_outputs


@pytest.mark.parametrize("source", ["explicit", "auto_selected", "inherited", "inferred", "unknown"])
def test_top_level_scientific_provenance_is_not_ignored_without_native_receipt(tmp_path, monkeypatch, source):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    request.conditions_source["basis"] = source
    answer = assess(store, run, request, goal, result)
    assert "scientific_condition_requires_native_evidence:basis" in answer["reasons"]


@pytest.mark.parametrize("record", ["automatic_selection", "condition_evidence", "selection_reasons"])
def test_retained_scientific_selection_or_evidence_requires_native_source(tmp_path, monkeypatch, record):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    request.conditions_source = {}
    if record == "automatic_selection":
        request.automatic_selection = ["method"]
    else:
        getattr(request, record)["method"] = "test scientific provenance"
    assert "scientific_condition_requires_native_evidence:method" in assess(
        store, run, request, goal, result)["reasons"]


@pytest.mark.parametrize("scope", ["request", "automatic_selection"])
@pytest.mark.parametrize("name", ["method", "basis", "charge", "multiplicity", "electronic_state", "environment"])
def test_scoped_scientific_evidence_requires_native_source(tmp_path, monkeypatch, scope, name):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    request.conditions_source = {}
    request.condition_evidence[scope + "." + name] = {"source": "explicit", "text_basis": "retained declaration"}
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "scientific_condition_requires_native_evidence:" + name in answer["reasons"]


def test_unused_authorized_defaults_do_not_impose_physics_on_pure_file_delivery(tmp_path, monkeypatch):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    # initialize_text provides this template before applying any conditions.
    request.method = request.basis = request.charge = request.multiplicity = None
    request.conditions_source = {}
    request.semantic_defaults = {"method": "HF", "basis": "STO-3G", "charge": 0,
        "multiplicity": 1, "electronic_state": "RHF", "environment": "gas_phase"}
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed" and answer["scientific_qualification"] is False
    assert answer["current_use"]["source_conditions"] == {}


@pytest.mark.parametrize("record", ["conditions", "condition_evidence"])
def test_applied_default_requires_native_source_even_if_value_is_historical_default(tmp_path, monkeypatch, record):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    request.conditions_source = {"method": "default"}
    request.semantic_defaults = {"method": "HF"}
    if record == "conditions":
        request.conditions["method"] = "HF"
    else:
        request.condition_evidence["request.method"] = {
            "value": "HF", "source": "default", "default_rule": "local-hf-1"}
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "scientific_condition_requires_native_evidence:method" in answer["reasons"]


@pytest.mark.parametrize("source", [None, "default"])
def test_only_incidental_historical_defaults_keep_pure_file_delivery(tmp_path, monkeypatch, source):
    store, run, request, goal, result = pure_file_case(tmp_path, monkeypatch)
    request.conditions_source = {} if source is None else dict.fromkeys(
        ("method", "basis", "charge", "multiplicity"), source)
    request.conditions["explain_results"] = True
    goal.conditions["requires_sources"] = True
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed" and answer["scientific_qualification"] is False
    assert answer["current_use"]["source_conditions"] == {}
    request.basis = "def2-SVP"  # A non-default scientific choice cannot be hidden by absent provenance.
    assert "scientific_condition_requires_native_evidence:basis" in assess(
        store, run, request, goal, result)["reasons"]


def request_with_aliases():
    goal = Goal(id="report", port="frequencies", conditions={"method": "pbe0", "basis": "6-31G**"},
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations", system_ids=["molecule"])
    return Request(method="pbe0", basis="6-31G(d)",
        conditions={"method": "PBE0", "basis": "6-31G*", "electronic_state": "rks", "environment": "gas"},
        systems=[SystemInput(id="molecule", conditions={"method": "Pbe0", "basis": "6-31G(d,p)"})],
        goals=[goal])


def test_native_aliases_resolve_across_request_system_and_goal_without_rewriting_history():
    request = request_with_aliases()
    before = request.model_dump(mode="json")
    assert effective_conditions(request, request.goals[0], "molecule")["status"] == "unresolved"
    result = _native_current_conditions(request, request.goals[0], "molecule")
    assert result["status"] == "passed" and not result["reasons"]
    assert result["conditions"] == dict(method="PBE0", basis="6-31G**", charge=0,
        multiplicity=1, electronic_state="RKS", environment="gas_phase")
    assert result["sources"]["basis"] == "system:molecule"
    assert request.model_dump(mode="json") == before
    assert effective_conditions(request, request.goals[0], "molecule")["status"] == "unresolved"


@pytest.mark.parametrize(("scope", "change", "reason"), [
    ("request", {"method": "B3LYP"}, "conflicting_request_condition:method"),
    ("system", {"basis": "STO-3G"}, "conflicting_goal_condition:basis"),
    ("goal", {"method": "B3LYP/G"}, "conflicting_goal_condition:method"),
    ("goal", {"basis": "unsupported-basis"}, "unknown_condition:basis"),
    ("system", {"charge": False}, "unknown_condition:charge"),
])
def test_native_projection_preserves_conflicts_and_unsupported_conditions(scope, change, reason):
    request = request_with_aliases()
    target = {"request": request, "system": request.systems[0], "goal": request.goals[0]}[scope]
    target.conditions.update(change)
    result = _native_current_conditions(request, request.goals[0], "molecule")
    assert result["status"] == "unresolved" and reason in result["reasons"]


@pytest.mark.parametrize("source", ["unknown", "inferred", "not_applicable"])
@pytest.mark.parametrize("scope", ["request", "system"])
def test_known_spelling_cannot_override_unresolved_provenance(source, scope):
    request = request_with_aliases()
    target = request if scope == "request" else request.systems[0]
    target.conditions["electronic_state"] = "RKS"
    target.conditions_source["electronic_state"] = source
    result = _native_current_conditions(request, request.goals[0], "molecule")
    assert result["status"] == "unresolved" and "unknown_condition:electronic_state" in result["reasons"]


def test_explicit_rhf_keeps_its_restricted_state_assertion():
    request = request_with_aliases()
    request.systems[0].conditions.update(method="rhf", electronic_state="RKS")
    request.goals[0].conditions["method"] = "HF"
    result = _native_current_conditions(request, request.goals[0], "molecule")
    assert "conflicting_condition:method/electronic_state" in result["reasons"]


def archived_case(tmp_path, *, explicit_system=False, run_type="single_point", final_geometry=False,
                  cited_file="stdout.out"):
    """Real file hashes and producer identities, with no process or launch mock."""
    execution = ExecutionConfiguration(backend="linux-container", image_id="sha256:" + "a" * 64,
        manifest_sha256="b" * 64, docker_context="desktop-linux", docker_path="E:/inert-docker.exe",
        native_input_policy=NativeInputPolicy())
    store = Store(tmp_path / "data", configuration=RunConfiguration(execution=execution))
    code_path, geometry_path = tmp_path / "task.py", tmp_path / "initial.xyz"
    code_path.write_text("# inert unit fixture; no execution\n", encoding="utf-8")
    geometry_path.write_text("2\nfixture\nH 0 0 0\nH 0 0 0.74\n", encoding="ascii")
    code = store.import_artifact(code_path, "code")
    geometry = store.import_artifact(geometry_path, "geometry")
    # Plan admission currently requires the same Request/Goal spelling. The
    # native receipt independently canonicalizes it to 6-31G* below.
    goal = Goal(id="report", port="frequencies", conditions={"basis": "6-31G(d)"},
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations", minimum_evidence=["purpose_preserved@1"],
        system_ids=["molecule"])
    request = Request(basis="6-31G(d)", conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        systems=[SystemInput(id="molecule", geometry_artifact_id=geometry.id)], goals=[goal])
    step = Step(id="parse", logical_id="parse", tool="python.execute",
        system_id="molecule" if explicit_system else None,
        parameters={"code": {"artifact_id": code.id, "sha256": code.sha256},
                    "observation_names": ["frequencies"]})
    plan = Plan(request_id=request.id, steps=[step],
        goal_map={goal.id: OutputBinding(step_id=step.id, port=goal.port)})
    run = store.create_run(request, plan, PermissionSnapshot(code_execution=True, scientific_execution=True,
        artifact_writes=True, allowed_tools=["python.execute", "orca.run"], artifact_ids=[code.id, geometry.id]))
    binding = native_input_binding(store, run, step)
    attempt = Attempt(step_id=step.id, logical_id=step.logical_id, number=1, tool=step.tool,
        execution_kind="python", input_fingerprint="c" * 64, directory="offline-fixture",
        request_version=request.version, frozen_step=step, state="completed")
    # Deliberately synthetic completed receipt: this test checks delivery only.
    run.attempts.append(attempt)
    raw = tmp_path / "stdout.out"
    raw.write_text("offline frequency fixture\n", encoding="utf-8")
    artifact = store.import_artifact(raw, "raw_output", run_id=run.id, attempt_id=attempt.id)
    native_artifacts = [artifact.id]
    if final_geometry:
        final = tmp_path / "job.xyz"
        final.write_bytes(geometry_path.read_bytes())
        native_artifacts.append(store.import_artifact(final, "raw_output", run_id=run.id,
                                                       attempt_id=attempt.id).id)
    if cited_file != "stdout.out":
        other = tmp_path / cited_file
        other.write_text("offline input or other non-result file\n", encoding="ascii")
        native_artifacts.append(store.import_artifact(other, "raw_output", run_id=run.id,
                                                       attempt_id=attempt.id).id)
    conditions = {**binding["conditions"], "origin": "admitted_native_input",
                  "request_version": request.version, "system_id": binding["system_id"],
                  "run_type": run_type, "input_geometry_sha256": geometry.sha256}
    run.internal_calls.append(InternalCall(request_key="native", input_fingerprint="d" * 64,
        parent_attempt_id=attempt.id, state="completed", actual_conditions=conditions,
        conditions={"admission_version": "native-orca-input-2", "input_binding": binding},
        inputs=InputManifest(entries=[InputManifestEntry(artifact_id=geometry.id, sha256=geometry.sha256,
            target_path="initial.xyz", purpose="geometry")]), artifact_ids=native_artifacts))
    manifest = tmp_path / "observations.json"
    manifest.write_text(json.dumps({"version": 1, "observations": [{"name": "frequencies", "value": [1.],
        "unit": "cm^-1", "sources": [{"filename": cited_file, "internal_request_key": "native"}]}]}))
    declaration = store.import_artifact(manifest, "task_output", run_id=run.id, attempt_id=attempt.id)
    files = {"task/observations.json": {"artifact_id": declaration.id, "sha256": declaration.sha256}}
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="completed",
        artifact_ids=[declaration.id, artifact.id], observations={"evidence_collection_complete": True},
        source={"files": files, "code_sha256": code.sha256},
        observation_records=collect(store, run, step, attempt, files))
    attempt.result_id = result.id
    run.result_ids.append(result.id)
    return store, run, request, goal, result


@pytest.mark.parametrize("explicit_system", [False, True])
def test_archived_alias_observation_uses_same_sole_system_as_native_binding(tmp_path, explicit_system):
    store, run, request, goal, result = archived_case(tmp_path, explicit_system=explicit_system)
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False and not result.qualified_outputs
    assert result.observation_records[0].validation_status == "unverified"
    assert answer["current_use"]["source_conditions"]["system_id"] == "molecule"
    assert run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("change", ["real_basis_conflict", "wrong_system", "ambiguous_system", "no_policy"])
def test_archived_observation_still_rejects_conflict_binding_and_historical_policy(tmp_path, change):
    store, run, request, goal, result = archived_case(tmp_path)
    if change == "real_basis_conflict":
        goal.conditions["basis"] = "STO-3G"
    elif change == "wrong_system":
        run.attempts[0].frozen_step.system_id = "different"
    elif change == "ambiguous_system":
        request.systems.append(SystemInput(id="second"))
    else:
        run.configuration = run.configuration.model_copy(update={"execution":
            run.configuration.execution.model_copy(update={"native_input_policy": None})})
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved" and answer["reasons"]
    assert answer["scientific_qualification"] is False


@pytest.mark.parametrize("scope", ["request", "system", "goal"])
@pytest.mark.parametrize("name,value", [("temperature_K", 298.15), ("standard_state", "1 bar"),
                                       ("unimplemented_physical_condition", "required")])
def test_unverified_observation_cannot_ignore_additional_scientific_conditions(tmp_path, scope, name, value):
    store, run, request, goal, result = archived_case(tmp_path)
    target = {"request": request, "system": request.systems[0], "goal": goal}[scope]
    target.conditions[name] = value
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved"
    assert "native_condition_not_verified:" + name in answer["reasons"]


def test_additional_conditions_keep_goal_conflicts_and_unknown_provenance(tmp_path):
    store, run, request, goal, result = archived_case(tmp_path)
    request.conditions["temperature_K"] = 298.15
    request.conditions_source["temperature_K"] = "unknown"
    goal.conditions["temperature_K"] = 350.
    answer = assess(store, run, request, goal, result)
    assert "conflicting_goal_condition:temperature_K" in answer["reasons"]
    assert "unknown_condition:temperature_K" in answer["reasons"]


def test_presentation_metadata_does_not_invent_physical_conditions(tmp_path):
    store, run, request, goal, result = archived_case(tmp_path)
    request.conditions["explain_results"] = True
    goal.conditions["requires_sources"] = True
    assert assess(store, run, request, goal, result)["status"] == "passed"


@pytest.mark.parametrize("relation,run_type,final_geometry", [
    ("fixed_initial", "single_point", False),
    ("optimized", "optimization_and_analytic_frequencies", True),
])
def test_geometry_relation_requires_matching_input_and_archived_native_output(
        tmp_path, relation, run_type, final_geometry):
    store, run, request, goal, result = archived_case(tmp_path, run_type=run_type, final_geometry=final_geometry)
    goal.conditions["geometry_relation"] = relation
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "passed", answer
    assert answer["scientific_qualification"] is False
    assert result.observation_records[0].validation_status == "unverified"


@pytest.mark.parametrize("change,reason", [
    ("single_point", "optimized_requires_optimization_native_output"),
    ("missing_geometry", "geometry_relation_native_file_missing_or_ambiguous:job.xyz"),
    ("input_source", "optimized_requires_original_native_result_source"),
    ("changed_initial", "geometry_relation_initial_geometry_hash_changed"),
    ("missing_dependency", "geometry_relation_initial_dependency_missing"),
    ("wrong_fixed_initial", "fixed_initial_requires_bound_single_point_output"),
    ("unknown_provenance", "unknown_condition:geometry_relation"),
])
def test_geometry_relation_cannot_be_satisfied_by_name_or_unrelated_files(tmp_path, change, reason):
    store, run, request, goal, result = archived_case(tmp_path,
        run_type="single_point" if change == "single_point" else "optimization_and_analytic_frequencies",
        final_geometry=change != "missing_geometry", cited_file="job.inp" if change == "input_source" else "stdout.out")
    goal.conditions["geometry_relation"] = "fixed_initial" if change == "wrong_fixed_initial" else "optimized"
    if change == "changed_initial":
        run.internal_calls[0].conditions["input_binding"]["geometry_sha256"] = "0" * 64
    elif change == "missing_dependency":
        run.internal_calls[0].inputs = InputManifest()
    elif change == "unknown_provenance":
        request.conditions["geometry_relation"] = "optimized"
        request.conditions_source["geometry_relation"] = "unknown"
    answer = assess(store, run, request, goal, result)
    assert answer["status"] == "unresolved" and reason in answer["reasons"], answer
