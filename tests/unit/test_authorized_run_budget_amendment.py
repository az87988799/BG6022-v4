"""Trusted same-Run budget amendments; no model, script or ORCA is executed."""

import hashlib
import json
from datetime import timedelta

import pytest

from orca_agent import entrypoints
from orca_agent.models import Run, utc_now
from orca_agent.store import StoreError
from orca_agent.tools.managed import _control
from tests.unit.test_managed_attempt_store import make_task

LIMITS = {"model_calls": 44, "model_tokens": 2_000_000, "decision_rounds": 80,
          "evidence_reads": 24, "analysis_executions": 8, "code_retries": 4,
          "attempts_per_step": 4}


def task(tmp_path, monkeypatch):
    store, run, step, _ = make_task(tmp_path, monkeypatch, budget_changes={
        "model_calls": 20, "model_tokens": 1_200_000, "decision_rounds": 40,
        "evidence_reads": 12, "analysis_executions": 4, "code_retries": 2,
        "attempts_per_step": 2, "orca_starts": 4, "extra_orca_starts": 3})
    run.usage.model_calls = 19
    run.usage.model_tokens_used = 395_000
    run.usage.model_tokens_unknown = 123
    run.usage.resource_usage_complete = False
    run.state = "budget_exhausted"
    store.save_run(run)
    return store, run, step


def authorize(store, run, *, limits=None, deadline=None, message_id="user_amendment"):
    payload = {"budget_amendment": {"limits": LIMITS if limits is None else limits,
        "deadline": (deadline or utc_now() + timedelta(hours=1)).isoformat()}}
    entrypoints.message(store, run.id, "继续原任务；明确增加预算，条件、资源和许可不变。",
                        update=payload, message_id=message_id)
    return message_id


def test_trusted_amendment_preserves_originals_usage_and_managed_admission(tmp_path, monkeypatch):
    store, run, step = task(tmp_path, monkeypatch)
    before_bytes = store.path(f"runs/{run.id}/run.json").read_bytes()
    originals = {name: store.path(f"runs/{run.id}/{name}.json").read_bytes()
                 for name in ("budget", "permission", "configuration")}
    message_id = authorize(store, run)
    amended = entrypoints.amend_budget(store, run.id, message_id=message_id,
                                      amendment_id="authorized_0040")
    assert amended.budget_revision == "authorized_0040"
    assert amended.usage == run.usage
    assert amended.permission == run.permission and amended.configuration == run.configuration
    assert amended.state == run.state
    assert (amended.request_id, amended.request_version, amended.plan_id, amended.plan_version) == (
        run.request_id, run.request_version, run.plan_id, run.plan_version)
    assert amended.budget.model_calls == 44 and amended.budget.model_tokens == 2_000_000
    assert (amended.budget.orca_starts, amended.budget.extra_orca_starts) == (4, 3)
    assert amended.budget.run_seconds == (amended.deadline - run.created_at).total_seconds()
    assert all(store.path(f"runs/{run.id}/{name}.json").read_bytes() == raw
               for name, raw in originals.items())
    relative = f"runs/{run.id}/budget-revisions/authorized_0040"
    receipt = store._read_json(relative + ".json")
    assert store.path(relative + ".before.json").read_bytes() == before_bytes
    assert receipt["before_sha256"] == hashlib.sha256(before_bytes).hexdigest()
    assert receipt["authorization_message"]["source"] == "user"
    assert amended.processed_messages == run.processed_messages + [message_id]
    # The normal managed pre-launch check accepts the new authorized snapshot,
    # without reading or executing the task code.
    _control(store, amended, step)
    assert entrypoints.status(store, run.id)["budget"]["model_calls"] == 44


