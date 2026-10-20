"""Three bounded developer probes, zero LLM; never formal autonomous task evidence."""

import json
import os
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
from orca_agent.task_observations import assess
from tests.live.test_container_execution import configured, save_evidence
from tests.live.test_refreeze_orca_repair import make_step

pytestmark = [pytest.mark.live, pytest.mark.container]
XYZ = "2\nimmutable developer probe\nH 0 0 0\nH 0 0 0.74\n"
NATIVE = "! HF STO-3G SP\n* xyzfile 0 1 initial.xyz\n"


@pytest.mark.parametrize("kind", ["direct", "sdk", "sdk_timeout"])
def test_materialized_input_and_settled_collection(tmp_path, kind):
    config = configured(tmp_path)
    config.workflow_policy = "simplified-1"
    config.model_profile = "disabled"
    if kind == "sdk_timeout":
        config.resources = config.resources.model_copy(update={
            "job_timeout_seconds": 1, "script_timeout_seconds": 60})
    shared = Path(os.environ["ORCA_AGENT_REPAIR_ENVIRONMENT"])
    assert shared.is_absolute() and shared.is_dir()
    store = Store(config.data_root, environment_root=shared)
    geometry_path = tmp_path / "initial.xyz"
    geometry_path.write_text(XYZ, encoding="ascii")
    geometry = store.import_artifact(geometry_path, "initial_geometry")
    native = kind == "direct"
    code = NATIVE if native else (
        "from pathlib import Path\nimport json\nfrom orca_task_sdk import run_orca\n"
        f"Path('probe.inp').write_text({NATIVE!r})\n"
        "receipt=run_orca('probe.inp', dependencies=['initial.xyz'], request_key='probe')\n"
        "Path('receipt.json').write_text(json.dumps(receipt))\n"
        "assert receipt['operation_status']=='completed', receipt\n")
    source_path = tmp_path / ("source.inp" if native else "source.py")
    source_path.write_text(code, encoding="ascii")
    artifact = store.import_artifact(source_path, "native_input" if native else "task_code")
    artifacts = [artifact.id, geometry.id]
    consumer = None
    step = make_step(artifact, geometry, native=native)
    steps = [step]
    if native:
        parser_path = tmp_path / "consume.py"
        parser_path.write_text(
            "from pathlib import Path\nimport json\n"
            "assert 'ORCA TERMINATED NORMALLY' in Path('direct.out').read_text()\n"
            "Path('observations.json').write_text(json.dumps({'version':1,'observations':["
            "{'name':'artifacts','value':{'normal_termination_observed':True},"
            "'sources':[{'filename':'direct.out','location':'normal termination marker'}],"
            "'limitations':['Execution evidence only; no independent numerical check.']}]}))\n",
            encoding="ascii")
        parser = store.import_artifact(parser_path, "task_code")
        artifacts.append(parser.id)
        consumer = Step(id="consume", logical_id="consume", tool="python.execute",
            depends_on=[step.id], parameters={
                "code": {"artifact_id": parser.id, "sha256": parser.sha256, "language": "python"},
                "sdk_orca": False, "observation_names": ["artifacts"],
                "inputs": {"entries": [{"producer_step_id": step.id, "file_key": "task/stdout.out",
                    "target_path": "direct.out", "purpose": "completed direct native evidence"}]}})
        steps.append(consumer)
    request = Request(
        original_text="Developer workflow execution probe; not a formal task or model test",
        geometry_artifact_id=geometry.id, method="HF", basis="STO-3G",
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        goals=[Goal(id="raw", port="artifacts", quantity="execution evidence",
                    minimum_check_version="generic-observation-1",
                    evidence_contract_version="generic-observation-1",
                    verification="reported_with_limitations")])
    plan = Plan(request_id=request.id, steps=steps,
                goal_map={"raw": OutputBinding(step_id=steps[-1].id, port="artifacts")})
    run = store.create_run(
        request, plan,
        PermissionSnapshot(allowed_tools=["orca.run", "python.execute"],
                           scientific_execution=True, code_execution=True,
                           artifact_writes=True, artifact_ids=artifacts),
        BudgetLimits(orca_starts=1, extra_orca_starts=0, internal_work_units=1,
                     analysis_executions=1, code_retries=0, run_seconds=600,
                     model_calls=0, model_tokens=0),
        configuration=config.run_configuration())
    result, outcome = _science(store, config, run, step, {}, None, None)
    save_evidence(tmp_path, run, result, outcome)
    summary = {"run_id": run.id, "kind": kind, "outcome": outcome,
               "usage": run.usage.model_dump(mode="json"), "developer_authored": True,
               "autonomous_acceptance": False, "model_calls": run.usage.model_calls,
               "environment_root": str(store.environment_root)}
    (tmp_path / "probe-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    assert not run.model_records and run.usage.model_calls == 0
    assert store.environment_lease() is None
    assert store.artifact_path(geometry.id).read_bytes() == XYZ.encode()
    assert run.usage.orca_starts_reserved == 1
    assert result.observations["evidence_collection_complete"]
    if kind == "sdk_timeout":
        assert outcome["state"] == "timed_out", outcome
        assert len(run.internal_calls) == 1 and run.internal_calls[0].state == "timed_out"
        assert not run.internal_calls[0].actual_conditions.get("collection_error")
        assert run.internal_calls[0].artifact_ids
        return
    assert outcome["state"] == "completed", outcome
    assert run.usage.orca_starts_actual == 1
    manifests = list(store.path(run.attempts[0].directory).rglob("input-manifest.json"))
    manifest = next(json.loads(path.read_bytes()) for path in manifests
                    if "materialization" in json.loads(path.read_bytes()))
    assert manifest["resources"]["cores"] == 4 and manifest["resources"]["maxcore_mb"] == 192
    assert manifest["actual_conditions"]["electronic_state"] == "RHF"
    assert manifest["source_input_sha256"] != manifest["input_sha256"]
    outputs = list(store.path(run.attempts[0].directory).rglob("stdout.out"))
    assert any("ORCA TERMINATED NORMALLY" in path.read_text(errors="replace") for path in outputs)
    if consumer is not None:
        consumed, consumer_outcome = _science(store, config, run, consumer, {step.id: result}, None, None)
        consumer_evidence = tmp_path / "observation-consumption"
        consumer_evidence.mkdir()
        save_evidence(consumer_evidence, run, consumed, consumer_outcome)
        answer = assess(store, run, request, request.goals[0], consumed)
        summary.update(usage=run.usage.model_dump(mode="json"), direct_observation_consumption={
            "outcome": consumer_outcome, "result_id": consumed.id,
            "observation_records": [item.model_dump(mode="json") for item in consumed.observation_records],
            "assessment": answer, "extra_python_executions": 1, "extra_orca_starts": 0})
        (tmp_path / "probe-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        assert consumer_outcome["state"] == "completed", consumer_outcome
        assert answer["status"] == "passed", answer
        observation, = consumed.observation_records
        original_source, = observation.sources
        assert original_source.artifact_id == result.source["files"]["task/stdout.out"]["artifact_id"]
        assert original_source.actual_conditions["native_attempt_id"] == result.attempt_id
        assert observation.validation_status == "unverified"
        assert answer["scientific_qualification"] is False and not consumed.qualified_outputs
        assert run.usage.analysis_executions == 1 and run.usage.orca_starts_reserved == 1
        assert run.usage.orca_starts_actual == 1 and run.usage.model_calls == 0
        assert not run.internal_calls and store.environment_lease() is None
