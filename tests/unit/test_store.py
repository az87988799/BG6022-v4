import json
import os
import subprocess
import sys

import pytest
from filelock import Timeout

from orca_agent.models import (
    BudgetLimits,
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
from orca_agent.store import (
    BudgetExceeded,
    EnvironmentBusy,
    Store,
    StoreError,
    atomic_write,
    controlled_path,
)
from orca_agent.versions import CURRENT_CHECK_VERSION

WATER = "3\nWater coordinates, angstrom\nO 0 0 0\nH 0 0.757 0.587\nH 0 -0.757 0.587\n"


@pytest.fixture
def setup_run(tmp_path):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    geometry = tmp_path / "water.xyz"
    geometry.write_text(WATER)
    artifact = store.import_artifact(geometry, "initial_geometry")
    request = Request(geometry_artifact_id=artifact.id, goals=[Goal(id="e", port="energy", minimum_check_version=CURRENT_CHECK_VERSION)])
    step = Step(id="sp", logical_id="energy", tool="orca.sp", geometry=InputRef(artifact_id=artifact.id))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"e": OutputBinding(step_id=step.id, port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[artifact.id],
    ))
    return store, run, step, artifact


@pytest.mark.parametrize("relative", [
    "../outside", "/absolute", "E:/outside", "E:relative", "x/../../outside",
    "x\\..\\outside", "x/stream:ads", "x//y", "x/./y", "NUL", "x/COM1.log",
    "x/trailing.", "x/trailing ",
])
def test_controlled_paths_reject_escapes_and_windows_devices(tmp_path, relative):
    with pytest.raises(StoreError):
        controlled_path(tmp_path, relative)


def test_artifact_snapshot_is_not_overwritten_and_hash_is_checked(setup_run, tmp_path):
    store, _, _, artifact = setup_run
    original = tmp_path / "water.xyz"
    original.write_text("changed original")
    snapshot = store.artifact_path(artifact.id)
    assert snapshot.read_text() == WATER
    second = store.import_artifact(snapshot, "geometry")
    assert second.id != artifact.id
    assert second.sha256 == artifact.sha256
    snapshot.write_text("corrupted archive")
    with pytest.raises(StoreError, match="hash changed"):
        store.artifact_path(artifact.id)


def test_initial_and_immutable_snapshots_survive_reservation(setup_run):
    store, run, step, artifact = setup_run
    before = store.artifact_path(artifact.id).read_bytes()
    attempt = store.reserve_attempt(run, step, artifact.id)
    assert store.path(attempt.directory).name == "attempt-001"
    assert store.path(f"{attempt.directory}/intent.json").is_file()
    assert store.artifact_path(artifact.id).read_bytes() == before
    assert store.load_run(run.id).usage.orca_starts_reserved == 1
    assert store.environment_lease()["attempt_id"] == attempt.id
    with pytest.raises(StoreError, match="immutable"):
        store._write_json(f"{attempt.directory}/intent.json", {}, immutable=True)


def test_unknown_lease_survives_new_store_and_other_data_root(setup_run, tmp_path):
    store, run, step, artifact = setup_run
    attempt = store.reserve_attempt(run, step, artifact.id)
    reopened = Store(store.root, environment_root=store.environment_root)
    assert reopened.environment_lease()["attempt_id"] == attempt.id
    other = Store(tmp_path / "other_data", environment_root=store.environment_root)
    other_geometry = other.import_artifact(store.artifact_path(artifact.id), "initial_geometry")
    request = Request(geometry_artifact_id=other_geometry.id, goals=[Goal(id="e", port="energy", minimum_check_version=CURRENT_CHECK_VERSION)])
    other_step = step.model_copy(update={"geometry": InputRef(artifact_id=other_geometry.id)})
    plan = Plan(request_id=request.id, steps=[other_step],
                goal_map={"e": OutputBinding(step_id=step.id, port="energy")})
    other_run = other.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[other_geometry.id]))
    with pytest.raises(EnvironmentBusy, match="quota"):
        other.reserve_attempt(other_run, other_step, other_geometry.id)
    with pytest.raises(EnvironmentBusy, match="unconfirmed"):
        store.release_environment(run.id, attempt.id, termination_confirmed=False)
    assert other.load_run(other_run.id).usage.orca_starts_reserved == 0


