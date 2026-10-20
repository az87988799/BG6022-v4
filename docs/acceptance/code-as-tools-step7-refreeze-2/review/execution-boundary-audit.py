"""Read archived execution/cost records; never invoke Store, tools, or containers."""

import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
REVIEW = Path(__file__).resolve().parent
FREEZE = REVIEW.parent / "freeze"
BATCH = ROOT / "data/code-tools-refreeze-2-batch"
TASKS = ("T1", "T2", "T3", "T4", "T5", "H1", "H2")
FILES = {}


def reference(path):
    path = path.resolve()
    path.relative_to(ROOT)
    stat_before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    stat_after = path.stat()
    value = {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": stat_after.st_size,
        "sha256": digest.hexdigest(),
        "stable_during_read": (
            stat_before.st_size, stat_before.st_mtime_ns
        ) == (stat_after.st_size, stat_after.st_mtime_ns),
    }
    FILES[value["path"]] = value
    return value


def read(path):
    path = path.resolve()
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    key = path.relative_to(ROOT).as_posix()
    FILES[key] = {
        "path": key, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
        "stable_during_read": (before.st_size, before.st_mtime_ns)
        == (after.st_size, after.st_mtime_ns),
    }
    return json.loads(raw)


def optional(path):
    return read(path) if path.is_file() else None


def execution_records(directory):
    values = {}
    for name in ("intent.json", "boundary.json", "started.json", "actual-start.json",
                 "prepared.json", "execution.json", "runtime-manifest.json", "reconciled.json"):
        value = optional(directory / name)
        if value is not None:
            values[name] = value
    for name in ("task/stdout.out", "task/stderr.txt", "stdout.out", "stderr.txt"):
        if (directory / name).is_file():
            reference(directory / name)
    return values


def collector_records(directory):
    return [{
        "receipt": read(path), "source": reference(path),
        "classification": "fixed auxiliary artifact export, not an ORCA computation",
        "receipt_semantics": "Written after docker create and before docker start; confirms container creation, not independent executable start proof.",
        "independently_confirmed_executable_start": None,
        "measured_cpu_and_memory_usage": None,
    } for path in sorted(directory.glob("orca-collector-*.started.json"))]


def confirmed_orca(records):
    execution = records.get("execution.json", {})
    handle = records.get("actual-start.json") or execution.get("handle") or {}
    proof = handle.get("actual_start_evidence") or {}
    identity = handle.get("workload_identity") or {}
    return (
        execution.get("actual_start_status") == "confirmed"
        and handle.get("launch_phase") == "executed"
        and proof.get("executable") == "/opt/orca/orca"
        and all(proof.get(key) == identity.get(key) and proof.get(key) is not None
                for key in ("pid", "start_ticks"))
    )


def boundary_checks(run, handle, execution):
    usage = run["usage"]
    configured = run["configuration"]["execution"]
    limits = run["configuration"]["resources"]
    identity = handle.get("workload_identity", {})
    cgroup = execution.get("resource_usage", {}).get("cgroup", {})
    checks = {
        "model_call_count_matches_receipts": len(run["model_records"]) == usage["model_calls"],
        "known_model_tokens_match_receipts": sum(
            item.get("total_tokens") or 0 for item in run["model_records"]
            if item["status"] == "known") == usage["model_tokens_used"],
        "image_matches_run": handle.get("image_id") == configured["image_id"],
        "manifest_matches_run": handle.get("manifest_sha256") == configured["manifest_sha256"],
        "product_recorded_isolation_verified": handle.get("isolation_verified") is True,
    }
    if identity:
        checks.update(
            uid_matches_execution_kind=identity.get("uid") == (
                10002 if handle.get("kind") == "orca" else 10001),
            capabilities_zero=all(int(identity.get(k, "1"), 16) == 0
                                  for k in ("cap_eff", "cap_prm", "cap_inh", "cap_amb")),
            no_new_privileges=identity.get("no_new_privs") == "1",
            seccomp_filter=identity.get("seccomp") == "2",
        )
    if cgroup:
        quota, period = map(int, cgroup["cpu.max"].split())
        checks.update(
            memory_within_run_limit=0 < int(cgroup["memory.max"]) <= limits["total_memory_mb"] * 1024**2,
            no_swap=int(cgroup["memory.swap.max"]) == 0,
            cpu_quota_within_run_limit=0 < quota <= limits["max_cores"] * period,
            pids_limit_256=int(cgroup["pids.max"]) == 256,
        )
    return checks


