"""One explicit, restartable application of the user's approved cumulative caps.

No model/ORCA execution, new ledger, receipt rewriting, or cost reset occurs.
The original ledger bytes and an immutable approval-bound receipt precede the
atomic publication. The existing batch lock excludes reservations throughout.
"""

from __future__ import annotations

import argparse
import copy
import json

from orca_agent.models import utc_now
from orca_agent.store import Store, atomic_write, sha256_file
from tests.helpers import phase_b_budget as budget

reference = budget.reference


def apply_approved_limits(book, *, execute=False, fault=None):
    if not execute:
        raise reference.ReferenceBlocked("budget migration requires explicit --apply")
    ledger = book.ledger
    with ledger._lock():
        reference.limit_approval()
        if not ledger.path.is_file():
            raise reference.ReferenceBlocked("migration requires the existing ledger; cannot initialize or reset it")
        before = book._snapshot_unlocked(allow_legacy_limits=True)
        directory = ledger.root / "budget-amendments" / reference.LIMIT_APPROVAL_ID
        receipt_path, before_path = directory / "amendment.json", directory / "before.json"
        if before["limits"] == reference.LIMITS:
            if before.get("limit_authority", {}).get("origin") != "amendment":
                raise reference.ReferenceBlocked("existing ledger is not this approved migration's original batch")
            return reference._json(receipt_path)
        if before["limits"] != reference.ORIGINAL_LIMITS or "limit_authority" in before:
            raise reference.ReferenceBlocked("migration source is not the exact old cumulative limit profile")
        original = ledger.path.read_bytes()
        digest = sha256_file(ledger.path)
        if before_path.exists():
            if before_path.read_bytes() != original:
                raise reference.ReferenceBlocked("interrupted migration baseline changed; reconcile without replacement")
        else:
            atomic_write(before_path, original, immutable=True)
        if fault:
            fault("after_original_snapshot")
        immutable = {"schema_version": 1, "approval_id": reference.LIMIT_APPROVAL_ID,
                     "approval_sha256": reference.LIMIT_APPROVAL_SHA256,
                     "previous_limits": reference.ORIGINAL_LIMITS, "approved_limits": reference.LIMITS,
                     "before_sha256": digest, "preserved_model_usage": before["model_usage"],
                     "preserved_entry_counts": {kind: len(before.get(kind, {}))
                                                for kind in ("entries", "model_records", "agent_science")}}
        if receipt_path.exists():
            receipt = reference._json(receipt_path)
            if {k: v for k, v in receipt.items() if k != "applied_at"} != immutable:
                raise reference.ReferenceBlocked("interrupted budget amendment differs from its approval or baseline")
        else:
            receipt = {**immutable, "applied_at": utc_now().isoformat()}
            reference._save(receipt_path, receipt, immutable=True)
        if fault:
            fault("after_amendment_receipt")
        updated = copy.deepcopy(before)
        updated["limits"] = copy.deepcopy(reference.LIMITS)
        updated["limit_authority"] = {**reference.initial_limit_authority(), "origin": "amendment",
                                      "receipt_sha256": sha256_file(receipt_path)}
        reference._save(ledger.path, updated)
        if fault:
            fault("after_ledger_publication")
        checked = book._snapshot_unlocked()
        if any(checked.get(k) != v for k, v in before.items() if k != "limits"):
            raise reference.ReferenceBlocked("budget migration changed existing accounting")
        return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="apply the exact recorded user approval once")
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({"approval": reference.limit_approval(), "applied": False}, ensure_ascii=False, indent=2))
        return 0
    book = budget.AcceptanceBudget(Store(reference.BATCH_ROOT / "reference"))
    receipt = apply_approved_limits(book, execute=True)
    print(json.dumps({"applied": True, "receipt": receipt,
                      "current_model_usage": book.snapshot()["model_usage"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