def test_unknown_reconciliation_does_not_double_charge_actual_usage(setup_run):
    store, run, step, artifact = setup_run
    attempt = store.reserve_attempt(run, step, artifact.id)
    store.finish_attempt(run, attempt.id, state="unknown", started=True,
                         elapsed_seconds=2, cpu_seconds=1, termination_confirmed=False)
    assert store.environment_lease()
    store.finish_attempt(run, attempt.id, state="cancelled", started=True,
                         elapsed_seconds=3, cpu_seconds=1.5, termination_confirmed=True)
    assert run.usage.orca_starts_actual == 1
    assert run.usage.elapsed_seconds == 3
    assert run.usage.cpu_seconds == 1.5
    assert store.environment_lease() is None


def test_fixed_plan_and_cumulative_budget_cannot_be_relabelled(setup_run):
    store, run, step, artifact = setup_run
    renamed = step.model_copy(update={"id": "new", "logical_id": "new"})
    with pytest.raises(StoreError, match="immutable plan"):
        store.reserve_attempt(run, renamed, artifact.id)
    attempt = store.reserve_attempt(run, step, artifact.id)
    store.finish_attempt(run, attempt.id, state="not_started", started=False,
                         termination_confirmed=True)
    run.usage.orca_starts_reserved = 0
    with pytest.raises(StoreError, match="cannot decrease"):
        store.save_run(run)
    reloaded = store.load_run(run.id)
    reloaded.plan_version = 2
    with pytest.raises(StoreError, match="fixed run field"):
        store.save_run(reloaded)


def test_attempt_limits_persist_across_restarts(setup_run):
    store, run, step, artifact = setup_run
    paths = []
    for _ in range(3):
        attempt = store.reserve_attempt(run, step, artifact.id)
        paths.append(attempt.directory)
        store.finish_attempt(run, attempt.id, state="not_started", started=False,
                             termination_confirmed=True)
        store = Store(store.root, environment_root=store.environment_root)
        run = store.load_run(run.id)
    assert len(set(paths)) == 3
    with pytest.raises(BudgetExceeded, match="attempt budget"):
        store.reserve_attempt(run, step, artifact.id)
    assert run.usage.orca_starts_reserved == 3


def test_identical_failed_input_cannot_be_automatically_retried(setup_run):
    store, run, step, artifact = setup_run
    attempt = store.reserve_attempt(run, step, artifact.id)
    store.finish_attempt(run, attempt.id, state="failed", termination_confirmed=True)
    with pytest.raises(StoreError, match="unchanged failed input"):
        store.reserve_attempt(run, step, artifact.id)


def test_signal_and_stale_permission_are_rechecked_before_reservation(setup_run):
    store, run, step, artifact = setup_run
    store.signal(run.id, "pause")
    with pytest.raises(StoreError, match="pause/cancel"):
        store.reserve_attempt(run, step, artifact.id)
    store.signal(run.id, None)
    # Model a reconciled explicit resume with no outstanding user messages.
    run.control_generation = store.read_control(run.id)["generation"]
    store.save_run(run)
    stale = run.model_copy(deep=True)
    run.state = "paused"
    store.save_run(run)
    with pytest.raises(StoreError, match="stale run"):
        store.reserve_attempt(stale, step, artifact.id)
    run.state = "ready"
    store.save_run(run)
    store.path(f"runs/{run.id}/permission.json").write_text(json.dumps({
        **run.permission.model_dump(), "scientific_execution": False,
    }))
    with pytest.raises(StoreError, match="not authorized"):
        store.reserve_attempt(run, step, artifact.id)
    assert store.environment_lease() is None


