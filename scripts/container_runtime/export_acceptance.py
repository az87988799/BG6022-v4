"""Export completed local evidence batches without executing workloads.

Every source file is indexed, including omitted binaries. Published text is
content-addressed; an existing batch or object with different bytes is an error.
Run usage is counted once, never again for its internal SDK calls. This is an
evidence curator, not a scientific validator or a general secret scanner.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import stat
import xml.etree.ElementTree as ET
from pathlib import Path

SCHEMA_VERSION = 1
TEXT_SUFFIXES = {
    ".json", ".py", ".inp", ".xyz", ".out", ".txt", ".md", ".xml",
    ".log", ".bibtex", ".opt", ".engrad", ".densitiesinfo", ".toml", ".csv", ".tsv", ".sdf",
}
MAX_TEXT_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
REPARSE_POINT = 0x400
SENSITIVE_KEYS = {
    "env", "httpproxy", "httpsproxy", "noproxy", "proxyconfig", "proxies",
    "apikey", "accesstoken", "refreshtoken", "password", "authorization",
    "registryauth", "identitytoken", "auths",
}
POLICY = {
    "source_files_are_unchanged": True,
    "objects_are_exact_bytes_unless_marked_projection": True,
    "binary_files_are_hashed_but_not_published": True,
    "duplicate_test_evidence_wrappers_are_hashed_but_not_published": True,
    "full_docker_environment_and_credentials_are_not_published": True,
    "resource_accounting": (
        "Sum persisted Run usage once per Run. Internal SDK measurements share "
        "the parent container and are not added. Incomplete samples are lower bounds."
    ),
    "status_semantics": (
        "Run state, attempt state, Result operation status, scientific checks, "
        "and pytest outcome are separate facts; none is inferred from another."
    ),
}


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_identity(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    )


def source_files(directory: Path) -> list[Path]:
    """Reject reparse points instead of traversing external evidence paths."""
    if is_link(directory.lstat()):
        raise ValueError(f"Source directory is a link/reparse point: {directory}")
    result = []
    for item in sorted(directory.iterdir()):
        info = item.lstat()
        if is_link(info):
            raise ValueError(f"Source evidence contains a link/reparse point: {item}")
        if stat.S_ISDIR(info.st_mode):
            result.extend(source_files(item))
        elif stat.S_ISREG(info.st_mode):
            result.append(item)
        else:
            raise ValueError(f"Source evidence contains a nonregular file: {item}")
    return result


def sanitize_json(value: object, removed: list[str], at: str = "$") -> object:
    """Remove full Docker environments and credential fields, including nested ones."""
    if isinstance(value, dict):
        if {"Id", "Config"} <= value.keys() and ("HostConfig" in value or "State" in value):
            # Container inspection is useful for resource/identity evidence, but
            # its complete Config.Env and host mount paths must stay local.
            removed.append(f"{at}.[Docker inspect fields outside the public allowlist]")
            public = select(value, ("Id", "Name", "Image", "Created", "State", "RestartCount"))
            public["HostConfig"] = select(value.get("HostConfig", {}), (
                "Memory", "MemorySwap", "NanoCpus", "CpusetCpus", "PidsLimit",
                "NetworkMode", "ReadonlyRootfs", "CapDrop", "CapAdd", "SecurityOpt",
                "Privileged", "PidMode", "IpcMode", "CgroupnsMode", "Tmpfs",
            ))
            config = value.get("Config", {})
            public["Config"] = select(config, ("User", "WorkingDir"))
            public["Config"]["Labels"] = {
                key: child for key, child in (config.get("Labels") or {}).items()
                if key.startswith("orca.")
            }
            public["Mounts"] = [
                select(mount, ("Type", "Name", "Destination", "RW", "Propagation"))
                for mount in value.get("Mounts", [])
            ]
            value = public
        clean = {}
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", key.lower())
            location = f"{at}.{key}"
            if normalized in SENSITIVE_KEYS:
                removed.append(location)
            else:
                clean[key] = sanitize_json(child, removed, location)
        return clean
    if isinstance(value, list):
        return [sanitize_json(child, removed, f"{at}[{i}]") for i, child in enumerate(value)]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            nested = json.loads(value)
        except ValueError:
            return value
        before = len(removed)
        cleaned = sanitize_json(nested, removed, f"{at}.[JSON string]")
        if len(removed) != before:
            return json.dumps(cleaned, ensure_ascii=False, sort_keys=True)
    return value


def public_bytes(path: Path, size: int) -> tuple[bytes | None, str, list[str]]:
    wrapper = path.name == "evidence.json"
    if wrapper:
        value = json.loads(path.read_bytes())
        if set(value) <= {"run", "result", "outcome"}:
            return None, "duplicate test wrapper; individual Run/Result/raw files retained", []
    name = path.name.lower()
    if any(part in name for part in ("docker-info", "docker_info", "proxy", "credential")):
        return None, "host environment or credential material; not a public raw file", []
    if path.suffix.lower() not in TEXT_SUFFIXES:
        return None, "binary or unsupported text format; original retained locally", []
    if size > MAX_TEXT_BYTES:
        return None, "per-file public text size limit exceeded; original retained locally", []
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError:
        return None, "not UTF-8 text; original retained locally", []
    if "\x00" in text:
        return None, "contains binary NUL bytes; original retained locally", []
    # This deliberately fails closed rather than silently publishing a credential.
    if re.search(r"https?://[^/\s:@]+:[^/\s@]+@|-----BEGIN .*PRIVATE KEY-----", text):
        return None, "potential credential-bearing text; manual review required", []
    if path.suffix.lower() == ".json":
        try:
            value = json.loads(text)
        except ValueError:
            return raw, "raw text (invalid JSON is retained as observed)", []
        removed: list[str] = []
        if wrapper:
            # Recovery/concurrency tests carry unique reconciliation facts in
            # their wrapper, so retain those instead of discarding all wrappers.
            for key in ("run", "result", "outcome", "first_result", "second_run"):
                if key in value:
                    del value[key]
                    removed.append(f"$.{key} (duplicate persisted domain evidence)")
        clean = sanitize_json(value, removed)
        if removed:
            return encode(clean), "JSON projection; sensitive/environment fields removed", removed
    return raw, "exact original bytes", []


def ref(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def select(value: dict, names: tuple[str, ...]) -> dict:
    return {name: value[name] for name in names if name in value}


def execution_summary(path: Path, root: Path) -> dict:
    value = read_json(path)
    usage = value.get("resource_usage", {})
    handle = value.get("handle", {})
    return {
        "source": ref(path, root),
        **select(value, ("state", "exit_code", "not_started", "reason", "actual_start_status")),
        "identity": select(handle, (
            "image_id", "manifest_sha256", "launch_id", "container_id", "kind",
            "launch_phase", "actual_start_evidence",
        )),
        "resource_usage": select(usage, (
            "wall_seconds", "user_cpu_seconds", "kernel_cpu_seconds", "peak_memory_bytes",
            "cgroup", "accounting", "usage_scope", "complete", "oom_killed",
            "aggregate_charged_to_parent",
        )),
    }


def summarize_run(path: Path, root: Path) -> dict:
    run = read_json(path)
    directory = path.parent
    data_root = directory.parent.parent
    environment_path = directory / "environment.json"
    environment = read_json(environment_path) if environment_path.is_file() else {}
    container = environment.get("container", {})
    manifest = container.get("manifest", {})
    attempts = []
    for attempt in run.get("attempts", []):
        item = select(attempt, (
            "id", "step_id", "tool", "state", "execution_kind", "started",
            "cpu_seconds", "elapsed_seconds", "result_id", "directory",
        ))
        attempt_dir = data_root / attempt["directory"]
        if not attempt_dir.resolve().is_relative_to(directory.resolve()):
            raise ValueError(f"Attempt directory escaped Run: {attempt_dir}")
        execution = attempt_dir / "execution.json"
        if execution.is_file():
            item["execution"] = execution_summary(execution, root)
        item["actual_start_records"] = [
            ref(p, root) for p in sorted(attempt_dir.glob("actual-start.json"))
        ]
        attempts.append(item)
    internals = []
    for call in run.get("internal_calls", []):
        item = select(call, ("request_key", "tool", "state", "attempt_id", "parent_attempt_id", "started"))
        item["resource_usage_not_added"] = call.get("resource_usage", {})
        item["actual_start_evidence"] = call.get("execution_handle", {}).get("actual_start_evidence")
        internals.append(item)
    results = []
    for result_path in sorted((directory / "results").glob("*.json")):
        result = read_json(result_path)
        results.append({
            "source": ref(result_path, root),
            **select(result, (
                "id", "attempt_id", "operation_status", "checks", "qualified_outputs",
                "observations", "diagnostics",
            )),
        })
    actual_start_paths = sorted((directory / "steps").rglob("actual-start.json"))
    positive_starts = set()
    for actual_start in actual_start_paths:
        handle = read_json(actual_start)
        evidence = handle.get("actual_start_evidence", {})
        if (
            evidence.get("method") != "proc_exe_inode_and_pid_start_ticks"
            or evidence.get("executable") != "/opt/orca/orca"
            or handle.get("launch_phase") != "executed"
        ):
            raise ValueError(f"Unrecognized positive start receipt: {actual_start}")
        positive_starts.add((
            handle.get("container_id"), evidence.get("pid"), evidence.get("start_ticks"),
            evidence.get("device"), evidence.get("inode"),
        ))
    observations = []
    # Attempt raw stdout only: do not recount the Artifact store's identical copies.
    for stdout in sorted((directory / "steps").rglob("stdout.out")):
        text = stdout.read_text(encoding="utf-8", errors="replace")
        if "O   R   C   A" in text or "ORCA TERMINATED" in text:
            observations.append({
                "source": ref(stdout, root),
                "normal_termination_marker": "ORCA TERMINATED NORMALLY" in text,
                "final_single_point_energies_text": re.findall(
                    r"FINAL SINGLE POINT ENERGY\s+([-+\d.Ee]+)", text
                ),
                "qualification": "raw textual observation; consult Result checks",
            })
    usage = run.get("usage", {})
    outer_cpu = math.fsum(float(a.get("cpu_seconds", 0)) for a in attempts)
    cpu = float(usage.get("cpu_seconds", 0))
    files = manifest.get("files", {})
    return {
        "source": ref(path, root),
        **select(run, ("id", "state", "goal_status", "delivery_status", "diagnostics")),
        "snapshots": {
            name: ref(directory / f"{name}.json", root)
            for name in ("configuration", "permission", "budget", "environment")
            if (directory / f"{name}.json").is_file()
        },
        "runtime_identity_at_execution": {
            **select(container, ("image_id", "manifest_sha256", "verified")),
            **select(manifest, ("isolation_policy", "python", "orca", "mpi", "distributions", "sdk")),
            "helper_files": {
                key: value for key, value in files.items()
                if key.startswith("/opt/orca-agent-runtime/") and "/venv/" not in key
            },
            "sandbox_source_sha256": manifest.get("sandbox_source_sha256"),
        },
        "usage": usage,
        "resource_summary": {
            "cpu_seconds_counted_once": cpu,
            "cpu_seconds_outer_attempt_sum": outer_cpu,
            "usage_matches_outer_attempt_sum": math.isclose(cpu, outer_cpu, abs_tol=1e-9),
            "elapsed_seconds_persisted": usage.get("elapsed_seconds"),
            "complete": usage.get("resource_usage_complete", False),
            "interpretation": "persisted aggregate samples; incomplete accounting is a lower bound",
            "internal_resource_measurements_added": False,
        },
        "orca_start_evidence": {
            "persisted_actual_count": usage.get("orca_starts_actual", 0),
            "actual_start_records": [ref(p, root) for p in actual_start_paths],
            "proc_exe_positive_evidence_count": len(positive_starts),
            "legacy_counting_caveat": (
                "No separate proc-exe receipt in this historical batch. Persisted counters "
                "are preserved; raw ORCA stdout is independent positive execution evidence."
                if usage.get("orca_starts_actual", 0) and not actual_start_paths else None
            ),
            "raw_orca_stdout": observations,
        },
        "attempts": attempts,
        "internal_calls_not_double_counted": internals,
        "results": results,
    }


def junit_summary(path: Path) -> dict:
    tree = ET.fromstring(path.read_bytes())
    suites = [tree] if tree.tag == "testsuite" else tree.findall("testsuite")
    return {
        "tests": sum(int(s.get("tests", 0)) for s in suites),
        "failures": sum(int(s.get("failures", 0)) for s in suites),
        "errors": sum(int(s.get("errors", 0)) for s in suites),
        "skipped": sum(int(s.get("skipped", 0)) for s in suites),
        "time_seconds": math.fsum(float(s.get("time", 0)) for s in suites),
    }


def prepare_batch(root: Path, batch: str, *, hash_only_contexts: bool = False) -> tuple[dict, dict[str, bytes]]:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", batch):
        raise ValueError("Batch must be a single plain directory name")
    directory = root / batch
    files = source_files(directory)
    attachments = [root / f"{batch}{suffix}" for suffix in (".log", ".xml")]
    files += [path for path in attachments if path.is_file()]
    records = []
    objects: dict[str, bytes] = {}
    for path in sorted(files):
        digest, size = file_identity(path)
        record = {
            "source": ref(path, root), "local_path": path.absolute().as_posix(),
            "sha256": digest, "bytes": size,
        }
        if hash_only_contexts and (path.name == "run-before.json" or (
                path.parent.name == "model" and path.name.endswith(".request.json"))):
            raw, reason, removed = None, "large context snapshot retained locally; original hash indexed", []
        else:
            raw, reason, removed = public_bytes(path, size)
        record["publication_reason"] = reason
        if raw is None:
            record["published"] = None
        else:
            published_hash = sha(raw)
            object_path = f"objects/{published_hash[:2]}/{published_hash}.txt"
            objects[object_path] = raw
            record["published"] = {
                "path": object_path, "sha256": published_hash, "bytes": len(raw),
                "exact_original": raw == path.read_bytes(),
            }
            if removed:
                record["removed_json_fields"] = removed
        records.append(record)
    runs = [summarize_run(path, root) for path in files if path.name == "run.json"]
    ids = [run["id"] for run in runs]
    if len(ids) != len(set(ids)):
        raise ValueError(f"Duplicate Run snapshots within {batch}; choose a unique source")
    junit = root / f"{batch}.xml"
    index = {
        "schema_version": SCHEMA_VERSION,
        "batch": batch,
        "source_root": root.as_posix(),
        "export_policy": POLICY,
        "summary": {
            "run_count": len(runs),
            "orca_starts_actual_persisted": sum(r["usage"].get("orca_starts_actual", 0) for r in runs),
            "orca_proc_exe_positive_receipts": sum(
                r["orca_start_evidence"]["proc_exe_positive_evidence_count"] for r in runs
            ),
            "cpu_seconds_lower_bound": math.fsum(r["usage"].get("cpu_seconds", 0) for r in runs),
            "resource_accounting_complete": all(r["usage"].get("resource_usage_complete", False) for r in runs),
            "model_calls": sum(r["usage"].get("model_calls", 0) for r in runs),
            "source_file_count": len(records),
            "source_bytes": sum(r["bytes"] for r in records),
            "published_source_entries": sum(r["published"] is not None for r in records),
            "omitted_source_entries": sum(r["published"] is None for r in records),
            "framework_report": junit_summary(junit) if junit.is_file() else None,
        },
        "runs": runs,
        "files": records,
    }
    return index, objects


def immutable_write(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Refusing to replace different existing evidence: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)


def verify_index(index: dict, destination: Path) -> None:
    for record in index["files"]:
        if file_identity(Path(record["local_path"])) != (record["sha256"], record["bytes"]):
            raise ValueError(f"Source changed: {record['source']}")
        published = record["published"]
        if published and file_identity(destination / published["path"]) != (
            published["sha256"], published["bytes"],
        ):
            raise ValueError(f"Published bytes changed: {published['path']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batches", nargs="+", help="Completed, immutable source batch names")
    parser.add_argument("--source-root", type=Path, default=Path("data/container-proof-20261018"))
    parser.add_argument("--destination", type=Path, default=Path("docs/acceptance/code-as-tools-step2-linux/production"))
    parser.add_argument("--max-total-bytes", type=int, default=MAX_TOTAL_BYTES)
    parser.add_argument("--hash-only-contexts", action="store_true",
                        help="Index original model request and pre-resume context hashes; retain their bytes locally")
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    root = args.source_root.resolve(strict=True)
    destination = args.destination.absolute()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("Source and publication directories must be separate")
    prepared = []
    pending: dict[str, bytes] = {".gitattributes": b"* -text -whitespace\n"}
    for batch in args.batches:
        index_path = f"batches/{batch}/index.json"
        if args.verify_only:
            index = read_json(destination / index_path)
            verify_index(index, destination)
        else:
            index, objects = prepare_batch(root, batch, hash_only_contexts=args.hash_only_contexts)
            pending.update(objects)
            pending[index_path] = encode(index)
        prepared.append(index)
    if not args.verify_only:
        existing = source_files(destination) if destination.exists() else []
        total = sum(path.stat().st_size for path in existing)
        for name, data in pending.items():
            target = destination / name
            if target.exists():
                if target.read_bytes() != data:
                    raise ValueError(f"Refusing to overwrite existing evidence: {target}")
            else:
                total += len(data)
        if total > args.max_total_bytes:
            raise ValueError(f"Publication would be {total} bytes, limit {args.max_total_bytes}")
        # Rehash all sources before publishing; incomplete changing batches are rejected.
        for index in prepared:
            for record in index["files"]:
                if file_identity(Path(record["local_path"])) != (record["sha256"], record["bytes"]):
                    raise ValueError(f"Source changed during export: {record['source']}")
        for name, data in pending.items():
            immutable_write(destination / name, data)
        for index in prepared:
            verify_index(index, destination)
    print(json.dumps({
        "verified": True, "batches": {i["batch"]: i["summary"] for i in prepared},
        "publication_total_bytes": sum(p.stat().st_size for p in source_files(destination)),
        "no_workloads_executed": True,
    }, indent=2))


if __name__ == "__main__":
    main()
