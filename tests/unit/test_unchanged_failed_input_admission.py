"""Reservation-only failures: no task process, live model, or ORCA is started."""

from copy import deepcopy

import pytest

from orca_agent.models import Result, fingerprint
from orca_agent.store import EnvironmentBusy, UnchangedFailedInput
from tests.unit.test_managed_attempt_store import make_task


def failed_input_case(tmp_path, monkeypatch, state="failed"):
    store, run, step, _ = make_task(tmp_path, monkeypatch,
        budget_changes={"code_retries": 2, "attempts_per_step": 4})
    prior = store.reserve_attempt(run, step)
    result = Result(run_id=run.id, step_id=step.id, attempt_id=prior.id, operation_status=state,
                    diagnostics=[{"category": "offline_fixture", "detail": "Synthetic settled failure."}])
    store.save_result(result)
    store.finish_attempt(run, prior.id, state=state, result_id=result.id, started=True,
                         elapsed_seconds=2, cpu_seconds=1, termination_confirmed=True)
    return store, run, step, prior, result


def archived_bytes(store):
    return {str(path.relative_to(store.root)): path.read_bytes()
            for path in store.root.rglob("*") if path.is_file() and path.suffix != ".lock"}


@pytest.mark.parametrize("state", ["failed", "timed_out"])
def test_unchanged_failure_is_typed_before_any_new_reservation(tmp_path, monkeypatch, state):
    store, run, step, prior, result = failed_input_case(tmp_path, monkeypatch, state)
    before, files = deepcopy(run), archived_bytes(store)
    callbacks = []
    with pytest.raises(UnchangedFailedInput, match="unchanged failed input") as caught:
        store.reserve_attempt(run, step, before_reserve=callbacks.append)
    detail = caught.value.detail
    assert detail["code"] == "unchanged_failed_input" and detail["phase"] == "pre_reservation"
    assert detail["execution_scope"] == "current_submission"
    assert detail["execution_started"] is detail["attempt_reserved"] is False
    assert detail["prior_started"] is True  # No claim that the historical attempt never started.
    assert detail["prior_state"] == state
    assert detail["prior_attempt_id"] == prior.id and detail["prior_result_id"] == result.id
    assert detail["step_id"] == step.id and detail["step_fingerprint"] == fingerprint(step)
    assert detail["input_fingerprint"] == prior.input_fingerprint
    assert detail["failed_input_ref"] == {"artifact_id": prior.frozen_step.parameters.code.artifact_id,
                                         "sha256": prior.frozen_step.parameters.code.sha256}
    assert "correct its code/input" in detail["instruction"]
    assert callbacks == [] and run == before and store.load_run(run.id) == before
    assert store.environment_lease() is None and archived_bytes(store) == files


@pytest.mark.parametrize("state", ["intent", "running", "unknown"])
def test_pending_attempt_after_old_failed_input_retains_reconciliation_priority(tmp_path, monkeypatch, state):
    store, run, step, prior, _ = failed_input_case(tmp_path, monkeypatch)
    # An explicit synthetic unfinished receipt after the older failure checks
    # order independence. No reservation or execution is fabricated by the API.
    pending = prior.model_copy(update={"id": "attempt_pending_fixture", "state": state,
        "input_fingerprint": "pending_fixture_fingerprint", "finished_at": None,
        "result_id": None, "started": state != "intent", "directory": "offline/pending_fixture"}, deep=True)
    run.attempts.append(pending)
    store.save_run(run)
    before, files = deepcopy(run), archived_bytes(store)
    callbacks = []
    with pytest.raises(EnvironmentBusy, match="needs reconciliation") as caught:
        store.reserve_attempt(run, step, before_reserve=callbacks.append)
    assert not isinstance(caught.value, UnchangedFailedInput)
    assert callbacks == [] and run == before and store.load_run(run.id) == before
    assert store.environment_lease() is None and archived_bytes(store) == files
