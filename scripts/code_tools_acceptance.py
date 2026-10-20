"""Explicit, non-scientific CLI for the fixed Code as Tools acceptance batch."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from orca_agent.config import load_config  # noqa: E402
from orca_agent.store import default_environment_root  # noqa: E402
from tests.helpers import code_tools_acceptance as acceptance  # noqa: E402
from tests.helpers.code_tools_freeze import build_product_manifest  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=[
        "freeze-product", "freeze-evaluation", "verify", "init-batch", "prepare"])
    parser.add_argument("--inspect-runtime", action="store_true",
        help="Explicitly read local Docker metadata; no pull, container or scientific run")
    parser.add_argument("--task")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--category", choices=["formal", "reference", "control"], default="formal")
    args = parser.parse_args(argv)
    directory = PROJECT / acceptance.DIRECTORY
    if args.action == "freeze-product":
        if not args.inspect_runtime:
            parser.error("product freezing requires explicit --inspect-runtime")
        from orca_agent.backends.container import inspect_environment
        config = load_config(directory / "runtime.toml")
        runtime = inspect_environment(config.execution)
        if not runtime.get("verified"):
            parser.error("the configured local runtime is not verified")
        manifest = build_product_manifest(PROJECT, config, runtime=runtime)
        target = directory / "product-freeze.json"
        acceptance.write_new(target, manifest)
        result = {"frozen_at_utc": datetime.now(timezone.utc).isoformat(),
            "product_commit": manifest["code_commit"],
            "product_manifest_sha256": acceptance.digest(target),
            "environment_root": str(default_environment_root().resolve()),
            "batch_root": str((PROJECT / "data/code-tools-formal-batch").resolve()),
            "model_calls": 0, "orca_starts": 0, "workloads_started": 0}
        acceptance.write_new(directory / "product-freeze-event.json", result)
    elif args.action == "freeze-evaluation":
        result = acceptance.build_evaluation_manifest(PROJECT)
        acceptance.write_new(directory / "evaluation-freeze.json", result)
        result = {"evaluation_sha256": acceptance.digest(directory / "evaluation-freeze.json"),
            "assets": len(result["assets"]), **result["rules"]}
    elif args.action == "verify":
        result = acceptance.verify(PROJECT, inspect_runtime=args.inspect_runtime)
    elif args.action == "init-batch":
        if not args.inspect_runtime:
            parser.error("batch preparation requires explicit --inspect-runtime")
        snapshot = acceptance.initialize_batch(PROJECT)
        result = {name: snapshot[name] for name in ("identity", "totals", "overrun_metrics")}
        result["reserved_slots"] = len(snapshot["rows"])
    else:
        if not args.inspect_runtime or not args.task:
            parser.error("prepare requires --inspect-runtime and --task")
        result = acceptance.prepare(PROJECT, task_id=args.task, repeat=args.repeat, category=args.category)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
