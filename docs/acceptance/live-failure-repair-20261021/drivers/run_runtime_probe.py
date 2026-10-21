"""Two explicitly budgeted runtime probes; run only after a005 has settled.

Usage: .venv/Scripts/python.exe -X utf8 data/live-repair/run_runtime_probe.py
       --source-manifest-sha256 <reviewed stable src + scripts/container_runtime hash>
No LLM, task repair, retries, or scientific qualification is performed here.
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
PROBE = CONTROL / "probes/p001"
RESERVATION = {"model_calls": 0, "model_tokens": 0, "orca_starts": 2,
               "internal_work_units": 128, "wall_seconds": 600}
XYZ = b"2\nDeveloper runtime probe: pinned H2 at 0.74 angstrom\nH 0 0 0\nH 0 0 0.74\n"


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
    return {p.relative_to(ROOT).as_posix(): digest(p.read_bytes())
            for folder in (ROOT / "src", ROOT / "scripts/container_runtime")
            for p in sorted(folder.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".pyo"}}


def manifest_hash(values):
    return digest(json.dumps(values, sort_keys=True, separators=(",", ":")).encode())


def safe_error(exc):
    return {"type": type(exc).__name__, "messages_or_headers_recorded": False,
            "frames": [{"file": Path(f.filename).name, "function": f.name, "line": f.lineno}
                       for f in traceback.extract_tb(exc.__traceback__)[-8:]]}


def settled_cost(manifest_path):
    outcome_path = manifest_path.parent / "outcome.json"
    require(outcome_path.exists(), "Prior attempt/probe is active or needs reconciliation")
    outcome = read(outcome_path)
    correction_path = manifest_path.parent / "accounting-reconciliation.json"
    correction = read(correction_path) if correction_path.exists() else {}
    if correction:
        require(correction["source_outcome_sha256"] == digest(outcome_path.read_bytes()),
                "Accounting correction does not bind its original outcome")
    unknown = set(outcome["unknown_fields"]) | set(correction.get("unknown_fields", []))
    require(unknown <= {"model_tokens", "orca_starts"} and not outcome["unsettled_execution"],
            "Prior execution still needs reconciliation")
    if "orca_starts" in unknown:
        rows = correction.get("unconfirmed_start_accounting", outcome.get("unconfirmed_start_accounting", []))
        require(rows and all(row["execution_terminated"] for row in rows),
                "Unknown start has no confirmed termination")
    cost = dict(outcome["known_usage"])
    cost["model_tokens"] += outcome["usage"]["model_tokens_unknown"]
    reservations = correction.get("unknown_usage_reservations", outcome.get("unknown_usage_reservations", {}))
    for key, value in reservations.items():
        require(key == "orca_starts" and type(value) is int
                and 0 <= value <= outcome["usage"]["orca_starts_reserved"],
                "Invalid unknown usage reservation")
        cost[key] += value
    return cost, [outcome_path, *([correction_path] if correction else [])]


def campaign_basis():
    manifest_path = CONTROL / "attempts/a005/attempt.json"
    meta = read(manifest_path)
    campaign_path = CONTROL / "campaign.json"
    campaign = read(campaign_path)
    baseline = Path(campaign["baseline_validation"])
    require(digest(baseline.read_bytes()) == campaign["baseline_sha256"], "Campaign baseline changed")
    previous = sorted((CONTROL / "attempts").glob("a*/attempt.json"))
    require(previous and previous[-1] == manifest_path, "a005 must remain the latest development attempt")
    require(not list((CONTROL / "probes").glob("*/probe.json")), "p001 is a one-time probe; never replay it")
    before = dict(campaign["baseline_usage"])
    pinned = [manifest_path, campaign_path, baseline]
    for path in previous[:-1]:
        cost, receipts = settled_cost(path)
        pinned.extend([path, *receipts])
        for key in before:
            before[key] += cost[key]
    require(all(math.isclose(before[key], meta["known_usage_before"][key], rel_tol=0, abs_tol=1e-6)
                for key in before), "a005 prior usage does not include current historical reservations")
    cost, receipts = settled_cost(manifest_path)
    pinned.extend(receipts)
    usage = {key: meta["known_usage_before"][key] + cost[key] for key in before}
    require(all(usage[key] + value <= campaign["limits"][key] for key, value in RESERVATION.items()),
            "Campaign does not have the complete probe reservation available")
    return meta, usage, pinned


def create_case(store, config, name, nuclear, inputs_root):
    from orca_agent.models import (
        BudgetLimits,
        Goal,
        OutputBinding,
        PermissionSnapshot,
        Plan,
        Request,
        Step,
    )

    directory = inputs_root / name
    directory.mkdir(parents=True, exist_ok=False)
    xyz_path = directory / "initial.xyz"
    xyz_path.write_bytes(XYZ)
    keyword = "NMR" if nuclear else "SP"
    text = (f"! HF STO-3G RHF {keyword} NoAutoStart\n%pal nprocs 4 end\n%maxcore 192\n"
            "%scf MaxIter 64 AutoTRAH false end\n* xyzfile 0 1 initial.xyz\n")
    if nuclear:
        text += "%eprnmr\n  Nuclei = all\nend\n"
    input_path = directory / "native.inp"
    input_path.write_text(text, encoding="ascii", newline="\n")
    geometry = store.import_artifact(xyz_path, "geometry", source={"purpose": "developer_runtime_probe"})
    code = store.import_artifact(input_path, "native_input", source={"purpose": "developer_runtime_probe"})
    request = Request(original_text="Developer runtime boundary probe; raw execution evidence only.",
        geometry_artifact_id=geometry.id, method="HF", basis="STO-3G", charge=0, multiplicity=1,
        conditions={"electronic_state": "RHF", "environment": "gas_phase"},
        conditions_source={key: "explicit" for key in (
            "method", "basis", "charge", "multiplicity", "geometry", "electronic_state", "environment")},
        goals=[Goal(id="runtime_evidence", port="artifacts", quantity="raw runtime evidence",
            evidence_contract_version="generic-observation-1", minimum_check_version="generic-observation-1",
            verification="reported_with_limitations")])
    step = Step(id=name, logical_id=name, tool="orca.run", parameters={
        "input": {"artifact_id": code.id, "sha256": code.sha256, "language": "orca"},
        "inputs": {"version": 1, "entries": [{"artifact_id": geometry.id, "sha256": geometry.sha256,
            "target_path": "initial.xyz", "purpose": "pinned_initial_geometry"}]}})
    plan = Plan(request_id=request.id, steps=[step],
                goal_map={"runtime_evidence": OutputBinding(step_id=step.id, port="artifacts")})
    budget = BudgetLimits(logical_steps=1, attempts_per_step=1, orca_starts=1, extra_orca_starts=0,
        internal_work_units=64, run_seconds=300, file_writes=0, artifact_count=200,
        model_calls=0, model_tokens=0, analysis_executions=0)
    permission = PermissionSnapshot(scientific_execution=True, artifact_writes=True,
        model_execution=False, code_execution=False, allowed_tools=["orca.run"],
        max_cores=4, max_memory_mb=1024, artifact_ids=[geometry.id, code.id])
    run = store.create_run(request, plan, permission, budget, configuration=config.run_configuration())
    save(directory / "case.json", {"case": name, "run_id": run.id, "step_id": step.id,
        "input_sha256": code.sha256, "geometry_sha256": geometry.sha256,
        "input_artifact_id": code.id, "geometry_artifact_id": geometry.id,
        "expected": "confirmed nonzero child exit" if nuclear else "confirmed four-rank normal termination",
        "scientific_qualification": "not_requested"})
    return run, step


def inspect_case(store, run, result, nuclear):
    attempt, = run.attempts
    execution = result.source.get("execution", {})
    proof = execution.get("handle", {}).get("actual_start_evidence", {})
    files = {}
    contents = {}
    for key in ("task/stdout.out", "task/stderr.txt"):
        entry = result.source.get("files", {}).get(key)
        require(isinstance(entry, dict) and entry.get("artifact_id") in result.artifact_ids,
                "Raw stdout/stderr is not discoverable in the Result")
        path = store.artifact_path(entry["artifact_id"])
        raw = path.read_bytes()
        require(digest(raw) == entry["sha256"], "Raw execution evidence hash mismatch")
        files[key] = {"artifact_id": entry["artifact_id"], "sha256": digest(raw), "path": str(path)}
        contents[key] = raw.decode("utf-8", errors="replace")
    checks = {
        "actual_start_confirmed": execution.get("actual_start_status") == "confirmed" and attempt.started,
        "trusted_start_proof": proof.get("method") == "proc_exe_inode_and_pid_start_ticks" or (
            proof.get("method") == "trusted_subprocess_exec_error_pipe"
            and proof.get("exec_error_pipe") == "closed_without_error"
            and type(proof.get("child_exit_code")) is int and proof["child_exit_code"] >= 0),
        "settled": attempt.finished_at is not None and attempt.state in {"completed", "failed"},
        "no_live_process": execution.get("resource_usage", {}).get("active_processes") == 0,
        "evidence_complete": result.observations.get("evidence_collection_complete") is True,
        "no_scientific_upgrade": not result.qualified_outputs,
    }
    if nuclear:
        checks["real_child_failed"] = (attempt.state == result.operation_status == "failed"
            and type(execution.get("exit_code")) is int and execution["exit_code"] != 0
            and execution.get("not_started") is False)
        checks["raw_failure_visible"] = bool(contents["task/stdout.out"].strip()
            and contents["task/stderr.txt"].strip())
        energy = None
    else:
        stdout = contents["task/stdout.out"]
        match = re.search(r"FINAL SINGLE POINT ENERGY\s+([-+0-9.EeDd]+)", stdout)
        energy = float(match[1].replace("D", "E").replace("d", "e")) if match else None
        checks.update(normal_termination="ORCA TERMINATED NORMALLY" in stdout,
            four_mpi_ranks=bool(re.search(r"4\s+parallel MPI-processes", stdout)),
            finite_raw_energy=energy is not None and math.isfinite(energy),
            zero_exit=attempt.state == result.operation_status == "completed" and execution.get("exit_code") == 0)
    return {"run_id": run.id, "attempt_id": attempt.id, "result_id": result.id,
        "checks": checks, "passed": all(checks.values()), "files": files,
        "raw_energy_hartree": energy, "energy_verification": "unverified raw output observation",
        "execution": execution, "proof_method": proof.get("method"),
        "run_state": run.state, "goal_status": run.goal_status}


def accounting(store, run_ids):
    known = {key: 0 for key in RESERVATION}
    usage = {"model_tokens_unknown": 0, "orca_starts_reserved": 0}
    unknown, unsettled, unconfirmed, runs = set(), [], [], []
    for run_id in run_ids:
        try:
            run = store.load_run(run_id)
            current = run.usage.model_dump(mode="json")
            runs.append({"run_id": run_id, "state": run.state, "usage": current})
            for key, field in (("model_calls", "model_calls"), ("model_tokens", "model_tokens_used"),
                               ("orca_starts", "orca_starts_actual"), ("internal_work_units", "internal_work_units")):
                known[key] += current[field]
            for key in usage:
                usage[key] += current[key]
            for attempt in run.attempts:
                if attempt.state in {"intent", "running", "unknown"} or attempt.finished_at is None:
                    unsettled.append(attempt.id)
                if not attempt.result_id:
                    continue
                result = store.load_result(run_id, attempt.result_id)
                execution = result.source.get("execution", {})
                if execution.get("actual_start_status") == "unconfirmed":
                    terminated = (attempt.finished_at is not None and attempt.state in {
                        "failed", "completed", "cancelled", "timed_out"}
                        and execution.get("state") == attempt.state
                        and execution.get("resource_usage", {}).get("active_processes") == 0)
                    unconfirmed.append({"run_id": run_id, "attempt_id": attempt.id, "result_id": result.id,
                                        "execution_terminated": terminated, "start_count_interval": [0, 1]})
                    unknown.add("orca_starts")
                    if not terminated:
                        unsettled.append(attempt.id)
            unsettled.extend(call.request_key for call in run.internal_calls
                             if call.state in {"reserved", "running", "unknown"} or call.finished_at is None)
        except Exception as exc:
            runs.append({"run_id": run_id, "accounting_error": safe_error(exc)})
            unsettled.append(run_id + ":accounting_unavailable")
    if unsettled:
        unknown.update(("orca_starts", "internal_work_units"))
    if usage["model_tokens_unknown"]:
        unknown.add("model_tokens")
    return known, usage, sorted(unknown), sorted(set(unsettled)), unconfirmed, runs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-manifest-sha256", required=True)
    args = parser.parse_args()
    require(not PROBE.exists(), "p001 already exists; reconcile it, never overwrite or replay")
    captured = sources()
    require(captured == sources() and manifest_hash(captured) == args.source_manifest_sha256,
            "Latest reviewed product source is not stable")
    driver_hash = digest(Path(__file__).read_bytes())
    meta, before, pinned = campaign_basis()
    environment_root = Path(meta["environment_root"]).resolve()
    os.environ["LOCALAPPDATA"] = str(environment_root.parent.parent)
    sys.path.insert(0, str(ROOT / "src"))
    import orca_agent
    from orca_agent import agent
    from orca_agent.config import Config
    from orca_agent.store import Store, default_environment_root

    require(Path(orca_agent.__file__).resolve().is_relative_to(ROOT / "src"), "Wrong product source import")
    require(default_environment_root().resolve() == environment_root, "Environment root changed")
    prior_config = CONTROL / "attempts/a004/configuration.json"
    prior_meta = read(CONTROL / "attempts/a004/attempt.json")
    require(digest(prior_config.read_bytes()) == prior_meta["configuration_sha256"], "Original configuration changed")
    build_path = CONTROL / "runtime/build-r1/result.json"
    build_manifest = CONTROL / "runtime/build-r1/runtime-manifest.json"
    build = read(build_path)
    require(digest(build_manifest.read_bytes()) == build["manifest_sha256"], "Built runtime manifest changed")
    values = read(prior_config)
    values["execution"].update({key: build[key] for key in ("image_id", "manifest_sha256")})
    values["resources"]["job_timeout_seconds"] = 120
    values["model_profile"] = "disabled"
    values["data_root"] = str(PROBE / "data")
    config = Config.model_validate(values)
    require((config.resources.max_cores, config.resources.total_memory_mb, config.resources.maxcore_mb,
             config.resources.environment_concurrency) == (4, 1024, 192, 1), "Resource envelope differs")
    prior_outcome = read(CONTROL / "attempts/a005/outcome.json")
    prior_store = Store(CONTROL / "attempts/a005/data", environment_root=environment_root)
    with prior_store.run_lock(prior_outcome["run_id"]):
        prior_run = prior_store.load_run(prior_outcome["run_id"])
        require(prior_run.state not in {"ready", "running", "unknown"}, "a005 remains active")
        require(not prior_store.environment_lease(), "Execution environment has an outstanding lease")
    pinned.extend([prior_config, build_path, build_manifest, CONTROL / "attempts/a004/attempt.json"])
    files = {str(path): digest(path.read_bytes()) for path in pinned}

    def verify():
        require(sources() == captured and digest(Path(__file__).read_bytes()) == driver_hash,
                "Product source or probe script changed")
        require(all(digest(Path(path).read_bytes()) == value for path, value in files.items()),
                "Pinned campaign/configuration/runtime input changed")

    verify()
    # This immutable external reservation precedes every Run and every launch.
    save(PROBE / "probe.json", {"probe": "p001", "purpose": "0059 trusted ORCA start confirmation",
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "environment_root": str(environment_root),
        "reservation": RESERVATION, "known_usage_before": before,
        "source_files": captured, "source_manifest_sha256": manifest_hash(captured),
        "driver": str(Path(__file__).resolve()), "driver_sha256": driver_hash,
        "pinned_inputs": files, "geometry_sha256": digest(XYZ),
        "runtime": {key: build[key] for key in ("image_id", "manifest_sha256")},
        "per_run_budget": {"orca_starts": 1, "internal_work_units": 64, "run_seconds": 300},
        "job_timeout_seconds": 120, "model_calls": 0, "model_tokens": 0,
        "reference_or_control_execution": False, "scientific_qualification": "not_requested"})
    started = time.monotonic()
    store, run_ids, cases, error = None, [], [], None
    unchanged = False
    try:
        save(PROBE / "configuration.json", config.model_dump(mode="json"))
        store = Store(config.data_root, environment_root=environment_root, configuration=config.run_configuration())
        for name, nuclear in (("fast_native_failure", True), ("four_rank_single_point", False)):
            verify()
            require(time.monotonic() - started < RESERVATION["wall_seconds"] - 300,
                    "Insufficient remaining wall reservation for a complete Run")
            require(not store.environment_lease(), "Environment became occupied; do not launch")
            run, step = create_case(store, config, name, nuclear, PROBE / "cases")
            run_ids.append(run.id)
            save(PROBE / (name + ".start.json"), {"run_id": run.id, "started_at_utc": datetime.now(timezone.utc).isoformat()})
            print(json.dumps({"event": "probe_case_started", "case": name, "run_id": run.id}), flush=True)
            with store.run_lock(run.id):
                verify()
                run = store.load_run(run.id)
                result, _ = agent._science(store, config, run, step, {}, None, None)
                run = store.load_run(run.id)
                checked = inspect_case(store, run, result, nuclear)
                cases.append({"case": name, **checked})
                save(PROBE / (name + ".json"), checked)
            verify()
            require(checked["passed"], "Probe case did not satisfy the recorded runtime checks; stop here")
            require(not store.environment_lease(), "Execution lease remains; do not launch another case")
    except BaseException as exc:
        error = safe_error(exc)
    finally:
        try:
            verify()
            unchanged = True
        except Exception as exc:
            error = error or safe_error(exc)
        known, usage, unknown, unsettled, unconfirmed, runs = accounting(store, run_ids)
        known["wall_seconds"] = time.monotonic() - started
        if store is not None and store.environment_lease():
            unsettled.append("environment_lease_retained")
            unknown = sorted(set(unknown) | {"orca_starts", "internal_work_units"})
        exceeded = [key for key in known if known[key] > RESERVATION[key]]
        passed = len(cases) == 2 and all(item["passed"] for item in cases) and not (error or unknown or exceeded)
        outcome = {"finished_at_utc": datetime.now(timezone.utc).isoformat(), "probe": "p001",
            "state": "completed" if passed else "failed", "runtime_probe_passed": passed,
            "run_ids": run_ids, "runs": runs, "cases": cases, "known_usage": known, "usage": usage,
            "unknown_fields": unknown, "unsettled_execution": unsettled, "error": error,
            "unconfirmed_start_accounting": unconfirmed,
            "unknown_usage_reservations": {"orca_starts": len(unconfirmed)},
            "reservation_exceeded": exceeded, "source_unchanged": unchanged,
            "source_manifest_sha256": manifest_hash(captured), "driver_sha256": driver_hash,
            "independent_reference": "not_run", "control_execution": "not_run",
            "scientific_qualification": "not_requested"}
        save(PROBE / "outcome.json", outcome)
        print(json.dumps({"event": "probe_finished", "state": outcome["state"],
                          "known_usage": known, "unknown_fields": unknown}), flush=True)
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
