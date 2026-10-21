"""Publish bounded evidence only; never copy task workspaces or credentials."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "data/live-repair"
DEST = ROOT / "docs/acceptance/live-failure-repair-20261021"
FILE_LIMIT = 262144
PACKAGE_LIMIT = 16 * 1024 * 1024
INDEX_RESERVE = 1024 * 1024
TEXT_SUFFIXES = {".out", ".err", ".txt", ".log", ".inp", ".xyz", ".csv", ".tsv", ".json", ".py", ".md", ".dat"}
RUNTIME_NAMES = {"execution.json", "intent.json", "started.json", "boundary.json", "environment.json",
                 "configuration.json", "inputs.json", "permission.json", "worker-outcome.json"}
ATTEMPT_RECORD_SUFFIXES = {".json", ".txt", ".md", ".patch"}
_published_bytes = 0
_dry_run = "--dry-run" in sys.argv
_planned = {}
REVIEW_RECORDS = (
    "a025-knowledge-window-replay-v89.json", "a025-python-runtime-layout-replay.json",
    "a026-native-goal-route-after.json", "a026-scoped-comparison-replay.json",
    "a027-capability-context-replay.json", "a027-balance-diagnosis-replay.json",
    "hcn-h2-native-source-readiness-v2.json", "live-attempt-matrix-a027.json",
    "live-attempt-matrix-a027.md",
)
AUDIT_DRIVERS = (
    "native_raw_sources.py", "audit_t5_raw_v3.py", "hcn_raw_audit_helpers.py",
    "h2_raw_audit_helpers.py", "audit_accounting_through_a027.py",
    "replay_a025_knowledge_windows.py", "replay_a025_python_runtime_layout.py",
    "replay_a026_native_routes_after.py", "replay_a027_capability_context.py",
)
REPORT_RECORDS = (
    "repair-report.md", "targeted-validation.json", "provider-service-block-a027.json",
    "remaining-six-task-exit-checklist.json", "native-source-adapter-verification-a010.json",
)
RUNTIME_RECORDS = (
    "selection.json", "selection-r1-before-r2.json",
    "build-r1/runtime-manifest.json", "build-r1/final-image-id.txt",
    "build-r2/runtime-manifest.json", "build-r2/final-image-id.txt",
    "context-r2/context-manifest.json", "context-r2/wheelhouse-manifest.json",
    "context-r2/license-manifest.json",
)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def file_sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def credential_scan(raw):
    # Compare actual configured secret values, never generic words like token.
    names = {"DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AZURE_OPENAI_API_KEY"}
    names.update(name for name in os.environ if name.upper().endswith(
        ("_API_KEY", "_TOKEN", "_SECRET_KEY", "_PASSWORD")))
    assert not any(os.environ.get(name) and os.environ[name].encode() in raw for name in names), \
        "Credential found; publication refused"


def controlled(root, relative):
    assert isinstance(relative, str) and not re.search(r"[\\:]", relative), "Invalid evidence path"
    assert all(part not in {"", ".", ".."} for part in relative.split("/")), "Invalid evidence path"
    path = (root / relative).resolve()
    assert path.is_relative_to(root.resolve()), "Evidence path escapes archive"
    return path


def publish(source, destination):
    size = source.stat().st_size
    digest = file_sha(source)
    entry = {"sha256": digest, "bytes": size, "local_ref": source.resolve().as_posix()}
    if size > FILE_LIMIT:
        return {**entry, "published": False, "omission_reason": "single_file_byte_limit"}
    raw = source.read_bytes()
    assert len(raw) == size and sha(raw) == digest, "Source changed during publication"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return {**entry, "published": False, "omission_reason": "non_utf8_or_binary"}
    if b"\x00" in raw:
        return {**entry, "published": False, "omission_reason": "non_utf8_or_binary"}
    return {**entry, **publish_payload(raw, destination)}


def attempt_records(folder):
    """Top-level evidence only; never traverse model archives or workspaces."""
    records = []
    for path in sorted(folder.iterdir()):
        name = path.name.casefold()
        if not path.is_file() or path.suffix.casefold() not in ATTEMPT_RECORD_SUFFIXES:
            continue
        if (name.startswith((".env", "model.", "model-", "model_"))
                or name in {"environment.json", "env.json", "credentials.json", "secrets.json",
                            "request.json", "response.json", "delivery.json", "request.txt", "response.txt"}
                or name.endswith((".request.json", ".response.json", ".delivery.json"))):
            continue
        assert path.resolve().is_relative_to(folder.resolve()), "Attempt evidence escapes archive"
        records.append(path)
    patch_record = folder / "source-patch.json"
    if patch_record.is_file() and (folder / "source.patch").is_file():
        metadata = json.loads(patch_record.read_bytes())
        assert file_sha(folder / "source.patch") == metadata["patch_sha256"], "Source patch hash mismatch"
        assert (folder / "source.patch").stat().st_size == metadata["bytes"], "Source patch size mismatch"
    return records


def campaign_records():
    current = CONTROL / "campaign.json"
    campaign = json.loads(current.read_bytes())
    records = []
    if campaign["version"] == 2:
        previous = CONTROL / "campaign-v1-original.json"
        original = json.loads(previous.read_bytes())
        assert original["version"] == 1, "Unexpected original campaign version"
        assert file_sha(previous) == campaign["previous_version_sha256"], "Campaign chain hash mismatch"
        assert original["baseline_sha256"] == campaign["baseline_sha256"], "Campaign baseline changed"
        assert original["baseline_usage"] == campaign["baseline_usage"], "Campaign baseline usage changed"
        records.append(previous)
    else:
        assert campaign["version"] == 1, "Unknown campaign chain; review before publication"
    return [*records, current]


def publish_payload(raw, destination):
    global _published_bytes
    size = len(raw)
    assert size <= FILE_LIMIT, "Generated publication file exceeds byte limit"
    entry = {"sha256": sha(raw), "bytes": size}
    credential_scan(raw)
    if destination in _planned:
        assert _planned[destination] == sha(raw), "Planned immutable evidence changed"
    elif destination.exists():
        assert destination.read_bytes() == raw, "Published immutable evidence changed"
    elif _published_bytes + size > PACKAGE_LIMIT - INDEX_RESERVE:
        return {**entry, "published": False, "omission_reason": "package_byte_limit"}
    else:
        if not _dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(raw)
        _planned[destination] = sha(raw)
        _published_bytes += size
    return {**entry, "path": destination.relative_to(DEST).as_posix(), "published": True}


def raw_evidence(folder, run_id):
    """Only exact Result membership and same-Run, hash-bound Artifacts qualify."""
    data = folder / "data"
    run_path = controlled(data, f"runs/{run_id}/run.json")
    run = json.loads(run_path.read_bytes())
    assert run["id"] == run_id, "Run identity mismatch"
    attempts = {row["id"]: row for row in run["attempts"]}
    calls = {row["id"]: row for row in run["calls"]}
    result_rows, indexed = [], {}
    for result_id in run["result_ids"]:
        result_path = controlled(data, f"runs/{run_id}/results/{result_id}.json")
        result = json.loads(result_path.read_bytes())
        assert (result["id"], result["run_id"]) == (result_id, run_id), "Result identity mismatch"
        producer = attempts.get(result.get("attempt_id")) or calls.get(result.get("call_id"))
        assert producer and producer.get("result_id") == result_id, "Result producer mismatch"
        assert result.get("step_id") == producer.get("step_id"), "Result Step mismatch"
        result_rows.append({"result_id": result_id, **publish(result_path,
            DEST / "attempts" / folder.name / "raw" / "results" / result_path.name)})
        references = result.get("source", {}).get("files", {})
        assert isinstance(references, dict), "Invalid Result file index"
        for reference in references.values():
            assert reference["artifact_id"] in result["artifact_ids"], "Unbound Result file reference"
        for artifact_id in result["artifact_ids"]:
            metadata_path = controlled(data, f"artifacts/{artifact_id}/artifact.json")
            artifact = json.loads(metadata_path.read_bytes())
            assert (artifact["id"], artifact.get("run_id")) == (artifact_id, run_id), "Artifact owner mismatch"
            assert artifact.get("attempt_id") is None or artifact["attempt_id"] in attempts, "Unknown Artifact Attempt"
            assert artifact["path"].startswith(f"artifacts/{artifact_id}/files/"), "Artifact path mismatch"
            path = controlled(data, artifact["path"])
            assert path.stat().st_size == artifact["size"] and file_sha(path) == artifact["sha256"], "Artifact hash mismatch"
            keys = [key for key, ref in references.items() if ref["artifact_id"] == artifact_id]
            assert all(references[key].get("sha256") == artifact["sha256"] for key in keys), "Result hash mismatch"
            if artifact_id in indexed:
                indexed[artifact_id]["result_ids"].append(result_id)
                indexed[artifact_id]["file_keys"] = sorted(set(indexed[artifact_id]["file_keys"] + keys))
                continue
            entry = {"artifact_id": artifact_id, "result_ids": [result_id], "file_keys": keys,
                "source_attempt_id": artifact.get("attempt_id"),
                "role": artifact["role"], "metadata_sha256": file_sha(metadata_path),
                "sha256": artifact["sha256"], "bytes": artifact["size"], "local_ref": path.as_posix()}
            name = path.name.casefold()
            omitted = ("excluded_binary_or_runtime" if path.suffix.casefold() not in TEXT_SUFFIXES
                or name in RUNTIME_NAMES or name.endswith((".request.json", ".delivery.json", ".response.json"))
                or name.startswith((".env", "environment", "orca-launch-transport", "orca-collector-")) else None)
            if omitted is None and artifact["size"] <= FILE_LIMIT:
                raw = path.read_bytes()
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    omitted = "non_utf8_or_binary"
                if b"\x00" in raw:
                    omitted = "non_utf8_or_binary"
            if omitted:
                entry.update(published=False, omission_reason=omitted)
            else:
                entry.update(publish(path, DEST / "attempts" / folder.name / "raw" / "artifacts" / artifact_id / path.name))
            indexed[artifact_id] = entry
    # Separate bounded pages keep the top-level index below the same file cap.
    pages, page = [], []
    for entry in indexed.values():
        candidate = json.dumps({"run_id": run_id, "artifacts": [*page, entry]}, ensure_ascii=False).encode()
        if page and len(candidate) > FILE_LIMIT:
            pages.append(publish_payload(json.dumps({"run_id": run_id, "artifacts": page},
                ensure_ascii=False).encode(), DEST / "attempts" / folder.name / "raw" / f"artifacts-{len(pages):03d}.json"))
            page = []
        page.append(entry)
    if page:
        pages.append(publish_payload(json.dumps({"run_id": run_id, "artifacts": page},
            ensure_ascii=False).encode(), DEST / "attempts" / folder.name / "raw" / f"artifacts-{len(pages):03d}.json"))
    return {"run_record_sha256": file_sha(run_path), "results": result_rows,
            "artifact_count": len(indexed), "artifact_index_pages": pages, "local_data_ref": data.resolve().as_posix()}


def main():
    global _published_bytes
    _published_bytes = sum(path.stat().st_size for path in DEST.rglob("*") if path.is_file()) if DEST.exists() else 0
    assert _published_bytes <= PACKAGE_LIMIT, "Existing publication exceeds package limit"
    # Reserve actual bytes for reviewable costs, frozen inputs and runtime proof
    # before optional old failure payloads can consume the bounded package.
    for path in sorted({*campaign_records(), *CONTROL.glob("accounting-through-a*.json"),
                        *CONTROL.glob("protocol-diagnosis-*.json"), *CONTROL.glob("model-catalog-*.json")}):
        publish(path, DEST / path.name)
    for name in REPORT_RECORDS:
        publish(CONTROL / name, DEST / name)
    for name in REVIEW_RECORDS:
        publish(ROOT / "data/workflow-root-repair" / name, DEST / "validation" / name)
    for name in AUDIT_DRIVERS:
        publish(ROOT / "data/workflow-root-repair" / name, DEST / "audit-drivers" / name)
    for name in RUNTIME_RECORDS:
        publish(CONTROL / "runtime" / name, DEST / "runtime" / name)
    for name in ("prepare_attempt.py", "run_attempt.py", "run_runtime_probe.py", "continue_original_tasks.py",
                 "publish.py", "source_patch.py", "run_python_log_probe.py", "prepare_p002_ledger_gate.py",
                 "select_verified_runtime_r2.py"):
        publish(CONTROL / name, DEST / "drivers" / name)
    for folder in sorted((CONTROL / "attempts").glob("a*")):
        for pattern in ("attempt.json", "outcome.json", "start.json", "configuration.json", "original.txt",
                        "independent-raw-check*.json", "manual-review*.json"):
            for path in sorted(folder.glob(pattern)):
                publish(path, DEST / "attempts" / folder.name / path.name)
    for folder in sorted((CONTROL / "probes").glob("*")):
        for name in ("probe.json", "outcome.json", "case.json", "case-result.json", "configuration.json", "start.json"):
            if (folder / name).is_file():
                publish(folder / name, DEST / "probes" / folder.name / name)
    rows, unsettled = [], []
    def publication_priority(folder):
        outcome_path = folder / "outcome.json"
        outcome = json.loads(outcome_path.read_bytes()) if outcome_path.is_file() else {}
        completed = outcome.get("state") == "completed" or outcome.get("delivery_status") == "complete"
        return (not completed, -int(folder.name.removeprefix("a")))

    # Complete scientific deliveries precede earlier failed runs when nearing the cap.
    for folder in sorted((CONTROL / "attempts").glob("a*"), key=publication_priority):
        meta = json.loads((folder / "attempt.json").read_bytes())
        config_path = folder / "configuration.json"
        assert file_sha(config_path) == meta["configuration_sha256"], "Candidate configuration changed"
        config = json.loads(config_path.read_bytes())
        model_metadata = {"model_name": config["model"]["model_name"],
                          "model_profile": config.get("model_profile"),
                          "task_code_policy": config.get("task_code_policy")}
        if not (folder / "outcome.json").is_file():
            unsettled.append({"candidate": folder.name, "task": meta["task"],
                "state": "active_not_settled", "metadata_only": True,
                "model_metadata": model_metadata,
                "source_manifest_sha256": meta["source_manifest_sha256"],
                "configuration_sha256": meta["configuration_sha256"],
                "local_ref": folder.resolve().as_posix()})
            continue
        outcome = json.loads((folder / "outcome.json").read_bytes())
        files = [publish(path, DEST / "attempts" / folder.name / path.name) for path in attempt_records(folder)]
        driver = Path(meta.get("driver", str(Path(meta["snapshot"]) / "run_attempt.py")))
        if driver.is_file():
            if meta.get("driver_sha256"):
                assert sha(driver.read_bytes()) == meta["driver_sha256"]
            files.append(publish(driver, DEST / "attempts" / folder.name / "run-driver.py"))
        else:
            missing_driver = {"published": False, "omission_reason": "missing_historical_driver",
                              "local_ref": driver.resolve().as_posix(),
                              "snapshot_local_ref": Path(meta["snapshot"]).resolve().as_posix()}
            if meta.get("driver_sha256"):
                missing_driver["expected_sha256"] = meta["driver_sha256"]
            files.append(missing_driver)
        rows.append({"candidate": folder.name, "task": meta["task"], "run_id": outcome["run_id"],
            "state": outcome["state"], "delivery_status": outcome["delivery_status"],
            "model_metadata": model_metadata,
            "known_usage": outcome["known_usage"], "unknown_fields": outcome["unknown_fields"],
            "source_manifest_sha256": meta["source_manifest_sha256"], "files": files,
            "raw_evidence": raw_evidence(folder, outcome["run_id"])})
    probes = []
    for folder in sorted((CONTROL / "probes").glob("*")):
        if not (folder / "outcome.json").is_file():
            continue
        files = [publish(folder / name, DEST / "probes" / folder.name / name) for name in (
            "probe.json", "outcome.json", "case.json", "case-result.json", "configuration.json", "start.json")
            if (folder / name).is_file()]
        probes.append({"id": folder.name, "files": files})
    drivers = [publish(CONTROL / name, DEST / "drivers" / name) for name in (
        "prepare_attempt.py", "run_attempt.py", "run_runtime_probe.py", "continue_original_tasks.py",
        "publish.py", "source_patch.py", "run_python_log_probe.py", "prepare_p002_ledger_gate.py",
        "select_verified_runtime_r2.py")]
    campaign_chain = [publish(path, DEST / path.name) for path in campaign_records()]
    index = {"kind": "continuous_live_development_not_formal_repetitions", "campaign": campaign_chain[-1],
        "campaign_version_chain": campaign_chain,
        "attempts": rows, "unsettled_candidates": unsettled,
        "developer_execution_probes": probes, "drivers": drivers,
        "report_records": [publish(CONTROL / name, DEST / name) for name in REPORT_RECORDS],
        "failure_replay_records": [publish(ROOT / "data/workflow-root-repair" / name,
            DEST / "validation" / name) for name in REVIEW_RECORDS],
        "audit_drivers": [publish(ROOT / "data/workflow-root-repair" / name,
            DEST / "audit-drivers" / name) for name in AUDIT_DRIVERS],
        "runtime_selection_records": [publish(CONTROL / "runtime" / name,
            DEST / "runtime" / name) for name in RUNTIME_RECORDS],
        "independent_reference": "not_run", "control_execution": "not_run",
        "publication_limits": {"single_file_bytes": FILE_LIMIT, "package_bytes": PACKAGE_LIMIT,
                               "index_reserve_bytes": INDEX_RESERVE, "truncation": "never"},
        "limitations": ["Failed attempts and unknown costs are retained.",
            "A completed observation report does not verify every free-text user obligation.",
            "Only bound small text evidence is published; omitted files retain hashes and local original references."]}
    audits = sorted((path for path in CONTROL.glob("accounting-through-a*.json")
                     if re.fullmatch(r"accounting-through-a\d+\.json", path.name)),
                    key=lambda path: int(re.search(r"a(\d+)\.json$", path.name)[1]))
    if audits:
        index["historical_accounting_audits"] = [publish(path, DEST / path.name) for path in audits]
        index["historical_accounting_audit"] = index["historical_accounting_audits"][-1]
        index["historical_accounting_cutoff"] = audits[-1].stem.removeprefix("accounting-through-")
    supplemental_records = sorted({path for pattern in (
        "protocol-diagnosis-*.json", "model-catalog-*.json", "accounting-through-*-probe-gate.json",
        "accounting-through-*-p002.json") for path in CONTROL.glob(pattern)
        if path.is_file()})
    index["supplemental_records"] = [publish(path, DEST / path.name) for path in supplemental_records]
    if not _dry_run:
        DEST.mkdir(parents=True, exist_ok=True)
    index_raw = (json.dumps(index, ensure_ascii=False, indent=2) + "\n").encode()
    assert len(index_raw) <= FILE_LIMIT, "Publication index exceeds single-file limit"
    credential_scan(index_raw)
    index_path = DEST / "index.json"
    prior_index_size = index_path.stat().st_size if index_path.exists() else 0
    assert _published_bytes - prior_index_size + len(index_raw) <= PACKAGE_LIMIT, "Publication index exceeds package limit"
    if not _dry_run:
        index_path.write_bytes(index_raw)
    print(json.dumps({"dry_run": _dry_run, "attempts": len(rows), "probes": len(probes),
        "payload_bytes": _published_bytes, "index_bytes": len(index_raw),
        "package_bytes": _published_bytes - prior_index_size + len(index_raw),
        "index": str(DEST / "index.json")}))


if __name__ == "__main__":
    main()
