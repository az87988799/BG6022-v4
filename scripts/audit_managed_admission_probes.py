"""Read archived live probes without launching work or changing their evidence.

Run with the repository root and src on PYTHONPATH, passing --live001,
--live002, --live003 and an --output outside those evidence directories.
The CASES and verify_probe helper come from the corresponding developer test.
"""

import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from orca_agent.models import Result, Run
from orca_agent.store import Store
from tests.live.test_live_managed_native import CASES, verify_probe


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root):
    """Include original evidence, registered artifacts, raw work files and leases."""
    return {path.relative_to(root).as_posix(): {
        "bytes": path.stat().st_size, "sha256": file_hash(path)}
        for path in sorted(root.rglob("*")) if path.is_file()}


def audit_probe(root, label, case, *, completed):
    before = inventory(root)
    evidence_path = root / "evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    run, result = Run.model_validate(evidence["run"]), Result.model_validate(evidence["result"])
    outcome = evidence["outcome"]
    assert (root / "data").is_dir() and (root / "environment").is_dir()
    store = Store(root / "data", environment_root=root / "environment")
    request = store.load_request(run)
    geometry_bytes = (root / "initial.xyz").read_bytes()
    assert store.artifact_path(request.geometry_artifact_id).read_bytes() == geometry_bytes
    assert len(run.internal_calls) == 1
    receipt = run.internal_calls[0]
    assert not result.qualified_outputs and not run.model_records
    assert run.usage.model_calls == run.usage.model_tokens_used == run.usage.model_tokens_unknown == 0
    assert store.environment_lease() is None
    assert run.configuration.execution.backend == "linux-container"
    assert run.configuration.execution.native_input_policy.profile == "managed-native-1"
    assert run.configuration.resources.max_cores == 4
    assert run.configuration.resources.total_memory_mb == 1024
    assert run.configuration.resources.maxcore_mb == 192
    assert run.configuration.resources.environment_concurrency == 1
    assert receipt.resources["cores"] == 1

    artifacts = {}
    for metadata in (root / "data" / "artifacts").glob("*/artifact.json"):
        artifact = store.load_artifact(metadata.parent.name)
        store.artifact_path(artifact.id)  # Verify every registered size and hash.
        artifacts[artifact.id] = {"name": Path(artifact.path).name, "sha256": artifact.sha256,
                                  "bytes": artifact.size, "role": artifact.role}
    assert set(result.artifact_ids) <= artifacts.keys()
    assert set(receipt.artifact_ids) <= artifacts.keys()
    key_files = {}
    for identifier in receipt.artifact_ids:
        artifact = artifacts[identifier]
        if artifact["name"] in {"job.inp", "stdout.out", "stderr.txt", "execution.json"}:
            assert artifact["name"] not in key_files
            key_files[artifact["name"]] = {"artifact_id": identifier, **artifact}
    execution = None
    if "execution.json" in key_files:
        execution = json.loads(store.artifact_path(key_files["execution.json"]["artifact_id"])
                               .read_text(encoding="utf-8"))
    proof = (receipt.execution_handle or {}).get("actual_start_evidence")
    if receipt.started:
        assert proof and proof["method"] == "proc_exe_inode_and_pid_start_ticks"
        assert proof["executable"] == "/opt/orca/orca"
        assert type(proof["device"]) is int and type(proof["inode"]) is int
        identity = receipt.execution_handle["workload_identity"]
        assert proof["pid"] == identity["pid"] and proof["start_ticks"] == identity["start_ticks"]
    else:
        assert not proof
    assert run.usage.orca_starts_actual == int(receipt.started)
    assert run.usage.orca_starts_reserved == 1

    termination = {"normal_termination": None, "finite_energy_count": None}
    if "stdout.out" in key_files:
        stdout = store.artifact_path(key_files["stdout.out"]["artifact_id"]).read_text(
            encoding="utf-8", errors="replace")
        energies = re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", stdout)
        assert all(math.isfinite(float(value)) for value in energies)
        termination = {"normal_termination": "ORCA TERMINATED NORMALLY" in stdout,
                       "finite_energy_count": len(energies)}
    if completed:
        verified = verify_probe(store, run, result, outcome, request.geometry_artifact_id,
                                geometry_bytes, case)
        assert termination == {"normal_termination": verified["normal_termination"],
                               "finite_energy_count": verified["finite_energy_count"]}
        assert execution["state"] == "completed"
        assert receipt.response["scientific_validation"] == "unverified"
        failure = None
    else:
        assert result.operation_status == outcome["state"] == receipt.state == "failed"
        if label == "live001":
            assert "collection_error" in outcome and not result.observations["evidence_collection_complete"]
            assert receipt.started
            failure = "long_host_path_evidence_collection_failure"
        else:
            assert execution["reason"] == "orca_actual_start_unconfirmed"
            assert execution["actual_start_status"] == "unconfirmed"
            assert not receipt.started and result.observations["evidence_collection_complete"]
            failure = "actual_start_unconfirmed"

    usage = outcome["resource_usage"]
    assert usage["cgroup"]["memory.max"] == str(1024 * 1024 * 1024)
    assert usage["cgroup"]["cpu.max"] == "400000 100000"
    assert math.isclose(run.usage.cpu_seconds,
                        usage["user_cpu_seconds"] + usage["kernel_cpu_seconds"])
    assert usage["active_processes"] == 0 and not usage["oom_killed"]
    conditions = receipt.actual_conditions
    row = {
        "series": label, "case": case[0], "run_id": run.id, "result_id": result.id,
        "operation_status": result.operation_status,
        "original_outcome_state": outcome["state"], "internal_call_state": receipt.state,
        "failure": failure, "evidence_collection_complete": result.observations["evidence_collection_complete"],
        "conditions": {key: conditions[key] for key in
            ("method", "basis", "charge", "multiplicity", "electronic_state", "environment",
             "run_type", "temperature_K", "standard_state") if key in conditions},
        "admission_version": receipt.conditions["admission_version"],
        "scientific_qualification": False, "scientific_validation": "unverified",
        "model_calls": 0, **termination,
        "orca_starts": {
            "reserved": run.usage.orca_starts_reserved,
            "actual_confirmed": run.usage.orca_starts_actual,
            "reserved_not_confirmed": run.usage.orca_starts_reserved - run.usage.orca_starts_actual,
            "unknown_actual_start": int(failure == "actual_start_unconfirmed"),
        },
        "actual_start_proof": proof,
        "resources": {
            "cpu_seconds_known_lower_bound": round(run.usage.cpu_seconds, 6),
            "elapsed_seconds_recorded": round(run.usage.elapsed_seconds, 6),
            "peak_memory_bytes_observed": usage["peak_memory_bytes"],
            "complete": run.usage.resource_usage_complete,
            "accounting": usage["accounting"], "usage_scope": usage["usage_scope"],
            "active_processes_at_collection": usage["active_processes"], "environment_lease_released": True,
            "internal_work_units_reserved": run.usage.internal_work_units,
        },
        "hashes": {
            "evidence_json_sha256": file_hash(evidence_path),
            "initial_geometry_sha256": hashlib.sha256(geometry_bytes).hexdigest(),
            "native_code_sha256": receipt.code.sha256,
            "task_code_sha256": run.attempts[0].frozen_step.parameters.code.sha256,
            "registered_artifacts_manifest_sha256": digest(artifacts),
            "registered_artifacts_verified": len(artifacts),
            "key_internal_artifacts": key_files,
        },
    }
    after = inventory(root)
    assert before == after, f"Evidence changed during read-only audit: {label}/{case[0]}"
    row["read_only_audit"] = {
        "source_files": len(before), "source_bytes": sum(item["bytes"] for item in before.values()),
        "source_tree_before_sha256": digest(before), "source_tree_after_sha256": digest(after),
        "unchanged": True, "initial_geometry_byte_comparison": "original file bytes; CRLF preserved",
    }
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("live001", "live002", "live003", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    roots = [getattr(args, name).resolve(strict=True) for name in ("live001", "live002", "live003")]
    output = args.output.resolve()
    assert not any(output.is_relative_to(root) for root in roots), "Output must not alter source evidence"
    paths = [(roots[2] / f"test_real_managed_scope_via_py{index}", case)
             for index, case in enumerate(CASES)]
    assert all((path / "evidence.json").is_file() for path, _ in paths), "All nine probes must finish first"
    rows = []
    for root, label in zip(roots[:2], ("live001", "live002"), strict=True):
        failures = sorted(root.glob("*/evidence.json"))
        assert len(failures) == 1
        rows.append(audit_probe(failures[0].parent, label, CASES[0], completed=False))
    rows.extend(audit_probe(root, "live003", case, completed=True) for root, case in paths)
    completed = [row for row in rows if row["operation_status"] == "completed"]
    summary = {
        "schema_version": 1, "evidence_kind": "real_local_ORCA_in_managed_Linux_container",
        "probe_authorship": "developer-written Python and ORCA input; no autonomous model decisions",
        "audit": "read-only replay of tests.live.test_live_managed_native.verify_probe; no scientific reruns",
        "test_assertion_correction": "Original post-run pytest check compared CRLF file bytes to an LF string. "
            "Replay compares the original initial.xyz file bytes; archived failed test results are not rewritten.",
        "limits": [
            "Admission and operation completion do not provide independent scientific qualification.",
            "Nine distinct input probes, not the full repeated autonomous cross-task acceptance matrix.",
            "CPU and peak-memory observations cover the container cgroup including helpers; final accounting is incomplete.",
            "The unconfirmed live002 start remains reserved and unknown; stdout/process names do not prove an actual start.",
        ],
        "runtime": {
            "image_id": json.loads((paths[0][0] / "evidence.json").read_text(encoding="utf-8"))
                ["run"]["configuration"]["execution"]["image_id"],
            "profile": "managed-native-1", "max_cores": 4, "total_memory_mb": 1024,
            "orca_nprocs_per_probe": 1, "maxcore_mb": 192, "environment_concurrency": 1,
        },
        "totals": {
            "probes": len(rows), "completed": len(completed), "failed_preserved": len(rows) - len(completed),
            "orca_starts_reserved": sum(row["orca_starts"]["reserved"] for row in rows),
            "orca_starts_actual_confirmed": sum(row["orca_starts"]["actual_confirmed"] for row in rows),
            "orca_starts_unknown": sum(row["orca_starts"]["unknown_actual_start"] for row in rows),
            "cpu_seconds_known_lower_bound": round(sum(row["resources"]["cpu_seconds_known_lower_bound"]
                                                       for row in rows), 6),
            "completed_cpu_seconds_known_lower_bound": round(sum(
                row["resources"]["cpu_seconds_known_lower_bound"] for row in completed), 6),
            "peak_memory_bytes_max_observed": max(row["resources"]["peak_memory_bytes_observed"] for row in rows),
            "internal_work_units_reserved": sum(row["resources"]["internal_work_units_reserved"] for row in rows),
            "model_calls": 0, "scientifically_qualified": 0,
            "all_source_trees_unchanged": True, "all_registered_artifact_hashes_verified": True,
            "all_environment_leases_released": True,
        },
        "probes": rows,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["totals"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("Do not disable audit assertions")
    main()