def test_amendment_needs_exact_user_fields_and_cannot_lower_or_expand_other_limits(
    tmp_path, monkeypatch,
):
    store, run, _ = task(tmp_path, monkeypatch)
    message_id = store.enqueue_message(run.id, "增加预算到44次")
    with pytest.raises(StoreError, match="trusted user update"):
        store.amend_budget(run.id, message_id=message_id, amendment_id="text_is_not_authority")
    for index, limits in enumerate(({"model_calls": 19}, {"orca_starts": 5},
                                    {"output_tokens": 100_000}, {"code_retries": True})):
        identity = authorize(store, run, limits=limits, message_id=f"bad_{index}")
        with pytest.raises(StoreError, match="decrease|undeclared|non-integer"):
            store.amend_budget(run.id, message_id=identity, amendment_id=f"rejected_{index}")
    identity = authorize(store, run, message_id="untrusted")
    control = store.read_control(run.id)
    control["messages"][-1]["source"] = "tool"
    store._write_json(f"runs/{run.id}/control.json", control)
    with pytest.raises(StoreError, match="trusted user update"):
        store.amend_budget(run.id, message_id=identity, amendment_id="tool_cannot_authorize")
    assert store.load_run(run.id) == run


def test_orphan_record_grants_nothing_and_same_authorization_replays_once(tmp_path, monkeypatch):
    store, run, _ = task(tmp_path, monkeypatch)
    message_id = authorize(store, run)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()

    def crash(point):
        assert point == "after_budget_revision_saved"
        raise RuntimeError("crash before activation")

    with pytest.raises(RuntimeError, match="before activation"):
        store.amend_budget(run.id, message_id=message_id, amendment_id="crash_window", fault=crash)
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert store.load_run(run.id).budget_revision is None
    assert store.load_run(run.id).usage.model_calls == 19
    assert store.path(f"runs/{run.id}/budget-revisions/crash_window.json").is_file()
    amended = store.amend_budget(run.id, message_id=message_id, amendment_id="crash_window")
    after = store.path(f"runs/{run.id}/run.json").read_bytes()
    assert store.amend_budget(run.id, message_id=message_id, amendment_id="crash_window") == amended
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == after
    with pytest.raises(StoreError, match="already consumed"):
        store.amend_budget(run.id, message_id=message_id, amendment_id="other_id")


def test_old_run_shape_direct_mutation_and_active_chain_tampering_are_rejected(tmp_path, monkeypatch):
    store, run, _ = task(tmp_path, monkeypatch)
    assert "budget_revision" not in run.model_dump(mode="json")
    assert Run.model_validate(json.loads(store.path(f"runs/{run.id}/run.json").read_bytes())) == run
    changed = run.model_copy(deep=True)
    changed.budget_revision = "invented"
    with pytest.raises(StoreError, match="fixed run field"):
        store.save_run(changed)
    message_id = authorize(store, run)
    amended = store.amend_budget(run.id, message_id=message_id, amendment_id="first")
    changed = amended.model_copy(deep=True)
    changed.budget.model_calls += 1
    with pytest.raises(StoreError, match="fixed run field"):
        store.save_run(changed)
    before_path = store.path(f"runs/{run.id}/budget-revisions/first.before.json")
    before_path.write_bytes(before_path.read_bytes() + b" ")
    with pytest.raises(StoreError, match="snapshot changed"):
        store.load_run(run.id)


def test_second_authorization_links_history_and_old_idempotent_replay_stays_current(
    tmp_path, monkeypatch,
):
    store, run, _ = task(tmp_path, monkeypatch)
    first_id = authorize(store, run)
    first = store.amend_budget(run.id, message_id=first_id, amendment_id="first")
    second_id = authorize(store, first, limits={"model_calls": 45},
                          deadline=first.deadline + timedelta(minutes=1), message_id="second_user")
    second = store.amend_budget(run.id, message_id=second_id, amendment_id="second")
    assert second.usage == run.usage and second.budget.model_tokens == 2_000_000
    assert second.budget.model_calls == 45
    assert store._read_json(f"runs/{run.id}/budget-revisions/second.json")["previous_revision"] == "first"
    assert store.amend_budget(run.id, message_id=first_id, amendment_id="first") == second
    store.validate_budget_snapshot(second)


