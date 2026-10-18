"""Offline reservation negatives; no process, model, or ORCA is launched."""

import json

import pytest

from orca_agent.models import (
    BudgetLimits,
    CodeRef,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import BudgetExceeded, Store, StoreError


def case(tmp_path, monkeypatch, *, additional=False, extra_limit=3, legacy=False):
    monkeypatch.setattr("orca_agent.store.require_execution_available", lambda _: None)
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    steps, artifacts = {}, []
    for kind, count in (("direct", 2), ("sdk", 3)):
        for index in range(count):
            identity = f"{kind}_{index}"
            source = tmp_path / f"{identity}.txt"
            source.write_text(f"# inert reservation fixture {identity}\n", encoding="utf-8")
            artifact = store.import_artifact(source, "code")
            artifacts.append(artifact.id)
            code = CodeRef(artifact_id=artifact.id, sha256=artifact.sha256,
                           language="orca" if kind == "direct" else "python")
            steps[identity] = Step(id=identity, logical_id=identity,
                tool="orca.run" if kind == "direct" else "python.execute",
                parameters={"input" if kind == "direct" else "code": code})
    geometry_id = None
    if legacy:
        source = tmp_path / "initial.xyz"
        source.write_text("3\nwater\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n", encoding="utf-8")
        geometry = store.import_artifact(source, "initial_geometry")
        geometry_id = geometry.id
        artifacts.append(geometry.id)
        steps["legacy"] = Step(id="legacy", logical_id="legacy", tool="orca.sp",
                               geometry=InputRef(artifact_id=geometry.id))
    request = Request(geometry_artifact_id=geometry_id,
        conditions={"electronic_state": "RHF", "environment": "gas_phase"} if legacy else {},
        goals=[Goal(id="files", port="artifacts",
        minimum_check_version="generic-observation-1",
        evidence_contract_version="generic-observation-1", verification="reported_with_limitations")])
    plan = Plan(request_id=request.id, steps=list(steps.values()),
                goal_map={"files": OutputBinding(step_id="sdk_2", port="artifacts")})
    permission = PermissionSnapshot(scientific_execution=True, code_execution=True,
        artifact_writes=True, artifact_ids=artifacts, allowed_tools=["orca.run", "python.execute", "orca.sp"],
        allow_additional_science=additional)
    budget = BudgetLimits(orca_starts=10, extra_orca_starts=extra_limit,
                          internal_work_units=10, code_retries=4)
    return store, store.create_run(request, plan, permission, budget), steps


def pending(store, run, steps, identity, tmp_path):
    step = steps[identity]
    if identity.startswith("direct"):
        return lambda: store.reserve_attempt(run, step), None
    parent = store.reserve_attempt(run, step)
    source = tmp_path / f"{identity}.inp"
    source.write_text(f"# inert native input {identity}\n", encoding="utf-8")
    artifact = store.import_artifact(source, "native_input", run_id=run.id, attempt_id=parent.id)
    code = CodeRef(artifact_id=artifact.id, sha256=artifact.sha256, language="orca")
    return lambda: store.reserve_internal_call(
        run, parent, identity, artifact.sha256, code=code), parent


def reserve_and_settle(store, run, steps, identity, tmp_path):
    reserve, parent = pending(store, run, steps, identity, tmp_path)
    item = reserve()
    if parent is not None:
        store.finish_internal_call(run, parent, identity, state="failed")
    store.finish_attempt(run, (parent or item).id, state="not_started", started=False,
                         termination_confirmed=True)
    assert run.usage.orca_starts_actual == 0
    return item


@pytest.mark.parametrize("first,second", [
    ("direct_0", "sdk_0"), ("sdk_0", "direct_0"), ("sdk_0", "sdk_1"),
])
def test_shared_extra_requires_permission_without_mutating_reservation(
        tmp_path, monkeypatch, first, second):
    store, run, steps = case(tmp_path, monkeypatch)
    reserve_and_settle(store, run, steps, first, tmp_path)
    reserve, _ = pending(store, run, steps, second, tmp_path)
    before, lease = run.model_dump(), store.environment_lease()
    with pytest.raises(StoreError, match="additional scientific"):
        reserve()
    assert store.load_run(run.id).model_dump() == before
    assert store.environment_lease() == lease


@pytest.mark.parametrize("first,second", [
    ("direct_0", "sdk_0"), ("sdk_0", "direct_0"), ("sdk_0", "sdk_1"),
])
def test_shared_extra_exhaustion_blocks_both_entry_points(tmp_path, monkeypatch, first, second):
    store, run, steps = case(tmp_path, monkeypatch, additional=True, extra_limit=0)
    reserve_and_settle(store, run, steps, first, tmp_path)
    reserve, _ = pending(store, run, steps, second, tmp_path)
    before = run.model_dump()
    with pytest.raises(BudgetExceeded, match="extra ORCA"):
        reserve()
    assert store.load_run(run.id).model_dump() == before


@pytest.mark.parametrize("first,second", [
    ("direct_0", "sdk_0"), ("sdk_0", "direct_0"), ("sdk_0", "sdk_1"),
])
def test_authorized_shared_extra_is_charged_once(tmp_path, monkeypatch, first, second):
    store, run, steps = case(tmp_path, monkeypatch, additional=True, extra_limit=1)
    reserve_and_settle(store, run, steps, first, tmp_path)
    reserve_and_settle(store, run, steps, second, tmp_path)
    assert run.usage.orca_starts_reserved == 2
    assert run.usage.extra_orca_starts_reserved == 1
    assert store.load_run(run.id).usage == run.usage


def test_initial_direct_steps_keep_their_existing_allowance(tmp_path, monkeypatch):
    store, run, steps = case(tmp_path, monkeypatch, extra_limit=0)
    reserve_and_settle(store, run, steps, "direct_0", tmp_path)
    reserve_and_settle(store, run, steps, "direct_1", tmp_path)
    assert run.usage.orca_starts_reserved == 2
    assert run.usage.extra_orca_starts_reserved == 0


def test_two_initial_direct_steps_then_sdk_charge_only_one_extra(tmp_path, monkeypatch):
    store, run, steps = case(tmp_path, monkeypatch, additional=True, extra_limit=1)
    reserve_and_settle(store, run, steps, "direct_0", tmp_path)
    reserve_and_settle(store, run, steps, "direct_1", tmp_path)
    reserve_and_settle(store, run, steps, "sdk_0", tmp_path)
    assert run.usage.orca_starts_reserved == 3
    assert run.usage.extra_orca_starts_reserved == 1
    reserve, _ = pending(store, run, steps, "sdk_1", tmp_path)
    with pytest.raises(BudgetExceeded, match="extra ORCA"):
        reserve()


def test_legacy_direct_retry_keeps_existing_repair_permission(tmp_path, monkeypatch):
    store, run, steps = case(tmp_path, monkeypatch, extra_limit=1, legacy=True)
    step = steps["legacy"]
    for expected in (0, 1):
        attempt = store.reserve_attempt(run, step, step.geometry.artifact_id)
        assert run.usage.extra_orca_starts_reserved == expected
        store.finish_attempt(run, attempt.id, state="not_started", started=False, termination_confirmed=True)
    assert not run.permission.allow_additional_science
    assert run.usage.orca_starts_reserved == 2 and not run.internal_calls


def test_prior_python_only_work_does_not_consume_first_orca_allowance(tmp_path, monkeypatch):
    store, run, steps = case(tmp_path, monkeypatch, extra_limit=0)
    parent = store.reserve_attempt(run, steps["sdk_0"])
    store.finish_attempt(run, parent.id, state="not_started", started=False, termination_confirmed=True)
    reserve_and_settle(store, run, steps, "sdk_1", tmp_path)
    assert run.usage.orca_starts_reserved == 1
    assert run.usage.extra_orca_starts_reserved == 0


def test_unbound_old_internal_history_cannot_authorize_new_mixed_work(tmp_path, monkeypatch):
    store, run, steps = case(tmp_path, monkeypatch, additional=True)
    reserve_and_settle(store, run, steps, "sdk_0", tmp_path)
    path = store.path(f"runs/{run.id}/run.json")
    historical = json.loads(path.read_text(encoding="utf-8"))
    historical["internal_calls"][0]["parent_attempt_id"] = None
    path.write_text(json.dumps(historical), encoding="utf-8")
    run = store.load_run(run.id)
    before = run.model_dump()
    with pytest.raises(StoreError, match="internal parent history"):
        store.reserve_attempt(run, steps["direct_0"])
    assert store.load_run(run.id).model_dump() == before
    assert store.environment_lease() is None


@pytest.mark.parametrize("third", ["direct_1", "sdk_1"])
def test_retained_mixed_history_cannot_reset_extra_allowance(tmp_path, monkeypatch, third):
    store, run, steps = case(tmp_path, monkeypatch, additional=True, extra_limit=1)
    reserve_and_settle(store, run, steps, "direct_0", tmp_path)
    reserve_and_settle(store, run, steps, "sdk_0", tmp_path)
    # Simulate a persisted pre-fix undercount, without deleting any reservation.
    path = store.path(f"runs/{run.id}/run.json")
    historical = json.loads(path.read_text(encoding="utf-8"))
    historical["usage"]["extra_orca_starts_reserved"] = 0
    path.write_text(json.dumps(historical), encoding="utf-8")
    run = store.load_run(run.id)
    reserve, _ = pending(store, run, steps, third, tmp_path)
    before = run.model_dump()
    with pytest.raises(BudgetExceeded, match="extra ORCA"):
        reserve()
    assert store.load_run(run.id).model_dump() == before


@pytest.mark.parametrize("window", ["callback", "before_intent", "after_intent", "after_lease", "after_run"])
def test_sdk_then_direct_prelaunch_recovery_preserves_one_extra_without_restart(
        tmp_path, monkeypatch, window):
    store, run, steps = case(tmp_path, monkeypatch, additional=True, extra_limit=1, legacy=True)
    reserve_and_settle(store, run, steps, "sdk_0", tmp_path)
    before = {"orca_starts_reserved": run.usage.orca_starts_reserved,
              "extra_orca_starts_reserved": run.usage.extra_orca_starts_reserved,
              "attempt_ids": [item.id for item in run.attempts]}
    captured = []

    def callback(draft):
        captured.append(draft.model_copy(deep=True))
        before["fingerprint_attempts"] = run.usage.fingerprint_attempts.get(draft.input_fingerprint, 0)
        if window == "callback":
            raise OSError("offline callback failure after external reservation")

    write, save = store._write_json, store.save_run

    def failing_write(relative, value, **kwargs):
        if relative.endswith("/intent.json") and window == "before_intent":
            raise OSError("offline before intent")
        write(relative, value, **kwargs)
        if relative.endswith("/intent.json") and window == "after_intent":
            raise OSError("offline after intent")

    def failing_save(value):
        if window == "after_lease":
            raise OSError("offline after lease")
        save(value)
        if window == "after_run":
            raise OSError("offline after Run")

    with monkeypatch.context() as fault:
        fault.setattr(store, "_write_json", failing_write)
        fault.setattr(store, "save_run", failing_save)
        with pytest.raises(OSError, match="offline"):
            store.reserve_attempt(run, steps["legacy"], steps["legacy"].geometry.artifact_id,
                                  before_reserve=callback)
    assert len(captured) == 1
    for _ in range(2):
        current = store.load_run(run.id)
        recovered = store.recover_unstarted_reservation(
            current, captured[0], before=before, reservation_sha256="a" * 64)
        assert recovered.id == captured[0].id and recovered.state == "not_started"
        assert not recovered.started and not recovered.execution_handle
        assert current.usage.orca_starts_reserved == 2
        assert current.usage.extra_orca_starts_reserved == 1
        assert current.usage.orca_starts_actual == 0
        assert len(current.attempts) == 2 and len(current.internal_calls) == 1
        assert store.environment_lease() is None
