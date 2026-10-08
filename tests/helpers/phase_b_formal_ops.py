"""Explicit developer-only source freeze and independent offline acceptance receipts.

No action without --execute. Live model/science evaluation remains in the
separately gated tests/evals entry points. This is not a model-callable Tool.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

from orca_agent.models import utc_now
from orca_agent.store import atomic_write, sha256_file
from tests.helpers.phase_b_freeze import (
    FIXED,
    FREEZE,
    evaluation_config,
    execution_budget_authority,
    execution_files,
    freeze_hash,
    runtime_environment,
    science_environment,
    validate_freeze,
)

PROJECT = Path(__file__).resolve().parents[2]


def git(*args):
    return subprocess.run(["git", *args], cwd=PROJECT, capture_output=True, check=True).stdout


def freeze(label, *, config_path=None, with_science=False, model_profile=None, cycle_repair_evidence=None):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,37}", label):
        raise ValueError("freeze label must be a local identifier of at most 38 characters")
    if FREEZE.exists():
        raise ValueError("active freeze already exists; preserve it and its immutable historical anchor before any new batch")
    tracked = [s for s in git("ls-files", "-z").decode().split("\0") if s]
    fixed = FIXED
    names = sorted(name for name in tracked if name in fixed or name.startswith(("src/", "tests/", "docs/decisions/")))
    if not fixed.issubset(names):
        raise ValueError(f"required freeze files are not committed: {sorted(fixed-set(names))}")
    changed = git("diff", "--name-only", "HEAD", "--", *names).decode().strip()
    if changed:
        raise ValueError("commit final frozen sources first: " + changed)
    untracked = git("ls-files", "--others", "--exclude-standard", "--", "src", "tests", "docs/decisions").decode().strip()
    if untracked:
        raise ValueError("uncommitted implementation/test files cannot be omitted: " + untracked)
    if names != execution_files():
        raise ValueError("execution inputs differ from committed files; commit or remove local additions first")
    normalized = [name for name in names if name.endswith(".py")
                  or (name.endswith(".md") and not name.startswith("tests/fixtures/"))
                  or name in {"pyproject.toml", "uv.lock", "config.example.toml"}]
    cycle_manifest = None
    if label.startswith("repair-cycle-"):
        from tests.helpers.phase_b_repair_cycle import formal_freeze_requirements
        cycle_manifest = formal_freeze_requirements(label, repair_evidence=cycle_repair_evidence)
        coverage_name = "docs/acceptance/phase-b/coverage-repair-cycle-20261008.json"
        if not with_science or model_profile != "disabled":
            raise ValueError("cycle formal freeze requires explicit science environment and disabled model profile")
    else:
        coverage_name = "docs/acceptance/phase-b/coverage.json"
    coverage = json.loads((PROJECT / coverage_name).read_text(encoding="utf-8"))
    revision = json.loads((PROJECT / "docs/acceptance/phase-b/coverage-repair-v2.json").read_text(encoding="utf-8"))
    if cycle_manifest is None and revision.get("final_matrix_complete") is not True:
        raise ValueError("repair coverage still has unmapped acceptance requirements; final freeze is premature")
    if cycle_manifest is None and (revision.get("development_gates_verified") is not True
            or revision.get("execution_budget_review_complete") is not True):
        raise ValueError("formal freeze requires verified development gates and cumulative execution budget review")
    allocation = {kind: [slot for item in coverage["entries"] if item["evidence_requirement"] == kind
                         for slot in item["formal_slots"]]
                  for kind in ("real_model_with_frozen_evidence", "joint_real_model_orca", "offline_fault_injection")}
    expected_count = len(cycle_manifest["coverage_bindings"]) if cycle_manifest else revision.get("final_slot_count")
    if expected_count != sum(len(slots) for slots in allocation.values()):
        raise ValueError("revised formal slot count differs from executable coverage")
    science_config = (evaluation_config(science=True, config_path=config_path, model_profile=model_profile)
                      if with_science else None)
    # One explicit effective mode covers both scopes, including a mode selected
    # in the scientific config file when the CLI does not override it.
    effective_profile = science_config.model_profile if science_config else model_profile
    model_config = evaluation_config(model_profile=effective_profile)
    environment = runtime_environment()
    record = {"schema_version": 2, "freeze_label": label, "code_commit": git("rev-parse", "HEAD").decode().strip(),
              "created_at": utc_now().isoformat(), "files": {name: freeze_hash(PROJECT/name, source_text=name in normalized) for name in names},
              "source_lf_normalization": normalized, "execution_files": names,
              "execution_environment": environment,
              "budget_authority": execution_budget_authority(),
              "configuration": {"model": model_config.model_dump(mode="json"),
                                "science": science_config.model_dump(mode="json") if science_config else None},
              "science_environment": science_environment(science_config) if science_config else None,
              "formal_slots": {"model": len(allocation["real_model_with_frozen_evidence"]),
                  "joint_trajectories": len({(s["joint_case"], s["repetition"]) for s in allocation["joint_real_model_orca"]}),
                  "joint_variant_slots": len(allocation["joint_real_model_orca"]),
                  "offline_variant_slots": len(allocation["offline_fault_injection"])}}
    if cycle_manifest is not None:
        record["cycle_coverage"] = {"path": coverage_name, "sha256": sha256_file(PROJECT / coverage_name)}
    atomic_write(FREEZE, (json.dumps(record, ensure_ascii=False, indent=2)+"\n").encode(), immutable=True)
    print(json.dumps(validate_freeze(label, config=model_config), ensure_ascii=False))


def offline(label, repetition, *, model_profile=None):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,37}", label):
        raise ValueError("freeze label must be a local identifier of at most 38 characters")
    binding = validate_freeze(label, config=evaluation_config(model_profile=model_profile))
    if label.startswith("repair-cycle-"):
        from tests.helpers.phase_b_repair_cycle import guard_formal_offline
        guard_formal_offline(label, repetition)
        coverage_path = PROJECT / "docs/acceptance/phase-b/coverage-repair-cycle-20261008.json"
        saved_freeze = json.loads(FREEZE.read_text(encoding="utf-8"))
        if saved_freeze.get("cycle_coverage") != {"path": coverage_path.relative_to(PROJECT).as_posix(), "sha256": sha256_file(coverage_path)}:
            raise ValueError("cycle offline matrix differs from formal source freeze")
    else:
        coverage_path = PROJECT/"docs/acceptance/phase-b/coverage.json"
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    slots = [slot for entry in coverage["entries"] if entry["evidence_requirement"] == "offline_fault_injection"
             for slot in entry["formal_slots"] if slot["repetition"] == repetition]
    if not slots:
        raise ValueError("frozen coverage has no offline slots for this repetition")
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
    if label.startswith("repair-cycle-"):
        from tests.helpers.phase_b_repair_cycle import record_formal_offline_receipt
        record_formal_offline_receipt(label, repetition, directory / "receipt.json")
    print(json.dumps({"receipt": str(directory/"receipt.json"), "returncode": completed.returncode, "nodes": len(nodes)}))
    return completed.returncode


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("freeze", "offline"))
    parser.add_argument("--label", default="formal-v1")
    parser.add_argument("--repetition", type=int, choices=(1, 2, 3))
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--config", type=Path, help="actual scientific configuration; no credentials")
    parser.add_argument("--with-science", action="store_true", help="bind doctor versions and executable hashes")
    parser.add_argument("--model-profile", choices=("disabled", "thinking_low"),
                        help="explicit model mode bound into the exact configuration freeze")
    args = parser.parse_args()
    if not args.execute:
        parser.error("operator must explicitly choose --execute; nothing changed")
    if args.mode == "freeze":
        freeze(args.label, config_path=args.config, with_science=args.with_science, model_profile=args.model_profile)
    else:
        if args.repetition is None:
            parser.error("offline invocation requires --repetition")
        os.environ["ORCA_AGENT_EVAL_FREEZE"] = args.label
        raise SystemExit(offline(args.label, args.repetition, model_profile=args.model_profile))
