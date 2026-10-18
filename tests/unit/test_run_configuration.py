"""Configured envelopes, immutable recovery and artifact reservation evidence."""

import json

import pytest
from pydantic import ValidationError

from orca_agent.config import Config, load_config
from orca_agent.llm import _parse_response, prepare_request
from orca_agent.model_usage import current_basis, read_model_reply, send_model
from orca_agent.models import (
    BudgetLimits,
    CalculationParameters,
    Goal,
    InputRef,
    ModelLimits,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    ResourceLimits,
    RunConfiguration,
    Step,
)
from orca_agent.store import BudgetExceeded, Store, StoreError
from tests.unit.test_model_usage import ScriptedTransport


def make_science(tmp_path, *, resources=None, parameters=None):
    config = RunConfiguration(resources=resources or ResourceLimits())
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment", configuration=config)
    source = tmp_path / "water.xyz"
    source.write_text("3\nwater\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n")
    artifact = store.import_artifact(source, "initial_geometry")
    request = Request(geometry_artifact_id=artifact.id,
                      goals=[Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")])
    step = Step(id="sp", logical_id="energy", tool="orca.sp", parameters=parameters or {},
                geometry=InputRef(artifact_id=artifact.id))
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"energy": OutputBinding(step_id="sp", port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(scientific_execution=True,
        artifact_ids=[artifact.id], max_cores=config.resources.max_cores,
        max_memory_mb=config.resources.total_memory_mb))
    return store, run, step, artifact


def test_new_configuration_is_loaded_and_saved_without_schema_upper_caps(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[resources]\nmax_cores=8\ntotal_memory_mb=4096\nmaxcore_mb=384\n"
                    "environment_concurrency=2\njob_timeout_seconds=1200\nscript_timeout_seconds=600\n"
                    "[model]\ninput_tokens=96000\noutput_tokens=16000\ntimeout_seconds=180\n"
                    "response_bytes=524288\nproposal_bytes=131072\nmessages=128\n")
    config = load_config(path)
    assert config.resources.max_cores == 8
    assert config.model.output_tokens == 16000
    assert CalculationParameters(cores=8, memory_mb=4096, maxcore_mb=384).cores == 8
    assert BudgetLimits(orca_starts=100, model_calls=100, model_tokens=1000000,
                        plan_revisions=20, code_retries=10, internal_work_units=100).orca_starts == 100
    assert Config().resources == ResourceLimits()


@pytest.mark.parametrize(("resources", "parameters", "reason"), [
    (ResourceLimits(max_cores=2), {}, "cores"),
    (ResourceLimits(total_memory_mb=512), {"cores": 1}, "memory_mb"),
    (ResourceLimits(maxcore_mb=128), {}, "maxcore_mb"),
    (ResourceLimits(job_timeout_seconds=10), {}, "timeout_seconds"),
    (ResourceLimits(environment_concurrency=2), {}, "not implemented"),
])
def test_launch_uses_frozen_configuration_before_any_budget_reservation(
    tmp_path, resources, parameters, reason,
):
    store, run, step, geometry = make_science(tmp_path, resources=resources, parameters=parameters)
    before = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(StoreError, match=reason):
        store.reserve_attempt(run, step, geometry.id)
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == before
    assert store.environment_lease() is None


def test_recovery_does_not_adopt_new_config_or_reset_usage(tmp_path):
    resources = ResourceLimits(max_cores=8, total_memory_mb=4096, maxcore_mb=384)
    store, run, step, geometry = make_science(tmp_path, resources=resources,
        parameters={"cores": 8, "memory_mb": 4096, "maxcore_mb": 384})
    store.reserve_attempt(run, step, geometry.id)
    restarted = Store(store.root, environment_root=store.environment_root,
                      configuration=RunConfiguration(resources=ResourceLimits(max_cores=1)))
    loaded = restarted.load_run(run.id)
    assert loaded.configuration.resources == resources
    assert loaded.usage.orca_starts_reserved == 1
    loaded.configuration = RunConfiguration()
    with pytest.raises(StoreError, match="fixed run field"):
        restarted.save_run(loaded)
    unchanged = restarted.load_run(run.id)
    unchanged.usage.orca_starts_reserved = 0
    with pytest.raises(StoreError, match="cannot decrease"):
        restarted.save_run(unchanged)


def test_legacy_run_read_preserves_absent_config_and_historical_bytes(tmp_path):
    store, run, _, _ = make_science(tmp_path)
    path = store.path(f"runs/{run.id}/run.json")
    payload = json.loads(path.read_text())
    payload.pop("configuration")
    payload["usage"]["orca_starts_reserved"] = 2
    path.write_text(json.dumps(payload))
    store.path(f"runs/{run.id}/configuration.json").unlink()
    original = path.read_bytes()
    store.configuration = RunConfiguration(resources=ResourceLimits(max_cores=32))
    old = store.load_run(run.id)
    assert old.configuration is None and old.usage.orca_starts_reserved == 2
    assert store.run_configuration(old).resources == ResourceLimits()
    assert store.artifact_budget_usage(old)["history_complete"] is False
    assert path.read_bytes() == original


def test_configuration_versions_missing_snapshot_and_invalid_copy_fail(tmp_path):
    store, run, _, _ = make_science(tmp_path)
    with pytest.raises(ValidationError):
        RunConfiguration(version=2)
    invalid = RunConfiguration(resources=ResourceLimits().model_copy(update={"max_cores": 0}))
    with pytest.raises(ValidationError):
        store.create_run(store.load_request(run), None, run.permission, configuration=invalid)
    store.path(f"runs/{run.id}/configuration.json").unlink()
    with pytest.raises(StoreError, match="snapshot"):
        store.load_run(run.id)


def test_removing_new_run_configuration_does_not_turn_it_into_a_legacy_run(tmp_path):
    store, run, _, _ = make_science(tmp_path)
    path = store.path(f"runs/{run.id}/run.json")
    payload = json.loads(path.read_text())
    payload.pop("configuration")
    path.write_text(json.dumps(payload))
    with pytest.raises(StoreError, match="configuration was removed"):
        store.load_run(run.id)


def test_initial_plan_obeys_configured_logical_step_budget(tmp_path):
    store, run, step, _ = make_science(tmp_path)
    request = store.load_request(run)
    plan = Plan(request_id=request.id,
                steps=[step, step.model_copy(update={"id": "second", "logical_id": "second"})],
                goal_map={"energy": OutputBinding(step_id="sp", port="energy")})
    before = set(store.path("runs").iterdir())
    with pytest.raises(BudgetExceeded, match="initial logical"):
        store.create_run(request, plan, run.permission, BudgetLimits(logical_steps=1))
    assert set(store.path("runs").iterdir()) == before


def test_artifact_count_and_bytes_remain_occupied_after_reopen(tmp_path):
    store = Store(tmp_path / "data")
    run = store.create_run(Request(goals=[Goal(id="g", port="observation")]), None,
        budget=BudgetLimits(artifact_count=2, artifact_bytes=4, artifact_total_bytes=5))
    file = tmp_path / "output.txt"
    file.write_bytes(b"abc")
    store.import_artifact(file, "analysis", run_id=run.id)
    file.write_bytes(b"123")
    with pytest.raises(BudgetExceeded, match="artifact"):
        store.import_artifact(file, "analysis", run_id=run.id)
    file.write_bytes(b"12")
    store.import_artifact(file, "analysis", run_id=run.id)
    reopened = Store(store.root)
    usage = reopened.artifact_budget_usage(reopened.load_run(run.id))
    assert usage["reserved_count"] == 2 and usage["reserved_bytes"] == 5
    file.write_bytes(b"")
    with pytest.raises(BudgetExceeded, match="artifact"):
        reopened.import_artifact(file, "analysis", run_id=run.id)


def test_failed_artifact_copy_reservation_is_not_free_or_reset(tmp_path):
    store = Store(tmp_path / "data")
    run = store.create_run(Request(goals=[Goal(id="g", port="observation")]), None,
                           budget=BudgetLimits(artifact_count=1))
    file = tmp_path / "output.txt"
    file.write_bytes(b"raw evidence")
    with pytest.raises(StoreError, match="authorized hash"):
        store.import_artifact(file, "analysis", run_id=run.id, expected_sha256="0" * 64)
    assert store.artifact_budget_usage(run)["reserved_count"] == 1
    with pytest.raises(BudgetExceeded):
        store.import_artifact(file, "analysis", run_id=run.id)
    assert file.read_bytes() == b"raw evidence"


def test_configured_model_limits_survive_transport_receipt_and_recovery(tmp_path):
    limits = ModelLimits(input_tokens=50000, output_tokens=6000, timeout_seconds=120,
                         response_bytes=524288, proposal_bytes=100000)
    store = Store(tmp_path / "data", configuration=RunConfiguration(model=limits))
    run = store.create_run(Request(goals=[Goal(id="g", port="observation")]), None,
        PermissionSnapshot(model_execution=True),
        BudgetLimits(model_calls=2, model_tokens=100000, input_tokens=50000, output_tokens=6000))
    prepared = prepare_request([{"role": "user", "content": "JSON " + "x" * 25000}], limits=limits)
    content = json.dumps({"text": "x" * 40000})
    raw = json.dumps({"choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()
    reply = _parse_response(prepared, raw, 200, None)
    assert reply.proposal is not None and prepared.output_token_bound == 6000
    send_model(store, run, prepared, ScriptedTransport(reply), basis=current_basis(store, run),
               logical_id="decision")
    loaded = Store(store.root).load_run(run.id)
    actual, _ = read_model_reply(store, loaded, loaded.model_records[0])
    assert actual.proposal == reply.proposal
    assert loaded.configuration.model == limits
    assert loaded.usage.model_tokens_unknown == prepared.reserved_tokens
    with pytest.raises(ValueError, match="configured bound"):
        prepare_request([{"role": "user", "content": "JSON"}], timeout_seconds=121, limits=limits)


def test_model_limits_cannot_be_changed_by_prepared_request(tmp_path):
    store = Store(tmp_path / "data")
    run = store.create_run(Request(goals=[Goal(id="g", port="observation")]), None,
        PermissionSnapshot(model_execution=True),
        BudgetLimits(model_calls=1, model_tokens=100000, input_tokens=50000, output_tokens=6000))
    prepared = prepare_request([{"role": "user", "content": "JSON"}],
                               limits=ModelLimits(output_tokens=6000))
    transport = ScriptedTransport()
    with pytest.raises(StoreError, match="frozen Run configuration"):
        send_model(store, run, prepared, transport, basis=current_basis(store, run), logical_id="decision")
    assert transport.sends == 0 and store.load_run(run.id).usage.model_calls == 0
