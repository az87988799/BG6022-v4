"""Opt-in, real ORCA acceptance. Each run retains its full evidence under data/."""

import json
import os
from pathlib import Path

import pytest

from orca_agent.config import Config
from orca_agent.runner import execute, initialize
from orca_agent.store import Store, sha256_file

pytestmark = pytest.mark.live
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "phase_a"
ROOT = Path(__file__).resolve().parents[2]


def live_config():
    return Config(orca_path=Path(os.environ.get("ORCA_AGENT_ORCA", "E:/orca/orca.exe")).resolve(),
                  mpi_path=Path(os.environ.get("ORCA_AGENT_MPI", "C:/Program Files/Microsoft MPI/Bin/mpiexec.exe")).resolve(),
                  data_root=ROOT / "data")


def record(case_id, store, run):
    receipt = {"case_id": case_id, "store_root": str(store.root), "run_id": run.id,
               "state": run.state, "goal_status": run.goal_status,
               "result_ids": run.result_ids, "usage": run.usage.model_dump(),
               "attempt_ids": [a.id for a in run.attempts]}
    archive = ROOT / "data" / "acceptance"
    archive.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt, indent=2) + "\n"
    (archive / f"{case_id}-{run.id}.json").write_text(payload, encoding="utf-8")
    # This is an index; the named Run/attempt and previous receipts stay immutable.
    (archive / f"{case_id}.json").write_text(payload, encoding="utf-8")
    return receipt


@pytest.mark.parametrize("case_id,expected", [
    ("water_sp", {"energy"}), ("methane_sp", {"energy"}),
    ("water_opt", {"energy", "optimized_geometry"}),
    ("methane_opt", {"energy", "optimized_geometry"}),
    ("water_scf_limit", set()), ("water_opt_limit", {"energy"}),
])
def test_real_science(case_id, expected):
    config = live_config()
    store = Store(config.data_root)
    spec = FIXTURES / case_id / "request.json"
    initial = spec.parent / "geometry.xyz"
    original_hash = sha256_file(initial)
    run = initialize(store, config, spec)
    run = execute(store, config, run.id)
    record(case_id, store, run)
    assert sha256_file(initial) == original_hash
    assert len(run.attempts) == 1
    assert run.attempts[0].result_id is not None, run.model_dump()
    result = store.load_result(run.id, run.attempts[0].result_id)
    outputs = set(result.qualified_outputs)
    assert outputs == expected, result.model_dump()
    if case_id == "water_opt_limit":
        # A converged intermediate SCF energy may be qualified only with its own geometry.
        assert "optimized_geometry" not in outputs
        assert any(c.name == "optimization_converged" and c.status == "failed"
                   for c in result.checks["optimized_geometry"])
        assert all(c.status == "passed" for c in result.checks["energy"])
        binding = result.observations["evidence"]["energy_geometry_binding"]
        scf = result.observations["evidence"]["scf_converged"]
        assert binding["geometry_line"] == scf["geometry_line"]
        assert binding["geometry_line"] < binding["converged_lines"][0] < binding["energy_line"]
        assert binding["geometry"]
    if case_id == "water_scf_limit":
        assert any(c.name == "scf_converged" and c.status == "failed"
                   for c in result.checks["energy"])
    if case_id in ("water_sp", "methane_sp", "water_opt", "methane_opt"):
        assert run.state == "completed", run.model_dump()
    else:
        assert run.state == "failed", run.model_dump()
    workdir = store.path(run.attempts[0].directory)
    assert not (workdir / "job.2jsonout").exists(), "unbudgeted GBW JSON conversion"
    assert sha256_file(workdir / "geometry.xyz") == original_hash
    assert store.environment_lease() is None
    assert run.usage.orca_starts_actual == 1


def test_real_static_opt_to_sp():
    config = live_config()
    store = Store(config.data_root)
    run = initialize(store, config, FIXTURES / "water_opt_sp" / "request.json")
    run = execute(store, config, run.id)
    record("water_opt_sp", store, run)
    assert run.state == "completed", run.model_dump()
    assert len(run.attempts) == 2
    producer = store.load_result(run.id, run.attempts[0].result_id)
    geometry = producer.qualified_outputs["optimized_geometry"].artifact_id
    assert run.attempts[1].geometry_artifact_id == geometry
    assert run.usage.orca_starts_actual == 2
    assert store.environment_lease() is None
