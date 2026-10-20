"""Replay a016 admission only: copied archive, scripted model, no execution backend."""

import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from orca_agent import agent, model_usage
from orca_agent.config import Config
from orca_agent.proposals import ProposalError, _validate_plan_parameters
from orca_agent.store import Store
from tests.unit.test_agent import ScriptedTransport

RUN_ID = "run_d43e58295336493ebad773115b7644bd"


def _archive():
    root = Path(__file__).parents[2] / "data/live-repair/attempts/a016"
    if not (root / "data/runs" / RUN_ID / "run.json").is_file():
        pytest.skip("local a016 archive is not available")
    return root


def test_actual_a016_unchanged_input_returns_to_model_without_new_reservation(tmp_path, monkeypatch):
    import orca_agent.store as store_module

    archive = _archive()
    original_files = [path for path in (archive / "data").rglob("*") if path.is_file()]
    original_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in original_files}
    root = tmp_path / "copied_a016"
    shutil.copytree(archive / "data", root)
    config_values = json.loads((archive / "configuration.json").read_bytes())
    config_values["data_root"] = str(root)
    config = Config.model_validate(config_values)
    store = Store(root, environment_root=tmp_path / "offline_environment", configuration=config.run_configuration())
    run = store.load_run(RUN_ID)
    assert run.state == "failed" and len(run.attempts) == 2 and run.usage.orca_starts_actual == 0
    assert all(item.finished_at and not item.started for item in run.attempts)
    original_attempts = run.model_copy(deep=True).attempts
    original_usage = run.usage.model_copy(deep=True)
    # This temporary replay restores the instant of the final admission. It
    # preserves original deadlines, limits, counters, artifacts and failed inputs.
    run.state = "ready"
    store.save_run(run)
    now = datetime.fromisoformat(run.model_records[-1]["created_at"])
    for module in (agent, model_usage, store_module):
        monkeypatch.setattr(module, "utc_now", lambda: now)

    admission_calls = []

    def admission_only(store, config, run, step, results, batch, fault):
        admission_calls.append(step.id)
        # The real reservation guard must reject before any backend, callback,
        # Attempt or lease. No environment probe/ORCA/Python process is invoked.
        store.reserve_attempt(run, step)
        pytest.fail("the unchanged failed input must never reserve an Attempt")

    monkeypatch.setattr(agent, "_science", admission_only)

    def receive_correction(data):
        error = data["CONTROL"]["validation_error"]
        assert error["category"] == "step_admission_rejected"
        detail = error["requirement"]
        assert detail["code"] == "unchanged_failed_input"
        assert detail["phase"] == "pre_reservation"
        assert detail["execution_scope"] == "current_submission"
        assert detail["execution_started"] is False and detail["attempt_reserved"] is False
        assert detail["prior_attempt_id"] == original_attempts[0].id
        assert detail["prior_result_id"] == original_attempts[0].result_id
        assert detail["input_fingerprint"] == original_attempts[0].input_fingerprint
        assert detail["failed_input_ref"]["sha256"] == "c39e5831a943c46d5cd9edaa2e9c6d472ef57be79bd8e24efa126d6690cec191"
        assert data["AUTHORITY"]["basis"]["plan_version"] == 3
        return {"action": "clarify", "parameters": {
            "questions": ["Offline fixture ends after observing the actionable admission feedback."],
            "unresolved": ["The actual scientific input still requires a corrected artifact."]}}

    transport = ScriptedTransport(receive_correction)
    ended = agent.execute(store, config, run.id, transport=transport)
    assert not transport.scripts and len(transport.sent) == 1, ended.diagnostics
    assert len(admission_calls) == 1 and ended.state == "waiting_user"
    assert ended.attempts == original_attempts and store.environment_lease() is None
    assert ended.usage.orca_starts_reserved == original_usage.orca_starts_reserved
    assert ended.usage.orca_starts_actual == original_usage.orca_starts_actual == 0
    assert ended.usage.analysis_executions == original_usage.analysis_executions
    assert ended.usage.model_calls == original_usage.model_calls + 1
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in original_hashes.items())


def test_actual_a016_repair_shape_feedback_does_not_require_forbidden_logical_key():
    archive = _archive()
    path = archive / "data/runs" / RUN_ID / "model/model_0437d4358f63494ba3845a0ce47a9271.response.json"
    original = path.read_bytes()
    parameters = json.loads(original)["proposal"]["parameters"]
    with pytest.raises(ProposalError) as caught:
        _validate_plan_parameters(parameters)
    detail = caught.value.detail
    assert detail["errors"] == [{"loc": ["parameters", "steps", 0, "logical_key"], "type": "extra_forbidden"}]
    assert "only that full form uses logical_key" in detail["step_instruction"]
    assert "repair_of shorthand inherits logical_id and must omit logical_key" in detail["step_instruction"]
    assert "logical_key" not in detail["code_repair_fields"]
    assert "respective execution permission" in detail["code_repair_instruction"]
    assert path.read_bytes() == original
