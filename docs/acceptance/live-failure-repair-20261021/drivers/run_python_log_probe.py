"""p002: one explicitly budgeted Python log-namespace probe, never a model/ORCA run.

Prepared for root invocation only after a026 and every other candidate has settled.
Required CLI hashes bind the reviewed product source, parent ledger and new image.
No retries; an existing p002 must be reconciled, never overwritten or replayed.
"""

import argparse
import hashlib
import json
import math
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "data/live-repair"
PROBE = CONTROL / "probes/p002"
RESERVATION = {"model_calls": 0, "model_tokens": 0, "orca_starts": 0,
               "internal_work_units": 0, "wall_seconds": 120}
INPUTS = {"stdout.out": b"p002 immutable prior native stdout\n",
          "stderr.txt": b"p002 immutable prior native stderr\n",
          ".launch.json": b'{"data_only":"p002 immutable task input, not runtime identity"}\n'}
STDOUT_MARKER = "P002_PYTHON_STDOUT"
STDERR_MARKER = "P002_PYTHON_STDERR"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read(path):
    return json.loads(path.read_bytes())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def sources():
    return {path.relative_to(ROOT).as_posix(): digest(path.read_bytes())
            for folder in (ROOT / "src", ROOT / "scripts/container_runtime")
            for path in sorted(folder.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}}


def manifest_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def safe_error(exc):
    return {"type": type(exc).__name__, "messages_or_headers_recorded": False,
            "frames": [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                       for frame in traceback.extract_tb(exc.__traceback__)[-8:]]}


def campaign_basis(parent_path, expected_hash):
    require(digest(parent_path.read_bytes()) == expected_hash, "Parent ledger hash changed")
    parent = read(parent_path)
    campaign_path = CONTROL / "campaign.json"
    campaign = read(campaign_path)
    require(digest(campaign_path.read_bytes()) == parent["campaign"]["sha256"], "Campaign changed")
    require((CONTROL / "attempts/a026/outcome.json").is_file(), "a026 must be settled before p002")
    baseline = Path(campaign["baseline_validation"])
    require(digest(baseline.read_bytes()) == campaign["baseline_sha256"], "Campaign baseline changed")
    usage = dict(campaign["baseline_usage"])
    pinned = [parent_path, campaign_path, baseline]
    manifests = sorted([*(CONTROL / "attempts").glob("a*/attempt.json"),
                        *(CONTROL / "probes").glob("*/probe.json")])
    for path in manifests:
        outcome_path = path.parent / "outcome.json"
        require(outcome_path.is_file(), "A prior candidate/probe is active or unaccounted")
        outcome = read(outcome_path)
        correction_path = path.parent / "accounting-reconciliation.json"
        correction = read(correction_path) if correction_path.exists() else {}
        if correction:
            require(correction["source_outcome_sha256"] == digest(outcome_path.read_bytes()), "Correction source mismatch")
            pinned.append(correction_path)
        unknown = set(outcome["unknown_fields"]) | set(correction.get("unknown_fields", []))
        require(unknown <= {"model_tokens", "orca_starts"} and not outcome["unsettled_execution"],
                "Prior execution needs reconciliation")
        if "orca_starts" in unknown:
            intervals = correction.get("unconfirmed_start_accounting", outcome.get("unconfirmed_start_accounting", []))
            require(intervals and all(row["execution_terminated"] for row in intervals), "Unknown start not terminated")
        for key, value in outcome["known_usage"].items():
            usage[key] += value
        usage["model_tokens"] += outcome["usage"]["model_tokens_unknown"]
        for key, value in correction.get("unknown_usage_reservations", outcome.get("unknown_usage_reservations", {})).items():
            require(key == "orca_starts" and type(value) is int
                    and 0 <= value <= outcome["usage"]["orca_starts_reserved"], "Invalid unknown reservation")
            usage[key] += value
        pinned.extend((path, outcome_path))
    occupied_keys = [key for key in parent["totals"] if key.startswith("budget_occupied_through_")]
    require(len(occupied_keys) == 1, "Parent ledger needs one occupied cumulative total")
    occupied = parent["totals"][occupied_keys[0]]
    require(all(math.isclose(usage[key], occupied[key], rel_tol=0, abs_tol=1e-6) for key in usage),
            "Parent ledger must include every settled candidate/probe and unknown reservation")
    require(all(usage[key] + RESERVATION[key] <= campaign["limits"][key] for key in usage), "Probe budget unavailable")
    return parent, usage, pinned


