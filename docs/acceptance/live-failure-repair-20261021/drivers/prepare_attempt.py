"""Capture an immutable development candidate; no model or calculation here."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "data/live-repair"
SNAPSHOTS = Path("C:/Users/阿妆/.codex/worktrees/live-repair/BG6022-V4/candidates")
TASKS = ("T1", "T2", "T3", "T4", "T5", "H1", "H2")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def source(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for folder in (root / "src", root / "scripts/container_runtime")
            for p in sorted(folder.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", choices=TASKS)
    parser.add_argument("--change", required=True, help="Concrete related fix, never a scientific solution")
    parser.add_argument("--source-candidate", help="Explicit stable captured version while development continues")
    parser.add_argument("--model-profile", choices=("disabled", "thinking_low"),
                        help="Explicit new-candidate profile; parent clones retain their frozen profile")
    parser.add_argument("--model-name", choices=("deepseek-flash", "deepseek-v4-pro"),
                        help="Explicit new-candidate model; parent clones retain their frozen model")
    args = parser.parse_args()
    capture_root = ROOT
    parent = None
    if args.source_candidate:
        import re
        assert re.fullmatch(r"a[0-9]{3}", args.source_candidate)
        parent = json.loads((CONTROL / "attempts" / args.source_candidate / "attempt.json").read_bytes())
        capture_root = SNAPSHOTS / args.source_candidate
        assert capture_root.resolve() == Path(parent["snapshot"]).resolve()
        assert all(digest((capture_root / name).read_bytes()) == value["sha256"]
                   for name, value in parent["source_files"].items())
    assert bool(os.environ.get("DEEPSEEK_API_KEY")), "Existing model credential is unavailable"
    old = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-4"
    prior = json.loads((old / "validation.json").read_bytes())
    campaign = CONTROL / "campaign.json"
    if not campaign.exists():
        save(campaign, {
            "version": 1, "authorization": "边真实LLM测试边修复，直到全部跑通；不做无意义全量回归",
            "baseline_validation": str(old / "validation.json"),
            "baseline_sha256": digest((old / "validation.json").read_bytes()),
            "baseline_usage": prior["cumulative_five_batch_known_usage"],
            "limits": {"model_calls": 600, "model_tokens": 30000000, "orca_starts": 390,
                       "internal_work_units": 858000, "wall_seconds": 321600},
        })
    policy = json.loads(campaign.read_bytes())
    assert policy["baseline_sha256"] == digest((old / "validation.json").read_bytes())
    attempts = sorted([*(CONTROL / "attempts").glob("a*/attempt.json"),
                       *(CONTROL / "probes").glob("*/probe.json")])
    usage = dict(policy["baseline_usage"])
    for previous in attempts:
        outcome = previous.parent / "outcome.json"
        if not outcome.exists():
            raise RuntimeError("Prior attempt is still active or needs reconciliation")
        settled = json.loads(outcome.read_bytes())
        correction_path = previous.parent / "accounting-reconciliation.json"
        correction = json.loads(correction_path.read_bytes()) if correction_path.exists() else {}
        if correction:
            assert correction["source_outcome_sha256"] == digest(outcome.read_bytes())
        unknown = set(settled["unknown_fields"]) | set(correction.get("unknown_fields", []))
        assert unknown <= {"model_tokens", "orca_starts"}, "Unknown execution requires reconciliation"
        assert not settled["unsettled_execution"]
        if "orca_starts" in unknown:
            rows = correction.get("unconfirmed_start_accounting", settled.get("unconfirmed_start_accounting", []))
            assert rows and all(row["execution_terminated"] for row in rows)
        for key, value in settled["known_usage"].items():
            usage[key] += value
        usage["model_tokens"] += settled["usage"]["model_tokens_unknown"]
        for key, value in correction.get("unknown_usage_reservations", settled.get("unknown_usage_reservations", {})).items():
            assert key == "orca_starts" and type(value) is int and 0 <= value <= settled["usage"]["orca_starts_reserved"]
            usage[key] += value
    config = tomllib.loads((old / "freeze/runtime.toml").read_text(encoding="utf-8"))
    runtime_selection = CONTROL / "runtime/selection.json"
    if parent:
        assert args.model_profile is None, "A parent clone must retain its exact model profile"
        assert args.model_name is None, "A parent clone must retain its exact model name"
        previous_config = CONTROL / "attempts" / args.source_candidate / "configuration.json"
        assert digest(previous_config.read_bytes()) == parent["configuration_sha256"]
        parent_config = json.loads(previous_config.read_bytes())
        config = json.loads(json.dumps(parent_config))
    elif runtime_selection.exists():
        selected = json.loads(runtime_selection.read_bytes())
        assert selected["verified_live"] is True
        config["execution"].update({key: selected[key] for key in ("image_id", "manifest_sha256")})
        if selected.get("python_runtime_layout") is not None:
            assert selected["python_runtime_layout"] == "isolated-1"
            config["execution"]["python_runtime_layout"] = selected["python_runtime_layout"]
    if parent is None:
        config["observation_delivery_policy"] = "reported-observation-2"
        config["task_code_policy"] = "nonempty-python-1"
        config["execution"]["native_input_policy"]["property_block_order"] = "coordinates-first-1"
        config["text"]["budget"].update({"logical_steps": 32, "model_calls": 48,
            "decision_rounds": 80, "plan_revisions": 32, "analysis_executions": 12,
            "code_retries": 11, "attempts_per_step": 12, "evidence_reads": 24,
            "knowledge_queries": 24})
        if args.model_profile is not None:
            config["model_profile"] = args.model_profile
        if args.model_name is not None:
            config["model"]["model_name"] = args.model_name
        config["text"]["budget"]["corrections_per_proposal"] = config["text"]["budget"]["model_calls"] - 1
    budget = config["text"]["budget"]
    reservation = {"model_calls": budget["model_calls"], "model_tokens": budget["model_tokens"],
                   "orca_starts": budget["orca_starts"], "internal_work_units": budget["internal_work_units"],
                   "wall_seconds": budget["run_seconds"]}
    assert all(usage[k] + value <= policy["limits"][k] for k, value in reservation.items()), "Campaign budget exhausted"
    identifiers = [int(p.name[1:]) for p in SNAPSHOTS.glob("a[0-9][0-9][0-9]")]
    candidate = "a" + str(max(identifiers, default=0) + 1).zfill(3)
    snapshot = SNAPSHOTS / candidate
    attempt = CONTROL / "attempts" / candidate
    assert not snapshot.exists() and not attempt.exists()
    captured = source(capture_root)
    assert captured == source(capture_root), "Sources changed during capture; retry after the edit settles"
    snapshot.mkdir(parents=True)
    for name, raw in captured.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    for name in ("pyproject.toml", "uv.lock"):
        (snapshot / name).write_bytes((capture_root / name).read_bytes())
    driver = snapshot / "run_attempt.py"
    driver.write_bytes((CONTROL / "run_attempt.py").read_bytes())
    config["data_root"] = str(attempt / "data")
    task = next(row for row in json.loads((old / "freeze/public-tasks.json").read_bytes())["tasks"]
                if row["id"] == args.task) if args.task.startswith("T") else next(
        row for row in json.loads((old / "freeze/holdout-selection.json").read_bytes())["tasks"]
        if row["id"] == args.task)
    original_path = ROOT / f"data/code-tools-refreeze-4-batch/workspaces/formal-{args.task}-001/original.txt"
    original_raw = original_path.read_bytes()
    original = original_raw.decode("utf-8")
    prior_start = json.loads((old / f"evidence/{args.task}/start.json").read_bytes())
    assert digest(original_raw) == prior_start["original_sha256"]
    assert original.startswith(task["original_text"])
    venv = snapshot / ".venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    site = venv / "Lib/site-packages"
    (site / "candidate.pth").write_text(str(snapshot / "src") + "\n" +
        str(ROOT / ".venv/Lib/site-packages") + "\n", encoding="locale")
    python = venv / "Scripts/python.exe"
    check = subprocess.check_output([str(python), "-c", "import orca_agent;print(orca_agent.__file__)"],
                                    encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"}).strip()
    assert Path(check).resolve().is_relative_to(snapshot.resolve())
    save(attempt / "configuration.json", config)
    preflight = subprocess.run([str(python), "-c", (
        "import json,sys;from pathlib import Path;from orca_agent.config import Config;"
        "from orca_agent.backends.container import inspect_environment;"
        "c=Config.model_validate_json(Path(sys.argv[1]).read_bytes());r=inspect_environment(c.execution);"
        "print(json.dumps({'verified':r['verified'],'issues':r['issues']}));sys.exit(0 if r['verified'] else 2)"
    ), str(attempt / "configuration.json")], capture_output=True, encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"}, timeout=60)
    if preflight.returncode:
        raise RuntimeError("Captured runtime failed read-only preflight: " + preflight.stdout[:2000])
    save(attempt / "environment-preflight.json", json.loads(preflight.stdout))
    attempt.joinpath("original.txt").write_text(original, encoding="utf-8", newline="\n")
    environment = json.loads((old / "freeze/product-freeze-event.json").read_bytes())["environment_root"]
    manifest = {"candidate": candidate, "task": args.task, "change": args.change,
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_commit": parent["base_commit"] if parent else subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_candidate": args.source_candidate,
        "parent_source_manifest_sha256": parent["source_manifest_sha256"] if parent else None,
        "snapshot": str(snapshot), "python": str(python), "environment_root": environment,
        "driver": str(driver), "driver_sha256": digest(driver.read_bytes()),
        "source_files": {name: {"sha256": digest(raw), "bytes": len(raw)} for name, raw in captured.items()},
        "source_manifest_sha256": digest(json.dumps({name: digest(raw) for name, raw in captured.items()},
            sort_keys=True, separators=(",", ":")).encode()),
        "configuration_sha256": digest((attempt / "configuration.json").read_bytes()),
        "original_sha256": digest((attempt / "original.txt").read_bytes()),
        "campaign": str(campaign), "known_usage_before": usage, "reservation": reservation,
        "reference_or_control_execution": False,
    }
    save(attempt / "attempt.json", manifest)
    print(json.dumps({"attempt": str(attempt), "python": str(python),
                      "task": args.task, "source_manifest_sha256": manifest["source_manifest_sha256"]}))


if __name__ == "__main__":
    main()
