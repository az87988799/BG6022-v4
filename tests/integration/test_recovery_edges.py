"""Recovery consumes stored facts; these tests never launch scientific programs."""

import json
import sys
from pathlib import Path

import pytest

from orca_agent import runner
from orca_agent.backends import local
from orca_agent.config import Config
from orca_agent.models import (
    CalculationParameters,
    Check,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    QualifiedOutput,
    Request,
    Result,
    Step,
)
from orca_agent.store import Store, StoreError, atomic_write, sha256_file
from orca_agent.tools import calculation
from orca_agent.versions import CURRENT_CHECK_VERSION


@pytest.fixture
def stored_attempt(tmp_path, monkeypatch):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    source = tmp_path / "water.xyz"
    source.write_text("3\nSynthetic fixture\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n")
    geometry = store.import_artifact(source, "initial_geometry")
    request = Request(geometry_artifact_id=geometry.id, goals=[Goal(id="e", port="energy", minimum_check_version=CURRENT_CHECK_VERSION)])
    step = Step(id="sp", logical_id="energy", tool="orca.sp",
                parameters=CalculationParameters(cores=1), geometry=InputRef(artifact_id=geometry.id))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"e": OutputBinding(step_id=step.id, port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[geometry.id]))
    attempt = store.reserve_attempt(run, step, geometry.id)
    attempt.execution_handle = {"pid": 424242, "create_time": 1.0, "job_name": "Local\\TestFixture",
                                "launch_id": "synthetic_fixture"}
    store.update_lease_handle(run.id, attempt.id, attempt.execution_handle)
    store.save_run(run)
    config = Config(data_root=store.root, orca_path=Path(sys.executable).resolve())
    monkeypatch.setattr(local, "run_managed", lambda *args, **kwargs: pytest.fail("unexpected launch"))
    return store, config, run, step, attempt


def test_unknown_nonzero_cost_is_reconciled_once_with_durable_lower_bounds(stored_attempt, monkeypatch):
    store, config, run, step, attempt = stored_attempt
    receipt = {"state": "unknown", "handle": attempt.execution_handle,
               "resource_usage": {"wall_seconds": 7.5, "user_cpu_seconds": 2.0,
                                  "kernel_cpu_seconds": 0.5},
               "reason": "synthetic termination confirmation unavailable"}
    path = store.path(f"{attempt.directory}/execution.json")
    atomic_write(path, json.dumps(receipt).encode(), immutable=True)
    original_receipt = path.read_bytes()
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="unknown", source={"synthetic_fixture": True})
    store.save_result(result)
    runner._settle(store, run, attempt, result, receipt)
    run.state = "unknown"
    store.save_run(run)
    assert run.usage.orca_starts_actual == 1
    assert run.usage.elapsed_seconds == 7.5
    assert run.usage.cpu_seconds == 2.5
    assert store.environment_lease() is not None
    monkeypatch.setattr(local, "reconcile", lambda handle: {
        "state": "terminated", "active_processes": 0,
        "reason": "synthetic recorded process and job absent",
    })
    recovered = runner.execute(store, config, run.id, resume=True)
    assert recovered.state == "failed"
    assert recovered.attempts[0].finished_at is not None
    assert recovered.attempts[0].state == "failed"
    assert recovered.usage.orca_starts_actual == recovered.usage.orca_starts_reserved == 1
    assert recovered.usage.elapsed_seconds == 7.5
    assert recovered.usage.cpu_seconds == 2.5
    assert recovered.usage.resource_usage_complete is False
    assert store.environment_lease() is None
    proofs = list(store.path(attempt.directory).glob("recon*.json"))
    assert len(proofs) == 1
    proof_bytes = proofs[0].read_bytes()
    proof = json.loads(proof_bytes)
    assert proof["attempt_id"] == attempt.id
    assert proof["reconciliation"]["state"] == "terminated"
    assert proof["cost"]["elapsed_seconds_lower_bound"] == 7.5
    assert proof["cost"]["cpu_seconds_lower_bound"] == 2.5
    assert "unknown" in proof["cost"]["additional_usage"]
    diagnostic = next(item for item in recovered.diagnostics if item["category"] == "termination_reconciled")
    assert store.artifact_path(diagnostic["receipt_artifact_id"]).read_bytes() == proof_bytes
    assert path.read_bytes() == original_receipt
    # Repeated resume cannot manufacture a new launch, count costs again or erase the proof.
    repeated = runner.execute(store, config, run.id, resume=True)
    assert repeated.state == "failed"
    assert len(repeated.attempts) == repeated.usage.orca_starts_actual == 1
    assert repeated.usage.elapsed_seconds == 7.5
    assert repeated.usage.cpu_seconds == 2.5
    assert repeated.usage.resource_usage_complete is False
    assert list(store.path(attempt.directory).glob("recon*.json")) == proofs
    assert proofs[0].read_bytes() == proof_bytes