def test_only_one_coordinator_and_control_signal_is_independent(setup_run):
    store, run, _, _ = setup_run
    competitor = Store(store.root, environment_root=store.environment_root)
    with store.run_lock(run.id):
        with pytest.raises(Timeout), competitor.run_lock(run.id):
            pytest.fail("second coordinator acquired the same run")
        competitor.signal(run.id, "cancel")
        assert store.read_signal(run.id) == "cancel"


@pytest.mark.backend
def test_coordinator_lock_is_cross_process(setup_run):
    store, run, _, _ = setup_run
    code = (
        "import sys; from filelock import FileLock, Timeout; "
        "lock=FileLock(sys.argv[1], timeout=0);\n"
        "try: lock.acquire()\n"
        "except Timeout: sys.exit(17)\n"
        "else: lock.release(); sys.exit(0)\n"
    )
    with store.run_lock(run.id):
        completed = subprocess.run(
            [sys.executable, "-c", code, str(store.path(f"runs/{run.id}/coordinator.lock"))],
            check=False, timeout=10, capture_output=True,
        )
    assert completed.returncode == 17, completed.stderr


def test_result_saved_before_run_reference_is_discoverable_orphan(setup_run):
    store, run, step, artifact = setup_run
    attempt = store.reserve_attempt(run, step, artifact.id)
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="failed", diagnostics=[{"category": "fixture"}])
    store.save_result(result)
    assert any("orphan result" in issue for issue in store.integrity_issues(run.id))
    assert store.load_run(run.id).usage.orca_starts_reserved == 1
    assert store.environment_lease() is not None
    with pytest.raises(StoreError, match="immutable"):
        result.observations["changed"] = True
        store.save_result(result)


def test_result_cannot_claim_another_attempt_artifact(setup_run):
    store, run, step, artifact = setup_run
    attempt = store.reserve_attempt(run, step, artifact.id)
    result = Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                    operation_status="completed", artifact_ids=[artifact.id])
    with pytest.raises(StoreError, match="provenance mismatch"):
        store.save_result(result)