def tool_call_accounting(run, definitions):
    counts = {key: 0 for key in ("analysis_executions", "evidence_reads", "identity_queries",
                                "structure_preparations", "knowledge_queries", "file_writes")}
    unknown_tools = []
    for call in run["calls"]:
        frozen = definitions.get(call["tool"], {}).get("definition")
        if frozen is None or set(frozen["effects"]) & {"execute_python", "execute_orca"}:
            unknown_tools.append(call["tool"])
            continue
        counter = frozen.get("usage_counter")
        if counter == "file_writes" and run["budget"].get("file_writes") is None:
            counter = "analysis_executions"
        counter = counter or ("analysis_executions" if "write_analysis" in frozen["effects"]
                              or frozen["output_ports"] else "evidence_reads")
        if counter not in counts:
            unknown_tools.append(call["tool"])
            continue
        counts[counter] += 1
    checks = {key + "_receipts_equal_usage": run["usage"].get(key, 0) == count
              for key, count in counts.items()}
    checks.update({key + "_within_frozen_limit": run["usage"].get(key, 0) <= run["budget"][key]
                   for key in counts if run["budget"].get(key) is not None})
    checks["all_calls_have_frozen_counter_definitions"] = not unknown_tools
    return {"counts_including_failed_unknown_and_reserved": counts, "checks": checks,
            "unknown_tools": unknown_tools, "write_counter_mode": (
                "file_writes" if run["budget"].get("file_writes") is not None else "legacy_analysis_executions")}


