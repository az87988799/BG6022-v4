"""Test-only coordinator: hard exit after durable positive ORCA exec evidence."""

import argparse
import json
import os
import time
from pathlib import Path

from orca_agent._atomic import atomic_write
from orca_agent.agent import _science
from orca_agent.config import Config
from orca_agent.store import Store

CRASH_EXIT = 73
ALLOWED_ENVIRONMENT = {
    "SYSTEMROOT", "WINDIR", "PATH", "USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH",
    "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "LC_CTYPE",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    if set(key.upper() for key in os.environ) - ALLOWED_ENVIRONMENT:
        raise RuntimeError("worker environment contains a non-allowlisted variable name")
    config = Config.model_validate_json(args.config.read_bytes())
    if config.execution.backend != "linux-container" or config.model_profile != "disabled":
        raise ValueError("this fixture requires a real fixed container and disabled model")
    store = Store(config.data_root, environment_root=args.environment)
    run = store.load_run(args.run_id)
    plan = store.load_plan(run)
    if (len(plan.steps) != 1 or plan.steps[0].tool != "orca.run"
            or run.permission.allowed_tools != ["orca.run"]
            or not run.permission.scientific_execution
            or run.budget.orca_starts != 1 or run.budget.extra_orca_starts != 0
            or run.budget.attempts_per_step != 1 or run.budget.model_calls != 0
            or run.budget.internal_work_units != 0
            or config.resources.max_cores != 4 or config.resources.total_memory_mb != 1024
            or config.resources.maxcore_mb != 192):
        raise ValueError("this fixture only permits one four-core, 1 GiB native ORCA attempt")
    args.evidence.mkdir(parents=True, exist_ok=True)

    def fault(point):
        if point != "after_actual_start_saved":
            return
        durable = store.load_run(run.id)
        if len(durable.attempts) != 1:
            raise AssertionError("hard-exit fixture requires exactly one attempt")
        attempt = durable.attempts[0]
        root = store.path(attempt.directory)
        gated = json.loads((root / "started.json").read_bytes())
        actual = json.loads((root / "actual-start.json").read_bytes())
        proof = actual["actual_start_evidence"]
        if (gated["launch_phase"] != "gated_launcher" or actual["launch_phase"] != "executed"
                or proof["executable"] != "/opt/orca/orca"
                or proof["method"] != "proc_exe_inode_and_pid_start_ticks"
                or actual != attempt.execution_handle
                or any(gated["workload_identity"][key] != proof[key]
                       for key in ("pid", "start_ticks"))):
            raise AssertionError("positive exec evidence is not durable and correlated")
        receipt = {"point": point, "pid": os.getpid(), "time": time.time(),
                   "run": durable.model_dump(mode="json"), "gated": gated, "actual": actual,
                   "lease": store.environment_lease(), "environment_keys": sorted(os.environ)}
        atomic_write(args.evidence / (point + ".json"),
                     json.dumps(receipt, indent=2, sort_keys=True).encode("utf-8"))
        os._exit(CRASH_EXIT)

    result, outcome = _science(store, config, run, plan.steps[0], {}, None, fault)
    atomic_write(args.evidence / "unexpected-worker-result.json", json.dumps({
        "result": result.model_dump(mode="json"), "outcome": outcome,
    }, indent=2).encode("utf-8"))
    raise AssertionError("ORCA did not reach the requested actual-start crash point")


if __name__ == "__main__":
    main()
