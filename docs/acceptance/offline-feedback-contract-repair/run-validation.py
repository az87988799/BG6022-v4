"""Bounded, serial development checks. This helper cannot enable a model test."""

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
OFFLINE = [
    "tests/unit/test_refreeze_evidence_projection.py",
    "tests/unit/test_refreeze_normalization_repair.py",
    "tests/unit/test_refreeze_plan_resources.py",
    "tests/unit/test_refreeze_runtime_repair.py",
    "tests/unit/test_code_context.py",
    "tests/unit/test_context.py",
    "tests/unit/test_decision_purpose_capacity.py",
    "tests/unit/test_readable_planning.py",
    "tests/unit/test_native_analytic.py",
    "tests/unit/test_evidence.py",
    "tests/unit/test_evidence_extended.py",
    "tests/unit/test_versioned_knowledge.py",
    "tests/unit/test_knowledge_completion.py",
]
LIVE = ["tests/live/test_refreeze_orca_repair.py"]
KNOWN_CAPACITY_FAILURES = {
    "tests.unit.test_context::"
    "test_actual_joint_sampling_ids_four_sp_and_two_analysis_results_fit_without_dropping_feedback"
    f"[{profile}-{phase}]"
    for profile in ("disabled", "thinking_low") for phase in ("final", "pending_analysis")
}


def snapshot(paths):
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(paths)}


def main():
    mode = sys.argv[1]
    assert mode in {"offline", "offline-final", "offline-closure", "orca", "orca-resume"} and len(sys.argv) == 2
    record = OUT / f"{mode}-validation.json"
    assert not record.exists(), "Use a distinct recorded follow-up; do not replace prior validation"
    selected = OFFLINE if mode.startswith("offline") else LIVE
    if mode in {"offline-final", "offline-closure"}:
        selected = [*selected, "tests/unit/test_nontext_dispatch_feedback.py",
                    "tests/unit/test_omitted_authorized_defaults.py"]
    if mode == "offline-closure":
        selected = [*selected, "tests/unit/test_native_quiesced_failure_repair.py"]
    if mode == "orca-resume":
        selected = ["tests/live/test_refreeze_orca_native_resume.py"]
    paths = list((ROOT / "src").rglob("*.py")) + [ROOT / p for p in selected]
    if mode == "orca-resume":
        paths += [ROOT / LIVE[0]]
    paths += [ROOT / "tests/conftest.py", Path(__file__)]
    before = snapshot(paths)
    env = os.environ.copy()
    for name in tuple(env):
        if name.endswith("_API_KEY"):
            env.pop(name)
    env["PYTHONUTF8"] = "1"
    junit = OUT / f"{mode}-junit.xml"
    command = [sys.executable, "-m", "pytest", "-q", *selected, "--junitxml", str(junit)]
    if mode.startswith("orca"):
        gate = "offline-closure" if mode == "orca-resume" else "offline-final"
        offline = json.loads((OUT / f"{gate}-validation.json").read_bytes())
        assert not offline["unexpected_failures"] and offline["source_unchanged"]
        assert (OUT / "context-capacity-baseline-validation.json").is_file()
        assert {k: v for k, v in offline["source_after"].items() if k.startswith("src/")} == {
            k: v for k, v in before.items() if k.startswith("src/")}
        prior = json.loads((ROOT / "docs/acceptance/code-as-tools-step7-refreeze-1/freeze/"
                           "product-freeze-event.json").read_bytes())
        environment = Path(prior["environment_root"])
        assert environment.is_dir() and not (environment / "lease.json").exists()
        env["LOCALAPPDATA"] = str(environment.parent.parent)
        env["ORCA_AGENT_REPAIR_ENVIRONMENT"] = str(environment)
        env["ORCA_AGENT_CONTAINER_CONFIG"] = str(
            ROOT / "docs/acceptance/code-as-tools-step7-refreeze-1/freeze/runtime.toml")
        temporary = ROOT / "data/offline-feedback-contract-repair-validation"
        command += ["--live-orca", "--live-container"]
        if mode == "orca":
            assert not temporary.exists(), "Refuse pytest basetemp cleanup of any existing evidence"
            command += ["--basetemp", str(temporary)]
        else:
            cases = list(temporary.glob("test_native_nmr*"))
            assert len(cases) == 1
            assert (OUT / "evidence/initial/index.json").is_file()
            env["ORCA_AGENT_REPAIR_EXISTING_CASE"] = str(cases[0])
    start = time.time()
    with (OUT / f"{mode}-pytest.log").open("wb") as output:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=output,
                                stderr=subprocess.STDOUT, timeout=7200)
    after = snapshot(paths)
    cases = ET.parse(junit).findall(".//testcase")
    failures = [f"{c.attrib.get('classname')}::{c.attrib.get('name')}" for c in cases
                if c.find("failure") is not None or c.find("error") is not None]
    allowed = KNOWN_CAPACITY_FAILURES if mode.startswith("offline") else set()
    data = {"command": command, "exit_code": result.returncode,
            "elapsed_seconds": time.time() - start, "source_before": before, "source_after": after,
            "source_unchanged": before == after, "real_model_tests": False,
            "model_api_keys_removed_from_child": True, "failures": failures,
            "unexpected_failures": [failure for failure in failures if failure not in allowed],
            "collected_test_cases": len(cases),
            "passed": len([c for c in cases if not any(
                c.find(tag) is not None for tag in ("failure", "error", "skipped"))])}
    record.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in data.items()
                      if key not in {"source_before", "source_after"}}), flush=True)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