def test_authorized_artifact_count_increase_keeps_usage_reservations_and_original_budget(tmp_path, monkeypatch):
    store, run, _ = task(tmp_path, monkeypatch)
    source = tmp_path / "existing-report.txt"
    source.write_bytes(b"retained report")
    store.import_artifact(source, "text", run_id=run.id)
    store._write_json(f"runs/{run.id}/artifact-reservations/artifact_interrupted.json", {
        "artifact_id": "artifact_interrupted", "run_id": run.id, "bytes": 7}, immutable=True)
    folder = store.path(f"runs/{run.id}/artifact-reservations")
    reservations = {path.name: path.read_bytes() for path in folder.glob("*.json")}
    usage = store.artifact_budget_usage(run)
    originals = {name: store.path(f"runs/{run.id}/{name}.json").read_bytes()
        for name in ("budget", "permission", "configuration")}
    deadline = run.deadline + timedelta(minutes=10)
    message_id = authorize(store, run, limits={"artifact_count": 400}, deadline=deadline)
    amended = entrypoints.amend_budget(store, run.id, message_id=message_id,
        amendment_id="authorized_artifact_count")
    assert amended.budget.artifact_count == 400 and amended.deadline == deadline
    assert amended.budget.model_dump(exclude={"artifact_count", "run_seconds"}) == run.budget.model_dump(
        exclude={"artifact_count", "run_seconds"})
    assert amended.usage == run.usage and amended.permission == run.permission
    assert amended.configuration == run.configuration
    assert store.artifact_budget_usage(amended) == usage and usage["reserved_count"] == 2
    assert {path.name: path.read_bytes() for path in folder.glob("*.json")} == reservations
    assert all(store.path(f"runs/{run.id}/{name}.json").read_bytes() == raw
        for name, raw in originals.items())
    store.validate_budget_snapshot(amended)
    assert entrypoints.amend_budget(store, run.id, message_id=message_id,
        amendment_id="authorized_artifact_count") == amended


def test_authorized_archive_timeout_preserves_configuration_usage_and_rejects_unauthorized_or_lower_values(
    tmp_path, monkeypatch,
):
    store, run, _ = task(tmp_path, monkeypatch)
    configuration = store.path(f"runs/{run.id}/configuration.json").read_bytes()
    assert store.archive_timeout(run) == 30
    plain = store.enqueue_message(run.id, "archive_timeout_seconds=300")
    with pytest.raises(StoreError, match="trusted user update"):
        entrypoints.amend_budget(store, run.id, message_id=plain, amendment_id="not_authorized")
    update = {"budget_amendment": {"limits": {}, "deadline": (
        run.deadline + timedelta(minutes=10)).isoformat(), "archive_timeout_seconds": 300}}
    entrypoints.message(store, run.id, "明确授权已有产物host归档上限300秒，计算资源不变。",
        update=update, message_id="archive_authorization")
    amended = entrypoints.amend_budget(store, run.id, message_id="archive_authorization",
        amendment_id="authorized_archive")
    assert store.archive_timeout(amended) == 300
    assert amended.configuration == run.configuration and amended.usage == run.usage
    assert store.path(f"runs/{run.id}/configuration.json").read_bytes() == configuration
    later_id = authorize(store, amended, limits={}, deadline=amended.deadline + timedelta(minutes=1),
        message_id="inherit_archive")
    inherited = entrypoints.amend_budget(store, run.id, message_id=later_id, amendment_id="inherited_archive")
    assert store.archive_timeout(inherited) == 300
    update["budget_amendment"].update(archive_timeout_seconds=150,
        deadline=(inherited.deadline + timedelta(minutes=1)).isoformat())
    entrypoints.message(store, run.id, "降低归档时限。", update=update, message_id="lower_archive")
    with pytest.raises(StoreError, match="cannot decrease"):
        entrypoints.amend_budget(store, run.id, message_id="lower_archive", amendment_id="rejected_lower_archive")
    assert store.load_run(run.id) == inherited and store.archive_timeout(inherited) == 300
