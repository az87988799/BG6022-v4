"""Explicit developer-only source freeze and independent offline acceptance receipts.

No action without --execute. Live model/science evaluation remains in the
separately gated tests/evals entry points. This is not a model-callable Tool.
"""

import argparse
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from orca_agent.context import PROMPT_VERSION
from orca_agent.llm import BASE_URL, MODEL, TOKEN_BOUND_VERSION
from orca_agent.models import utc_now
from orca_agent.store import atomic_write, sha256_file
from tests.helpers.phase_b_freeze import FREEZE, freeze_hash, validate_freeze

PROJECT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.run(["git", *args], cwd=PROJECT, capture_output=True, check=True).stdout


def freeze(label):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,37}", label):
        raise ValueError("freeze label must be a local identifier of at most 38 characters")
    if FREEZE.exists():
        raise ValueError("active freeze already exists; preserve it and its immutable historical anchor before any new batch")
    tracked = [s for s in git("ls-files", "-z").decode().split("\0") if s]
    fixed = {"pyproject.toml", "uv.lock", "config.example.toml", "AGENTS.md",
             "docs/ORCA-Agent-Project-Blueprint.md", "docs/acceptance/phase-b/coverage.json",
             "docs/acceptance/phase-b/profile.json", "docs/acceptance/phase-b/pricing-basis.json",
             "docs/acceptance/phase-b/pricing-recheck-b04.json", "docs/acceptance/phase-b/environment.json",
             "docs/acceptance/phase-b/reference-review.json", "docs/acceptance/phase-b/cases.md",
             "docs/acceptance/phase-b/runtime-profile.json"}
    names = sorted(name for name in tracked if name in fixed or name.startswith(("src/", "tests/", "docs/decisions/")))
    if not fixed.issubset(names):
        raise ValueError(f"required freeze files are not committed: {sorted(fixed-set(names))}")
    changed = git("diff", "--name-only", "HEAD", "--", *names).decode().strip()
    if changed:
        raise ValueError("commit final frozen sources first: " + changed)
    untracked = git("ls-files", "--others", "--exclude-standard", "--", "src", "tests", "docs/decisions").decode().strip()
    if untracked:
        raise ValueError("uncommitted implementation/test files cannot be omitted: " + untracked)
    normalized = [name for name in names if name.endswith(".py")
                  or (name.endswith(".md") and not name.startswith("tests/fixtures/"))
                  or name in {"pyproject.toml", "uv.lock", "config.example.toml"}]
    record = {"schema_version": 1, "freeze_label": label, "code_commit": git("rev-parse", "HEAD").decode().strip(),
              "created_at": utc_now().isoformat(), "files": {name: freeze_hash(PROJECT/name, source_text=name in normalized) for name in names},
              "source_lf_normalization": normalized, "prompt_version": PROMPT_VERSION,
              "model": MODEL, "base_url": BASE_URL, "token_bound_version": TOKEN_BOUND_VERSION,
              "python_version": sys.version.split()[0],
              "packages": {name: importlib.metadata.version(name) for name in ("openai", "httpx", "orca-pi", "pydantic", "pytest")},
              "configuration": {"orca_path": "E:/orca/orca.exe", "mpi_path": "C:/Program Files/Microsoft MPI/Bin/mpiexec.exe",
                                "cores": 4, "memory_mb": 1024, "maxcore_mb": 192,
                                "credentials": "process environment only; excluded"},
              "formal_slots": {"model": 75, "joint_trajectories": 18, "joint_variant_slots": 21, "offline_variant_slots": 78}}
    atomic_write(FREEZE, (json.dumps(record, ensure_ascii=False, indent=2)+"\n").encode(), immutable=True)
    print(json.dumps(validate_freeze(label), ensure_ascii=False))


def offline(label, repetition):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,37}", label):
        raise ValueError("freeze label must be a local identifier of at most 38 characters")
    binding = validate_freeze(label)
    coverage_path = PROJECT/"docs/acceptance/phase-b/coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    slots = [slot for entry in coverage["entries"] if entry["evidence_requirement"] == "offline_fault_injection"
             for slot in entry["formal_slots"] if slot["repetition"] == repetition]
    assert len(slots) == 26
    nodes = sorted({node for slot in slots for node in slot["pytest_nodeids"]})
    assert all(node.startswith(("tests/unit/", "tests/integration/")) for node in nodes)
    directory = PROJECT/"data/phase-b/offline-evaluations"/label/str(repetition)
    directory.mkdir(parents=True, exist_ok=False)
    xml, log = directory/"results.xml", directory/"pytest.txt"
    command = [sys.executable, "-m", "pytest", "-q", *nodes, f"--junitxml={xml}"]
    started = utc_now().isoformat()
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(command, cwd=PROJECT, stdout=stream, stderr=subprocess.STDOUT, check=False)
    if not xml.exists():
        raise ValueError("pytest produced no JUnit; invocation remains unverified and must not be silently replaced")
    receipt = {"schema_version": 1, "category": "formal_offline_fault_injection",
               "freeze_label": label, "freeze_sha256": binding["freeze_sha256"], "code_commit": binding["code_commit"],
               "repetition": repetition, "invocation_id": str(uuid.uuid4()), "started_at": started,
               "finished_at": utc_now().isoformat(), "junit_path": xml.name, "junit_sha256": sha256_file(xml),
               "log_path": log.name, "log_sha256": sha256_file(log), "pytest_returncode": completed.returncode,
               "coverage_sha256": sha256_file(coverage_path), "pytest_nodeids": nodes,
               "variant_ids": sorted(s["variant_id"] for s in slots), "live_model": False, "live_orca": False}
    atomic_write(directory/"receipt.json", (json.dumps(receipt, ensure_ascii=False, indent=2)+"\n").encode(), immutable=True)
    print(json.dumps({"receipt": str(directory/"receipt.json"), "returncode": completed.returncode, "nodes": len(nodes)}))
    return completed.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "offline"))
    parser.add_argument("--label", default="formal-v1")
    parser.add_argument("--repetition", type=int, choices=(1, 2, 3))
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("operator must explicitly choose --execute; nothing changed")
    if args.mode == "freeze":
        freeze(args.label)
    else:
        if args.repetition is None:
            parser.error("offline invocation requires --repetition")
        os.environ["ORCA_AGENT_EVAL_FREEZE"] = args.label
        raise SystemExit(offline(args.label, args.repetition))
