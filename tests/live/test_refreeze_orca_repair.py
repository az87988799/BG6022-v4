"""Seven bounded ORCA starts, developer-authored tests, zero model calls.

Run serially with --live-orca --live-container, a fresh --basetemp, and
ORCA_AGENT_REPAIR_ENVIRONMENT pointing at the existing shared environment.
These are contract/physics probes, not formal seven-task autonomous acceptance.
"""

import hashlib
import json
import math
import os
import re
from pathlib import Path

import pytest

from orca_agent.agent import _repair_evidence, _science
from orca_agent.model_usage import current_basis
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store
from tests.live.test_container_execution import configured, save_evidence

pytestmark = [pytest.mark.live, pytest.mark.container]
ROOT = Path(__file__).resolve().parents[2]
OLD = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-1/evidence"
LIMITS = "%maxcore 192\n%pal nprocs 4 end\n%scf MaxIter 128 AutoTRAH false end\n"


def geometry_fixture(task, artifact):
    return (OLD / task / "data/artifacts" / artifact / "files/prepared.xyz").read_text(encoding="ascii")


def setup(tmp_path, xyz, sources, *, native=False, method="HF", basis="STO-3G", state="RHF", starts=1):
    config = configured(tmp_path)
    config.model_profile = "disabled"
    assert config.execution.native_input_policy.profile == "managed-native-1"
    assert config.execution.native_input_policy.allow_task_geometry
    assert (config.resources.max_cores, config.resources.total_memory_mb,
            config.resources.maxcore_mb, config.resources.environment_concurrency) == (4, 1024, 192, 1)
    environment = os.environ.get("ORCA_AGENT_REPAIR_ENVIRONMENT")
    assert environment and Path(environment).is_absolute(), "existing shared environment is required"
    store = Store(config.data_root, environment_root=Path(environment))
    initial = tmp_path / "initial.xyz"
    initial.write_text(xyz, encoding="ascii")
    geometry = store.import_artifact(initial, "initial_geometry")
    codes = []
    for i, source in enumerate(sources):
        path = tmp_path / f"source-{i}.{'inp' if native else 'py'}"
        path.write_text(source, encoding="utf-8")
        codes.append(store.import_artifact(path, "native_input" if native else "task_code"))
    request = Request(original_text="Developer contract probe using registered geometry; no model test",
        geometry_artifact_id=geometry.id, method=method, basis=basis,
        conditions={"electronic_state": state, "environment": "gas_phase"},
        goals=[Goal(id="raw", port="artifacts", quantity="bounded execution evidence",
            minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
            verification="reported_with_limitations")])
    step = make_step(codes[0], geometry, native=native)
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(
        allowed_tools=["python.execute", "orca.run"], code_execution=True,
        scientific_execution=True, artifact_writes=True, allow_additional_science=True,
        model_execution=False, artifact_ids=[geometry.id, *(item.id for item in codes)]),
        BudgetLimits(orca_starts=starts, extra_orca_starts=max(0, starts-1), attempts_per_step=2,
            internal_work_units=100, run_seconds=1800, model_calls=0, model_tokens=0,
            plan_revisions=2, code_retries=1),
        configuration=config.run_configuration())
    return config, store, run, step, geometry, codes


def make_step(code, geometry, *, native, identifier="calculation", logical="calculation"):
    params = {("input" if native else "code"): {
        "artifact_id": code.id, "sha256": code.sha256, "language": "orca" if native else "python"},
        "inputs": {"entries": [{"artifact_id": geometry.id, "sha256": geometry.sha256,
            "target_path": "initial.xyz", "purpose": "current_geometry"}]}}
    if not native:
        params["sdk_orca"] = True
    return Step(id=identifier, logical_id=logical, tool="orca.run" if native else "python.execute", parameters=params)