def create_case(store, config):
    from orca_agent.models import BudgetLimits, Goal, OutputBinding, PermissionSnapshot, Plan, Request, Step

    folder = PROBE / "inputs"
    folder.mkdir(exist_ok=False)
    artifacts = {}
    for name, content in INPUTS.items():
        (folder / name).write_bytes(content)
        artifacts[name] = store.import_artifact(folder / name, "probe_input", source={"purpose": "p002_runtime_probe"})
    expected = {name: digest(raw) for name, raw in INPUTS.items()}
    source = ("import hashlib, json, sys\nfrom pathlib import Path\n"
              f"expected = {expected!r}\n"
              "observed = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in expected}\n"
              "assert observed == expected\n"
              f"print({STDOUT_MARKER!r}, flush=True)\n"
              "print(json.dumps(observed, sort_keys=True, separators=(',', ':')), flush=True)\n"
              f"print({STDERR_MARKER!r}, file=sys.stderr, flush=True)\n")
    program = folder / "probe.py"
    program.write_text(source, encoding="utf-8", newline="\n")
    code = store.import_artifact(program, "code", source={"purpose": "p002_runtime_probe"})
    request = Request(original_text="Developer Python runtime log namespace probe; no science requested.",
        goals=[Goal(id="runtime_evidence", port="artifacts", quantity="raw runtime evidence",
            evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
            verification="reported_with_limitations")])
    step = Step(id="python_log_namespace", logical_id="python_log_namespace", tool="python.execute", parameters={
        "code": {"artifact_id": code.id, "sha256": code.sha256, "language": "python"}, "sdk_orca": False,
        "inputs": {"version": 1, "entries": [{"artifact_id": artifact.id, "sha256": artifact.sha256,
            "target_path": name, "purpose": "immutable probe input"} for name, artifact in artifacts.items()]}})
    plan = Plan(request_id=request.id, steps=[step], goal_map={"runtime_evidence": OutputBinding(step_id=step.id, port="artifacts")})
    budget = BudgetLimits(logical_steps=1, attempts_per_step=1, orca_starts=0, extra_orca_starts=0,
        internal_work_units=0, run_seconds=90, file_writes=0, artifact_count=64,
        artifact_bytes=1024*1024, artifact_total_bytes=4*1024*1024,
        model_calls=0, model_tokens=0, analysis_executions=1, code_retries=0)
    permission = PermissionSnapshot(scientific_execution=False, code_execution=True, artifact_writes=True,
        model_execution=False, allowed_tools=["python.execute"], max_cores=4, max_memory_mb=1024,
        artifact_ids=[code.id, *(artifact.id for artifact in artifacts.values())])
    run = store.create_run(request, plan, permission, budget, configuration=config.run_configuration())
    save(PROBE / "case.json", {"run_id": run.id, "step_id": step.id, "code_sha256": code.sha256,
        "code_artifact_id": code.id, "input_artifacts": {name: {"artifact_id": artifact.id, "sha256": artifact.sha256}
            for name, artifact in artifacts.items()}, "scientific_qualification": "not_requested"})
    return run, step, artifacts


