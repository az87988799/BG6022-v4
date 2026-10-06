"""Explicit offline developer replay; never updates historical domain objects.

Run against the original local evidence tree or a restored archive with the
same provenance paths. This is an acceptance utility, not a product Tool.
"""

import argparse
import json
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from orca_agent.orca.adapter import read_outputs
from orca_agent.store import Store, atomic_write, sha256_file
from orca_agent.versions import CURRENT_CHECK_VERSION


def replay(index_path: Path, output_path: Path) -> dict:
    index = json.loads(index_path.read_text(encoding="utf-8"))
    root = Path(__file__).resolve().parents[2]
    calls = []

    def prohibited(*args, **kwargs):
        calls.append("prohibited external action")
        raise AssertionError("read-only replay attempted execution or network access")

    records = []
    with ExitStack() as stack:
        for target in ("subprocess.Popen", "os.system", "os.popen", "socket.create_connection",
                       "socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo",
                       "orca_agent.backends.local._native"):
            stack.enter_context(patch(target, prohibited))
        for item in index["history"]:
            if "live-lifecycle" in Path(item["store_root"]).parts:
                continue  # Lifecycle snapshots are not scientific acceptance fixtures.
            store = Store(item["store_root"])
            run = store.load_run(item["run"]["id"])
            plan = store.load_plan(run)
            environment_path = Path(item["environment_path"])
            environment = json.loads(environment_path.read_text(encoding="utf-8"))
            for attempt in run.attempts:
                if not attempt.result_id:
                    continue
                old = store.load_result(run.id, attempt.result_id)
                workdir = store.path(attempt.directory)
                paths = {p for p in workdir.iterdir() if p.is_file()}
                paths.update(store.artifact_path(a) for a in old.artifact_ids)
                result_path = store.path(f"runs/{run.id}/results/{old.id}.json")
                paths.update([result_path, Path(item["run_path"]), environment_path])
                paths.update(store.path(f"runs/{run.id}").glob("*-revisions/*.json"))
                before = {str(p): sha256_file(p) for p in sorted(paths)}
                step = next(s for s in plan.steps if s.id == attempt.step_id)
                parsed = read_outputs(workdir, step.parameters, step.tool,
                                      expected_orca_version=environment["orca"]["version"])
                after = {str(p): sha256_file(p) for p in sorted(paths)}
                assert before == after, "historical evidence changed during replay"
                assert paths.issuperset(p for p in workdir.iterdir() if p.is_file())
                assert not calls
                checks = {port: [check.model_dump(mode="json") for check in items]
                          for port, items in parsed["checks"].items()}
                changes = []
                for port, items in parsed["checks"].items():
                    previous = {c.name: c.status for c in old.checks.get(port, [])}
                    changes.extend({"port": port, "check": c.name,
                                    "old_status": previous.get(c.name), "new_status": c.status}
                                   for c in items if previous.get(c.name) != c.status)
                records.append({
                    "run_id": run.id, "step_id": step.id, "attempt_id": attempt.id,
                    "original_result_id": old.id, "original_result_path": str(result_path),
                    "original_result_sha256": before[str(result_path)],
                    "tool": step.tool, "parameters": step.parameters.model_dump(mode="json"),
                    "source_hashes": before,
                    "original_artifacts": [store.load_artifact(a).model_dump(mode="json")
                                           for a in old.artifact_ids],
                    "old_output_ports": sorted(old.qualified_outputs),
                    "new_reader_outputs": parsed["qualified_outputs"], "checks": checks,
                    "observations": parsed["observations"], "diagnostics": parsed["diagnostics"],
                    "status_changes": changes, "historical_usage": run.usage.model_dump(mode="json"),
                    "application": "Independent replay only; no historical Result, goal or permission changed.",
                    "execution_limitations": {
                        "original_operation_status": old.operation_status,
                        "historical_postprocess_starts": run.usage.postprocess_starts,
                        "replay_cannot_requalify_execution_or_resolve_rule_migration": True,
                    },
                    "source_unchanged": True,
                })
    report = {
        "kind": "new_checker_readonly_replay_of_real_historical_artifacts",
        "check_version": CURRENT_CHECK_VERSION, "source_index": str(index_path.resolve()),
        "source_index_sha256": sha256_file(index_path),
        "implementation_sha256": {
            str(p.relative_to(root / "src")): sha256_file(p)
            for p in sorted((root / "src").rglob("*.py"))},
        "new_orca_starts": 0, "external_action_attempts": len(calls), "records": records,
    }
    atomic_write(output_path, (json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
                              + "\n").encode(), immutable=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.index, args.output)
    print(f"Replayed {len(report['records'])} historical attempts; sources unchanged, new ORCA starts 0")