@pytest.mark.parametrize("mutation", ["changed", "missing"])
def test_completed_run_resume_revalidates_archived_artifacts(stored_attempt, tmp_path, mutation):
    store, config, run, step, attempt = stored_attempt
    output = tmp_path / "stdout.out"
    output.write_bytes(b"Synthetic evidence only.\n")
    artifact = store.import_artifact(output, "synthetic_test_evidence", run_id=run.id,
                                     attempt_id=attempt.id)
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="completed", artifact_ids=[artifact.id], qualified_outputs={
                        "energy": QualifiedOutput(value=-1.0, unit="Eh", checks=[
                            Check(name="synthetic_fixture", status="passed", rule_version=CURRENT_CHECK_VERSION,
                                  detail="Synthetic fixture, not scientific evidence"),
                        ]),
                    })
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         elapsed_seconds=0.5, cpu_seconds=0.1, termination_confirmed=True)
    run.state = "completed"
    run.goal_status["e"] = "satisfied"
    run.delivery_status = "complete"
    store.save_run(run)
    archive = store.artifact_path(artifact.id)
    if mutation == "changed":
        archive.write_bytes(b"Tampered evidence.\n")
    else:
        archive.unlink()
    with pytest.raises((StoreError, FileNotFoundError)):
        runner.execute(store, config, run.id, resume=True)
    retained = store.load_run(run.id)
    assert len(retained.attempts) == retained.usage.orca_starts_actual == 1
    assert retained.usage.elapsed_seconds == 0.5
    assert retained.attempts[0].result_id == result.id
    assert store.environment_lease() is None


@pytest.mark.parametrize("result_already_saved", [False, True])
def test_unknown_with_postprocess_violation_keeps_lease_and_recovers_cost_fact(
    stored_attempt, monkeypatch, result_already_saved
):
    store, config, run, step, attempt = stored_attempt
    environment_path = store.path(f"runs/{run.id}/environment.json")
    atomic_write(environment_path, json.dumps({"orca": {
        "version": "6.1.1", "path": str(config.orca_path), "sha256": sha256_file(config.orca_path),
    }}).encode(), immutable=True)

    def prepare(workdir, *args):
        path = workdir / "synthetic.inp"
        path.write_bytes(b"Synthetic fixture, no science.\n")
        return {"input_path": str(path)}

    def unknown_backend(*args, **kwargs):
        handle = dict(attempt.execution_handle)
        kwargs["on_started"](handle)
        kwargs["fault"]("after_resumed")
        return {"state": "unknown", "handle": handle, "exit_code": None,
                "reason": "synthetic process tree termination unconfirmed",
                "resource_usage": {"wall_seconds": 7.5, "user_cpu_seconds": 2.0,
                                   "kernel_cpu_seconds": 0.5, "observed_processes": [
                                       {"pid": 99999, "executable": "E:/fixture/orca_2json.exe"},
                                   ]}}

    original_import = store.import_artifact
    monkeypatch.setattr(calculation, "prepare_input", prepare)
    monkeypatch.setattr(local, "run_managed", unknown_backend)
    monkeypatch.setattr(calculation, "read_outputs", lambda *args, **kwargs: pytest.fail("unknown tree parsed"))
    monkeypatch.setattr(store, "import_artifact", lambda *args, **kwargs: pytest.fail("unknown tree archived"))
    result, outcome = calculation.execute_calculation(store, run, step, attempt, config)
    assert outcome["state"] == result.operation_status == "unknown"
    assert outcome["reason"] == "synthetic process tree termination unconfirmed"
    assert outcome["postprocess_starts_detected"] == 1
    assert any(item["category"] == "postprocess_budget_exceeded" for item in result.diagnostics)
    assert result.artifact_ids == []
    assert not result.qualified_outputs
    assert store.environment_lease()["attempt_id"] == attempt.id
    receipt_path = store.path(f"{attempt.directory}/execution.json")
    original_receipt = receipt_path.read_bytes()
    if result_already_saved:
        store.save_result(result)
        runner._settle(store, run, attempt, result, outcome)
        assert run.usage.postprocess_starts == run.usage.orca_starts_actual == 1
    run.state = "unknown"
    run.usage.resource_usage_complete = False
    store.save_run(run)
    monkeypatch.setattr(store, "import_artifact", original_import)
    monkeypatch.setattr(local, "run_managed", lambda *args, **kwargs: pytest.fail("recovery launched science"))
    monkeypatch.setattr(local, "reconcile", lambda handle: {
        "state": "terminated", "active_processes": 0, "reason": "synthetic recorded tree absent",
    })
    monkeypatch.setattr(calculation, "read_outputs", lambda *args, **kwargs: {
        "checks": {}, "qualified_outputs": {}, "observations": {"synthetic_fixture": True},
        "diagnostics": [],
    })
    recovered = runner.execute(store, config, run.id, resume=True)
    assert recovered.state == "failed"
    assert recovered.usage.orca_starts_actual == recovered.usage.postprocess_starts == 1
    assert recovered.budget.postprocess_starts == 0
    assert recovered.usage.elapsed_seconds == 7.5
    assert recovered.usage.cpu_seconds == 2.5
    assert recovered.usage.resource_usage_complete is False
    assert store.environment_lease() is None
    assert receipt_path.read_bytes() == original_receipt
    settled = store.load_result(run.id, recovered.attempts[0].result_id)
    assert not settled.qualified_outputs
    assert any(item["category"] == "postprocess_budget_exceeded" for item in settled.diagnostics)
    repeated = runner.execute(store, config, run.id, resume=True)
    assert repeated.usage.orca_starts_actual == repeated.usage.postprocess_starts == 1


