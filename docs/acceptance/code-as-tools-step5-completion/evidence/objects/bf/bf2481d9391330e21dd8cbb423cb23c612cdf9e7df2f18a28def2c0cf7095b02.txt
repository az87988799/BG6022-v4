"""Read final unique Run ledgers once; never execute product code or query an API.

Run only after the coordinator declares the continuation finished. The initial
live005 outcome and its continuation describe one Run and are never summed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path


LIMITS = {"runs": 6, "model_calls": 72, "model_tokens": 3_600_000, "orca_starts": 12}
TERMINAL = {"completed", "failed", "cancelled", "budget_exhausted", "waiting_user", "paused", "unknown"}


def read_json(path):
    before = path.stat()
    raw = path.read_bytes()
    after = path.stat()
    if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
        raise RuntimeError("Source changed while being read: " + str(path))
    return json.loads(raw), {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
                             "bytes": len(raw)}


def nonnegative(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return amount if amount.is_finite() and amount >= 0 else None


def amount_sum(amounts):
    values = [item for item in amounts if item is not None]
    return str(sum(values, Decimal(0))) if values else None


def counter(usage, name, gaps):
    value = usage.get(name)
    if type(value) is not int or value < 0:
        gaps.append("missing_or_invalid_counter:" + name)
        return None
    return value


def known_sum(rows, field):
    values = [row[field] for row in rows]
    return sum(value for value in values if value is not None) if any(value is not None for value in values) else None


def summarize_run(name, run, source):
    usage = run.get("usage", {})
    gaps, seen, records = [], set(), []
    duplicate_ids = []
    for record in run.get("model_records", []):
        identifier = record.get("id")
        if not isinstance(identifier, str) or identifier in seen:
            duplicate_ids.append(identifier)
            continue
        seen.add(identifier)
        records.append(record)
    if duplicate_ids:
        gaps.append("duplicate_or_missing_model_record_id")
    known = [record for record in records if record.get("status") == "known"]
    unsettled = [record for record in records if record.get("status") != "known"]
    known_tokens = [nonnegative(record.get("total_tokens")) for record in known]
    reserved_tokens = [(nonnegative(record.get("input_reserved")), nonnegative(record.get("output_reserved")))
                       for record in unsettled]
    known_fees = [nonnegative(record.get("cost_known_usd")) for record in known]
    reserved_fees = [nonnegative(record.get("cost_reserved_usd")) for record in unsettled]
    if any(value is None for value in known_tokens):
        gaps.append("known_model_token_accounting_incomplete")
    if any(left is None or right is None for left, right in reserved_tokens):
        gaps.append("unknown_model_token_reservation_incomplete")
    if any(value is None for value in known_fees):
        gaps.append("known_tokens_with_unknown_fee")
    if any(value is None for value in reserved_fees):
        gaps.append("unsettled_model_fee_reservation_unknown")
    accounting_tokens = int(sum((value for value in known_tokens if value is not None), Decimal(0)))
    accounting_unknown = int(sum((left + right for left, right in reserved_tokens
                                  if left is not None and right is not None), Decimal(0)))
    counts = {key: counter(usage, key, gaps) for key in (
        "model_calls", "model_tokens_used", "model_tokens_unknown", "orca_starts_reserved",
        "orca_starts_actual", "extra_orca_starts_reserved", "internal_work_units", "code_retries",
        "postprocess_starts", "analysis_executions", "plan_revisions", "evidence_reads",
        "identity_queries", "structure_preparations", "knowledge_queries")}
    reconciled = (counts["model_calls"] == len(records)
                  and counts["model_tokens_used"] == accounting_tokens
                  and counts["model_tokens_unknown"] == accounting_unknown
                  and not any("token_accounting" in gap or "token_reservation" in gap for gap in gaps))
    if not reconciled:
        gaps.append("run_usage_and_model_accounting_differ")
    attempts = run.get("attempts", [])
    python_attempts = [item for item in attempts if item.get("execution_kind", "orca") == "python"]
    internal = run.get("internal_calls", [])
    unknown_attempts = [item.get("id") for item in attempts if item.get("state") in {"intent", "running", "unknown"}]
    unknown_internal = [item.get("request_key") for item in internal if item.get("state") in {"reserved", "running", "unknown"}]
    resources = {}
    for key in ("cpu_seconds", "elapsed_seconds"):
        value = nonnegative(usage.get(key))
        resources[key + "_recorded_lower_bound"] = str(value) if value is not None else None
        if value is None:
            gaps.append("resource_unknown:" + key)
    resources.update(complete=usage.get("resource_usage_complete") is True,
                     unknown_attempt_ids=unknown_attempts, unknown_internal_request_keys=unknown_internal)
    return {
        "label": name, "run_id": run["id"], "state": run["state"],
        "delivery_status": run.get("delivery_status"), "created_at": run.get("created_at"),
        "original_deadline": run.get("deadline"), "run_source": source,
        "model": {"calls": counts["model_calls"], "tokens_known": counts["model_tokens_used"],
            "tokens_unknown_reserved": counts["model_tokens_unknown"],
            "accounting_record_count": len(records), "known_call_count": len(known),
            "unknown_call_count": len(unsettled), "accounting_known_tokens": accounting_tokens,
            "accounting_unknown_reserved_tokens": accounting_unknown, "reconciled": reconciled,
            "duplicate_record_ids": duplicate_ids, "currency": "USD",
            "known_token_fee_upper_bound": amount_sum(known_fees),
            "unknown_call_fee_reservations": amount_sum(reserved_fees),
            "known_fee_missing_count": sum(value is None for value in known_fees),
            "unknown_fee_reservation_missing_count": sum(value is None for value in reserved_fees),
            "cost_basis": "frozen_run_prices_from_recorded_model_accounting",
            "provider_billing_queried": False, "provider_charged_amount": None,
            "record_facts": [{key: record[key] for key in ("id", "status", "total_tokens", "input_tokens",
                "output_tokens", "input_reserved", "output_reserved", "cost_known_usd", "cost_reserved_usd",
                "error_category") if key in record} for record in records]},
        "compute": {"orca_actual": counts["orca_starts_actual"], "orca_reserved": counts["orca_starts_reserved"],
            "extra_orca_reserved": counts["extra_orca_starts_reserved"],
            "python_attempts": len(python_attempts), "python_attempt_states": dict(Counter(item.get("state") for item in python_attempts)),
            "code_retries_recorded": counts["code_retries"],
            "additional_python_attempts_effective": max(counts["code_retries"] or 0, len(python_attempts) - 1, 0),
            "internal_work_units_reserved": counts["internal_work_units"],
            "internal_call_count": len(internal), "internal_call_states": dict(Counter(item.get("state") for item in internal)),
            "postprocess_starts": counts["postprocess_starts"], "analysis_executions": counts["analysis_executions"]},
        "resources": resources, "other_cumulative_usage": {key: counts[key] for key in (
            "plan_revisions", "evidence_reads", "identity_queries", "structure_preparations", "knowledge_queries")},
        "gaps": gaps,
    }


def aggregate(batch):
    rows, sources, unique = [], [], set()
    for number in range(1, 7):
        label = f"live-{number:03}"
        location, location_source = read_json(batch / label / "run-location.json")
        run_path = Path(location["data_root"]) / "runs" / location["run_id"] / "run.json"
        run, source = read_json(run_path)
        if run.get("id") != location["run_id"] or run["id"] in unique:
            raise RuntimeError("Run identity differs or was already counted: " + label)
        if run.get("state") not in TERMINAL:
            raise RuntimeError("Wait for the coordinator's final outcome before aggregating: " + label)
        unique.add(run["id"])
        rows.append(summarize_run(label, run, source))
        sources.extend([location_source, source])
    model_rows = [row["model"] for row in rows]
    compute_rows = [row["compute"] for row in rows]
    known_calls = known_sum(model_rows, "calls")
    known_tokens = known_sum(model_rows, "tokens_known")
    unknown_tokens = known_sum(model_rows, "tokens_unknown_reserved")
    orca_actual = known_sum(compute_rows, "orca_actual")
    orca_reserved = known_sum(compute_rows, "orca_reserved")
    occupancy = {
        "runs": len(rows),
        "model_calls": sum(max(row["model"]["calls"] or 0, row["model"]["accounting_record_count"]) for row in rows),
        "model_tokens": sum(max((row["model"]["tokens_known"] or 0) + (row["model"]["tokens_unknown_reserved"] or 0),
            row["model"]["accounting_known_tokens"] + row["model"]["accounting_unknown_reserved_tokens"]) for row in rows),
        "orca_starts": sum(max(row["compute"]["orca_actual"] or 0, row["compute"]["orca_reserved"] or 0) for row in rows),
    }
    incomplete = any(row["gaps"] for row in rows)
    result = {
        "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "six unique Run final ledgers; live005 initial outcome and continuations counted once",
        "read_only": True, "API_calls": 0, "run_or_product_modified": False,
        "runs": rows, "unique_run_count": len(rows),
        "totals": {"model_calls": known_calls, "model_tokens_known": known_tokens,
            "model_tokens_unknown_reserved": unknown_tokens,
            "known_token_fee_upper_bound_usd": amount_sum(nonnegative(row["known_token_fee_upper_bound"]) for row in model_rows),
            "unknown_call_fee_reservations_usd": amount_sum(nonnegative(row["unknown_call_fee_reservations"]) for row in model_rows),
            "provider_charged_amount": None, "provider_billing_queried": False,
            "fee_complete": not any(row["known_fee_missing_count"] or row["unknown_fee_reservation_missing_count"] for row in model_rows),
            "orca_actual": orca_actual, "orca_reserved": orca_reserved,
            "python_attempts": sum(row["python_attempts"] for row in compute_rows),
            "code_retries_recorded": known_sum(compute_rows, "code_retries_recorded"),
            "additional_python_attempts_effective": known_sum(compute_rows, "additional_python_attempts_effective"),
            "internal_work_units_reserved": known_sum(compute_rows, "internal_work_units_reserved"),
            "internal_call_count": sum(row["internal_call_count"] for row in compute_rows),
            "cpu_seconds_recorded_lower_bound": amount_sum(nonnegative(row["resources"]["cpu_seconds_recorded_lower_bound"]) for row in rows),
            "elapsed_seconds_recorded_lower_bound": amount_sum(nonnegative(row["resources"]["elapsed_seconds_recorded_lower_bound"]) for row in rows),
            "resource_sampling_complete": all(row["resources"]["complete"] for row in rows)},
        "batch_budget": {"limits": LIMITS, "occupancy_including_unknown_reservations": occupancy,
            "remaining": {key: max(0, LIMITS[key] - occupancy[key]) for key in LIMITS},
            "exceeded": {key: max(0, occupancy[key] - LIMITS[key]) for key in LIMITS},
            "complete_accounting": not incomplete,
            "meaning": "Remaining values include unknown reservations; gaps make reported occupancy a lower bound, not permission to spend."},
        "sources": sources,
        "limitations": ["Provider actual billing is unqueried; unknown fees are not zero.",
            "Resource counters are recorded lower bounds, especially when sampling is incomplete.",
            "No scientific result or success status is inferred from accounting.",
            "Continuation never clears prior Run consumption or original deadline."],
    }
    # A final report must not combine different versions of a ledger.
    for source in sources:
        if hashlib.sha256(Path(source["path"]).read_bytes()).hexdigest() != source["sha256"]:
            raise RuntimeError("Source changed during aggregation: " + source["path"])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="cumulative-costs-final.json")
    args = parser.parse_args()
    batch = Path(__file__).resolve().parent
    output = (batch / args.output).resolve()
    if output.parent != batch.resolve() or output.exists():
        raise RuntimeError("Choose a new output filename within this batch directory.")
    result = aggregate(batch)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": str(output), "totals": result["totals"], "batch_budget": result["batch_budget"]},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
