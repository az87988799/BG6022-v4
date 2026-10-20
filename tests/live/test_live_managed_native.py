"""Bounded developer probes of newly admitted inputs; no model or qualification."""

import json
import math
import re
from pathlib import Path

import pytest

from orca_agent.agent import _science
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

H2 = "2\nimmutable initial geometry\nH 0 0 0\nH 0 0 0.74\n"
CASES = [
    ("open_shell", "HF", "def2-SVP", "UHF", 0, 2, "gas_phase", "SP", "", "1\nfluorine\nF 0 0 0\n"),
    ("heavy_ion", "PBE", "def2-SVP", "RKS", 2, 1, "gas_phase", "SP", "", "1\ncalcium ion\nCa 0 0 0\n"),
    ("method_basis", "MP2", "cc-pVDZ", "RHF", 0, 1, "gas_phase", "SP", "", H2),
    ("solvent", "B3LYP", "def2-SVP", "RKS", 0, 1, "CPCM(water)", "SP CPCM(Water)", "", H2),
    ("excited", "B3LYP", "def2-SVP", "RKS", 0, 1, "gas_phase", "SP", "%tddft NRoots 1 MaxIter 40 end", H2),
    ("opt_only", "HF", "STO-3G", "RHF", 0, 1, "gas_phase", "Opt", "%geom MaxIter 20 end", H2),
    ("freq_only", "HF", "STO-3G", "RHF", 0, 1, "gas_phase", "Freq", "%method Z_MaxIter 40 end\n%freq Temp 298.15 end", H2),
    ("numerical_freq", "HF", "STO-3G", "RHF", 0, 1, "gas_phase", "NumFreq", "", H2),
    ("scan", "HF", "STO-3G", "RHF", 0, 1, "gas_phase", "Opt", "%geom MaxIter 20 Scan B 0 1 = 0.7, 0.8, 2 end end", H2),
]


@pytest.mark.parametrize("case", CASES, ids=[item[0] for item in CASES])
def test_real_managed_scope_via_python_sdk(tmp_path, case):
    name, method, basis, state, charge, multiplicity, environment, job, controls, xyz = case
    config = configured(tmp_path)
    assert config.execution.native_input_policy.profile == "managed-native-1"
    # Every probe is bounded and serial; the aggregate environment stays 4 cores/1 GiB.
    config.resources = config.resources.model_copy(update={"script_timeout_seconds": 600,
        "job_timeout_seconds": 300, "collection_timeout_seconds": 180})
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    geometry_path = tmp_path / "initial.xyz"
    geometry_path.write_text(xyz, encoding="ascii")
    original_geometry_bytes = geometry_path.read_bytes()
    geometry = store.import_artifact(geometry_path, "initial_geometry")
    native = (f"! {method} {basis} {state} {job} TightSCF NoAutoStart\n"
        "%maxcore 192\n%pal nprocs 1 end\n%scf MaxIter 100 AutoTRAH false end\n"
        f"{controls}\n* xyzfile {charge} {multiplicity} initial.xyz\n")
    code = ("import json\nfrom pathlib import Path\nfrom orca_task_sdk import run_orca\n"
        f"Path('calculation.inp').write_text({native!r})\n"
        "receipt = run_orca('calculation.inp', dependencies=['initial.xyz'], request_key='probe')\n"
        "Path('receipt.json').write_text(json.dumps(receipt))\n"
        "assert receipt['operation_status'] == 'completed', receipt\n")
    code_path = tmp_path / "task.py"
    code_path.write_text(code, encoding="utf-8")
    artifact = store.import_artifact(code_path, "task_code")
    request = Request(original_text=f"Developer managed input probe: {name}; not autonomous acceptance",
        geometry_artifact_id=geometry.id, method=method, basis=basis, charge=charge, multiplicity=multiplicity,
        conditions={"electronic_state": state, "environment": environment},
        goals=[Goal(id="raw", port="artifacts", quantity="raw execution evidence",
            minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
            verification="reported_with_limitations")])
    step = Step(id="dynamic", logical_id="dynamic", tool="python.execute", parameters={
        "code": {"artifact_id": artifact.id, "sha256": artifact.sha256},
        "inputs": {"entries": [{"artifact_id": geometry.id, "sha256": geometry.sha256,
                                  "target_path": "initial.xyz", "purpose": "current_geometry"}]}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(allowed_tools=["python.execute", "orca.run"],
        code_execution=True, scientific_execution=True, artifact_writes=True,
        artifact_ids=[artifact.id, geometry.id]),
        BudgetLimits(orca_starts=1, extra_orca_starts=0, internal_work_units=1000, run_seconds=900),
        configuration=config.run_configuration())
    result, outcome = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    summary = verify_probe(store, run, result, outcome, geometry.id, original_geometry_bytes, case)
    (tmp_path / "probe-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def verify_probe(store, run, result, outcome, geometry_id, original_geometry_bytes, case):
    """Also usable for read-only replay of immutable developer evidence."""
    name, method, _, _, _, multiplicity, environment, _, _, _ = case
    assert outcome["state"] == "completed", json.dumps(outcome)
    assert result.observations["evidence_collection_complete"], result.diagnostics
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 1
    assert len(run.internal_calls) == 1 and run.internal_calls[0].state == "completed"
    assert not result.qualified_outputs
    receipt = run.internal_calls[0]
    files = [store.load_artifact(identifier) for identifier in receipt.artifact_ids]
    stdout = next(item for item in files if Path(item.path).name == "stdout.out")
    text = store.artifact_path(stdout.id).read_text(encoding="utf-8", errors="replace")
    assert "ORCA TERMINATED NORMALLY" in text
    energies = re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", text)
    assert energies and all(math.isfinite(float(value)) for value in energies)
    assert receipt.actual_conditions["method"].casefold() == method.casefold()
    assert receipt.actual_conditions["multiplicity"] == multiplicity
    assert receipt.actual_conditions["environment"].casefold() == environment.casefold()
    if name == "freq_only":
        assert receipt.actual_conditions["temperature_K"] == 298.15
    assert store.artifact_path(geometry_id).read_bytes() == original_geometry_bytes
    assert store.environment_lease() is None
    return {
        "case": name, "run_id": run.id, "operation_status": result.operation_status,
        "actual_conditions": receipt.actual_conditions, "normal_termination": True,
        "finite_energy_count": len(energies), "stdout_sha256": stdout.sha256,
        "usage": run.usage.model_dump(mode="json"), "scientific_qualification": False,
    }
