"""Read archived H1/H2 facts only. No product imports, network or execution."""

import hashlib
import json
import stat
from collections import Counter
from decimal import Decimal
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
BATCH = PROJECT / "data/code-tools-refreeze-2-batch"
REVIEW = Path(__file__).resolve().parent


def checked(path, root=PROJECT):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise ValueError("Outside review input root")
    for part in (path, *path.parents):
        if part.exists():
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Linked review input")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Resolved input escaped root")
    return path


def sha(path):
    digest = hashlib.sha256()
    with checked(path).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    path = checked(path)
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("JSON exceeds review read limit")
    return json.loads(path.read_bytes())


def ref(path):
    path = checked(path)
    return {"path": path.relative_to(PROJECT).as_posix(),
            "sha256": sha(path), "bytes": path.stat().st_size}


def result_fingerprint(value):
    # Match the frozen documented Result identity without importing production.
    value = json.loads(json.dumps(value))
    if not value.get("observation_records"):
        value.pop("observation_records", None)
    else:
        for row in value["observation_records"]:
            for key, empty in (("formula", None), ("artifact_ids", [])):
                if row.get(key) == empty:
                    row.pop(key, None)
            for source in row.get("sources", []):
                if not source.get("actual_conditions"):
                    source.pop("actual_conditions", None)
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def snapshot(task):
    if task not in {"H1", "H2"}:
        raise ValueError("Only assigned holdout tasks")
    evidence = BATCH / "first-repetition-evidence" / task
    start, outcome = read(evidence / "start.json"), read(evidence / "outcome.json")
    data = checked(BATCH / "workspaces" / f"formal-{task}-001" / "data", BATCH)
    if checked(start["data_root"], BATCH) != data:
        raise ValueError("Start data binding differs")
    run_path = data / "runs" / start["run_id"] / "run.json"
    run = read(run_path)
    if any(run[key] != outcome[key] for key in ("state", "usage", "goal_status", "delivery_status")):
        raise ValueError("Run is not the settled outcome")
    rp = run_path.parent
    frozen = REVIEW.parent / "freeze"
    task_spec = next(row for row in read(frozen / "holdout-selection.json")["tasks"]
                     if row["id"] == task)
    originals = data.parent / "original.txt"
    if sha(originals) != start["original_sha256"]:
        raise ValueError("Original prompt bytes differ")
    requests = [(1, rp / "request-revisions/1.json", read(rp / "request-revisions/1.json"))]
    plans = []
    for path in (rp / "decisions").glob("*.json"):
        row = read(path)
        if row.get("request"):
            requests.append((row["request"]["version"], path, row["request"]))
        if row.get("plan"):
            plans.append((row["plan"]["version"], path, row["plan"]))
    _, request_path, request = max(requests, key=lambda row: row[0])
    results = {path.stem: read(path) for path in (rp / "results").glob("*.json")}
    artifacts = {}
    for path in (data / "artifacts").glob("*/artifact.json"):
        meta = read(path)
        body = checked(data / meta["path"], data)
        actual = sha(body)
        artifacts[meta["id"]] = {"metadata": meta, "metadata_file": ref(path),
            "body_file": ref(body), "bytes_match": actual == meta["sha256"]
                and body.stat().st_size == meta["size"],
            "run_matches": meta["run_id"] == run["id"]}
    model_records = []
    for row in run["model_records"]:
        path = rp / "model" / (row["id"] + ".request.json")
        response_path = path.with_name(row["id"] + ".response.json")
        request_body, response = read(path), read(response_path)
        texts = "\n".join(str(message.get("content", ""))
                          for message in request_body.get("messages", []))
        model_records.append({"id": row["id"], "status": row["status"],
            "created_at": row["created_at"], "model": row["model"],
            "action": (response.get("proposal") or {}).get("action"),
            "request_file": ref(path), "response_file": ref(response_path),
            "request_hash_matches": sha(path) == row["request_hash"],
            "response_record_hash_matches": sha(response_path) == row.get("response_record_sha256"),
            "original_full_task_visible": task_spec["original_text"] in texts,
            "goal_ids_visible": [goal["id"] for goal in request["goals"] if goal["id"] in texts],
            "prompt_tokens": row.get("input_tokens"), "completion_tokens": row.get("output_tokens"),
            "total_tokens": row.get("total_tokens"), "cost_known_usd": row.get("cost_known_usd")})
    source_bindings, knowledge = [], []
    for rid, result in results.items():
        for name, row in result.get("source", {}).get("files", {}).items():
            if isinstance(row, dict) and row.get("artifact_id"):
                artifact = artifacts[row["artifact_id"]]
                source_bindings.append({"result_id": rid, "name": name,
                    "artifact_id": row["artifact_id"], "matches":
                        row.get("sha256") == artifact["body_file"]["sha256"]
                        and artifact["bytes_match"] and row["artifact_id"] in result["artifact_ids"]})
        excerpt = result.get("observations", {}).get("document_excerpt")
        if isinstance(excerpt, dict):
            artifact = artifacts[excerpt["document_artifact_id"]]
            call = next(call for call in run["calls"] if call["id"] == result["call_id"])
            knowledge.append({"result_id": rid, "call_id": call["id"],
                "url": excerpt.get("url"), "document_id": excerpt.get("document_id"),
                "operation_status": result["operation_status"],
                "archive_complete": excerpt.get("document_archive_complete"),
                "document_sha256": excerpt.get("document_sha256"),
                "artifact_id": excerpt["document_artifact_id"],
                "body_file": artifact["body_file"],
                "bound": (artifact["bytes_match"]
                    and artifact["body_file"]["sha256"] == excerpt.get("document_sha256")
                    and artifact["body_file"]["bytes"] == excerpt.get("document_bytes")
                    and artifact["metadata"]["source"]["call_id"] == call["id"]
                    and call["result_id"] == rid)})
    consumption = []
    for call in run["calls"]:
        for role, binding in call.get("consumption", {}).items():
            if isinstance(binding, dict) and binding.get("result_fingerprint"):
                prior = results[binding["result_id"]]
                consumption.append({"call_id": call["id"], "role": role,
                    "producer_result_id": prior["id"],
                    "result_fingerprint_matches":
                        result_fingerprint(prior) == binding["result_fingerprint"],
                    "artifact_hashes_match": all(artifacts[aid]["body_file"]["sha256"] == digest
                        for aid, digest in binding["artifact_hashes"].items())})
    index = {"run": ref(run_path), "original": ref(originals), "request_source": ref(request_path),
             "driver": {name: ref(evidence / name) for name in (
                 "start.json", "outcome.json", "pre-verify.json", "post-verify.json",
                 "budget-check.json", "knowledge-audit.json")}}
    audit = {"evidence_index": index, "final_state": outcome["state"],
        "delivery_status": outcome["delivery_status"], "goal_status": outcome["goal_status"],
        "usage": outcome["usage"], "settlement": outcome["settlement"],
        "driver_wall_seconds": outcome["driver_wall_seconds"],
        "model_receipts": model_records, "model_receipt_totals": {
            "calls": len(model_records),
            "total_tokens": sum(row.get("total_tokens") or 0 for row in model_records),
            "known_cost_usd": str(sum((Decimal(row["cost_known_usd"] or "0")
                                      for row in model_records), Decimal(0)))},
        "call_counts": dict(Counter(call["tool"] for call in run["calls"])),
        "artifact_count": len(artifacts),
        "all_artifact_bytes_hashes_and_run_match":
            all(a["bytes_match"] and a["run_matches"] for a in artifacts.values()),
        "knowledge_archive_bindings": knowledge, "result_source_bindings": source_bindings,
        "input_consumption_bindings": consumption,
        "artifacts": {aid: {"role": a["metadata"]["role"], "source": a["metadata"]["source"],
                           "body_file": a["body_file"]} for aid, a in artifacts.items()},
        "results": [{"id": rid, "file": ref(rp / "results" / (rid + ".json")),
                     "operation_status": result["operation_status"], "call_id": result.get("call_id"),
                     "attempt_id": result.get("attempt_id"),
                     "qualified_output_ports": list(result["qualified_outputs"]),
                     "diagnostics": result["diagnostics"]} for rid, result in results.items()],
        "pre_post_freeze_matched": all(read(evidence / name)["returncode"] == 0
                                      for name in ("pre-verify.json", "post-verify.json")),
        "budget_overruns": read(evidence / "budget-check.json")["slot_overruns"]}
    return {"audit": audit, "run": run, "request": request, "results": results,
            "artifacts": artifacts, "plans": plans, "outcome": outcome, "task_spec": task_spec}
