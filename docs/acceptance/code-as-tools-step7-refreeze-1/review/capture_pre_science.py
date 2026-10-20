"""Read-only product observation; writes immutable review records only.

Never imports the product, executes task code, reads scientific output, or starts
model/ORCA work. Captures approved Request/Plan and source inputs while the Run's
persisted actual ORCA-start counter is zero. This is observation, not proof that
every input existed before admission; the terminal audit must reconcile timing.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import time


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read(path: pathlib.Path):
    data = path.read_bytes()
    return json.loads(data.decode("utf-8")), {
        "path": str(path.resolve()),
        "sha256": digest(data),
        "bytes": len(data),
    }


def observe(workspace: pathlib.Path, task: str, review: pathlib.Path):
    evidence = workspace / "data/code-tools-refreeze-1-batch/first-repetition-evidence" / task
    start_path = evidence / "start.json"
    if not start_path.exists():
        return None
    start, start_source = read(start_path)
    data_root = pathlib.Path(start["data_root"])
    run_path = data_root / "runs" / start["run_id"] / "run.json"
    run, run_source = read(run_path)
    status = {
        "task": task,
        "run_id": start["run_id"],
        "state": run["state"],
        "request_version": run.get("request_version"),
        "plan_version": run.get("plan_version"),
        "model_calls": run["usage"]["model_calls"],
        "orca_starts_actual": run["usage"]["orca_starts_actual"],
        "attempts": len(run.get("attempts", [])),
        "outcome_exists": (evidence / "outcome.json").exists(),
    }
    # Once science has started, never inspect Request/Plan/code in this blind
    # capture loop: later revisions may incorporate observed scientific values.
    if status["orca_starts_actual"] != 0:
        return status
    approved = {}
    sources = [start_source, run_source]
    for kind in ("request", "plan"):
        revision = run.get(f"{kind}_version")
        for decision in reversed(run.get("decisions", [])):
            if decision.get(f"{kind}_version") != revision:
                continue
            path = run_path.parent / "decisions" / f"{decision['id']}.json"
            if not path.exists():
                continue
            record, source = read(path)
            value = record.get(kind)
            if value is not None and value.get("version") == revision:
                approved[kind] = value
                sources.append(source)
                break
        if kind not in approved:
            path = run_path.parent / f"{kind}-revisions" / f"{revision}.json"
            if path.exists():
                approved[kind], source = read(path)
                sources.append(source)
    inputs = []
    # Only source code and source input geometry/input text, never .out/.csv.
    for path in sorted((data_root / "artifacts").glob("*/files/*")):
        if path.is_file() and path.suffix.lower() in {".py", ".inp", ".xyz"}:
            data = path.read_bytes()
            inputs.append({
                "path": str(path.resolve()),
                "sha256": digest(data),
                "bytes": len(data),
                "text": data.decode("utf-8", errors="replace"),
            })
    signature = digest(json.dumps({
        "approved": approved,
        "inputs": [(f["path"], f["sha256"]) for f in inputs],
    }, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    output = review / f"blind-capture-{task.lower()}-{signature[:16]}.json"
    if not output.exists():
        payload = {
            "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "scope": "Source-only pre-science observation, no numeric outputs read",
            "status": status,
            "source_records": sources,
            "approved": approved,
            "source_inputs": inputs,
            "observed_orca_actual_zero": True,
            "timing_limitation": (
                "Run counter observed zero at read time; no claim of atomicity "
                "with a concurrent launch. Reconcile immutable launch chronology "
                "in terminal review. Missing actual settings remain unverified."
            ),
        }
        with output.open("x", encoding="utf-8", newline="\n") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        status["new_blind_capture"] = str(output)
    return status


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=30)
    parser.add_argument("--interval", type=float, default=30)
    args = parser.parse_args()
    workspace = pathlib.Path(__file__).resolve().parents[4]
    review = pathlib.Path(__file__).resolve().parent
    previous = None
    for iteration in range(args.iterations):
        rows = []
        for task in ("T2", "T3", "T4", "T5", "H1", "H2"):
            try:
                status = observe(workspace, task, review)
            except (OSError, ValueError, KeyError) as exc:
                status = {"task": task, "read_error": str(exc)}
            if status:
                rows.append(status)
        signature = json.dumps(rows, sort_keys=True)
        if signature != previous:
            print(json.dumps({
                "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "rows": rows,
            }, ensure_ascii=False), flush=True)
            previous = signature
        if len(rows) == 6 and all(row.get("outcome_exists") for row in rows):
            break
        if iteration + 1 < args.iterations:
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