ledger = read(BATCH / "ledger.json")
frozen_product = read(FREEZE / "product-freeze.json")
freeze_event = read(FREEZE / "product-freeze-event.json")
scope = read(BATCH / "first-repetition-evidence/scope.json")
rows = []
issues = []
for task in TASKS:
    slot_id = f"formal-{task}-001"
    evidence = BATCH / "first-repetition-evidence" / task
    start = optional(evidence / "start.json")
    if start is None:
        rows.append({"task_id": task, "slot_id": slot_id, "status": "not_started",
                     "actual_usage": None, "unknown_not_zero": True})
        continue
    data_root = BATCH / "workspaces" / slot_id / "data"
    run_root = data_root / "runs" / start["run_id"]
    run = read(run_root / "run.json")
    outcome = optional(evidence / "outcome.json")
    report = optional(evidence / "report.json")
    post_verify = optional(evidence / "post-verify.json")
    pre_verify = optional(evidence / "pre-verify.json")
    attempts = []
    for attempt in run["attempts"]:
        directory = data_root / attempt["directory"]
        records = execution_records(directory)
        execution = records.get("execution.json", {})
        handle = execution.get("handle") or attempt.get("execution_handle") or {}
        prepared = records.get("prepared.json", {})
        workload = attempt.get("native_workload")
        result_path = run_root / "results" / f"{attempt.get('result_id')}.json"
        result = optional(result_path)
        is_orca = attempt["execution_kind"] == "orca"
        entry = {
            "id": attempt["id"], "tool": attempt["tool"],
            "execution_kind": attempt["execution_kind"], "state": attempt["state"],
            "created_at": attempt["created_at"], "finished_at": attempt["finished_at"],
            "recorded_started": attempt["started"], "directory": attempt["directory"],
            "result_id": attempt.get("result_id"), "execution_handle": handle,
            "execution_receipt": execution, "native_workload": workload,
            "prepared_admission_version": prepared.get("admission_version"),
            "prepared_native_policy": prepared.get("native_input_policy"),
            "direct_native_reservation_exercised": bool(is_orca and workload),
            "native_version_consistent": (
                prepared.get("admission_version") == workload.get("admission_version")
            ) if workload else None,
            "actual_orca_executable_confirmed": confirmed_orca(records) if is_orca else False,
            "actual_python_execution": bool(
                not is_orca and attempt["started"] and execution
                and not execution.get("not_started", True)),
            "resource_accounting_complete": execution.get("resource_usage", {}).get("complete"),
            "observed_boundary_checks": boundary_checks(
                run, handle, execution) if handle.get("container_id") else None,
            "reservation_without_actual_start": bool(is_orca and not attempt["started"]),
            "result_operation_status": result.get("operation_status") if result else None,
            "qualified_output_names": list(result.get("qualified_outputs", {})) if result else [],
            "checks": result.get("checks") if result else None,
            "scientific_validation": (
                result.get("observations", {}).get("scientific_validation") if result else None),
            "evidence_records_present": list(records),
            "auxiliary_collectors": collector_records(directory),
        }
        if workload and not entry["native_version_consistent"]:
            issues.append({"task": task, "attempt": attempt["id"], "issue": "native_version_mismatch"})
        if workload:
            input_files = {
                name: reference(directory / name)
                for name in ("task/job.inp", "task-inputs/job.inp")
                if (directory / name).is_file()
            }
            input_hash = workload["input_sha256"]
            native_checks = {
                "prepared_policy_matches_reservation": prepared.get("native_input_policy")
                    == workload["native_input_policy"],
                "prepared_policy_matches_frozen_run": prepared.get("native_input_policy")
                    == run["configuration"]["execution"].get("native_input_policy"),
                "prepared_input_matches_reservation": prepared.get("input_sha256") == input_hash,
                "frozen_step_input_matches_reservation": attempt["frozen_step"]["parameters"].get(
                    "input", {}).get("sha256") == input_hash,
                "both_task_and_staged_input_bytes_match": len(input_files) == 2
                    and all(item["sha256"] == input_hash for item in input_files.values()),
            }
            entry["native_input_chain"] = {
                "checks": native_checks, "input_files": input_files,
                "prepared_resources": prepared.get("resources"),
                "declared_work_units_are_measured_evaluations": False,
            }
            if not all(native_checks.values()):
                issues.append({"task": task, "attempt": attempt["id"],
                               "issue": "native_input_chain_mismatch"})
        if is_orca and attempt["started"] and not entry["actual_orca_executable_confirmed"]:
            issues.append({"task": task, "attempt": attempt["id"],
                           "issue": "recorded_actual_without_complete_executable_proof"})
        attempts.append(entry)
    internal = []
    for call in run["internal_calls"]:
        parent = next(a for a in run["attempts"] if a["id"] == call["parent_attempt_id"])
        records = execution_records(data_root / parent["directory"] / "internal" / call["request_key"])
        prepared = records.get("prepared.json", {})
        entry = {key: call.get(key) for key in (
            "request_key", "parent_attempt_id", "purpose", "retry_of", "state", "started",
            "work_units", "created_at", "finished_at", "execution_handle", "resource_usage",
            "actual_conditions", "result_id")}
        entry.update(
            execution_receipt=records.get("execution.json"),
            prepared_admission_version=prepared.get("admission_version"),
            prepared_native_policy=prepared.get("native_input_policy"),
            actual_orca_executable_confirmed=confirmed_orca(records),
            evidence_records_present=list(records),
            auxiliary_collectors=collector_records(
                data_root / parent["directory"] / "internal" / call["request_key"]),
            accounting_rule="Child resource samples are not added again to parent aggregate cgroup usage.",
        )
        if entry.get("execution_handle") and entry["execution_receipt"]:
            entry["observed_boundary_checks"] = boundary_checks(
                run, entry["execution_handle"], entry["execution_receipt"])
        if call["started"] and not entry["actual_orca_executable_confirmed"]:
            issues.append({"task": task, "request_key": call["request_key"],
                           "issue": "recorded_internal_actual_without_complete_executable_proof"})
        internal.append(entry)
    direct = [a for a in run["attempts"] if a["execution_kind"] == "orca"]
    observed_reservations = len(direct) + len(internal)
    observed_actual = sum(a["started"] for a in direct) + sum(c["started"] for c in run["internal_calls"])
    units = sum((a.get("native_workload") or {}).get("execution_impact", {}).get(
        "internal_work_units", 0) for a in direct) + sum(c["work_units"] for c in run["internal_calls"])
    usage = run["usage"]
    batch_row = ledger["rows"].get(slot_id)
    original_budget = read(data_root.parent / "run-budget.json")
    checks = {
        "reserved_starts_match_receipt_objects": observed_reservations == usage["orca_starts_reserved"],
        "actual_starts_match_recorded_started": observed_actual == usage["orca_starts_actual"],
        "known_native_work_units_match": units == usage["internal_work_units"],
        "parent_cpu_sum_matches_run": math.isclose(sum(a["cpu_seconds"] for a in run["attempts"]),
                                                   usage["cpu_seconds"], abs_tol=1e-6),
        "parent_elapsed_sum_matches_run": math.isclose(sum(a["elapsed_seconds"] for a in run["attempts"]),
                                                       usage["elapsed_seconds"], abs_tol=1e-6),
        "frozen_budget_keys_unchanged": all(run["budget"].get(k) == v for k, v in original_budget.items()),
        "run_binding_matches_ledger": batch_row is not None and batch_row["run_id"] == run["id"],
    }
    call_accounting = tool_call_accounting(run, frozen_product["contract"]["tools"])
    checks.update(call_accounting["checks"])
    for label, verification in (("pre", pre_verify), ("post", post_verify)):
        if verification:
            verified = json.loads(verification["stdout"])
            checks[f"{label}_freeze_verification_matched"] = (
                verification["returncode"] == 0 and verified.get("status") == "matched"
                and verified.get("code_commit") == scope["product_commit"]
                and verified.get("scientific_execution_performed") is False
            )
    if batch_row and batch_row.get("settlement"):
        settled = batch_row["settlement"]["usage"]
        checks["settled_known_usage_matches_run"] = all(
            settled.get(left) is None or settled[left] == usage[right]
            for left, right in (("model_calls", "model_calls"),
                                ("model_tokens", "model_tokens_used"),
                                ("orca_starts", "orca_starts_actual"),
                                ("internal_work_units", "internal_work_units")))
    for key, passed in checks.items():
        if not passed:
            issues.append({"task": task, "issue": key})
    rows.append({
        "task_id": task, "slot_id": slot_id, "run_id": run["id"], "status": run["state"],
        "frozen_resources": run["configuration"]["resources"],
        "frozen_execution": run["configuration"]["execution"],
        "start": start, "outcome": outcome, "usage": usage, "attempts": attempts,
        "termination_and_validation_diagnostics": run["diagnostics"],
        "internal_calls": internal, "tool_call_accounting": call_accounting,
        "reservation_count_from_objects": observed_reservations,
        "actual_count_from_recorded_started": observed_actual, "record_consistency": checks,
        "batch_reservation_and_settlement": batch_row,
        "cost": report.get("budget", {}).get("model_cost") if report else None,
        "independent_reference": outcome.get("independent_reference") if outcome else None,
        "pre_verify_result": json.loads(pre_verify["stdout"]) if pre_verify else None,
        "post_verify_result": json.loads(post_verify["stdout"]) if post_verify else None,
        "model_receipts": [{k: item.get(k) for k in (
            "id", "logical_id", "created_at", "status", "input_reserved", "output_reserved",
            "total_tokens", "cost_reserved_usd", "cost_known_usd", "http_status",
            "response_hash", "error_category")} for item in run["model_records"]],
        "goal_status": run["goal_status"], "delivery_status": run["delivery_status"],
    })

