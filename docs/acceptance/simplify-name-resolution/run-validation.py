"""Remaining integration regression, with before/after source manifests.

The non-overlapping identity suite is recorded in identity-targeted-validation.json.
"""

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
TESTS = [
    "tests/unit/test_semantic_named_identity.py",
    "tests/unit/test_worker_identity_publication.py",
    "tests/unit/test_general_intake_correction.py",
    "tests/unit/test_refreeze_plan_resources.py",
    "tests/unit/test_code_context.py",
    "tests/unit/test_context.py::test_actual_proposal_schema_and_bounded_default_context",
    "tests/unit/test_prompt_contract_versions.py",
]


def manifest():
    paths = {*ROOT.joinpath("src/orca_agent").rglob("*.py"), Path(__file__).resolve(),
             ROOT / "tests/conftest.py",
             *(ROOT / test.split("::")[0] for test in TESTS)}
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths)}


def main():
    command = [sys.executable, "-m", "pytest", "-q", *TESTS,
               "--junitxml", str(OUT / "offline-junit.xml")]
    record = {"started_at": datetime.now(timezone.utc).isoformat(), "command": command,
              "source_before": manifest(), "real_model": False, "real_orca": False,
              "external_network": False, "model_api_keys_removed_from_child": True}
    (OUT / "validation-plan.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.endswith("API_KEY")}
    started = time.monotonic()
    with (OUT / "offline-pytest.log").open("w", encoding="utf-8") as log:
        outcome = subprocess.run(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    record.update(exit_code=outcome.returncode, elapsed_seconds=time.monotonic() - started,
                  source_after=manifest())
    record["source_unchanged"] = record["source_before"] == record["source_after"]
    if (OUT / "offline-junit.xml").exists():
        cases = ET.parse(OUT / "offline-junit.xml").findall(".//testcase")
        record["collected"] = len(cases)
        record["failures"] = [f"{case.get('classname')}::{case.get('name')}"
                              for case in cases
                              if case.find("failure") is not None or case.find("error") is not None]
        record["skipped"] = sum(case.find("skipped") is not None for case in cases)
        record["passed"] = len(cases) - len(record["failures"]) - record["skipped"]
    (OUT / "offline-validation.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: record.get(key) for key in
                      ("exit_code", "source_unchanged", "collected", "passed", "skipped", "failures")}))
    return outcome.returncode or (0 if record["source_unchanged"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
