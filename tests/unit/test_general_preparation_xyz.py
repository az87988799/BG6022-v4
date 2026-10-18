"""OPI serialization compatibility; generated geometry is never scientific qualification."""

import hashlib
import json
import os

import pytest
from opi.input.structures import Structure
from test_general_identity_selection import normalized, pubchem
from test_structure_tools import XYZ, environment, resolve_identity, tool_call

from orca_agent.model_usage import current_basis
from orca_agent.models import EvidenceRef, OutputBinding, Plan, Step
from orca_agent.tools import structure
from orca_agent.tools.dispatch import execute_call

ETHANOL = ("9\nsynthetic initial geometry, not optimized\nC 0 0 0\nC 1.5 0 0\nO 2.9 0 0\n"
           "H -.5 .9 0\nH -.5 -.9 0\nH 0 0 1\nH 1.5 1 0\nH 1.5 -1 0\nH 3.3 .85 0\n")


def serialized(tmp_path, xyz):
    source = tmp_path / "synthetic.xyz"
    source.write_bytes(xyz.encode())
    # Use the actual pinned OPI serializer, with supplied synthetic coordinates;
    # this unit helper performs no molecular embedding or process launch.
    return Structure.from_xyz(source).to_xyz_block()


@pytest.mark.parametrize("name,smiles,xyz", [("water", "O", XYZ["water"]),
    ("methane", "C", XYZ["methane"]), ("ethanol", "CCO", ETHANOL)])
def test_actual_opi_serializer_terminal_blank_is_valid_without_rewriting(tmp_path, name, smiles, xyz):
    generated = serialized(tmp_path, xyz)
    original = generated.encode()
    graph = structure._general_graph(smiles)
    assert generated.endswith("\n\n") and generated.splitlines()[1] == ""
    for suffix in ("", " \t\n\r\n"):
        raw = generated + suffix
        checked = structure.validate_prepared_geometry(raw, name, graph["elements"], graph=graph)
        assert checked["atom_mapping"] == list(range(len(graph["elements"])))
        assert raw == generated + suffix
    if name in ("water", "methane"):
        assert structure.validate_prepared_geometry(generated, name, graph["elements"])["unit"] == "angstrom"
    assert generated.encode() == original


@pytest.mark.parametrize("mutation", ["internal_blank", "missing_atom", "extra_atom", "nan", "identity", "distance"])
def test_only_trailing_blank_rows_are_ignored(tmp_path, mutation):
    generated = serialized(tmp_path, ETHANOL)
    rows = generated.splitlines()
    if mutation == "internal_blank":
        rows.insert(4, " \t")
    elif mutation == "missing_atom":
        rows.pop(-2)
    elif mutation == "extra_atom":
        rows.insert(-1, "H 10 10 10")
    else:
        first = rows[2].split()
        first[0 if mutation == "identity" else 1] = {
            "nan": "nan", "identity": "N", "distance": "100"}[mutation]
        rows[2] = " ".join(first)
    graph = structure._general_graph("CCO")
    with pytest.raises(ValueError):
        structure.validate_prepared_geometry("\n".join(rows) + "\n", "ethanol", graph["elements"], graph=graph)


def worker_output(monkeypatch, generated):
    class Generated:
        def to_xyz_block(self):
            return generated
    monkeypatch.setattr(Structure, "from_smiles", lambda *args, **kwargs: Generated())


def worker_input(path, *, general=True):
    path.write_text(json.dumps({"smiles": "CCO" if general else "O",
        "canonical_name": "ethanol" if general else "water", "charge": 0, "multiplicity": 1,
        "identity_rule": structure.GENERAL_IDENTITY_RULE if general else structure.IDENTITY_RULE}), encoding="utf-8")


def test_worker_keeps_actual_serializer_bytes_and_hash(tmp_path, monkeypatch):
    generated = serialized(tmp_path, ETHANOL)
    worker_output(monkeypatch, generated)
    path = tmp_path / "worker-input.json"
    worker_input(path)
    structure._worker(path)
    raw = (tmp_path / "prepared.xyz").read_bytes()
    assert raw == generated.encode()
    assert json.loads((tmp_path / "worker-result.json").read_bytes())["sha256"] == hashlib.sha256(raw).hexdigest()


def test_worker_retains_bounded_rejected_bytes_without_success_receipt(tmp_path, monkeypatch):
    generated = "9\ninvalid generated evidence\nC nan 0 0\n"
    worker_output(monkeypatch, generated)
    path = tmp_path / "worker-input.json"
    worker_input(path)
    with pytest.raises(ValueError):
        structure._worker(path)
    assert (tmp_path / "prepared.xyz").read_bytes() == generated.encode()
    assert not (tmp_path / "worker-result.json").exists()


