"""Read existing local receipts; no network, model, or science execution."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from orca_agent.store import Store


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


root = Path("data/knowledge-completion")
destination = Path(__file__).parent
ledger = read(root / "ledger.json")
store = Store("data")
records = []
for item in ledger["runs"]:
    directory = Path("data/runs") / item["run_id"]
    run = read(directory / "run.json")
    request = store.load_request(store.load_run(item["run_id"])).model_dump(mode="json")
    answers, documents, reads = [], [], []
    for identifier in run["result_ids"]:
        result = read(directory / "results" / f"{identifier}.json")
        observation = result["observations"]
        if answer := observation.get("knowledge_answer"):
            answers.append({key: answer.get(key) for key in (
                "answer", "limitations", "classification", "scientific_qualification")})
        if document := observation.get("document_excerpt"):
            documents.append({key: document.get(key) for key in (
                "url", "title", "accessed_at", "document_sha256", "status", "coverage")})
        if result["source"].get("tool", "").startswith("evidence."):
            reads.append({"result_id": identifier, "tool": result["source"]["tool"],
                          "operation_status": result["operation_status"],
                          "observation_kinds": list(observation), "artifact_ids": result["artifact_ids"]})
    case, index = item["case"], item["index"]
    verdict = ("content_passed" if case == "saved" or case == "concept" and index in (1, 4, 5, 6)
               else "content_partial" if case in ("concept", "manual") else "not_passed")
    records.append({**item, "request_text": request["original_text"], "run_state": run["state"],
        "goal_status": run["goal_status"], "usage": run["usage"], "independent_content_review": verdict,
        "answers": answers, "documents": documents, "reads": reads,
        "terminal_contracts": [d["contract_status"] for d in run["terminal_deliveries"]],
        "diagnostics": run["diagnostics"],
        "cost_usd_upper": str(sum((Decimal(m.get("cost_known_usd", m.get("cost_reserved_usd", "0")))
                                   for m in run["model_records"]), Decimal(0))),
        "run_sha256": digest(directory / "run.json"),
        "model_receipts": {p.name: digest(p) for p in sorted((directory / "model").glob("*.json"))}})
usage = {key: sum(r["usage"][key] for r in records) for key in (
    "model_calls", "model_tokens_used", "model_tokens_unknown", "knowledge_queries",
    "orca_starts_actual", "identity_queries", "structure_preparations")}
manifest = {"baseline": "9e6ac3f9ae742c085665e3a39d99a8f27b6b8bfa", "status": "awaiting_user_acceptance",
    "limits": ledger["limits"], "usage": usage,
    "cost_usd_upper": str(sum((Decimal(r["cost_usd_upper"]) for r in records), Decimal(0))),
    "candidate_scope": "Incremental development receipts, not a formal fixed-candidate suite.",
    "d4_ledger_sha256": digest(Path("data/phase-b/batch-ledger.json")),
    "web": read(root / "web-checks.json"), "runs": records}
assert usage["model_calls"] <= ledger["limits"]["model_calls"]
assert usage["model_tokens_used"] + usage["model_tokens_unknown"] <= ledger["limits"]["model_tokens"]
assert Decimal(manifest["cost_usd_upper"]) <= Decimal(ledger["limits"]["cost_usd"])
assert usage["orca_starts_actual"] == usage["identity_queries"] == usage["structure_preparations"] == 0
assert manifest["d4_ledger_sha256"] == "52c3308a75723025325f221537678595212b02f574b30c37d5295a039a906608"
(destination / "receipts.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"usage": usage, "cost_usd_upper": manifest["cost_usd_upper"],
                  "runs": len(records)}, ensure_ascii=False))
