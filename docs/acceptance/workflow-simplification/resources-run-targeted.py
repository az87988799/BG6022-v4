"""Reproducible offline resource checks; never enables live execution."""
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
FILES = [
    "src/orca_agent/models.py", "src/orca_agent/tools/registry.py", "src/orca_agent/store.py",
    "src/orca_agent/planning.py", "src/orca_agent/tools/managed.py",
    "src/orca_agent/delivery.py",
    "tests/helpers/code_tools_batch.py", "tests/unit/test_code_tools_batch.py",
    "tests/unit/test_refreeze_plan_resources.py", "tests/unit/test_workflow_resource_simplification.py",
    "tests/unit/test_workspace_patch.py",
    "tests/unit/test_generated_delivery.py",
]


def hashes():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}


if __name__ == "__main__":
    label = sys.argv[1]
    tests = sys.argv[2:] or [
        "tests/unit/test_workflow_resource_simplification.py",
        "tests/unit/test_refreeze_plan_resources.py", "tests/unit/test_code_tools_batch.py",
    ]
    command = [sys.executable, "-m", "pytest", *tests, "-q", "--tb=short"]
    before = hashes()
    started = datetime.now(timezone.utc).isoformat()
    result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    log = HERE / f"resources-{label}.log"
    log.write_text(result.stdout, encoding="utf-8")
    after = hashes()
    record = {"command": command, "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(), "exit_code": result.returncode,
        "source_and_test_hashes_before": before, "source_and_test_hashes_after": after,
        "owned_sources_stable": before == after, "log": log.name,
        "log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "real_llm": False, "real_orca": False, "network": False}
    (HERE / f"resources-{label}.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    print(result.stdout)
    sys.exit(result.returncode)
