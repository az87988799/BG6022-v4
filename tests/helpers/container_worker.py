"""Test-only real-container coordinator; faults kill this host process, not Docker."""

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
    "SYSTEMROOT",
    "WINDIR",
    "PATH",
    "USERPROFILE",
    "HOME",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "LC_CTYPE",
}


def publish(path, value):
    atomic_write(path, json.dumps(value, sort_keys=True, indent=2).encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument(
        "--crash-at", choices=("after_resumed", "after_boundary_saved", "after_container_created")
    )
    args = parser.parse_args()
    # Even the trusted host fixture receives no inherited API-token variables.
    unexpected = set(key.upper() for key in os.environ) - ALLOWED_ENVIRONMENT
    if unexpected:
        raise RuntimeError("worker environment contains a non-allowlisted variable name")
    config = Config.model_validate_json(args.config.read_bytes())
    if config.execution.backend != "linux-container" or config.model_profile != "disabled":
        raise ValueError("this fixture requires a real fixed container and disabled model")
    store = Store(config.data_root, environment_root=args.environment)
    run = store.load_run(args.run_id)
    plan = store.load_plan(run)
    if (
        len(plan.steps) != 1
        or plan.steps[0].tool != "python.execute"
        or run.permission.scientific_execution
        or run.budget.orca_starts
    ):
        raise ValueError("this fixture only runs one non-scientific Python task")
    args.evidence.mkdir(parents=True, exist_ok=True)
    publish(
        args.evidence / "worker-started.json",
        {
            "pid": os.getpid(),
            "time": time.time(),
            "environment_keys": sorted(os.environ),
            "model_profile": config.model_profile,
            "run_id": run.id,
            "crash_at": args.crash_at,
        },
    )

    def fault(point):
        if point == "after_resumed" or point == args.crash_at:
            durable = store.load_run(run.id)
            publish(
                args.evidence / (point + ".json"),
                {
                    "point": point,
                    "pid": os.getpid(),
                    "time": time.time(),
                    "attempts": [item.model_dump(mode="json") for item in durable.attempts],
                    "lease": store.environment_lease(),
                },
            )
        if point == args.crash_at:
            os._exit(CRASH_EXIT)

    result, outcome = _science(store, config, run, plan.steps[0], {}, None, fault)
    publish(
        args.evidence / "worker-result.json",
        {
            "run": run.model_dump(mode="json"),
            "result": result.model_dump(mode="json"),
            "outcome": outcome,
        },
    )


if __name__ == "__main__":
    main()
