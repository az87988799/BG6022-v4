"""Real locked OPI/RDKit geometry checks and offline provenance regressions."""

import copy
import hashlib
import json

import pytest
from test_general_identity_selection import normalized, pubchem, selections, source
from test_structure_tools import tool_call

from orca_agent.condition_selection import commit_selections, selection_available
from orca_agent.input_bindings import validate_input_result
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    EvidenceRef,
    ExecutionConfiguration,
    NativeInputPolicy,
    OutputBinding,
    Plan,
    Result,
    Step,
    fingerprint,
)
from orca_agent.tools import structure


def execution(profile="managed-native-1"):
    return ExecutionConfiguration(backend="linux-container", image_id="sha256:" + "a" * 64,
        manifest_sha256="b" * 64, docker_context="offline-test", docker_path="C:/Docker/docker.exe",
        native_input_policy=NativeInputPolicy(profile=profile))


def resolved(store, run, monkeypatch, smiles="CCO", formula="C2H6O", charge=0):
    monkeypatch.setattr(structure, "_paced_query", lambda *args, **kwargs:
        (200, pubchem(smiles, formula, charge=charge), True))
    call = tool_call(store, run, "structure.resolve", {"system_id": "system_target"})
    data = structure.resolve(store, run, call)
    assert data["operation_status"] == "completed", data
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id,
        operation_status="completed", artifact_ids=data["artifact_ids"],
        qualified_outputs=data["qualified_outputs"],
        checks={"resolved_identity": data["qualified_outputs"]["resolved_identity"]["checks"]},
        source={"tool": call.tool, **data["source"]})
    store.finish_call(run, call, result)
    consumption = {"identity": {"run_id": run.id, "result_id": result.id, "port": "resolved_identity",
        "status": "qualified", "result_fingerprint": fingerprint(result), "artifact_hashes": {
            identifier: store.load_artifact(identifier).sha256 for identifier in result.artifact_ids}}}
    return call, result, consumption