complete = all(row.get("outcome") is not None and row.get("post_verify_result") for row in rows)
known = [row for row in rows if "usage" in row]
totals = {key: sum(row["usage"].get(key, 0) for row in known) for key in (
    "model_calls", "model_tokens_used", "model_tokens_unknown", "orca_starts_reserved",
    "orca_starts_actual", "internal_work_units", "code_retries", "elapsed_seconds", "cpu_seconds",
    "file_writes", "analysis_executions")}
reservations = {
    key: sum(row["slot"]["reservation"][key] for row in ledger["rows"].values())
    for key in ("model_calls", "model_tokens", "orca_starts", "internal_work_units", "wall_seconds")
}
intervals = [(row["task_id"], a) for row in known for a in row["attempts"]
             if a["recorded_started"] and a["finished_at"]]
overlaps = []
for index, (left_task, left) in enumerate(intervals):
    for right_task, right in intervals[index + 1:]:
        if max(left["created_at"], right["created_at"]) < min(
                left["finished_at"], right["finished_at"]):
            overlaps.append({"left": [left_task, left["id"]],
                             "right": [right_task, right["id"]]})
extra_slots = set(ledger["rows"]) - {f"formal-{task}-001" for task in TASKS}
if extra_slots:
    issues.append({"issue": "extra_slots", "slots": sorted(extra_slots)})
