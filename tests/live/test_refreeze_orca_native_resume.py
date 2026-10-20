"""One bounded continuation of the existing failed developer NMR probe."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from orca_agent.store import Store
from tests.live.test_container_execution import configured
from tests.live.test_refreeze_orca_repair import repair_native_nmr

pytestmark = [pytest.mark.live, pytest.mark.container]


def test_continue_settled_native_failure_without_repeating_bad_input():
    root = os.environ.get("ORCA_AGENT_REPAIR_EXISTING_CASE")
    assert root and Path(root).is_absolute()
    root = Path(root)
    evidence = json.loads((root / "evidence.json").read_bytes())
    config = configured(root)
    config.model_profile = "disabled"
    store = Store(config.data_root, environment_root=Path(os.environ["ORCA_AGENT_REPAIR_ENVIRONMENT"]))
    run = store.load_run(evidence["run"]["id"])
    assert run.configuration == config.run_configuration()
    assert len(run.attempts) == 1 and run.attempts[0].state == "failed"
    assert run.usage.orca_starts_reserved == 1 and run.budget.orca_starts == 2
    assert run.usage.model_calls == run.budget.model_calls == 0
    request, plan = store.load_request(run), store.load_plan(run)
    geometry = store.load_artifact(request.geometry_artifact_id)
    corrected_hash = hashlib.sha256((root / "source-1.inp").read_bytes()).hexdigest()
    corrected, = [item for identity in run.permission.artifact_ids
                   if (item := store.load_artifact(identity)).sha256 == corrected_hash]
    failed = store.load_result(run.id, run.attempts[0].result_id)
    repair_native_nmr(root, config, store, run, plan.steps[0], geometry, corrected, failed)