def test_crash_after_reconciled_attempt_saved_before_release_can_release_on_next_resume(
    stored_attempt, monkeypatch
):
    store, config, run, step, attempt = stored_attempt
    outcome = {"state": "unknown", "handle": attempt.execution_handle,
               "resource_usage": {"wall_seconds": 2, "user_cpu_seconds": 1},
               "reason": "synthetic original unknown outcome"}
    path = store.path(f"{attempt.directory}/execution.json")
    atomic_write(path, json.dumps(outcome).encode(), immutable=True)
    original_receipt = path.read_bytes()
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="unknown", source={"synthetic_fixture": True})
    store.save_result(result)
    original_result = store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes()
    runner._settle(store, run, attempt, result, outcome)
    run.state = "unknown"
    store.save_run(run)
    reconciliations = []

    def reconcile(handle):
        reconciliations.append(dict(handle))
        return {"state": "terminated", "reason": "synthetic identity and tree termination confirmed"}

    monkeypatch.setattr(local, "reconcile", reconcile)
    release = store.release_environment
    monkeypatch.setattr(store, "release_environment", lambda *args, **kwargs: (
        (_ for _ in ()).throw(RuntimeError("injected crash before durable lease release"))))
    with pytest.raises(RuntimeError, match="injected crash"):
        runner.execute(store, config, run.id, resume=True)
    intermediate = store.load_run(run.id)
    assert intermediate.attempts[0].finished_at is not None
    assert intermediate.attempts[0].state == "failed"
    assert store.environment_lease()["attempt_id"] == attempt.id
    assert len(reconciliations) == 1
    monkeypatch.setattr(store, "release_environment", release)
    recovered = runner.execute(store, config, run.id, resume=True)
    assert recovered.state == "failed"
    assert len(reconciliations) == 2  # Old unknown receipt did not bypass fresh proof.
    assert store.environment_lease() is None
    assert recovered.usage.orca_starts_actual == 1
    assert recovered.usage.elapsed_seconds == 2
    assert recovered.usage.cpu_seconds == 1
    assert path.read_bytes() == original_receipt
    assert store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes() == original_result
    assert len([item for item in recovered.diagnostics if item["category"] == "termination_reconciled"]) == 1
    assert len(recovered.result_ids) == 2  # Reattach the durable supplement, never duplicate it.

