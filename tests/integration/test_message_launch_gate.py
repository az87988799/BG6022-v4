"""Real controller boundaries with synthetic evidence; zero ORCA launches."""

import os

import pytest

from orca_agent import agent
from orca_agent.backends import local
from orca_agent.tools import calculation
from tests.integration.test_runner import FIXTURE, make_run


def test_persisted_condition_message_after_input_blocks_backend_before_creation(tmp_path, monkeypatch):
    store, config, run = make_run(tmp_path)
    monkeypatch.setattr(calculation, "prepare_input", FIXTURE.prepare_fixture)
    monkeypatch.setattr(local, "run_managed", lambda *a, **k: pytest.fail("backend was reached"))

    def update(point):
        if point == "after_input_prepared":
            store.enqueue_message(run.id, "条件变更：不要启动之前的计算")

    stopped = agent.execute(store, config, run.id, fault=update)
    assert stopped.state == "waiting_user", stopped.diagnostics
    assert stopped.usage.orca_starts_reserved == 1
    assert stopped.usage.orca_starts_actual == 0 and not stopped.attempts[0].started
    assert stopped.attempts[0].control_generation == 0
    assert store.environment_lease() is None
    result = store.load_result(run.id, stopped.result_ids[0])
    assert result.source["execution"]["not_started"]
    assert not result.qualified_outputs


@pytest.mark.backend
@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process gate")
def test_suspended_python_child_is_recorded_and_terminated_without_resuming(tmp_path, monkeypatch):
    store, config, run = make_run(tmp_path)
    monkeypatch.setattr(calculation, "prepare_input", FIXTURE.prepare_fixture)
    monkeypatch.setattr(calculation, "read_outputs", FIXTURE.read_fixture)
    seen = []

    def update(point):
        seen.append(point)
        if point == "after_process_created":
            # Same coordinator injecting the persisted fact avoids waiting on
            # another client while the startup control lock is held.
            store.enqueue_message(run.id, "条件已经变化")

    stopped = agent.execute(store, config, run.id, fault=update)
    assert "after_process_created" in seen and "after_resumed" not in seen
    assert stopped.state == "waiting_user"
    assert stopped.usage.orca_starts_reserved == stopped.usage.orca_starts_actual == 1
    attempt = stopped.attempts[0]
    assert attempt.started and attempt.execution_handle["pid"]
    assert store.path(attempt.directory + "/started.json").is_file()
    assert store.environment_lease() is None
    result = store.load_result(run.id, attempt.result_id)
    assert not result.qualified_outputs and result.source["execution"]["handle"]["pid"]
    assert not result.source["execution"].get("not_started", False)
