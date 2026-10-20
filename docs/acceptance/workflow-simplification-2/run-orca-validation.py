"""Offline-pass gate, three bounded developer executions, no model credentials."""

import hashlib
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent


def hashes():
    paths = [*ROOT.joinpath("src/orca_agent").rglob("*.py"),
             *ROOT.joinpath("tests").rglob("*.py"), Path(__file__).resolve()]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def main():
    label, offline_label = sys.argv[1:]
    assert label.isalnum() and offline_label.isalnum()
    offline = json.loads((OUT / f"{offline_label}-validation.json").read_bytes())
    assert offline["exit_code"] == 0 and offline["source_unchanged"] and not offline["failures"]
    before = hashes()
    assert all(offline["source_after"].get(name) == digest
               for name, digest in before.items() if name.startswith(("src/", "tests/")))
    event = json.loads((ROOT / "docs/acceptance/code-as-tools-step7-refreeze-2/freeze/"
                       "product-freeze-event.json").read_bytes())
    shared = Path(event["environment_root"])
    assert shared.is_dir() and not (shared / "lease.json").exists()
    temporary = ROOT / f"data/workflow-simplification-2-{label}"
    assert not temporary.exists(), "Never allow pytest to clear previous execution evidence"
    env = {k: v for k, v in os.environ.items() if not k.endswith("API_KEY")}
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
               LOCALAPPDATA=str(shared.parent.parent),
               ORCA_AGENT_REPAIR_ENVIRONMENT=str(shared),
               ORCA_AGENT_CONTAINER_CONFIG=str(
                   ROOT / "docs/acceptance/code-as-tools-step7-refreeze-2/freeze/runtime.toml"))
    junit = OUT / f"{label}-junit.xml"
    command = [sys.executable, "-m", "pytest", "-q", "--tb=short",
               "tests/live/test_simplified_workflow_orca.py", "--live-orca", "--live-container",
               "--basetemp", str(temporary), "--junitxml", str(junit)]
    record = {"command": command, "source_before": before, "real_model": False,
              "real_orca_enabled": True, "developer_authored": True,
              "autonomous_acceptance": False, "maximum_orca_reservations": 3,
              "environment_root": str(shared), "offline_gate": offline_label}
    with (OUT / f"{label}-plan.json").open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    start = time.monotonic()
    with (OUT / f"{label}-pytest.log").open("x", encoding="utf-8") as f:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT)
    cases = ET.parse(junit).findall(".//testcase")
    record.update(exit_code=result.returncode, elapsed_seconds=time.monotonic() - start,
                  source_after=hashes(), collected=len(cases),
                  failures=[c.get("name") for c in cases
                            if c.find("failure") is not None or c.find("error") is not None],
                  skipped=sum(c.find("skipped") is not None for c in cases),
                  probes=[json.loads(path.read_bytes()) for path in temporary.glob("*/probe-summary.json")])
    record["source_unchanged"] = before == record["source_after"]
    with (OUT / f"{label}-validation.json").open("x", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    print(json.dumps({k: record[k] for k in ("exit_code", "failures", "skipped", "source_unchanged")}))
    return result.returncode or (0 if record["source_unchanged"] and not record["skipped"] else 2)


if __name__ == "__main__":
    raise SystemExit(main())