@pytest.mark.parametrize("saved, finished", [(False, False), (True, False), (True, True)])
def test_unknown_termination_archives_evidence_and_preserves_previous_result(
    stored_attempt, monkeypatch, saved, finished
):
    store, config, run, step, attempt = stored_attempt
    directory = store.path(attempt.directory)
    (directory / "stdout.out").write_text("Raw synthetic stdout from stopped attempt")
    (directory / "stderr.txt").write_text("Raw synthetic stderr")
    receipt = {"state": "unknown", "handle": attempt.execution_handle,
               "resource_usage": {"wall_seconds": 3, "user_cpu_seconds": 1}}
    atomic_write(directory / "execution.json", json.dumps(receipt).encode(), immutable=True)
    original_receipt = (directory / "execution.json").read_bytes()
    old = None
    if saved:
        old = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                     operation_status="unknown", source={"execution": receipt})
        store.save_result(old)
        old_bytes = store.path(f"runs/{run.id}/results/{old.id}.json").read_bytes()
        runner._settle(store, run, attempt, old, receipt)
        if finished:
            store.finish_attempt(run, attempt.id, state="failed", result_id=old.id,
                                 elapsed_seconds=3, cpu_seconds=1, termination_confirmed=True)
    monkeypatch.setattr(local, "reconcile", lambda handle: {
        "state": "terminated", "reason": "synthetic confirmed absent tree"})
    monkeypatch.setattr(calculation, "read_outputs", lambda *args, **kwargs: {
        "checks": {}, "qualified_outputs": {}, "observations": {"synthetic_fixture": True},
        "diagnostics": []})
    recovered = runner.execute(store, config, run.id, resume=True)
    current = store.load_result(run.id, recovered.attempts[0].result_id)
    assert current.operation_status == "failed"
    assert current.artifact_ids
    assert not current.qualified_outputs
    archived = current.source["files"]["stdout.out"]
    from orca_agent.tools.evidence import inspect_artifact

    observed = inspect_artifact(store, archived["artifact_id"])
    assert observed["lines"][0]["text"] == "Raw synthetic stdout from stopped attempt"
    assert store.artifact_path(archived["artifact_id"]).read_text() == (
        "Raw synthetic stdout from stopped attempt")
    if old:
        assert current.id != old.id
        assert current.supersedes_result_id == old.id
        assert recovered.result_ids == [old.id, current.id]
        assert store.path(f"runs/{run.id}/results/{old.id}.json").read_bytes() == old_bytes
    assert (directory / "execution.json").read_bytes() == original_receipt
    assert store.environment_lease() is None
    for _ in range(2):
        repeated = runner.execute(store, config, run.id, resume=True)
        assert repeated.result_ids == recovered.result_ids
        assert repeated.usage == recovered.usage
        assert repeated.attempts[0].result_id == current.id


def test_recovery_result_saved_before_run_reference_is_reattached(stored_attempt, monkeypatch):
    store, config, run, step, attempt = stored_attempt
    directory = store.path(attempt.directory)
    (directory / "stdout.out").write_text("Synthetic stopped evidence")
    outcome = {"state": "unknown", "handle": attempt.execution_handle, "resource_usage": {"wall_seconds": 2}}
    atomic_write(directory / "execution.json", json.dumps(outcome).encode(), immutable=True)
    old = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="unknown")
    store.save_result(old)
    runner._settle(store, run, attempt, old, outcome)
    monkeypatch.setattr(local, "reconcile", lambda handle: {"state": "terminated", "reason": "synthetic stopped"})
    monkeypatch.setattr(calculation, "read_outputs", lambda *args, **kwargs: {
        "checks": {}, "qualified_outputs": {}, "observations": {}, "diagnostics": []})
    original_save = store.save_result

    def save_then_crash(result):
        original_save(result)
        raise RuntimeError("crash after recovery Result publication")

    monkeypatch.setattr(store, "save_result", save_then_crash)
    with pytest.raises(RuntimeError, match="crash after recovery"):
        runner.execute(store, config, run.id, resume=True)
    intermediate = store.load_run(run.id)
    assert intermediate.attempts[0].result_id == old.id
    assert intermediate.result_ids == [old.id]
    paths = list(store.path(f"runs/{run.id}/results").glob("*.json"))
    assert len(paths) == 2
    before = {path.name: path.read_bytes() for path in paths}
    monkeypatch.setattr(store, "save_result", original_save)
    monkeypatch.setattr(calculation, "collect_result", lambda *args: pytest.fail("duplicate recovery collection"))
    for _ in range(2):
        recovered = runner.execute(store, config, run.id, resume=True)
        assert recovered.state == "failed"
        assert len(recovered.result_ids) == 2
        assert recovered.attempts[0].result_id != old.id
        assert recovered.usage.orca_starts_actual == 1
        assert recovered.usage.elapsed_seconds == 2
        assert store.environment_lease() is None
    assert {path.name: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("invalid", ["unrelated", "branch", "cycle", "cross_attempt", "wrong_step"])
def test_recovery_rejects_ambiguous_or_invalid_result_chains(stored_attempt, monkeypatch, invalid):
    store, config, run, step, attempt = stored_attempt
    first = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="unknown")
    second = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="failed",
                    source={"execution": {"state": "failed", "reconciliation": {"state": "terminated"}}})
    third = None
    if invalid == "branch":
        second.supersedes_result_id = first.id
        third = second.model_copy(update={"id": "result_third"})
    elif invalid == "cycle":
        first.supersedes_result_id = second.id
        second.supersedes_result_id = first.id
    elif invalid == "cross_attempt":
        first.attempt_id = "attempt_other"
        second.supersedes_result_id = first.id
    elif invalid == "wrong_step":
        second.step_id = "different_step"
    # Corrupted records bypass the writer deliberately; recovery must validate persisted data.
    for item in [first, second, *([third] if third else [])]:
        atomic_write(store.path(f"runs/{run.id}/results/{item.id}.json"),
                     item.model_dump_json().encode(), immutable=True)
    monkeypatch.setattr(local, "reconcile", lambda handle: pytest.fail("invalid chain must fail before reconciliation"))
    with pytest.raises(StoreError):
        runner.execute(store, config, run.id, resume=True)
    assert store.environment_lease()["attempt_id"] == attempt.id
    assert store.load_run(run.id).usage.orca_starts_actual == 0


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_recovery_missing_or_corrupt_output_records_limits_without_success(stored_attempt, monkeypatch, failure):
    store, config, run, step, attempt = stored_attempt
    directory = store.path(attempt.directory)
    if failure == "corrupt":
        (directory / "job.property.json").write_text("not valid JSON")
    atomic_write(directory / "execution.json", json.dumps({
        "state": "unknown", "handle": attempt.execution_handle, "resource_usage": {},
    }).encode(), immutable=True)
    monkeypatch.setattr(local, "reconcile", lambda handle: {"state": "terminated", "reason": "synthetic stopped"})
    recovered = runner.execute(store, config, run.id, resume=True)
    result = store.load_result(run.id, recovered.attempts[0].result_id)
    assert result.operation_status == "failed"
    assert result.artifact_ids
    assert result.diagnostics
    assert not result.qualified_outputs
    assert recovered.delivery_status != "complete"
    assert store.environment_lease() is None


