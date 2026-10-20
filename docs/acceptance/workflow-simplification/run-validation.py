"""Offline integration regression with immutable per-run logs and source hashes."""

import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
PATTERNS = (
    "test_context*.py", "test_semantic*.py", "test_general*.py",
    "test_*identity*.py", "test_structure*.py", "test_worker_identity*.py",
    "test_*plan*.py", "test_refreeze*.py", "test_code*.py",
    "test_model_usage.py", "test_mechanical_progression.py", "test_native*.py",
    "test_terminal*.py", "test_bounded*.py", "test_decision*.py",
    "test_agent.py", "test_proposals.py", "test_prompt_contract_versions.py",
    "test_managed_shared_orca_budget.py", "test_python_repair_budget.py",
    "test_resource*.py", "test_workflow*.py", "test_file_write*.py",
    "test_workspace*.py", "test_models.py", "test_store.py",
    "test_managed_attempt_store.py", "test_authorized_run_budget_amendment.py",
    "test_generated_delivery.py",
)


def manifest(tests):
    paths = {*ROOT.joinpath("src/orca_agent").rglob("*.py"),
             *ROOT.joinpath("tests/helpers").rglob("*.py"), Path(__file__).resolve(),
             ROOT / "tests/conftest.py", *(ROOT / test for test in tests)}
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def main():
    label = sys.argv[1]
    if not label.isalnum():
        raise ValueError("Use an alphanumeric run label")
    tests = sorted({p.relative_to(ROOT).as_posix()
                    for pattern in PATTERNS for p in (ROOT / "tests/unit").glob(pattern)})
    junit = OUT / f"{label}-junit.xml"
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short", *tests, "--junitxml", str(junit)]
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "command": command,
              "source_before": manifest(tests), "real_model": False, "real_orca": False,
              "external_network": False, "model_api_keys_removed_from_child": True}
    with (OUT / f"{label}-plan.json").open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    with (OUT / f"{label}-pytest.log").open("x", encoding="utf-8") as log:
        outcome = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    record.update(exit_code=outcome.returncode, elapsed_seconds=time.monotonic() - started,
                  source_after=manifest(tests))
    record["source_unchanged"] = record["source_before"] == record["source_after"]
    if junit.exists():
        cases = ET.parse(junit).findall(".//testcase")
        record["collected"] = len(cases)
        record["failures"] = [f"{c.get('classname')}::{c.get('name')}" for c in cases
                              if c.find("failure") is not None or c.find("error") is not None]
        record["skipped"] = sum(c.find("skipped") is not None for c in cases)
        record["passed"] = len(cases) - len(record["failures"]) - record["skipped"]
    with (OUT / f"{label}-validation.json").open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(json.dumps({k: record.get(k) for k in
                      ("exit_code", "source_unchanged", "collected", "passed", "skipped", "failures")}))
    return outcome.returncode or (0 if record["source_unchanged"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
