"""Frozen model modes cannot prevent collecting already-started science."""

import pytest

from orca_agent import runner
from orca_agent.backends import local
from orca_agent.model_usage import _freeze_model_profile
from orca_agent.store import StoreError, sha256_file
from tests.integration.test_runner import make_run
from tests.integration.test_runner import synthetic as synthetic


class NoModel:
    def send(self, *args, **kwargs):
        pytest.fail("scientific recovery must not send a model request")


@pytest.mark.parametrize("profile,other", [("disabled", "thinking_low"), ("thinking_low", "disabled")])
@pytest.mark.parametrize("window", ["after_execution_saved", "after_result_saved"])
def test_mode_mismatch_collects_existing_science_before_rejecting_without_resuming_control(
    tmp_path, monkeypatch, synthetic, profile, other, window,
):
    store, config, run = make_run(tmp_path, steps=2)
    config = config.model_copy(update={"model_profile": profile})
    with store.run_lock(run.id):
        _freeze_model_profile(store, run, profile)

    def crash(stage):
        if stage == window:
            raise RuntimeError("offline interruption before science attachment")

    interrupted = runner.execute(store, config, run.id, fault=crash, transport=NoModel())
    assert interrupted.state == "unknown" and len(synthetic) == 1
    assert len(interrupted.attempts) == 1 and not interrupted.model_records
    attempt_dir = store.path(interrupted.attempts[0].directory)
    raw_before = {p.name: sha256_file(p) for p in attempt_dir.iterdir() if p.is_file()}
    orphan_ids = {p.stem for p in store.path(f"runs/{run.id}/results").glob("*.json")}
    profile_path = store.path(f"runs/{run.id}/model-profile.json")
    frozen_profile = profile_path.read_bytes()
    store.signal(run.id, "pause")
    control = store.read_control(run.id)
    generation = interrupted.control_generation

    def no_science(*args, **kwargs):
        pytest.fail("recovery must not start another scientific process")

    monkeypatch.setattr(local, "run_managed", no_science)
    changed = config.model_copy(update={"model_profile": other})
    with pytest.raises(StoreError, match="profile is frozen"):
        runner.execute(store, changed, run.id, resume=True, transport=NoModel())
    recovered = store.load_run(run.id)
    assert len(recovered.attempts) == len(synthetic) == 1
    attempt = recovered.attempts[0]
    assert attempt.state == "completed" and attempt.result_id in recovered.result_ids
    result = store.load_result(run.id, attempt.result_id)
    assert result.operation_status == "completed" and "energy" in result.qualified_outputs
    if orphan_ids:
        assert recovered.result_ids == list(orphan_ids)
    assert len(list(store.path(f"runs/{run.id}/results").glob("*.json"))) == 1
    assert store.environment_lease() is None
    assert recovered.usage.orca_starts_actual == recovered.usage.orca_starts_reserved == 1
    assert recovered.usage.elapsed_seconds == 0.25
    assert not recovered.calls and not recovered.model_records and not recovered.decisions
    assert recovered.usage.model_calls == 0
    assert recovered.permission == interrupted.permission and recovered.budget == interrupted.budget
    assert store.read_control(run.id) == control and store.read_signal(run.id) == "pause"
    assert recovered.control_generation == generation
    assert profile_path.read_bytes() == frozen_profile
    assert {p.name: sha256_file(p) for p in attempt_dir.iterdir() if p.is_file()} == raw_before
    settled = store.path(f"runs/{run.id}/run.json").read_bytes()
    with pytest.raises(StoreError, match="profile is frozen"):
        runner.execute(store, changed, run.id, resume=True, transport=NoModel())
    assert store.path(f"runs/{run.id}/run.json").read_bytes() == settled
    assert store.read_control(run.id) == control


@pytest.mark.parametrize("profile,other", [("disabled", "thinking_low"), ("thinking_low", "disabled")])
def test_unresolved_scientific_identity_keeps_occupancy_and_pause_without_new_actions(
    tmp_path, synthetic, profile, other,
):
    store, config, run = make_run(tmp_path)
    config = config.model_copy(update={"model_profile": profile})
    with store.run_lock(run.id):
        _freeze_model_profile(store, run, profile)

    def crash(stage):
        if stage == "after_intent_saved":
            raise RuntimeError("offline interruption before process identity exists")

    interrupted = runner.execute(store, config, run.id, fault=crash, transport=NoModel())
    assert interrupted.state == "unknown" and synthetic == []
    store.signal(run.id, "pause")
    control = store.read_control(run.id)
    lease = store.environment_lease()
    changed = config.model_copy(update={"model_profile": other})
    recovered = runner.execute(store, changed, run.id, resume=True, transport=NoModel())
    assert recovered.state == "unknown" and synthetic == []
    assert len(recovered.attempts) == 1 and recovered.usage.orca_starts_reserved == 1
    assert recovered.usage.orca_starts_actual == interrupted.usage.orca_starts_actual
    assert not recovered.calls and not recovered.model_records and not recovered.result_ids
    assert store.environment_lease() == lease and store.read_control(run.id) == control
    assert recovered.control_generation == interrupted.control_generation
