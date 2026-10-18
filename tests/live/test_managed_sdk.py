"""Developer-written smoke only; not model autonomy or scientific qualification."""

import json
from pathlib import Path

import pytest

from orca_agent.agent import _science
from orca_agent.config import Config
from orca_agent.models import (
    BudgetLimits,
    Goal,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    ResourceLimits,
    Step,
)
from orca_agent.store import Store

pytestmark = pytest.mark.live


def test_dynamic_python_sdk_real_orca_single_point(tmp_path):
    code = tmp_path / "dynamic_task.py"
    code.write_text('''from pathlib import Path
import json
from orca_task_sdk import run_orca
from rdkit import Chem
assert Chem.MolFromSmiles("O").GetNumAtoms() == 1
Path("water.inp").write_text("""! HF STO-3G TightSCF NoAutoStart
%maxcore 192
%scf maxiter 100 end
* xyz 0 1
O 0 0 0
H 0 .757 .587
H 0 -.757 .587
*
""")
receipt = run_orca("water.inp", request_key="water_sp")
Path("receipt.json").write_text(json.dumps(receipt, indent=2))
assert receipt["operation_status"] == "completed", receipt
text = Path(receipt["files"]["stdout.out"]).read_text()
assert "ORCA TERMINATED NORMALLY" in text
assert "FINAL SINGLE POINT ENERGY" in text
Path("smoke.json").write_text(json.dumps({"real_orca_completed": True, "scientific_validation": "unverified"}))
''', encoding="utf-8")
    resources = ResourceLimits(job_timeout_seconds=90, script_timeout_seconds=180)
    config = Config(data_root=tmp_path / "data", orca_path=Path("E:/orca/orca.exe"),
                    mpi_path=Path("C:/Program Files/Microsoft MPI/Bin/mpiexec.exe"), resources=resources)
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    artifact = store.import_artifact(code, "task_code")
    request = Request(goals=[Goal(id="raw", port="artifacts", quantity="raw ORCA evidence",
        minimum_check_version="generic-observation-1", evidence_contract_version="generic-observation-1",
        verification="reported_with_limitations")])
    step = Step(id="dynamic", logical_id="dynamic", tool="python.execute", parameters={
        "code": {"artifact_id": artifact.id, "sha256": artifact.sha256, "language": "python"}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(request, plan, PermissionSnapshot(
        allowed_tools=["python.execute", "orca.run"], code_execution=True,
        scientific_execution=True, artifact_writes=True, artifact_ids=[artifact.id]),
        BudgetLimits(orca_starts=1, extra_orca_starts=0, internal_work_units=1, run_seconds=600),
        configuration=config.run_configuration())
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = {"run_id": run.id, "outcome": outcome, "result": result.model_dump(mode="json"),
                "usage": run.usage.model_dump(mode="json"),
                "internal_calls": [call.model_dump(mode="json") for call in run.internal_calls]}
    (tmp_path / "smoke-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert outcome["state"] == "completed", evidence
    assert len(run.internal_calls) == 1 and run.internal_calls[0].state == "completed", evidence
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == 1
    assert run.usage.internal_work_units == 1
    assert run.usage.model_calls == 0
    assert not result.qualified_outputs
    assert result.observations["scientific_validation"] == "unverified"
    assert "task/smoke.json" in result.source["files"]
    assert not result.diagnostics, evidence
    assert outcome["resource_usage"]["total_processes"] > 1
    assert store.environment_lease() is None


@pytest.mark.parametrize("case", ["water_sp", "water_opt"])
def test_legacy_opi_serial_uses_isolated_native_chain(tmp_path, case):
    from orca_agent.runner import execute, initialize
    from orca_agent.store import sha256_file

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "phase_a" / case
    specification = json.loads((fixture / "request.json").read_text("utf-8"))
    # Explicit developer serial case; the frozen four-core fixture is unchanged.
    specification["steps"][0]["parameters"]["cores"] = 1
    (tmp_path / "geometry.xyz").write_bytes((fixture / "geometry.xyz").read_bytes())
    source = tmp_path / "request.json"
    source.write_text(json.dumps(specification), encoding="utf-8")
    config = Config(data_root=tmp_path / "data", orca_path=Path("E:/orca/orca.exe"),
                    mpi_path=Path("C:/Program Files/Microsoft MPI/Bin/mpiexec.exe"))
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    before = sha256_file(fixture / "geometry.xyz")
    run = initialize(store, config, source)
    run = execute(store, config, run.id)
    evidence = {"case": case, "run": run.model_dump(mode="json"),
                "results": [store.load_result(run.id, item).model_dump(mode="json") for item in run.result_ids]}
    (tmp_path / "smoke-evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    assert run.state == "completed", evidence
    assert run.usage.orca_starts_actual == 1
    result = store.load_result(run.id, run.attempts[0].result_id)
    assert "energy" in result.qualified_outputs
    if case == "water_opt":
        assert "optimized_geometry" in result.qualified_outputs
    execution = result.source["execution"]
    assert execution["isolation"]["lpac_confirmed"] is True
    assert execution["resource_usage"]["cores"] == 1
    assert not execution["resource_usage"]["active_processes"]
    assert not (store.path(run.attempts[0].directory) / "task" / "job.2jsonout").exists()
    assert sha256_file(fixture / "geometry.xyz") == before
    assert store.environment_lease() is None