def finish(tmp_path, store, run, result, outcome, geometry, starts, details, *, actual_starts=None):
    save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", outcome
    assert run.usage.model_calls == 0 and not run.model_records
    assert run.usage.orca_starts_reserved == starts
    assert run.usage.orca_starts_actual == (starts if actual_starts is None else actual_starts)
    assert not result.qualified_outputs
    assert store.environment_lease() is None
    assert hashlib.sha256(store.artifact_path(geometry.id).read_bytes()).hexdigest() == geometry.sha256
    summary = {"run_id": run.id, "model_calls": 0,
        "actual_orca_starts": run.usage.orca_starts_actual,
        "reserved_orca_starts": run.usage.orca_starts_reserved,
        "developer_authored": True, "autonomous_acceptance": False,
        "scientific_qualification": False, "usage": run.usage.model_dump(mode="json"),
        "environment_root": str(store.environment_root), **details}
    (tmp_path / "probe-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")


def task_json(store, result, filename):
    ref = result.source["files"]["task/" + filename]
    return json.loads(store.artifact_path(ref["artifact_id"]).read_bytes())


def test_native_nmr_bad_input_then_same_logical_repair(tmp_path):
    xyz = "3\nregistered HCN fixture\nH 0 0 -1.06\nC 0 0 0\nN 0 0 1.16\n"
    good = "! PBE0 def2-TZVP RKS NMR TightSCF NoAutoStart\n" + LIMITS + "* xyzfile 0 1 initial.xyz\n"
    bad = good.replace("* xyzfile", "%elprop Nuclei=all end\n* xyzfile")
    config, store, run, step, geometry, codes = setup(
        tmp_path, xyz, [bad, good], native=True, method="PBE0", basis="def2-TZVP", state="RKS", starts=2)
    failed, failure = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, failed, failure)
    assert failure["state"] == "failed" and run.usage.orca_starts_reserved == 1
    repair_native_nmr(tmp_path, config, store, run, step, geometry, codes[1], failed)


