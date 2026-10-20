"""Local, read-only final audit; does not execute any product/scientific Tool."""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT))
from tests.helpers.code_tools_batch import snapshot

ROOT = PROJECT / "docs/acceptance/code-as-tools-step7-refreeze-1"
BATCH = PROJECT / "data/code-tools-refreeze-1-batch"
TASKS = ("T1", "T2", "T3", "T4", "T5", "H1", "H2")


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return value.hexdigest()


def main():
    prior = read(ROOT / "freeze/prior-batch.json")
    assert snapshot(Path(prior["source_batch_root"])) == prior["snapshot"]
    ledger = snapshot(BATCH)
    assert set(ledger["rows"]) == {f"formal-{t}-001" for t in TASKS}
    assert not ledger["overrun_metrics"]
    assert not any(ledger["totals"]["unknown_slots"].values())
    e = BATCH / "first-repetition-evidence"
    assert read(e / "finished.json")["tasks"] == list(TASKS)
    scope = read(e / "scope.json")
    assert (scope["tasks"], scope["repeat"], scope["references"], scope["controls"]) == (
        list(TASKS), 1, 0, 0)
    evaluation_hash = digest(ROOT / "freeze/evaluation-freeze.json")
    rows, receipts, starts, attempts = [], [], [], []
    for task in TASKS:
        local = e / task
        start, outcome = read(local / "start.json"), read(local / "outcome.json")
        data = Path(start["data_root"])
        run = read(data / "runs" / start["run_id"] / "run.json")
        assert run["id"] == outcome["run_id"] == start["run_id"]
        assert run["state"] == outcome["state"] and run["usage"] == outcome["usage"]
        assert outcome["error"] is None and not outcome["settlement"]["unknown_fields"]
        assert not read(local / "knowledge-audit.json")["issues"]
        assert not read(local / "budget-check.json")["slot_overruns"]
        for name in ("pre-verify.json", "post-verify.json"):
            receipt = read(local / name)
            answer = json.loads(receipt["stdout"])
            assert receipt["returncode"] == 0 and answer["status"] == "matched"
            assert answer["runtime_reobserved"] is True
            assert answer["evaluation_sha256"] == evaluation_hash
            receipts.append({"task": task, "path": str(local / name),
                             "sha256": digest(local / name)})
        row = {"task": task, "run_id": run["id"], "state": run["state"],
               "goal_status": run["goal_status"], "delivery_status": run["delivery_status"],
               "usage": run["usage"], "driver_wall_seconds": outcome["driver_wall_seconds"],
               "attempt_count": len(run["attempts"])}
        rows.append(row)
        attempts += [{"task": task, "id": a["id"], "tool": a["tool"], "state": a["state"],
                      "started": a.get("started"), "execution_kind": a.get("execution_kind")}
                     for a in run["attempts"]]
        starts.append(start)
    public = ROOT / "evidence"
    index = read(public / "index.json")
    assert digest(public / "index.json") == (public / "index.sha256").read_text().split()[0]
    assert [r["task"] for r in index["tasks"]] == list(TASKS)
    expected_files = {public / "index.json", public / "index.sha256"}
    indexed = 0

    def check(entry, source):
        nonlocal indexed
        assert source.stat().st_size == entry["bytes"] and digest(source) == entry["sha256"]
        indexed += 1
        if entry["published_path"]:
            target = ROOT / entry["published_path"]
            assert target.stat().st_size == entry["bytes"] and digest(target) == entry["sha256"]
            expected_files.add(target)

    for entry in index["global_files"]:
        check(entry, e / entry["path"])
    for row in index["tasks"]:
        for entry in row["driver_files"]:
            check(entry, e / row["task"] / entry["path"])
        for entry in row["files"]:
            check(entry, Path(row["local_data_root"]) / entry["path"])
    actual_files = {p for p in public.rglob("*") if p.is_file()}
    assert actual_files == expected_files
    assert sum(p.stat().st_size for p in actual_files) == index["published_bytes"]
    assert index["published_bytes"] <= 20 * 1024 * 1024
    known = ledger["totals"]["known_usage"]
    assert known["model_calls"] == sum(r["usage"]["model_calls"] for r in rows)
    assert known["model_tokens"] == sum(r["usage"]["model_tokens_used"] for r in rows)
    assert known["orca_starts"] == sum(r["usage"]["orca_starts_actual"] for r in rows)
    result = {
        "audited_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "Independent local read/hash audit; no model, ORCA, network or reference execution.",
        "script_sha256": digest(Path(__file__)),
        "product_commit": scope["product_commit"],
        "evaluation_sha256": evaluation_hash,
        "prior_batch_unchanged": True,
        "prior_batch_totals": prior["snapshot"]["totals"],
        "current_batch_totals": ledger["totals"],
        "cumulative_two_batch_known_usage": {
            k: known[k] + prior["snapshot"]["totals"]["known_usage"][k] for k in known},
        "rows": rows, "attempts": attempts, "freeze_receipts": receipts,
        "all_14_before_after_receipts_matched": len(receipts) == 14,
        "unknown_settlements": 0, "overruns": [],
        "publication": {"indexed_source_files": indexed, "published_files": len(actual_files),
                        "bytes": index["published_bytes"], "index_sha256": digest(public / "index.json"),
                        "source_and_payload_hashes_match": True},
        "limitations": [
            "Resource sampling completeness is independent from known model/ORCA count settlement.",
            "Supplier monetary charges, developer-agent cost and missing collector/resource sampling are unmeasured.",
            "No repeat 2/3, reference or control execution; H1/H2 are previously exposed regression tasks."
        ],
    }
    target = ROOT / "validation.json"
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    print(json.dumps({"known_usage": known, "indexed_source_files": indexed,
                      "publication_bytes": index["published_bytes"],
                      "validation_sha256": digest(target)}))


if __name__ == "__main__":
    main()