@pytest.mark.parametrize("rule", [None, "orca-hf-1", CURRENT_CHECK_VERSION])
def test_derived_geometry_requires_concrete_qualified_producer(setup_run, tmp_path, rule):
    store, _, _, artifact = setup_run
    request = Request(geometry_artifact_id=artifact.id, goals=[Goal(id="e", port="energy", minimum_check_version=CURRENT_CHECK_VERSION)])
    opt = Step(id="opt", logical_id="optimization", tool="orca.opt",
               geometry=InputRef(artifact_id=artifact.id))
    sp = Step(id="sp", logical_id="energy", tool="orca.sp", depends_on=["opt"],
              geometry=InputRef(producer_step_id="opt", port="optimized_geometry"))
    plan = Plan(request_id=request.id, steps=[opt, sp],
                goal_map={"e": OutputBinding(step_id="sp", port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[artifact.id]))
    attempt = store.reserve_attempt(run, opt, artifact.id)
    final = tmp_path / "optimized.xyz"
    final.write_text(WATER)
    geometry = store.import_artifact(final, "optimized_geometry", run_id=run.id, attempt_id=attempt.id)
    result = Result(run_id=run.id, step_id=opt.id, attempt_id=attempt.id,
                    operation_status="completed", artifact_ids=[geometry.id],
                    qualified_outputs={"optimized_geometry": QualifiedOutput(
                        artifact_id=geometry.id, checks=[Check(
                            name="optimization", status="passed", rule_version=rule)])} if rule else {})
    store.save_result(result)
    store.finish_attempt(run, attempt.id, state="completed", result_id=result.id,
                         termination_confirmed=True)
    if rule is None:
        with pytest.raises(StoreError, match="qualified producer"):
            store.reserve_attempt(run, sp, geometry.id)
        return
    if rule != CURRENT_CHECK_VERSION:
        with pytest.raises(StoreError, match="rule|version"):
            store.reserve_attempt(run, sp, geometry.id)
        assert len(run.attempts) == 1
        return
    # A matching rule label alone is not the complete checked structure or its
    # frozen Attempt/input/manifest provenance. Full Opt -> SP positive coverage
    # lives in test_current_applicability with the production parser/checker.
    with pytest.raises((StoreError, ValueError), match="source_|qualified_"):
        store.reserve_attempt(run, sp, geometry.id)
    assert len(run.attempts) == 1


def test_default_environment_is_independent_of_store_root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    first = Store(tmp_path / "one")
    second = Store(tmp_path / "two")
    assert first.environment_root == second.environment_root


def test_symlink_cannot_escape_controlled_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "data"
    root.mkdir()
    try:
        os.symlink(outside, root / "escape", target_is_directory=True)
    except OSError:
        pytest.skip("creating test symlinks requires Windows developer mode/privilege")
    with pytest.raises(StoreError, match="links"):
        controlled_path(root, "escape/file.xyz")


def test_budget_and_permission_cannot_be_enlarged_after_creation(setup_run):
    store, run, _, _ = setup_run
    changed = run.model_copy(deep=True)
    changed.budget = BudgetLimits(orca_starts=1)
    with pytest.raises(StoreError, match="fixed run field"):
        store.save_run(changed)
    changed = run.model_copy(deep=True)
    changed.permission.version += 1
    with pytest.raises(StoreError, match="fixed run field"):
        store.save_run(changed)


@pytest.mark.backend
@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_is_rejected_without_python_is_junction(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "data"
    root.mkdir()
    junction = root / "junction"
    made = subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True, timeout=10, check=False,
    )
    assert made.returncode == 0, made.stderr
    with pytest.raises(StoreError, match="links"):
        controlled_path(root, "junction/secret.xyz")


def test_create_run_revalidates_copied_budget_models(setup_run):
    store, run, _, _ = setup_run
    changed = run.budget.model_copy(update={"orca_starts": 100})
    with pytest.raises(ValueError):
        store.create_run(store.load_request(run), store.load_plan(run), run.permission, changed)


def test_actual_postprocessing_overrun_is_a_persistent_fact_not_a_new_allowance(setup_run):
    store, run, _, _ = setup_run
    assert run.budget.postprocess_starts == 0
    run.usage.postprocess_starts = 1
    store.save_run(run)
    persisted = store.load_run(run.id)
    assert persisted.budget.postprocess_starts == 0
    assert persisted.usage.postprocess_starts == 1
    persisted.usage.postprocess_starts = 0
    with pytest.raises(StoreError, match="cannot decrease"):
        store.save_run(persisted)


@pytest.mark.parametrize("immutable", [False, True])
def test_atomic_write_uses_short_temporary_name_near_windows_path_limit(tmp_path, immutable):
    filename = "evidence-" + "x" * 60 + ".json"
    component_length = 245 - len(str(tmp_path)) - len(filename) - 2
    assert 1 <= component_length <= 255
    target = tmp_path / ("d" * component_length) / filename
    assert len(str(target)) == 245
    old_temporary = target.parent / f".{target.name}.{'f' * 32}.tmp"
    assert len(str(old_temporary)) > 260
    atomic_write(target, b'{"complete": true}\n', immutable=immutable)
    assert target.read_bytes() == b'{"complete": true}\n'
    assert sorted(path.name for path in target.parent.iterdir()) == [filename]


def test_unknown_historical_cost_cannot_be_silently_relabelled_complete(setup_run):
    store, run, _, _ = setup_run
    run.usage.resource_usage_complete = False
    store.save_run(run)
    persisted = store.load_run(run.id)
    assert not persisted.usage.resource_usage_complete
    persisted.usage.resource_usage_complete = True
    with pytest.raises(StoreError, match="silently declared complete"):
        store.save_run(persisted)