def repair_native_nmr(tmp_path, config, store, run, step, geometry, corrected_code, failed):
    """Continue a settled failure once; no duplicate launch or counter rewrite."""
    initial_actual = run.usage.orca_starts_actual
    assert initial_actual in {0, 1} and run.usage.orca_starts_reserved == 1
    original_attempt = run.attempts[0].model_dump(mode="json")
    original_request = store.load_request(run).model_dump(mode="json")
    repaired = make_step(corrected_code, geometry, native=True, identifier="corrected", logical=step.logical_id)
    old_plan = store.load_plan(run)
    _repair_evidence(store, run, old_plan)
    plan = old_plan.model_copy(update={"version": old_plan.version+1, "steps": [repaired],
        "goal_map": {"raw": OutputBinding(step_id=repaired.id, port="artifacts")}})
    run = store.commit_revision(run, plan, decision_id="developer_native_repair",
                               basis=current_basis(store, run), related_results=[failed.id])
    result, outcome = _science(store, config, run, repaired, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    stdout = next(store.load_artifact(a) for a in result.artifact_ids
                  if Path(store.load_artifact(a).path).name == "stdout.out")
    text = store.artifact_path(stdout.id).read_text(encoding="utf-8")
    tensors = re.findall(r"Total shielding tensor \(ppm\):\s*((?:[ \t]*[-+0-9.Ee]+\s+[-+0-9.Ee]+\s+[-+0-9.Ee]+\s*){3})", text)
    assert len(tensors) == 3, text[-4000:]
    assert all(math.isfinite(float(x)) for tensor in tensors for x in tensor.split())
    assert "ORCA TERMINATED NORMALLY" in text
    assert run.attempts[0].model_dump(mode="json") == original_attempt
    assert store.load_request(run).model_dump(mode="json") == original_request
    assert run.attempts[-1].logical_id == run.attempts[0].logical_id
    finish(tmp_path, store, run, result, outcome, geometry, 2,
        {"expected_input_failure_preserved": True, "shielding_tensors": 3,
         "first_start_confirmation": original_attempt["started"],
         "stdout_sha256": stdout.sha256}, actual_starts=initial_actual + 1)


def test_tddft_real_output_parsed_inside_managed_python(tmp_path):
    xyz = geometry_fixture("T4", "artifact_1fbac0c4b77b44088f8b0cbc8af620b2")
    native = ("! PBE0 def2-SVP RKS TightSCF NoAutoStart\n" + LIMITS +
              "%tddft NRoots 5 MaxIter 128 TDA false Triplets false end\n* xyzfile 0 1 initial.xyz\n")
    code = '''import json, re, math
from pathlib import Path
from orca_task_sdk import run_orca
Path("excitation.inp").write_text(NATIVE)
r = run_orca("excitation.inp", dependencies=["initial.xyz"], request_key="excitation")
assert r["operation_status"] == "completed", r
text = Path(r["files"]["stdout.out"]).read_text()
assert "ORCA TERMINATED NORMALLY" in text
section = text.split("ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS", 1)[1]
rows = []
for line in section.splitlines():
    m = re.match(r"\\s*0-1A\\s*->\\s*(\\d+)-1A\\s+(.+)", line)
    if m:
        values = [float(x) for x in m.group(2).split()]
        assert len(values) >= 6 and all(math.isfinite(x) for x in values)
        rows.append({"root": int(m.group(1)), "values": values})
        if len(rows) == 5: break
assert [r["root"] for r in rows] == [1,2,3,4,5], rows
assert all(row["values"][0] > 0 and row["values"][3] >= 0 for row in rows)
Path("parsed.json").write_text(json.dumps({"rows": rows, "receipt": r,
    "basis": "actual stdout length-gauge table; raw columns retained", "scientific_validation": "unverified"}))
'''.replace("NATIVE", repr(native))
    config, store, run, step, geometry, _ = setup(
        tmp_path, xyz, [code], method="PBE0", basis="def2-SVP", state="RKS")
    result, outcome = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", outcome
    parsed = task_json(store, result, "parsed.json")
    assert len(parsed["rows"]) == 5
    finish(tmp_path, store, run, result, outcome, geometry, 1, {"parsed_roots": 5})


def test_two_independent_polarizability_calculations_and_rotation(tmp_path):
    xyz = geometry_fixture("H2", "artifact_fac94c88d3704dae82bc92a1e0b28af0")
    native = ("! HF def2-SVP RHF TightSCF NoAutoStart\n" + LIMITS +
              "%elprop Polar true end\n* xyzfile 0 1 GEOMETRY\n")
    code = '''import json, re, math
from pathlib import Path
import numpy as np
from orca_task_sdk import run_orca
lines = Path("initial.xyz").read_text().splitlines()
count = int(lines[0])
records = lines[2:2+count]
assert len(records) == count
atoms = [line.split()[0] for line in records]
xyz = np.array([[float(x) for x in line.split()[1:4]] for line in records])
u = np.array([1.,2.,3.]); u /= np.linalg.norm(u)
t = math.radians(37); K = np.array([[0,-u[2],u[1]],[u[2],0,-u[0]],[-u[1],u[0],0]])
R = np.eye(3)*math.cos(t)+(1-math.cos(t))*np.outer(u,u)+math.sin(t)*K
rot = xyz @ R.T
Path("rotated.xyz").write_text(str(len(atoms))+"\\nactive right handed 37 deg r'=Rr\\n"+
    "\\n".join(a+" "+" ".join(f"{v:.12f}" for v in row) for a,row in zip(atoms,rot))+"\\n")
assert np.allclose(np.linalg.norm(xyz[:,None]-xyz[None,:],axis=2),
                   np.linalg.norm(rot[:,None]-rot[None,:],axis=2),atol=1e-10)
tensors, receipts = [], []
for key, geometry in [("original","initial.xyz"),("rotated","rotated.xyz")]:
    Path(key+".inp").write_text(NATIVE.replace("GEOMETRY",geometry))
    r = run_orca(key+".inp",dependencies=[geometry],request_key=key,purpose="new_candidate")
    assert r["operation_status"] == "completed",r
    text = Path(r["files"]["stdout.out"]).read_text()
    assert "ORCA TERMINATED NORMALLY" in text
    section = text.split("STATIC POLARIZABILITY TENSOR (Dipole/Dipole)",1)[1]
    block = section.split("The raw cartesian tensor (atomic units):",1)[1].split("diagonalized",1)[0]
    A = np.array([[float(x) for x in line.split()] for line in block.strip().splitlines()[:3]])
    assert A.shape == (3,3) and np.isfinite(A).all()
    tensors.append(A); receipts.append(r)
error = float(np.max(np.abs(tensors[1] - R @ tensors[0] @ R.T)))
assert error < 1e-3,error
assert abs(np.trace(tensors[1])-np.trace(tensors[0]))/3 < 1e-4
Path("rotation.json").write_text(json.dumps({"tensors":[x.tolist() for x in tensors],
    "R":R.tolist(),"max_abs_covariance_error_au":error,"receipts":receipts,
    "scientific_validation":"unverified"}))
'''.replace("NATIVE", repr(native))
    config, store, run, step, geometry, _ = setup(
        tmp_path, xyz, [code], basis="def2-SVP", starts=2)
    result, outcome = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", outcome
    parsed = task_json(store, result, "rotation.json")
    assert len(run.internal_calls) == 2
    assert run.internal_calls[0].input_fingerprint != run.internal_calls[1].input_fingerprint
    finish(tmp_path, store, run, result, outcome, geometry, 2,
        {"max_abs_covariance_error_au": parsed["max_abs_covariance_error_au"]})


def test_conformer_generation_uses_existing_conformer_ids_and_registered_anchor(tmp_path):
    from rdkit import Chem
    from rdkit.Chem import AllChem
    molecule = Chem.AddHs(Chem.MolFromSmiles("CCCCCC"))
    assert AllChem.EmbedMolecule(molecule, randomSeed=1701) == 0
    xyz = Chem.MolToXYZBlock(molecule)
    native = "! HF STO-3G RHF TightSCF NoAutoStart\n" + LIMITS + "* xyzfile 0 1 GEOMETRY\n"
    code = '''import json, re, math
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolAlign
from orca_task_sdk import run_orca
m = Chem.AddHs(Chem.MolFromSmiles("CCCCCC"))
ids = list(AllChem.EmbedMultipleConfs(m,numConfs=6,randomSeed=1701,pruneRmsThresh=-1))
assert len(ids)==6
# Copy only after conformers exist; use actual IDs, never positional assumptions.
probe = Chem.Mol(m)
rms = rdMolAlign.GetBestRMS(probe,m,prbId=ids[0],refId=ids[1])
assert math.isfinite(rms)
rows = []
for i,cid in enumerate(ids):
    Path(f"candidate_{i}.xyz").write_text(Chem.MolToXYZBlock(m,confId=cid))
for i in range(2):
    geometry=f"candidate_{i}.xyz"
    Path(f"candidate_{i}.inp").write_text(NATIVE.replace("GEOMETRY",geometry))
    r=run_orca(f"candidate_{i}.inp",dependencies=[geometry],request_key=f"candidate_{i}",purpose="new_candidate")
    assert r["operation_status"]=="completed",r
    text=Path(r["files"]["stdout.out"]).read_text()
    energy=float(re.findall(r"FINAL SINGLE POINT ENERGY\\s+([-+0-9.]+)",text)[-1])
    assert math.isfinite(energy) and "ORCA TERMINATED NORMALLY" in text
    rows.append({"candidate":i,"energy_hartree":energy,"receipt":r})
Path("conformers.json").write_text(json.dumps({"generated":6,"computed":2,"excluded_by_scope":4,
    "rms_probe":rms,"rows":rows,"scientific_validation":"unverified",
    "limitations":["developer probe; no global minimum or full task claim"]}))
'''.replace("NATIVE", repr(native))
    config, store, run, step, geometry, _ = setup(tmp_path, xyz, [code], starts=2)
    result, outcome = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", outcome
    parsed = task_json(store, result, "conformers.json")
    assert parsed["generated"] == 6 and parsed["computed"] == 2
    finish(tmp_path, store, run, result, outcome, geometry, 2,
        {"generated_candidates": 6, "computed_candidates": 2, "excluded_by_scope": 4})