def test_old_rule_unknown_result_can_be_collected_without_execution(stored_attempt, monkeypatch):
    store, config, run, step, attempt = stored_attempt
    path = store.path(f"runs/{run.id}/request-revisions/1.json")
    request = json.loads(path.read_text())
    request["goals"][0]["minimum_check_version"] = "orca-hf-1"
    path.write_text(json.dumps(request))  # Synthetic persisted historical record.
    original = path.read_bytes()
    (store.path(attempt.directory) / "stdout.out").write_text("Synthetic historical stdout")
    old = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id, operation_status="unknown")
    store.save_result(old)
    outcome = {"state": "unknown", "handle": attempt.execution_handle, "resource_usage": {}}
    atomic_write(store.path(f"{attempt.directory}/execution.json"), json.dumps(outcome).encode(), immutable=True)
    runner._settle(store, run, attempt, old, outcome)
    monkeypatch.setattr(local, "reconcile", lambda handle: {"state": "terminated", "reason": "synthetic stopped"})
    recovered = runner.execute(store, config, run.id, resume=True)
    result = store.load_result(run.id, recovered.attempts[0].result_id)
    assert result.id != old.id
    assert result.supersedes_result_id == old.id
    assert result.artifact_ids
    assert store.environment_lease() is None
    assert path.read_bytes() == original
    assert recovered.usage.orca_starts_reserved == recovered.usage.orca_starts_actual == 1


def test_reconciled_termination_does_not_qualify_even_successful_parser_observations(
    stored_attempt, monkeypatch
):
    store, config, run, step, attempt = stored_attempt
    directory = store.path(attempt.directory)
    (directory / "stdout.out").write_text("Synthetic success-looking text without durable exit status")
    monkeypatch.setattr(local, "reconcile", lambda handle: {"state": "terminated", "reason": "synthetic stopped"})
    monkeypatch.setattr(calculation, "read_outputs", lambda *args, **kwargs: {
        "checks": {"energy": [{"name": "synthetic_fixture", "status": "passed", "rule_version": CURRENT_CHECK_VERSION}]},
        "qualified_outputs": {"energy": {"value": -1.0, "unit": "Eh", "source": {"synthetic_fixture": True}}},
        "observations": {"synthetic_fixture": True, "energy_eh": -1.0}, "diagnostics": []})
    recovered = runner.execute(store, config, run.id, resume=True)
    result = store.load_result(run.id, recovered.attempts[0].result_id)
    assert result.operation_status == "failed"
    assert result.observations["energy_eh"] == -1.0
    assert result.checks["energy"][0].status == "passed"
    assert not result.qualified_outputs
    assert recovered.goal_status["e"] == "insufficient_evidence"
    assert store.environment_lease() is None
