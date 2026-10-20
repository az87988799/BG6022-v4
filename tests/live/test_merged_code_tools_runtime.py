"""One bounded SDK/MPI integration probe, never autonomous task acceptance."""

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

pytestmark = [pytest.mark.container, pytest.mark.live]


def test_managed_four_rank_sdk_source_and_observation(tmp_path):
    config = configured(tmp_path)
    assert config.execution.native_input_policy.profile == "managed-native-1"
    config.resources = config.resources.model_copy(update={
        "job_timeout_seconds": 300, "script_timeout_seconds": 600,
        "collection_timeout_seconds": 180})
    store = Store(config.data_root, environment_root=tmp_path / "environment")
    geometry_path = tmp_path / "initial.xyz"
    geometry_path.write_text(
        "3\nimmutable water\nO 0 0 0\nH .757160 0 .586260\nH -.757160 0 .586260\n",
        encoding="ascii")
    geometry = store.import_artifact(geometry_path, "initial_geometry")
    initial_bytes = geometry_path.read_bytes()
    native = (
        "! HF STO-3G RHF SP TightSCF NoRI NoAutoStart\n%maxcore 192\n"
        "%pal nprocs 4 end\n%scf MaxIter 100 AutoTRAH false end\n"
        "* xyzfile 0 1 initial.xyz\n")
    code = (
        "import json, re\nfrom pathlib import Path\n"
        "from orca_task_sdk import run_orca, receipt_source\n"
        f"Path('water.inp').write_text({native!r})\n"
        "receipt = run_orca('water.inp', dependencies=['initial.xyz'], request_key='water')\n"
        "Path('receipt.json').write_text(json.dumps(receipt))\n"
        "assert receipt['operation_status'] == 'completed', receipt\n"
        "text = Path(receipt['files']['stdout.out']).read_text()\n"
        "assert '4 parallel MPI-processes' in text\n"
        "energy = float(re.findall(r'FINAL SINGLE POINT ENERGY\\s+(-?\\d+\\.\\d+)', text)[-1])\n"
        "Path('observations.json').write_text(json.dumps({'version': 1, 'observations': [{\n"
        "    'name': 'energy', 'value': energy, 'unit': 'Eh',\n"
        "    'sources': [receipt_source(receipt, 'stdout.out')],\n"
        "    'limitations': ['Developer smoke; generated parsing is unverified.']\n"
        "}]}))\n")
    code_path = tmp_path / "task.py"
    code_path.write_text(code, encoding="utf-8")
    artifact = store.import_artifact(code_path, "task_code")
    request = Request(
        original_text="Developer SDK/MPI integration probe; not autonomous acceptance",
        geometry_artifact_id=geometry.id, method="HF", basis="STO-3G", charge=0, multiplicity=1,
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        goals=[Goal(id="raw", port="artifacts", quantity="raw execution evidence",
                    minimum_check_version="generic-observation-1",
                    evidence_contract_version="generic-observation-1",
                    verification="reported_with_limitations")])
    step = Step(id="dynamic", logical_id="dynamic", tool="python.execute", parameters={
        "code": {"artifact_id": artifact.id, "sha256": artifact.sha256},
        "observation_names": ["energy"],
        "inputs": {"entries": [{"artifact_id": geometry.id, "sha256": geometry.sha256,
                               "target_path": "initial.xyz", "purpose": "current_geometry"}]}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"raw": OutputBinding(step_id=step.id, port="artifacts")})
    run = store.create_run(
        request, plan, PermissionSnapshot(
            allowed_tools=["python.execute", "orca.run"], code_execution=True,
            scientific_execution=True, artifact_writes=True,
            artifact_ids=[artifact.id, geometry.id]),
        BudgetLimits(orca_starts=1, extra_orca_starts=0, internal_work_units=100, run_seconds=900),
        configuration=config.run_configuration())
    result, outcome = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    assert outcome["state"] == "completed", outcome
    assert result.observations["evidence_collection_complete"], result.diagnostics
    assert run.usage.orca_starts_actual == run.usage.orca_starts_reserved == 1
    assert run.usage.model_calls == 0
    receipt, = run.internal_calls
    assert receipt.state == "completed"
    assert outcome["handle"]["container_id"] == receipt.execution_handle["container_id"]
    observation, = result.observation_records
    source, = observation.sources
    stdout = next(store.load_artifact(identifier) for identifier in receipt.artifact_ids
                  if Path(store.load_artifact(identifier).path).name == "stdout.out")
    text = store.artifact_path(stdout.id).read_text(encoding="utf-8")
    assert "ORCA TERMINATED NORMALLY" in text and "4 parallel MPI-processes" in text
    energy = float(re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", text)[-1])
    assert observation.value == energy
    assert abs(energy - (-74.962991615317)) < 1e-7
    assert (source.artifact_id, source.sha256) == (stdout.id, stdout.sha256)
    assert source.internal_request_key == "water"
    assert observation.validation_status == "unverified" and not result.qualified_outputs
    assert store.artifact_path(geometry.id).read_bytes() == initial_bytes
    assert store.environment_lease() is None