def test_worker_does_not_publish_oversized_generation(tmp_path, monkeypatch):
    worker_output(monkeypatch, "x" * (structure.MAX_GEOMETRY_BYTES + 1))
    path = tmp_path / "worker-input.json"
    worker_input(path)
    with pytest.raises(ValueError, match="size bound"):
        structure._worker(path)
    assert not (tmp_path / "prepared.xyz").exists() and not (tmp_path / "worker-result.json").exists()


def test_failed_generation_candidate_is_archived_without_input_qualification(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    generated = "3\ninvalid generated evidence\nO nan 0 0\nH 0 .8 .6\nH 0 -.8 .6\n\n"
    worker_output(monkeypatch, generated)
    def rejected_worker(store, run, call, document, parameters):
        path = store.path(f"runs/{run.id}/calls/{call.id}/worker-input.json")
        worker_input(path, general=False)
        with pytest.raises(ValueError):
            structure._worker(path)
        return {"state": "failed", "exit_code": 1}  # Unit simulation, not process evidence.
    monkeypatch.setattr(structure, "_generate", rejected_worker)
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    assert data["operation_status"] == "failed" and not data["qualified_outputs"]
    candidates = [store.load_artifact(identifier) for identifier in data["artifact_ids"]]
    candidates = [artifact for artifact in candidates if artifact.role == "prepared_geometry_candidate"]
    assert len(candidates) == 1
    assert store.artifact_path(candidates[0].id).read_bytes() == generated.encode()
    assert candidates[0].sha256 == hashlib.sha256(generated.encode()).hexdigest()
    assert "prepared_geometry" not in run.input_bindings.get("water", {})


@pytest.mark.backend
@pytest.mark.skipif(os.name != "nt" or os.environ.get("ORCA_AGENT_STRUCTURE_SMOKE") != "1",
                    reason="explicit managed OPI preparation smoke not enabled")
def test_real_managed_opi_preparation_same_live007_worker_input(tmp_path, monkeypatch):
    store, run = normalized(tmp_path, automatic=False)
    request = store.load_request(run)
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="system_target",
                   parameters={"system_id": "system_target"})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="system_target",
        parameters={"system_id": "system_target"}, depends_on=[resolve.id],
        inputs={"identity": EvidenceRef(producer_step_id=resolve.id, port="resolved_identity",
                                       rule_version="structure-identity-2")})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[resolve, prepare],
        goal_map={goal.id: OutputBinding(port=goal.port, gap="Initial preparation only.") for goal in request.goals})
    run = store.commit_revision(run, plan, decision_id="offline_probe_plan", basis=current_basis(store, run))
    monkeypatch.setattr(structure, "_paced_query", lambda *args, **kwargs: (200, pubchem("CCO", "C2H6O"), True))
    identity = execute_call(store, run, resolve.tool, resolve.parameters.model_dump(), step=resolve)
    assert identity.operation_status == "completed"
    result = execute_call(store, run, prepare.tool, prepare.parameters.model_dump(), step=prepare,
                          results={resolve.id: identity})
    evidence = {"scope": "Real OPI embedding under the production Windows Job; synthetic identity fixture, no HTTP/model/ORCA.",
        "result": result.model_dump(mode="json"), "run_usage": run.usage.model_dump(mode="json")}
    (tmp_path / "probe.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    assert result.operation_status == "completed", result.diagnostics
    execution = result.source["execution"]
    assert execution["state"] == "completed" and execution["exit_code"] == 0
    assert execution["handle"]["atomic_job_assignment"] is True
    usage = execution["resource_usage"]
    assert usage["active_processes"] == 0 and usage["cores"] == 1
    assert usage["job_commit_limit_bytes"] == 1024 * 1024 * 1024
    directory = store.path(f"runs/{run.id}/calls/{result.call_id}")
    assert json.loads((directory / "worker-input.json").read_bytes()) == {
        "canonical_name": "ethanol", "charge": 0, "identity_rule": "structure-identity-2",
        "multiplicity": 1, "smiles": "CCO"}
    raw = (directory / "prepared.xyz").read_bytes()
    output = result.qualified_outputs["prepared_geometry"]
    artifact = store.load_artifact(output.artifact_id)
    assert store.artifact_path(artifact.id).read_bytes() == raw and raw.endswith(b"\n\n")
    assert artifact.sha256 == hashlib.sha256(raw).hexdigest()
    assert output.checks[0].rule_version == "structure-prepare-2"
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == run.usage.model_calls == 0
    assert not run.attempts and all(value == "insufficient_evidence" for value in run.goal_status.values())
