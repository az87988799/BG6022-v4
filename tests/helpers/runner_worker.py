"""Test-only coordinator/process fixtures. Never available through the product CLI."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil


def prepare_fixture(workdir, geometry, parameters, tool):
    """Deterministic synthetic input, deliberately unrelated to ORCA syntax."""
    directory = Path(workdir)
    (directory / "geometry.xyz").write_bytes(Path(geometry).read_bytes())
    input_path = directory / "fixture-input.json"
    input_path.write_text(json.dumps({"test_fixture": True}), encoding="utf-8")
    return {"input_path": str(input_path), "fixture": "no scientific calculation"}


def read_fixture(workdir, parameters, tool):
    """Synthetic result only verifies coordinator control flow, never science."""
    exists = (Path(workdir) / "fixture-success.txt").exists()
    checks = [{"name": "synthetic_fixture", "status": "passed" if exists else "failed",
               "rule_version": "orca-hf-1", "detail": "Synthetic fixture; not scientific evidence"}]
    return {"checks": {"energy": checks}, "qualified_outputs": {
        "energy": {"value": -1.0, "unit": "Eh", "source": {"synthetic_fixture": True}},
    } if exists else {}, "observations": {"synthetic_fixture": True}, "diagnostics": []}


def task(directory, delay, depth):
    current = psutil.Process()
    (directory / f"task-{current.pid}.json").write_text(json.dumps({
        "pid": current.pid, "create_time": current.create_time(),
    }), encoding="utf-8")
    if depth:
        subprocess.Popen([sys.executable, __file__, "task", str(directory), str(delay),
                          str(depth - 1)])
    time.sleep(delay)
    (directory / "fixture-success.txt").write_text("Synthetic fixture only", encoding="utf-8")


def main():
    if sys.argv[1] == "task":
        task(Path(sys.argv[2]), float(sys.argv[3]), int(sys.argv[4]))
        return
    if sys.argv[1] == "live":
        from orca_agent.config import Config
        from orca_agent.runner import execute
        from orca_agent.store import Store

        data, run_id, executable, mpi = sys.argv[2:6]
        store = Store(data)
        config = Config(data_root=Path(data), orca_path=Path(executable), mpi_path=Path(mpi))
        result = execute(store, config, run_id)
        (Path(data) / "worker-result.json").write_text(result.model_dump_json(), encoding="utf-8")
        return
    from orca_agent import runner
    from orca_agent.backends import local
    from orca_agent.config import Config
    from orca_agent.store import Store
    from orca_agent.tools import calculation

    data, environment, run_id, mode, crash_point = sys.argv[2:7]
    store = Store(data, environment_root=environment)
    config = Config(data_root=Path(data), orca_path=Path(sys.executable).resolve())
    original = local.run_managed

    def managed_fixture(executable, args, workdir, **kwargs):
        delay = 1.2 if mode == "pause" else 60 if mode in ("cancel", "atomic") else 0.05
        return original(Path(sys.executable), [__file__, "task", str(workdir), str(delay), "1"],
                        workdir, **kwargs)

    def fault(point):
        if point == "after_process_created":
            pids = [p.pid for p in psutil.Process().children()]
            (Path(data) / "created-pids.json").write_text(json.dumps(pids), encoding="utf-8")
        if mode == "atomic" and point == "after_handle_saved":
            (Path(data) / "control-held.txt").write_text("held", encoding="utf-8")
            deadline = time.monotonic() + 4
            while not (Path(data) / "release-control.txt").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test handshake timed out")
                time.sleep(0.01)
        if point == crash_point:
            (Path(data) / "crash-point.txt").write_text(point, encoding="utf-8")
            os._exit(62)

    calculation.prepare_input = prepare_fixture
    calculation.read_outputs = read_fixture
    local.run_managed = managed_fixture
    result = runner.execute(store, config, run_id, fault=fault)
    (Path(data) / "worker-result.json").write_text(result.model_dump_json(), encoding="utf-8")


if __name__ == "__main__":
    main()
