"""Drive the seven frozen first formal repetitions; contains no task solution.

Run from the repository with its frozen Python. Evidence stays outside each
product workspace. No retries, reference jobs, or control jobs are added here.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT))
FREEZE = Path(__file__).resolve().parent
EVENT = json.loads((FREEZE / "product-freeze-event.json").read_bytes())
BATCH = Path(EVENT["batch_root"])
EVIDENCE = BATCH / "first-repetition-evidence"
TASKS = ("T1", "T2", "T3", "T4", "T5", "H1", "H2")


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def cli(action, path, *arguments):
    started = now()
    command = [sys.executable, "scripts/code_tools_acceptance.py", action,
               "--inspect-runtime", "--directory", str(FREEZE.relative_to(PROJECT)), *arguments]
    result = subprocess.run(command, cwd=PROJECT, capture_output=True,
                            text=True, encoding="utf-8")
    write(path, {"started_at_utc": started, "finished_at_utc": now(),
                 "command": command, "returncode": result.returncode,
                 "stdout": result.stdout, "stderr": result.stderr,
                 "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest()})
    if result.returncode:
        raise RuntimeError(f"{action} failed; retained at {path}")
    return json.loads(result.stdout)


def audit_knowledge(store, run, evidence):
    frozen_index = json.loads((FREEZE / "product-freeze.json").read_bytes())["contract"]["knowledge"]
    previous = {}
    for path in EVIDENCE.glob("*/knowledge-audit.json"):
        for item in json.loads(path.read_bytes())["documents"]:
            previous[(item["document_id"], item["document_version"])] = item["document_sha256"]
    documents, issues = [], []
    for identifier in run.result_ids:
        result = store.load_result(run.id, identifier)
        document = result.observations.get("document_excerpt")
        if not document or not document.get("document_sha256"):
            continue
        for field in ("index_version", "index_sha256"):
            if field in frozen_index and document[field] != frozen_index[field]:
                issues.append({"kind": "knowledge_index_changed", "field": field})
        key = (document["document_id"], document["document_version"])
        digest = document["document_sha256"]
        if key in previous and previous[key] != digest:
            issues.append({"kind": "document_changed", "document": list(key)})
        previous[key] = digest
        excerpt_hash = hashlib.sha256(document["excerpt"].encode("utf-8")).hexdigest()
        if excerpt_hash != document["excerpt_sha256"]:
            issues.append({"kind": "excerpt_hash_mismatch", "result_id": identifier})
        body_archive = {"status": "missing"}
        artifact_id = document.get("document_artifact_id")
        try:
            if (not document.get("document_archive_complete")
                    or artifact_id not in result.artifact_ids):
                raise ValueError("retrieval has no complete bound body artifact")
            artifact = store.load_artifact(artifact_id)
            path = store.artifact_path(artifact_id)
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if (actual_hash != digest or artifact.run_id != run.id
                    or artifact.source.get("call_id") != result.call_id
                    or artifact.size != document.get("document_bytes")):
                raise ValueError("retrieval body identity differs")
            body_archive = {"status": "verified", "artifact_id": artifact_id,
                            "path": str(path), "bytes": artifact.size,
                            "sha256": actual_hash,
                            "byte_semantics": artifact.source["byte_semantics"]}
        except (ValueError, OSError, RuntimeError, KeyError) as exc:
            issues.append({"kind": "document_body_archive_mismatch",
                           "result_id": identifier, "reason": str(exc)})
        documents.append({"result_id": identifier, **document,
                          "independently_rehashed_excerpt": excerpt_hash,
                          "raw_body_archive": body_archive})
    write(evidence / "knowledge-audit.json", {
        "audited_at_utc": now(), "knowledge_queries": run.usage.knowledge_queries,
        "documents": documents, "issues": issues,
        "coverage": "Independently hash the exact body archived by this retrieval and its excerpt; "
                    "verify Run/Call/Result/Artifact binding and compare same-document digests.",
    })
    return issues


def run_one(task):
    from orca_agent.config import Config
    from orca_agent.entrypoints import execute
    from orca_agent.natural import initialize_text
    from orca_agent.report import build_report, render_report
    from orca_agent.store import Store, default_environment_root
    from tests.helpers.code_tools_batch import bind_run, settle_slot, snapshot

    evidence = EVIDENCE / task
    evidence.mkdir(exist_ok=False)
    cli("verify", evidence / "pre-verify.json")
    manifest = cli("prepare", evidence / "prepare.json",
                   "--task", task, "--repeat", "1", "--category", "formal")
    workspace = Path(manifest["data_root"]).parent
    config = Config.model_validate_json((workspace / "config.json").read_bytes())
    assert str(default_environment_root().resolve()) == manifest["environment_root"]
    assert not any(config.data_root.iterdir())
    store = Store(config.data_root, environment_root=default_environment_root(),
                  configuration=config.run_configuration())
    original = (workspace / "original.txt").read_bytes()
    run = initialize_text(store, config, original.decode("utf-8"))
    bind_run(BATCH, manifest["slot_id"], run.id)
    write(evidence / "start.json", {
        "started_at_utc": now(), "task": task, "repeat": 1,
        "slot_id": manifest["slot_id"], "run_id": run.id,
        "data_root": str(config.data_root),
        "environment_root": str(default_environment_root().resolve()),
        "original_sha256": hashlib.sha256(original).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "method_registration": "Use immutable product Request/Plan/code/input records "
                               "with their launch ordering; no external solver or callback.",
    })
    print(json.dumps({"event": "started", "task": task, "run_id": run.id}), flush=True)
    started = time.monotonic()
    error = None
    try:
        run = execute(store, config, run.id)
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        run = store.load_run(run.id)
    elapsed = time.monotonic() - started
    usage = run.usage.model_dump(mode="json")
    known = {"model_calls": usage["model_calls"], "wall_seconds": elapsed}
    unknown = {}
    if usage["model_tokens_unknown"]:
        unknown["model_tokens"] = (
            f'Unknown model receipts retain {usage["model_tokens_unknown"]} reserved tokens; '
            f'known token subtotal {usage["model_tokens_used"]}')
    else:
        known["model_tokens"] = usage["model_tokens_used"]
    unsettled = [a.id for a in run.attempts if a.state in {"intent", "running", "unknown"}]
    unsettled += [c.request_key for c in run.internal_calls
                  if c.state in {"reserved", "running", "unknown"}]
    if unsettled or run.state == "unknown" or error:
        reason = "Unreconciled execution or driver exception: " + ", ".join(unsettled)
        unknown["orca_starts"] = reason
        unknown["internal_work_units"] = unknown["orca_starts"]
    else:
        known["orca_starts"] = usage["orca_starts_actual"]
        known["internal_work_units"] = usage["internal_work_units"]
    settle_slot(BATCH, manifest["slot_id"], known_usage=known, unknown_fields=unknown)
    write(evidence / "outcome.json", {
        "finished_at_utc": now(), "run_id": run.id, "state": run.state,
        "goal_status": run.goal_status, "delivery_status": run.delivery_status,
        "usage": usage, "driver_wall_seconds": elapsed, "error": error,
        "settlement": {"known_usage": known, "unknown_fields": unknown},
        "independent_reference": "not_run_in_this_user_authorized_first_round",
    })
    cli("verify", evidence / "post-verify.json")
    report = build_report(store, run)
    write(evidence / "report.json", report)
    with (evidence / "report.txt").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(render_report(report))
    batch_after = snapshot(BATCH)
    write(evidence / "batch-after.json", batch_after)
    reservation = batch_after["rows"][manifest["slot_id"]]["slot"]["reservation"]
    overruns = {key: value for key, value in known.items() if value > reservation[key]}
    write(evidence / "budget-check.json", {"slot_overruns": overruns})
    knowledge_issues = audit_knowledge(store, run, evidence)
    print(json.dumps({"event": "finished", "task": task, "state": run.state,
                      "usage": usage, "error": error}), flush=True)
    if unknown or unsettled or run.state == "unknown" or error or knowledge_issues or overruns:
        raise RuntimeError("Execution or knowledge evidence requires reconciliation; "
                           "remaining task slots not started")


def main():
    # Restore the original desktop process context, using the existing global
    # environment/lease root from the freeze event. Never select a new root.
    original_local = os.environ.get("LOCALAPPDATA")
    frozen_environment = Path(EVENT["environment_root"])
    if not frozen_environment.is_dir():
        raise RuntimeError("Frozen global environment root is missing")
    os.environ["LOCALAPPDATA"] = str(frozen_environment.parent.parent)
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if len(sys.argv) == 3 and sys.argv[1] == "--task":
        if sys.argv[2] not in TASKS:
            raise ValueError("Only the seven first formal repetitions are allowed")
        run_one(sys.argv[2])
        return
    if len(sys.argv) != 1:
        raise ValueError("No additional batches or repetitions are supported")
    EVIDENCE.mkdir(exist_ok=False)
    write(EVIDENCE / "scope.json", {
        "created_at_utc": now(), "user_scope": "停止离线测试，开始真实LLM测试",
        "tasks": list(TASKS), "repeat": 1, "references": 0, "controls": 0,
        "original_process_localappdata": original_local,
        "restored_process_localappdata": os.environ["LOCALAPPDATA"],
        "frozen_environment_root": str(frozen_environment),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "product_commit": EVENT["product_commit"],
    })
    for task in TASKS:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                                 "--task", task], cwd=PROJECT)
        if result.returncode:
            write(EVIDENCE / "stopped.json", {
                "at_utc": now(), "task": task, "returncode": result.returncode,
                "reason": "Driver, freeze, or reconciliation error; no automatic retry",
            })
            try:
                cli("verify", EVIDENCE / f"{task}-driver-failure-verify.json")
            except RuntimeError as exc:
                print(json.dumps({"event": "failure_verification_failed", "reason": str(exc)}), flush=True)
            raise SystemExit(result.returncode)
    write(EVIDENCE / "finished.json", {"at_utc": now(), "tasks": list(TASKS)})


if __name__ == "__main__":
    main()