docker_observations = []
for path in REVIEW.glob("*-container-inspect.json"):
    capture = read(path)
    row = next((item for item in known if item["run_id"] == capture["run_id"]), None)
    attempt = next((item for item in row["attempts"]
                    if item["id"] == capture["attempt_id"]), None) if row else None
    check = {"source": reference(path), "run_id": capture["run_id"],
             "attempt_id": capture["attempt_id"], "returncode": capture["returncode"]}
    if attempt and capture.get("metadata"):
        metadata = capture["metadata"]
        host = metadata["HostConfig"]
        limits = row["frozen_resources"]
        handle = attempt["execution_handle"]
        memory = limits["total_memory_mb"] * 1024**2
        check.update(
            container_state=metadata["State"],
            task_exit_code=attempt["execution_receipt"].get("exit_code"),
            checks={
                "container_id_matches": metadata["Id"] == handle["container_id"],
                "image_matches": metadata["Image"] == handle["image_id"],
                "read_only_root": host["ReadonlyRootfs"] is True,
                "no_network": host["NetworkMode"] == "none",
                "not_privileged": host["Privileged"] is False,
                "caps_dropped": host.get("CapAdd") in (None, []) and host["CapDrop"] == ["ALL"],
                "keeper_user": capture["config_identity"]["User"] == "10003:10003",
                "memory_limit": host["Memory"] == memory,
                "no_extra_swap": host["MemorySwap"] == memory,
                "cpu_limit": host["NanoCpus"] == limits["max_cores"] * 10**9,
                "pids_limit": host["PidsLimit"] == 256,
                "no_new_privileges": "no-new-privileges:true" in host["SecurityOpt"],
                "restart_disabled": host["RestartPolicy"]["Name"] == "no",
                "single_private_work_volume": len(metadata["Mounts"]) == 1
                    and metadata["Mounts"][0]["Type"] == "volume"
                    and metadata["Mounts"][0]["Name"] == handle["volume_name"]
                    and metadata["Mounts"][0]["Destination"] == "/work",
            },
        )
        if not all(check["checks"].values()):
            issues.append({"attempt": attempt["id"], "issue": "independent_docker_boundary_mismatch"})
    else:
        check["status"] = "unverified_current_metadata_unavailable_or_unbound"
    docker_observations.append(check)