@pytest.mark.parametrize(("smiles", "formula", "charge", "multiplicity"), [
    ("[NH4+]", "H4N+", 1, 1), ("[CH3]", "CH3", 0, 2),
    ("N[C@@H](C)C(=O)O", "C3H7NO2", 0, 1), ("F/C=C/F", "C2H2F2", 0, 1),
])
def test_new_profile_prepares_confirmed_ions_radicals_and_specified_stereo(
        tmp_path, monkeypatch, smiles, formula, charge, multiplicity):
    store, run = normalized(tmp_path, execution=execution(), automatic=False,
        state={"charge": charge, "multiplicity": multiplicity, "electronic_state": "UKS" if multiplicity > 1 else "RKS"})
    _, _, consumption = resolved(store, run, monkeypatch, smiles, formula, charge)
    structure.validate_call_inputs(store, run, {"system_id": "system_target", "charge": charge,
        "multiplicity": multiplicity}, "structure.prepare", consumption)
    worker = tmp_path / "worker-input.json"
    worker.write_text(json.dumps({"smiles": smiles, "canonical_name": "test identity",
        "charge": charge, "multiplicity": multiplicity, "identity_rule": structure.GENERAL_IDENTITY_RULE,
        "general_admission": True}))
    structure._worker(worker)  # Actual locked OPI / RDKit, no simulated embedding.
    raw = (tmp_path / "prepared.xyz").read_bytes()
    receipt = json.loads((tmp_path / "worker-result.json").read_bytes())
    assert receipt["sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["versions"]["orca-pi"] == "2.0.0" and receipt["versions"]["rdkit"] == "2025.9.6"
    graph = structure._general_graph(smiles)
    checked = structure.validate_prepared_geometry(raw.decode(), "test identity", graph["elements"],
        graph=graph, smiles=smiles)
    assert checked["atom_mapping"] == list(range(len(graph["elements"])))
    if "@" in smiles:
        rows = raw.decode().splitlines()
        reflected = rows[:2] + [" ".join([row.split()[0], str(-float(row.split()[1])), *row.split()[2:]])
                               for row in rows[2:] if row.strip()]
        with pytest.raises(ValueError, match="specified stereochemistry"):
            structure.validate_prepared_geometry("\n".join(reflected), "test identity", graph["elements"],
                graph=graph, smiles=smiles)


@pytest.mark.parametrize(("smiles", "formula", "state", "error"), [
    ("CC(F)Cl", "C2H4ClF", {}, "unspecified stereochemistry"),
    ("[NH4+]", "H4N+", {}, "charge differs"),
    ("[CH3]", "CH3", {}, "electron count"),
])
def test_general_preparation_does_not_guess_identity_or_electronic_state(tmp_path, monkeypatch, smiles, formula, state, error):
    store, run = normalized(tmp_path, execution=execution(), automatic=False, state=state)
    _, _, consumption = resolved(store, run, monkeypatch, smiles, formula, 1 if "NH4" in smiles else 0)
    with pytest.raises(ValueError, match=error):
        structure.validate_call_inputs(store, run, {"system_id": "system_target", "charge": 0,
            "multiplicity": 1}, "structure.prepare", consumption)


def test_historical_profile_keeps_neutral_singlet_scope(tmp_path, monkeypatch):
    store, run = normalized(tmp_path, execution=execution("bounded-analytic-1"), automatic=False,
        state={"charge": 0, "multiplicity": 2, "electronic_state": "UKS"})
    _, _, consumption = resolved(store, run, monkeypatch, "[CH3]", "CH3")
    with pytest.raises(ValueError, match="frozen admission profile"):
        structure.validate_call_inputs(store, run, {"system_id": "system_target", "charge": 0,
            "multiplicity": 2}, "structure.prepare", consumption)


@pytest.mark.parametrize("profile", ["bounded-analytic-1", "managed-native-1"])
def test_electron_count_check_follows_frozen_preparation_profile(tmp_path, monkeypatch, profile):
    # RDKit records no radicals for this odd-electron graph. The old geometry
    # checker admitted it without certifying spin; new checks cannot rewrite that history.
    graph = structure._general_graph("[FeH]")
    assert graph["radical_electrons"] == 0 and graph["elements"] == ["Fe", "H"]
    store, run = normalized(tmp_path, execution=execution(profile), automatic=False)
    _, _, consumption = resolved(store, run, monkeypatch, "[FeH]", graph["formula"])
    parameters = {"system_id": "system_target", "charge": 0, "multiplicity": 1}
    worker = tmp_path / "worker-input.json"
    worker.write_text(json.dumps({"smiles": "[FeH]", "canonical_name": "test identity",
        "charge": 0, "multiplicity": 1, "identity_rule": structure.GENERAL_IDENTITY_RULE,
        "general_admission": profile == "managed-native-1"}))
    if profile == "managed-native-1":
        with pytest.raises(ValueError, match="electron count"):
            structure.validate_call_inputs(store, run, parameters, "structure.prepare", consumption)
        with pytest.raises(ValueError, match="electron count"):
            structure._worker(worker)
        assert not (tmp_path / "prepared.xyz").exists()
    else:
        structure.validate_call_inputs(store, run, parameters, "structure.prepare", consumption)
        structure._worker(worker)  # Real locked OPI/RDKit retains the old initial-geometry contract.
        assert (tmp_path / "worker-result.json").is_file()
        call = tool_call(store, run, "structure.prepare", parameters, consumption)
        recorded = {"operation_status": "completed", "artifact_ids": [], "source": {"historical": True}}
        structure._finish(store, run, call, recorded)
        monkeypatch.setattr(structure, "_generate", lambda *args: pytest.fail("replayed a completed Call"))
        assert structure.prepare(store, run, call) == recorded


def test_selection_after_identity_preserves_original_receipt_and_current_binding(tmp_path, monkeypatch):
    store, run = normalized(tmp_path, execution=execution())
    call, identity, consumption = resolved(store, run, monkeypatch)
    prior = store.load_request(run)
    frozen_call, frozen_result = copy.deepcopy(call), copy.deepcopy(identity)
    frozen_artifacts = {item: store.artifact_path(item).read_bytes() for item in identity.artifact_ids}
    reference = source(store, run, excerpt="Hybrid functionals combine density functional and exact exchange contributions.")
    proposal = selections(store, run, reference)
    proposal["choices"]["method"]["value"] = "PBE0 D4"
    assert selection_available(store, run)
    run = commit_selections(store, run, proposal, decision_id="select_after_identity", basis=current_basis(store, run))
    current = store.load_request(run)
    binding = run.input_bindings["system_target"]["resolved_identity"]
    assert binding["request_version"] == current.version and binding["source_request_version"] == prior.version
    assert validate_input_result(store, run, current, identity, "resolved_identity") == binding
    assert store.load_result(run.id, identity.id) == frozen_result
    assert next(item for item in run.calls if item.id == call.id) == frozen_call
    assert all(store.artifact_path(item).read_bytes() == raw for item, raw in frozen_artifacts.items())
    evidence = current.condition_evidence["request.method"]
    assert evidence["schema"] == "condition-selection-2" and evidence["sources"][0]["quote"] == reference["quote"]
    assert evidence["reason"] == proposal["choices"]["method"]["reason"]
    structure.validate_call_inputs(store, run, {"system_id": "system_target", "charge": 0,
        "multiplicity": 1}, "structure.prepare", consumption)
    for mutation in ("identity", "geometry"):
        changed = current.model_copy(deep=True)
        if mutation == "identity":
            changed.systems[0].identity["requested_names"] = ["another molecule"]
        else:
            changed.systems[0].geometry_source = "artifact"
        with pytest.raises(ValueError, match="activated_request"):
            validate_input_result(store, run, changed, identity, "resolved_identity")
    tool_call(store, run, "structure.prepare", {"system_id": "system_target", "charge": 0, "multiplicity": 1}, consumption)
    assert not selection_available(store, run)


def test_new_selection_still_requires_intact_official_sources_and_nonempty_reason(tmp_path):
    from pydantic import ValidationError

    from orca_agent.proposals import ProposalError
    store, run = normalized(tmp_path, execution=execution())
    reference = source(store, run, kind="project_guide", excerpt="general advice")
    with pytest.raises(ProposalError, match="Correct"):
        commit_selections(store, run, selections(store, run, reference), decision_id="bad", basis=current_basis(store, run))
    proposal = selections(store, run, reference)
    proposal["choices"]["method"]["reason"] = ""
    with pytest.raises(ValidationError):
        commit_selections(store, run, proposal, decision_id="empty_reason", basis=current_basis(store, run))


def test_preparation_dispatch_consumes_identity_from_prior_selection_revision(tmp_path, monkeypatch):
    from orca_agent.input_bindings import prepared_geometry
    from orca_agent.tools.dispatch import execute_call

    store, run = normalized(tmp_path, execution=execution())
    _, identity, _ = resolved(store, run, monkeypatch)
    reference = source(store, run)
    run = commit_selections(store, run, selections(store, run, reference), decision_id="select",
                            basis=current_basis(store, run))
    request = store.load_request(run)
    step = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="system_target",
        parameters={"system_id": "system_target", "charge": 0, "multiplicity": 1},
        inputs={"identity": EvidenceRef(run_id=run.id, result_id=identity.id,
            port="resolved_identity", rule_version="structure-identity-3")})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[step],
        goal_map={request.goals[0].id: OutputBinding(port="polarizability", gap="Prepare initial coordinates first.")})
    run = store.commit_revision(run, plan, decision_id="prepare_plan", basis=current_basis(store, run))
    def generate(store, run, call, document, params):
        worker = store.path(f"runs/{run.id}/calls/{call.id}/worker-input.json")
        worker.write_text(json.dumps({"smiles": document["smiles"], "canonical_name": document["canonical_name"],
            "charge": params.charge, "multiplicity": params.multiplicity, "identity_rule": document["schema"],
            "general_admission": True}))
        structure._worker(worker)  # Fixed local worker only; managed isolation is not under test here.
        return {"state": "completed", "exit_code": 0}
    monkeypatch.setattr(structure, "_generate", generate)
    result = execute_call(store, run, step.tool, step.parameters.model_dump(mode="json"), step=step)
    assert result.operation_status == "completed", result.diagnostics
    assert prepared_geometry(store, run, request, "system_target") == result.qualified_outputs["prepared_geometry"].artifact_id
    assert store.load_result(run.id, identity.id).source["request_version"] == request.version - 1
