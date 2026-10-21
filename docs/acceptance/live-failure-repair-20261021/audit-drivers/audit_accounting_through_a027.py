"""Append settled a027 costs; keep HTTP 402 token reservation unknown."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from native_raw_sources import discover_native_sources

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "data/live-repair"
seen = {}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def ref(path):
    raw = path.read_bytes()
    digest = sha(raw)
    assert path not in seen or seen[path] == digest
    seen[path] = digest
    return {"path": path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else str(path),
            "sha256": digest, "bytes": len(raw)}


def read(path):
    ref(path)
    return json.loads(path.read_bytes())


def canonical(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def unchanged():
    return all(sha(path.read_bytes()) == digest for path, digest in seen.items())


parent_path = BASE / "accounting-through-a026-p002.json"
assert ref(parent_path)["sha256"] == "be0eecbf10fb8583037055c51c56c75dfe536c32bd2807dd0707a72d7654afa1"
parent = read(parent_path)
assert ref(ROOT / parent["inherited_ledger"]["path"])["sha256"] == parent["inherited_ledger"]["sha256"]
campaign = read(BASE / "campaign.json")
assert ref(BASE / "campaign.json")["sha256"] == parent["campaign"]["sha256"]
reconciliations = []
for path in sorted(BASE.rglob("*reconcil*.json")):
    identity = ref(path)
    inherited = next(row for row in parent["all_existing_reconciliations_reviewed"] if row["path"] == identity["path"])
    assert identity["sha256"] == inherited["sha256"]
    reconciliations.append({**identity, "treatment": "Already included in authenticated parent; not added again"})
assert len(reconciliations) == len(parent["all_existing_reconciliations_reviewed"])
folder = BASE / "attempts/a027"
metadata, outcome, start = (read(folder / name) for name in ("attempt.json", "outcome.json", "start.json"))
run_dir = folder / "data/runs" / start["run_id"]
run = read(run_dir / "run.json")
frozen, config = read(run_dir / "configuration.json"), read(folder / "configuration.json")
assert run["id"] == start["run_id"] == outcome["run_id"] == "run_bcb0cc84ad554e99af47183b52ee25ba"
assert run["state"] == outcome["state"] == "failed" and outcome["delivery_status"] == "partial"
assert run["usage"] == outcome["usage"] and run["configuration"] == frozen
assert ref(folder / "configuration.json")["sha256"] == metadata["configuration_sha256"]
assert ref(folder / "original.txt")["sha256"] == metadata["original_sha256"]
assert outcome["source_unchanged"] and outcome["unknown_fields"] == ["model_tokens"] and not outcome["unsettled_execution"]
assert not outcome["unconfirmed_start_accounting"] and outcome["unknown_usage_reservations"]["orca_starts"] == 0
assert run["usage"]["model_tokens_unknown"] == 93280
for name, identity in metadata["source_files"].items():
    observed = ref(Path(metadata["snapshot"]) / name)
    assert observed["sha256"] == identity["sha256"] and observed["bytes"] == identity["bytes"]
receipts = []
for number, record in enumerate(run["model_records"], 1):
    qpath, rpath = (run_dir / "model" / f"{record['id']}.{kind}.json" for kind in ("request", "response"))
    request, response = read(qpath), read(rpath)
    assert canonical(request) == record["request_hash"] == response["request_hash"]
    assert ref(rpath)["sha256"] == record["response_record_sha256"]
    assert record["response_hash"] == response["response_hash"]
    assert record["http_status"] == response["http_status"]
    assert record["model"] == request["model"] == frozen["model"]["model_name"] == "deepseek-v4-pro"
    assert request["thinking"] == {"type": "enabled"} and request["reasoning_effort"] == "low"
    assert record.get("cost_known_usd") is None
    if record["status"] == "known":
        assert record["http_status"] == 200 and record["usage"] == response["usage"]
        assert record["total_tokens"] == response["usage"]["total_tokens"]
        assert record["total_tokens"] == response["usage"]["prompt_tokens"] + response["usage"]["completion_tokens"]
        tokens, reservation = record["total_tokens"], 0
    else:
        assert number == 11 and record["status"] == "unknown" and record["http_status"] == 402
        assert response["usage"] is None and response["retryable"] is False and response["error_category"] == "http_error"
        assert record.get("total_tokens") is None
        tokens, reservation = None, record["input_reserved"] + record["output_reserved"]
        assert reservation == 93280
    receipts.append({"number": number, "model_id": record["id"], "status": record["status"],
        "request": ref(qpath), "receipt": ref(rpath), "request_hash": record["request_hash"],
        "recorded_http_response_sha256": record["response_hash"], "http_status": record["http_status"],
        "known_tokens": tokens, "unknown_token_reservation": reservation, "cost_known_usd": None,
        "retryable": response.get("retryable"), "error_category": response.get("error_category")})
executions = []
for attempt in run["attempts"]:
    assert attempt["execution_kind"] == "python" and attempt["state"] not in {"intent", "running", "unknown"}
    result_path = run_dir / "results" / f"{attempt['result_id']}.json"
    execution_path = folder / "data" / attempt["directory"] / "execution.json"
    result, execution = read(result_path), read(execution_path)
    assert result["run_id"] == run["id"] and result["attempt_id"] == attempt["id"]
    assert result["source"]["execution"] == execution
    assert result["operation_status"] == execution["state"] == attempt["state"]
    executions.append({"attempt_id": attempt["id"], "result_id": result["id"], "state": attempt["state"],
        "tool": attempt["tool"], "started": attempt["started"], "exit_code": execution["exit_code"],
        "result": ref(result_path), "execution": ref(execution_path),
        "resource_usage_complete": execution.get("resource_usage", {}).get("complete")})
assert len(executions) == 5 and sum(row["state"] == "failed" for row in executions) == 2
native = discover_native_sources(folder / "data", run, ref)
assert len(native) == 2 and {row["request_key"] for row in native} == {"gas_sp", "water_sp"}
assert all(row["completed"] and row["source_authenticated"] and row["actual_started_confirmed"] for row in native)
internal = [{"native_identity": row["native_identity"], "state": row["state"], "started": row["started"],
             "outer_operation_status": row["outer_operation_status"],
             "actual_start_evidence": row["receipt"]["execution_handle"]["actual_start_evidence"],
             "launch_receipt_sha256": row["receipt"]["execution_handle"]["launch_receipt_sha256"],
             "execution": row["files"]["execution.json"][0], "actual_start": row["files"]["actual-start.json"][0],
             "source_authenticated": True, "actual_started_confirmed": True} for row in native]
known = outcome["known_usage"]
assert known["model_calls"] == len(receipts) == run["usage"]["model_calls"] == 11
assert known["model_tokens"] == sum(row["known_tokens"] or 0 for row in receipts) == run["usage"]["model_tokens_used"] == 208431
assert known["orca_starts"] == run["usage"]["orca_starts_actual"] == 2
assert known["internal_work_units"] == run["usage"]["internal_work_units"] == 2
assert abs(known["wall_seconds"] - 620.547) < 1e-6
unknown_addition = dict.fromkeys(known, 0)
unknown_addition["model_tokens"] = 93280
prior_known = parent["totals"]["cumulative_known_usage_through_a026_p002"]
prior_unknown = parent["totals"]["unknown_occupied_upper_bounds_through_a026_p002"]
total = {key: prior_known[key] + known[key] for key in known}
unknown = {key: prior_unknown[key] + unknown_addition[key] for key in known}
assert unknown["model_tokens"] == 149658 and unknown["orca_starts"] == 2
occupied = {key: total[key] + unknown[key] for key in total}
ledger = {"schema": "incremental-campaign-accounting-1", "audited_at_utc": datetime.now(timezone.utc).isoformat(),
    "inherited_ledger": ref(parent_path), "inherited_totals": {"known": prior_known, "unknown_occupied": prior_unknown},
    "campaign": ref(BASE / "campaign.json"), "all_existing_reconciliations_reviewed": reconciliations,
    "settled_additions": [{"candidate": "a027", "run_id": run["id"], "state": run["state"],
        "delivery_status": outcome["delivery_status"], "known_usage": known, "unknown_usage": unknown_addition,
        "metadata": ref(folder / "attempt.json"), "outcome": ref(folder / "outcome.json"), "run": ref(run_dir / "run.json"),
        "configuration": ref(folder / "configuration.json"), "frozen_run_configuration": ref(run_dir / "configuration.json"),
        "source_manifest_sha256": metadata["source_manifest_sha256"], "frozen_source_files_verified": len(metadata["source_files"]),
        "model": {"name": config["model"]["model_name"], "profile": config["model_profile"], "thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        "model_receipts": receipts, "outer_execution_receipts": executions, "internal_execution_receipts": internal,
        "resource_usage_complete": run["usage"]["resource_usage_complete"],
        "monetary_cost": {"known_recorded_subtotal_usd": "0", "receipts_without_cost": len(receipts), "complete": False,
            "meaning": "Missing fees remain unknown, not free; the HTTP 402 has no token usage and keeps its full reservation."}}],
    "totals": {"known_additions_a027": known, "unknown_additions_a027": unknown_addition,
        "cumulative_known_usage_through_a027": total, "unknown_occupied_upper_bounds_through_a027": unknown,
        "budget_occupied_through_a027": occupied, "campaign_v2_limits": campaign["limits"],
        "remaining_after_snapshot_usage_excluding_later_reservations": {key: campaign["limits"][key] - occupied[key] for key in occupied}},
    "clarifications": ["The authenticated parent includes p002 once; neither its runtime gate nor any earlier cost is added again.",
        "11 submitted model calls include ten HTTP 200 known-token receipts and one non-retryable HTTP 402 with no usage. The final 93280-token reservation remains unknown occupied cost, not zero tokens.",
        "Exactly two settled internal ORCA receipts have authenticated actual-start proof. Their original Python parents failed; two repaired Python attempts and the delta analysis completed. Reuse does not create additional ORCA starts.",
        "Run failed/partial and unresolved delivery remain historical facts. This cost ledger assigns no scientific or complete delivery qualification.",
        "Prior 56378 unknown tokens and two unknown starts remain occupied; current unknown executions and unsettled executions are zero. Resource sampling is incomplete.",
        "Only current candidate evidence and immediate parent/reconciliation hashes were authenticated; historical raw runs were not re-audited. No model, ORCA, scientific calculation or product test was executed."],
    "original_files_unchanged": unchanged(), "verified_source_file_count": len(seen),
    "audit_script": ref(Path(__file__)), "native_discovery_script": ref(Path(__file__).with_name("native_raw_sources.py"))}
payload = (json.dumps(ledger, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
assert len(payload) <= 262144 and unchanged()
destination = BASE / "accounting-through-a027.json"
with destination.open("xb") as stream:
    stream.write(payload)
assert unchanged()
print(json.dumps({"path": str(destination), "bytes": len(payload), "sha256": sha(payload),
    "known": total, "unknown": unknown, "occupied": occupied, "original_files_unchanged": unchanged()}))
