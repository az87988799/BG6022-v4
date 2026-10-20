"""Audit saved local evidence only; never invoke product or scientific execution."""

import hashlib
import json
import math
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
ROOT = PROJECT / "docs/acceptance/code-as-tools-step7-refreeze-3"
BATCH = PROJECT / "data/code-tools-refreeze-3-batch"
PRIOR_BATCH = PROJECT / "data/code-tools-refreeze-2-batch"
EARLIER_BATCH = PROJECT / "data/code-tools-refreeze-1-batch"
EARLIER_RECORD = PROJECT / "docs/acceptance/code-as-tools-step7-refreeze-2/freeze/prior-batch.json"
TASKS = ("T1", "T2", "T3", "T4", "T5", "H1", "H2")
MAX_TOTAL_BYTES = 20 * 1024 * 1024
DRIVER_FILES = {
    "start.json", "outcome.json", "pre-verify.json", "post-verify.json",
    "prepare.json", "knowledge-audit.json", "budget-check.json",
    "report.txt", "report.json", "batch-after.json",
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def checked(path, root=PROJECT):
    path, root = Path(path).absolute(), Path(root).absolute()
    require(path.is_relative_to(root), "Evidence path is outside its fixed root")
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            require(not (stat.S_ISLNK(info.st_mode)
                         or getattr(info, "st_file_attributes", 0) & 0x400),
                    "Linked/reparse evidence paths are forbidden")
    require(path.resolve().is_relative_to(root.resolve()), "Evidence path escaped its root")
    return path


def read(path):
    path = checked(path)
    require(path.stat().st_size <= MAX_TOTAL_BYTES, "JSON exceeds the audit read bound")
    return json.loads(path.read_bytes())


def digest(path):
    value = hashlib.sha256()
    with checked(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def files(root):
    checked(root)
    for directory, directories, names in os.walk(root, followlinks=False):
        for name in directories + names:
            checked(Path(directory) / name, root)
        for name in names:
            yield Path(directory) / name


def main():
    sys.path.insert(0, str(PROJECT))
    from tests.helpers.code_tools_batch import METRICS, snapshot

    target = checked(ROOT / "validation.json")
    require(not target.exists(), "Never overwrite a prior audit")
    wrapper_path = ROOT / "freeze/prior-batch.json"
    prior = read(wrapper_path)
    require(checked(prior["source_batch_root"]) == PRIOR_BATCH, "Prior batch root differs")
    require(checked(PROJECT / prior["earlier_batch_record"]) == EARLIER_RECORD,
            "Earlier batch record differs")
    require(digest(EARLIER_RECORD) == prior["earlier_batch_record_sha256"],
            "Earlier frozen record hash differs")
    earlier = read(EARLIER_RECORD)
    require(checked(earlier["source_batch_root"]) == EARLIER_BATCH, "Earlier batch root differs")
    history = [
        (EARLIER_BATCH, earlier["snapshot"], EARLIER_RECORD),
        (PRIOR_BATCH, prior["snapshot"], wrapper_path),
    ]
    original_record = PROJECT / earlier["earlier_batch_record"]
    require(digest(original_record) == earlier["earlier_batch_record_sha256"],
            "Original batch wrapper changed")
    original = read(original_record)
    original_batch = checked(original["source_batch_root"])
    require(original_batch == PROJECT / "data/code-tools-formal-batch", "Original batch root differs")
    history.insert(0, (original_batch, original["snapshot"], original_record))
    for batch, frozen, _ in history:
        require(snapshot(batch) == frozen, f"Historical batch changed: {batch.name}")

    ledger = snapshot(BATCH)
    event = read(ROOT / "freeze/product-freeze-event.json")
    require(checked(event["batch_root"]) == BATCH, "Current frozen batch root differs")
    require(set(ledger["rows"]) == {f"formal-{task}-001" for task in TASKS},
            "Missing or additional task/repetition/category slots")
    require(not ledger["overrun_metrics"], "Current batch exceeds its reserved limits")
    require(not any(ledger["totals"]["unknown_slots"].values()), "Unsettled current batch usage")
    require(all(frozen["identity"]["environment_root"] == ledger["identity"]["environment_root"]
                for _, frozen, _ in history), "Batches do not share the original environment")
    require(event["environment_root"] == ledger["identity"]["environment_root"],
            "Current environment differs from freeze")
    initialization = read(ROOT / "init-batch.json")
    initialized = json.loads(initialization["stdout"])
    require(initialization["returncode"] == 0 and initialized["reserved_slots"] == 0
            and initialized["identity"] == ledger["identity"] and not initialized["overrun_metrics"]
            and not any(value for group in initialized["totals"].values() for value in group.values()),
            "Batch initialization is missing, different or already consumed")
    evidence = BATCH / "first-repetition-evidence"
    require(read(evidence / "finished.json")["tasks"] == list(TASKS), "Driver is incomplete")
    require(not (evidence / "stopped.json").exists(), "Driver recorded a stop")
    scope = read(evidence / "scope.json")
    require((scope["tasks"], scope["repeat"], scope["references"], scope["controls"])
            == (list(TASKS), 1, 0, 0), "Driver scope differs")
    require(scope["product_commit"] == event["product_commit"], "Product commit differs")
    driver_hash = digest(ROOT / "freeze/run_first_repetition.py")
    require(scope["driver_sha256"] == driver_hash, "Driver source differs")
    evaluation_hash = digest(ROOT / "freeze/evaluation-freeze.json")
    rows, receipts, attempts, data_roots = [], [], [], {}
    for task in TASKS:
        slot = f"formal-{task}-001"
        local = evidence / task
        start, outcome = read(local / "start.json"), read(local / "outcome.json")
        data = checked(BATCH / "workspaces" / slot / "data", BATCH)
        data_roots[task] = data
        record = ledger["rows"][slot]
        require(re.fullmatch(r"run_[a-f0-9]{32}", start["run_id"]), "Invalid Run identity")
        require((start["task"], start["repeat"], start["slot_id"]) == (task, 1, slot),
                "Start belongs to a different slot")
        require(checked(start["data_root"], BATCH) == data, "Start workspace differs")
        require(start["driver_sha256"] == driver_hash, "Task used a different driver")
        require(start["run_id"] == record["run_id"], "Ledger Run binding differs")
        require(checked(record["workspace"]["data_root"], BATCH) == data,
                "Ledger workspace binding differs")
        require({p.name for p in checked(data / "runs").iterdir()} == {start["run_id"]},
                "Workspace contains a different or additional Run")
        run = read(data / "runs" / start["run_id"] / "run.json")
        require(run["id"] == outcome["run_id"] == start["run_id"], "Run identity differs")
        require(all(run[key] == outcome[key] for key in
                    ("state", "usage", "goal_status", "delivery_status")), "Run changed after outcome")
        require(outcome["error"] is None and not outcome["settlement"]["unknown_fields"],
                "Driver error or unknown settlement")
        require(record["settlement"]["usage"] == outcome["settlement"]["known_usage"]
                and not record["settlement"]["unknown_reasons"], "Settlement receipt differs")
        require(not read(local / "knowledge-audit.json")["issues"], "Knowledge audit has issues")
        require(not read(local / "budget-check.json")["slot_overruns"], "Slot exceeds its limits")
        for name in ("pre-verify.json", "post-verify.json"):
            receipt = read(local / name)
            answer = json.loads(receipt["stdout"])
            require(receipt["returncode"] == 0 and answer["status"] == "matched"
                    and answer["runtime_reobserved"] is True
                    and answer["evaluation_sha256"] == evaluation_hash,
                    f"Freeze verification failed: {task}/{name}")
            receipts.append({"task": task, "path": str(local / name),
                             "sha256": digest(local / name)})
        rows.append({"task": task, "run_id": run["id"], "state": run["state"],
                     "goal_status": run["goal_status"], "delivery_status": run["delivery_status"],
                     "usage": run["usage"], "driver_wall_seconds": outcome["driver_wall_seconds"],
                     "attempt_count": len(run["attempts"])})
        attempts.extend({"task": task, "id": a["id"], "tool": a["tool"], "state": a["state"],
                         "started": a.get("started"), "execution_kind": a.get("execution_kind")}
                        for a in run["attempts"])

    public = checked(ROOT / "evidence")
    index = read(public / "index.json")
    require(digest(public / "index.json") == (public / "index.sha256").read_text().split()[0],
            "Publication index checksum differs")
    require([row["task"] for row in index["tasks"]] == list(TASKS), "Published task scope differs")
    require(index["reference_runs"] == index["control_runs"] == 0, "Extra published run categories")
    expected_files = {public / "index.json", public / "index.sha256"}
    indexed_sources = set()

    def check(entry, source):
        source = checked(source)
        require(source not in indexed_sources, "Duplicate source index entry")
        require(source.stat().st_size == entry["bytes"] and digest(source) == entry["sha256"],
                "Indexed source bytes/hash differ")
        indexed_sources.add(source)
        if entry["published_path"]:
            payload = checked(ROOT / entry["published_path"], public)
            require(payload not in expected_files, "Duplicate published payload")
            require(payload.stat().st_size == entry["bytes"] and digest(payload) == entry["sha256"],
                    "Published payload bytes/hash differ")
            require(entry["bytes"] <= 524288, "Published payload exceeds its size limit")
            expected_files.add(payload)

    global_sources = {"scope.json": evidence / "scope.json",
                      "finished.json": evidence / "finished.json",
                      "init-batch.json": ROOT / "init-batch.json"}
    require([entry["path"] for entry in index["global_files"]] == list(global_sources),
            "Missing or additional global index entries")
    for entry in index["global_files"]:
        require(entry["published_path"] is not None, "Required global driver receipt is not published")
        check(entry, global_sources[entry["path"]])
    for published, row in zip(index["tasks"], rows):
        task = row["task"]
        data = data_roots[task]
        require(all(published[key] == row[key] for key in
                    ("run_id", "state", "goal_status", "delivery_status", "usage")),
                "Published task metadata differs")
        require(checked(published["local_data_root"], BATCH) == data, "Indexed workspace differs")
        require({entry["path"] for entry in published["driver_files"]} == DRIVER_FILES,
                "Missing or additional driver receipts")
        for entry in published["driver_files"]:
            check(entry, checked(evidence / task / entry["path"], evidence / task))
        indexed_data = set()
        for entry in published["files"]:
            source = checked(data / entry["path"], data)
            indexed_data.add(source)
            check(entry, source)
        require(indexed_data == {p for p in files(data) if p.suffix != ".lock"},
                "Workspace source inventory is incomplete or changed")
    actual_files = set(files(public))
    require(actual_files == expected_files, "Published file inventory differs")
    require(sum(p.stat().st_size for p in actual_files) == index["published_bytes"]
            and index["published_bytes"] <= MAX_TOTAL_BYTES, "Publication size differs/exceeds limit")
    known = ledger["totals"]["known_usage"]
    for metric, field in (("model_calls", "model_calls"), ("model_tokens", "model_tokens_used"),
                          ("orca_starts", "orca_starts_actual"),
                          ("internal_work_units", "internal_work_units")):
        require(known[metric] == sum(row["usage"][field] for row in rows), f"{metric} total differs")
    require(math.isclose(known["wall_seconds"], sum(row["driver_wall_seconds"] for row in rows),
                         rel_tol=1e-12, abs_tol=1e-9), "Driver duration total differs")
    for batch, frozen, _ in history:
        require(snapshot(batch) == frozen, f"Historical batch changed during audit: {batch.name}")
    require(snapshot(BATCH) == ledger, "Current batch changed during audit")
    require(digest(EARLIER_RECORD) == prior["earlier_batch_record_sha256"]
            and read(wrapper_path) == prior, "Historical records changed during audit")
    totals = [frozen["totals"] for _, frozen, _ in history] + [ledger["totals"]]
    cumulative = {category: {metric: sum(total[category][metric] for total in totals)
                             for metric in METRICS}
                  for category in ("reserved", "occupied", "known_usage", "unknown_slots")}
    result = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Local read/hash audit; no model, ORCA, network or reference execution.",
        "script_sha256": digest(Path(__file__)),
        "product_commit": scope["product_commit"], "evaluation_sha256": evaluation_hash,
        "prior_batches_unchanged": True,
        "prior_batches": [
            {"batch_root": str(batch), "frozen_record": str(record),
             "frozen_record_sha256": digest(record), "snapshot_unchanged": True,
             "totals": frozen["totals"], "overrun_metrics": frozen["overrun_metrics"]}
            for batch, frozen, record in history
        ],
        "current_batch_totals": ledger["totals"],
        "cumulative_four_batch_totals": cumulative,
        "cumulative_four_batch_known_usage": cumulative["known_usage"],
        "rows": rows, "attempts": attempts, "freeze_receipts": receipts,
        "all_14_before_after_receipts_matched": len(receipts) == 14,
        "current_unknown_settlements": 0, "current_overruns": [],
        "publication": {"indexed_source_files": len(indexed_sources),
                        "published_files": len(actual_files), "bytes": index["published_bytes"],
                        "index_sha256": digest(public / "index.json"),
                        "source_and_payload_hashes_match": True},
        "limitations": [
            "Historical unchanged claims cover batch identity, policy, receipts and ledger snapshots; "
            "this audit does not rehash all historical Run payloads.",
            "Known usage is a subtotal; unknown metric counts and occupied reservations remain separate.",
            "Resource sampling completeness and scientific validity are not established by count settlement.",
            "Supplier monetary charges, developer-agent cost and missing resource samples remain unmeasured.",
            "No repeat 2/3, reference or control execution; H1/H2 are previously exposed regression tasks.",
        ],
    }
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"known_usage": known, "cumulative_four_batch_known_usage": cumulative["known_usage"],
                      "indexed_source_files": len(indexed_sources),
                      "publication_bytes": index["published_bytes"],
                      "validation_sha256": digest(target)}))


if __name__ == "__main__":
    main()