def inspect_case(store, run, result, input_artifacts):
    require(len(run.attempts) == 1 and not run.internal_calls and not run.model_records, "Unexpected work occurred")
    attempt = run.attempts[0]
    require(attempt.state == result.operation_status == "completed", "Python probe did not complete")
    require(result.observations["evidence_collection_complete"] is True, "Collection is incomplete")
    require(run.usage.orca_starts_actual == run.usage.orca_starts_reserved == run.usage.internal_work_units == 0,
            "Probe consumed forbidden scientific work")
    execution = result.source["execution"]
    require(execution["exit_code"] == 0, "Python exit is not zero")
    files = result.source["files"]
    expected_keys = [*("task/" + name for name in INPUTS), "runtime-logs/stdout.out", "runtime-logs/stderr.txt"]
    require(all(key in files for key in expected_keys), "Required task/runtime files were not archived separately")
    retained, content = {}, {}
    for key in expected_keys:
        reference = files[key]
        artifact = store.load_artifact(reference["artifact_id"])
        require(artifact.id in result.artifact_ids and artifact.run_id == run.id
                and artifact.attempt_id == attempt.id and artifact.source.get("task_path") == key,
                "Archived file owner/role differs")
        raw = store.artifact_path(artifact.id).read_bytes()
        require(digest(raw) == artifact.sha256 == reference["sha256"], "Archived bytes differ from reference")
        retained[key] = {**reference, "bytes": len(raw)}
        content[key] = raw
    require(len({row["artifact_id"] for row in retained.values()}) == len(expected_keys), "File identities overlap")
    for name, raw in INPUTS.items():
        require(content["task/" + name] == raw and store.artifact_path(input_artifacts[name].id).read_bytes() == raw,
                "Original or staged input changed")
    lines = content["runtime-logs/stdout.out"].decode("utf-8").splitlines()
    require(len(lines) == 2 and lines[0] == STDOUT_MARKER, "Runtime stdout marker differs")
    require(json.loads(lines[1]) == {name: digest(raw) for name, raw in INPUTS.items()}, "Child input hashes differ")
    require(content["runtime-logs/stderr.txt"].decode("utf-8").splitlines() == [STDERR_MARKER], "Runtime stderr marker differs")
    require(store.environment_lease() is None, "Execution lease remains")
    return {"passed": True, "run_id": run.id, "attempt_id": attempt.id, "result_id": result.id,
        "registered_files": retained, "runtime_stdout_marker": STDOUT_MARKER, "runtime_stderr_marker": STDERR_MARKER,
        "input_hashes_unchanged": True, "execution": execution,
        "scientific_qualification": "not_requested; runtime behavior only"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-result", type=Path, required=True)
    parser.add_argument("--runtime-manifest", type=Path, required=True)
    parser.add_argument("--source-manifest-sha256", required=True)
    parser.add_argument("--parent-ledger", type=Path, required=True)
    parser.add_argument("--parent-ledger-sha256", required=True)
    args = parser.parse_args()
    require(not PROBE.exists(), "p002 already exists; reconcile, never replay")
    require(all(re.fullmatch(r"[a-f0-9]{64}", value) for value in
        (args.source_manifest_sha256, args.parent_ledger_sha256)), "Explicit reviewed hashes are required")
    captured, driver_hash = sources(), digest(Path(__file__).read_bytes())
    require(manifest_hash(captured) == args.source_manifest_sha256, "Product source differs from reviewed source")
    parent, before, pinned = campaign_basis(args.parent_ledger.resolve(), args.parent_ledger_sha256)
    build_path, manifest_path = args.build_result.resolve(), args.runtime_manifest.resolve()
    build, runtime_manifest = read(build_path), read(manifest_path)
    require(digest(manifest_path.read_bytes()) == build["manifest_sha256"], "Runtime manifest changed")
    for name in ("helper.py", "python_task.py"):
        require(runtime_manifest["files"]["/opt/orca-agent-runtime/" + name]["sha256"]
                == digest((ROOT / "src/orca_agent/backends/container_runtime" / name).read_bytes()),
                "Built runtime helper differs from reviewed source")
    prior_folder = CONTROL / "attempts/a026"
    prior = read(prior_folder / "attempt.json")
    prior_config = prior_folder / "configuration.json"
    require(digest(prior_config.read_bytes()) == prior["configuration_sha256"], "a026 configuration changed")
    environment_root = Path(prior["environment_root"])
    os.environ["LOCALAPPDATA"] = str(environment_root.parent.parent)
    sys.path.insert(0, str(ROOT / "src"))
    import orca_agent
    from orca_agent import agent
    from orca_agent.config import Config
    from orca_agent.store import Store, default_environment_root

    require(Path(orca_agent.__file__).resolve().is_relative_to(ROOT / "src"), "Wrong product import")
    require(default_environment_root().resolve() == environment_root.resolve(), "Execution environment changed")
    values = read(prior_config)
    values["execution"].update({key: build[key] for key in ("image_id", "manifest_sha256")})
    values["execution"]["python_runtime_layout"] = "isolated-1"
    values["resources"].update(script_timeout_seconds=30, collection_timeout_seconds=30)
    values["model_profile"] = "disabled"
    values["data_root"] = str(PROBE / "data")
    config = Config.model_validate(values)
    require((config.resources.max_cores, config.resources.total_memory_mb, config.resources.maxcore_mb,
             config.resources.environment_concurrency) == (4, 1024, 192, 1), "Resource envelope differs")
    pinned.extend((prior_config, prior_folder / "attempt.json", build_path, manifest_path))
    pinned_hashes = {str(path): digest(path.read_bytes()) for path in pinned}

    def verify():
        require(sources() == captured and digest(Path(__file__).read_bytes()) == driver_hash, "Source/driver changed")
        require(all(digest(Path(path).read_bytes()) == value for path, value in pinned_hashes.items()), "Pinned input changed")

    # Inspect an existing Store; do not create the p002 data directory until its
    # external reservation has been saved. Ordinary reservation rechecks the lease.
    prior_store = Store(prior_folder / "data", environment_root=environment_root)
    require(not prior_store.environment_lease(), "Environment is busy; do not start p002")
    verify()
    save(PROBE / "probe.json", {"probe": "p002", "purpose": "0077 Python runtime namespace separation",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "command_argv": sys.argv,
        "environment_root": str(environment_root), "reservation": RESERVATION,
        "budget_occupied_before": before, "prior_totals": parent["totals"],
        "parent_ledger": {"path": str(args.parent_ledger.resolve()), "sha256": args.parent_ledger_sha256},
        "source_files": captured, "source_manifest_sha256": manifest_hash(captured),
        "driver": str(Path(__file__).resolve()), "driver_sha256": driver_hash, "pinned_inputs": pinned_hashes,
        "runtime": {key: build[key] for key in ("image_id", "manifest_sha256")},
        "python_runtime_layout": "isolated-1", "scientific_qualification": "not_requested"})
    started = time.monotonic()
    store, run_id, checked, error, unchanged = None, None, None, None, False
    try:
        save(PROBE / "configuration.json", config.model_dump(mode="json"))
        verify()
        store = Store(config.data_root, environment_root=environment_root, configuration=config.run_configuration())
        run, step, inputs = create_case(store, config)
        run_id = run.id
        save(PROBE / "start.json", {"run_id": run_id, "started_at_utc": datetime.now(timezone.utc).isoformat()})
        with store.run_lock(run_id):
            verify()
            require(time.monotonic() - started < 30, "Insufficient full 90-second Run reservation remains")
            result, _ = agent._science(store, config, run, step, {}, None, None)
            run = store.load_run(run_id)
            checked = inspect_case(store, run, result, inputs)
            save(PROBE / "case-result.json", checked)
    except BaseException as exc:
        error = safe_error(exc)
    finally:
        try:
            verify()
            unchanged = True
        except Exception as exc:
            error = error or safe_error(exc)
        known = dict.fromkeys(RESERVATION, 0)
        usage = {"model_tokens_unknown": 0, "orca_starts_reserved": 0}
        unknown, unsettled, runs = [], [], []
        try:
            if run_id:
                run = store.load_run(run_id)
                usage = run.usage.model_dump(mode="json")
                for key, field in (("model_calls", "model_calls"), ("model_tokens", "model_tokens_used"),
                                   ("orca_starts", "orca_starts_actual"), ("internal_work_units", "internal_work_units")):
                    known[key] = usage[field]
                unsettled = [attempt.id for attempt in run.attempts if attempt.state in {"intent", "running", "unknown"}
                             or attempt.finished_at is None]
                unsettled += [call.request_key for call in run.internal_calls if call.state in {"reserved", "running", "unknown"}
                              or call.finished_at is None]
                runs = [{"run_id": run_id, "state": run.state, "usage": usage}]
            if store is not None and store.environment_lease():
                unsettled.append("environment_lease_retained")
        except Exception as exc:
            error = error or safe_error(exc)
            unsettled.append("probe_accounting_unavailable")
        if unsettled:
            unknown = ["orca_starts", "internal_work_units"]
        known["wall_seconds"] = time.monotonic() - started
        exceeded = [key for key in known if known[key] > RESERVATION[key]]
        passed = bool(checked and checked["passed"] and not (error or unknown or exceeded) and unchanged)
        outcome = {"probe": "p002", "finished_at_utc": datetime.now(timezone.utc).isoformat(),
            "state": "completed" if passed else "failed", "runtime_probe_passed": passed,
            "run_ids": [run_id] if run_id else [], "runs": runs, "known_usage": known, "usage": usage,
            "unknown_fields": unknown, "unsettled_execution": unsettled, "unconfirmed_start_accounting": [],
            "unknown_usage_reservations": {"orca_starts": 0}, "reservation_exceeded": exceeded,
            "error": error, "source_unchanged": unchanged, "source_manifest_sha256": manifest_hash(captured),
            "driver_sha256": driver_hash, "scientific_qualification": "not_requested",
            "independent_reference": "not_run", "control_execution": "not_run"}
        save(PROBE / "outcome.json", outcome)
        print(json.dumps({"state": outcome["state"], "known_usage": known, "unknown_fields": unknown,
            "unsettled_execution": unsettled, "source_unchanged": unchanged}, ensure_ascii=False), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