result = {
    "schema_version": 1, "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    "scope": "Independent read-only execution/receipts/cost review; no extra model, scientific or container execution.",
    "status": "all_seven_first_repetition_outcomes_reviewed" if complete else "partial_live_snapshot",
    "product_freeze_event": freeze_event, "authorized_scope": scope, "rows": rows,
    "totals_observed": totals, "issues": issues,
    "batch_reserved_caps": reservations,
    "settled_driver_wall_seconds": sum(
        row["outcome"]["driver_wall_seconds"] for row in known if row.get("outcome")),
    "known_model_cost_upper_bound_usd": str(sum(
        (Decimal(row["cost"]["known_cost"]) for row in known if row.get("cost")), Decimal(0))),
    "provider_charged_amount": None,
    "provider_billing_queried_by_reviewer": False,
    "conservative_parent_attempt_intervals_overlap": overlaps,
    "concurrency_scope": "Compare parent Attempt created/finished windows only; internal calls naturally overlap their owning Python parent and do not consume another independent environment slot.",
    "independent_readonly_docker_observations": docker_observations,
    "direct_native_reservations_exercised": sum(a["direct_native_reservation_exercised"]
                                               for row in known for a in row["attempts"]),
    "direct_native_actual_orca_confirmed": sum(a["actual_orca_executable_confirmed"]
                                              for row in known for a in row["attempts"]),
    "internal_actual_orca_confirmed": sum(c["actual_orca_executable_confirmed"]
                                         for row in known for c in row["internal_calls"]),
    "actual_python_attempts": sum(a["actual_python_execution"] for row in known for a in row["attempts"]),
    "auxiliary_collector_created_receipts": sum(
        len(item["auxiliary_collectors"]) for row in known
        for item in [*row["attempts"], *row["internal_calls"]]),
    "resource_usage_complete_for_all_observed_runs": all(
        row["usage"]["resource_usage_complete"] for row in known),
    "limitations": [
        "Reservations, confirmed executable starts, completed processes, scientific qualification and goal completion are distinct.",
        "Native SDK calls exercise native admission but not Store.reserve_native_workload's direct Attempt branch.",
        "A Python container start does not establish an ORCA executable start.",
        "Real Python may perform geometry/force-field preparation even with zero ORCA; its task failure does not erase that computation.",
        "complete=false cgroup samples are lower bounds, never exact complete resource charges.",
        "Summed Attempt cgroup CPU is not whole-application CPU: host-side tools, model/network work and auxiliary collectors have no complete independent resource total in these receipts.",
        "Container keeper exit status and task execution exit status are separate; 137 alone is not OOM proof.",
        "Fixed auxiliary artifact collectors are distinct from ORCA starts; the files named started.json are written after create but before start, and have no separate measured CPU/memory usage. Frozen code removes each collector after export, so later Docker metadata may be unavailable.",
        "No reference computations or new isolation/failure experiments were authorized or performed.",
        "Missing/not-started task usage is unverified rather than silently assigned zero in this reviewer snapshot.",
        "Hashes bind observed archived bytes; live Run/ledger files can change after a partial capture.",
        "Nonoverlapping recorded Attempt windows support this batch's observed sequential execution; they do not independently prove the absence of unrelated activity throughout the shared environment.",
    ],
    "evidence_index": [],
}
reference(Path(__file__))
for path in (ROOT / "src/orca_agent/store.py", ROOT / "src/orca_agent/backends/container.py"):
    reference(path)
for path in REVIEW.glob("*-container-inspect.json"):
    reference(path)
result["evidence_index"] = list(FILES.values())
if "--final" in sys.argv and not complete:
    raise SystemExit("Seven driver outcomes/post-verifies are required before final publication")
destination = REVIEW / ("execution-boundary-review.json" if "--final" in sys.argv else
                        "execution-boundary-snapshot-" + datetime.now(timezone.utc).strftime("%H%M%S") + ".json")
with destination.open("x", encoding="utf-8", newline="\n") as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    stream.write("\n")
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps({"path": str(destination), "sha256": reference(destination)["sha256"],
                  "status": result["status"], "totals": totals, "issues": issues}, ensure_ascii=False))
