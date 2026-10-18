"""Developer-authored primitive probe, not autonomous model acceptance."""

import json

import pytest
from rdkit import Chem
from rdkit.Chem import AllChem

from orca_agent.agent import _science
from orca_agent.models import (
    BudgetLimits,
    Goal,
    NativeInputPolicy,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store
from tests.helpers.analytic_oracle import inspect_optfreq
from tests.live.test_container_execution import configured, save_evidence

pytestmark = [pytest.mark.live, pytest.mark.container]


def test_real_sdk_bounded_analytic_ethanol(tmp_path):
    config = configured(tmp_path)
    config.execution = config.execution.model_copy(update={"native_input_policy": NativeInputPolicy()})
    config.resources = config.resources.model_copy(update={"script_timeout_seconds": 300,
                                                          "job_timeout_seconds": 900})
    assert (config.resources.max_cores, config.resources.total_memory_mb,
            config.resources.maxcore_mb, config.resources.script_timeout_seconds) == (4, 1024, 192, 300)
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    molecule = Chem.AddHs(Chem.MolFromSmiles("CCO"))
    assert AllChem.EmbedMolecule(molecule, randomSeed=20261018) == 0
    geometry_path = tmp_path / "initial.xyz"
    geometry_path.write_text(Chem.MolToXYZBlock(molecule), encoding="ascii")
    geometry = store.import_artifact(geometry_path, "initial_geometry")
    code = '''import json
from pathlib import Path
from orca_task_sdk import run_orca
Path("ethanol.inp").write_text("""! RHF STO-3G TightSCF NORI NoAutoStart Opt Freq
%maxcore 192
%pal nprocs 4 end
%scf MaxIter 128 ConvForced true AutoTRAH false end
%geom MaxIter 64 EnforceStrictConvergence true end
%method Z_MaxIter 64 end
* xyzfile 0 1 initial.xyz
""")
receipt = run_orca("ethanol.inp", dependencies=["initial.xyz"], request_key="ethanol_analytic")
Path("receipt.json").write_text(json.dumps(receipt, indent=2))
assert receipt["operation_status"] == "completed", receipt
'''
    code_path = tmp_path / "task.py"
    code_path.write_text(code, encoding="utf-8")
    artifact = store.import_artifact(code_path, "task_code")
    request = Request(original_text="Developer primitive ethanol Opt+Freq probe; not autonomous acceptance",
        geometry_artifact_id=geometry.id, conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        goals=[Goal(id="raw", port="artifacts", quantity="raw Opt+Freq evidence",
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
        BudgetLimits(orca_starts=1, extra_orca_starts=0, internal_work_units=93, run_seconds=600),
        configuration=config.run_configuration())
    result, outcome = _science(store, config, run, step, {}, None, None)
    evidence = save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", evidence
    assert result.observations["evidence_collection_complete"], evidence
    assert run.usage.orca_starts_reserved == run.usage.orca_starts_actual == 1
    assert run.usage.internal_work_units == 93 and len(run.internal_calls) == 1
    assert run.internal_calls[0].actual_conditions["origin"] == "admitted_native_input"
    assert not result.qualified_outputs
    internal = store.path(run.attempts[0].directory) / "native" / "task"
    oracle = inspect_optfreq(internal / "stdout.out", internal / "job.hess", internal / "job.xyz")
    (tmp_path / "independent-oracle.json").write_text(json.dumps(oracle, indent=2), encoding="utf-8")
    assert oracle["full_mode_count"] == 27 and oracle["vibrational_mode_count"] == 21
    assert store.environment_lease() is None
