"""One explicit, gate-only D4 transfer on the existing acceptance ledger.

The adopted scope and limit authority remain unchanged. No approval is built in;
only an external reviewed record can publish this separate execution overlay.
"""

import copy
import hashlib
from pathlib import Path

from tests.helpers import phase_b_cycle_formal_amendment as formal
from tests.helpers import phase_b_repair_cycle as cycle

APPROVAL_ID = "repair-cycle-development-4-20261008"
AUTHORITY_KEY = "development_authority"
PROPOSAL_PATH = "docs/reviews/2026-10-08-repair-cycle-development-4-amendment.md"
MODEL_LIMITS = {"http_requests": 2140, "tokens": 14746912, "usd": 20}
GATE_LIMITS = {"gates-1": (8, 64000), "gates-2": (56, 448000),
               "gates-3": (64, 512000), "gates-4": (64, 512000)}
CLOSED = tuple(f"{cycle.CYCLE_ID}-development-{n}" for n in (1, 2, 3))


def _directory(book):
    return book.root / "budget-amendments" / APPROVAL_ID


def _gate_manifest():
    # Generate from the same actual case definitions without authorizing D4.
    value = cycle.model_manifest(3)
    return {**value, "slots": {key.replace("gates-3/", "gates-4/", 1): row
                              for key, row in value["slots"].items()}}


def _proposal(before_sha256, previous_authority):
    _, reference, _ = cycle._parts()
    path = reference.PROJECT / PROPOSAL_PATH
    return {"schema_version": 1, "approval_id": APPROVAL_ID,
        "cycle_id": cycle.CYCLE_ID, "status": "proposal_only",
        "decision_document": PROPOSAL_PATH,
        "decision_document_sha256": reference.sha256_file(path) if path.is_file() else None,
        "original_cycle_approval_sha256": reference.CYCLE_APPROVAL_SHA256,
        "original_cycle_scope_sha256": cycle._digest(cycle.scope()),
        "before_sha256": before_sha256, "previous_limit_authority": previous_authority,
        "unchanged_limits": {"model": MODEL_LIMITS, "orca_starts": cycle.LIMITS["orca_starts"]},
        "development_candidates": 4, "closed_candidates": list(CLOSED),
        "transfers": [{"from": "gates-1", "to": "gates-4", "http_requests": 56, "tokens": 448000},
                      {"from": "gates-2", "to": "gates-4", "http_requests": 8, "tokens": 64000}],
        "gate_allocations": {name: {**dict.fromkeys(cycle.DIMENSIONS, 0),
                                     "http_requests": values[0], "tokens": values[1]}
                             for name, values in GATE_LIMITS.items()},
        "gate_manifest_sha256": cycle._digest(_gate_manifest()),
        "unchanged": ["original_scope", "original_approval", "history", "bound_declarations",
                      "non_gate_allocations", "single_run_limits", "execution_activity_seconds",
                      "resources", "scientific_scope", "formal_candidates", "failure_policy"]}


def proposal():
    """Read a concrete baseline; never apply, freeze, reserve, or execute."""
    book = cycle._book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        if AUTHORITY_KEY in ledger:
            return validate_applied(ledger, book.ledger)
        _baseline(ledger)
        return _proposal(hashlib.sha256(book.ledger.path.read_bytes()).hexdigest(),
                         ledger["limit_authority"])


def _validate_approval(value, before, before_sha256):
    required = {"schema_version", "approval_id", "status", "user_statement", "question_text", "proposal"}
    if (not isinstance(value, dict) or set(value) != required or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1 or value.get("approval_id") != APPROVAL_ID
            or value.get("status") != "user_approved"
            or any(not isinstance(value.get(k), str) or not value[k].strip()
                   for k in ("user_statement", "question_text"))):
        cycle._fail("D4 requires an explicit actual-user approval transcript and exact schema")
    expected = _proposal(before_sha256, before.get("limit_authority"))
    # Canonical bytes also distinguish booleans/floats from integer budgets.
    if cycle._digest(value.get("proposal")) != cycle._digest(expected):
        cycle._fail("D4 approval differs from the exact gate-only transfer and baseline")
    cycle._evidence({"path": expected["decision_document"], "sha256": expected["decision_document_sha256"]})
    return expected


def _baseline(ledger, *, allow_pending=False):
    _, reference, _ = cycle._parts()
    if AUTHORITY_KEY in ledger or ledger.get("limits") != {
            "model": MODEL_LIMITS, "orca_starts": cycle.LIMITS["orca_starts"]}:
        cycle._fail("D4 must follow the unchanged applied formal allowance")
    formal.validate_applied(ledger, reference.BatchLedger())
    state = cycle._state(ledger, allow_development_pending=allow_pending)
    cycle._no_unknown(ledger, state)
    if set(state["candidates"]) != set(CLOSED):
        cycle._fail("D4 needs exactly the three completed original development candidates")
    for number, expected in ((1, 8), (2, 8), (3, 12)):
        label, allocation = CLOSED[number - 1], f"gates-{number}"
        owned = [s for s in state["slots"].values() if s["candidate"] == label]
        if (any(s["allocation"] != allocation for s in owned)
                or sum(s["declared"]["http_requests"] for s in owned) != expected
                or sum(s["declared"]["tokens"] for s in owned) != expected * 8000
                or any(s["declared"][d] for s in owned for d in cycle.DIMENSIONS[2:])):
            cycle._fail("D4 donor occupancy differs from the approved 8/8/12 gate declarations")
        if any(s["receipt"]["sha256"] not in state["outcomes"] for s in owned):
            cycle._fail("D4 cannot close a slot pending independent review")
        if not any(o["candidate"] == label and o["status"] == "failed" for o in state["outcomes"].values()):
            cycle._fail("D4 requires retained failure evidence for every closed candidate")
    third = [o for o in state["outcomes"].values() if o["candidate"] == CLOSED[2]]
    if sorted(o["status"] for o in third) != ["failed", "passed", "passed"]:
        cycle._fail("D4 baseline must retain the actual two passes and one failure")
    return state


def validate_applied(ledger, book):
    """Validate independent authority plus unchanged old scope/costs/receipts."""
    _, reference, _ = cycle._parts()
    authority = ledger.get(AUTHORITY_KEY, {})
    directory = _directory(book)
    before_path, approval_path, receipt_path = (directory / n for n in ("before.json", "approval.json", "amendment.json"))
    if (set(authority) != {"approval_id", "approval_sha256", "receipt_sha256"}
            or authority.get("approval_id") != APPROVAL_ID
            or not all(p.is_file() for p in (before_path, approval_path, receipt_path))
            or reference.sha256_file(approval_path) != authority["approval_sha256"]
            or reference.sha256_file(receipt_path) != authority["receipt_sha256"]):
        cycle._fail("D4 lacks its immutable applied approval and receipt")
    before, approval, receipt = (reference._json(p) for p in (before_path, approval_path, receipt_path))
    p = _validate_approval(approval, before, reference.sha256_file(before_path))
    formal.validate_applied(before, book)
    if (AUTHORITY_KEY in before or before.get("limits") != p["unchanged_limits"]
            or ledger.get("limits") != before["limits"]
            or ledger.get("limit_authority") != before.get("limit_authority")
            or {k: v for k, v in receipt.items() if k != "applied_at"} != {
                "approval_id": APPROVAL_ID, "approval_sha256": authority["approval_sha256"],
                "before_sha256": p["before_sha256"], "proposal_sha256": cycle._digest(p)}):
        cycle._fail("D4 altered cumulative limits, predecessor authority or approved receipt")
    book._validate_preserved_costs(before, ledger)
    old, current = before.get("repair_cycle", {}), ledger.get("repair_cycle", {})
    if old.get("scope_sha256") != current.get("scope_sha256"):
        cycle._fail("D4 changed the original cycle scope")
    for kind, entries in old.items():
        if isinstance(entries, dict) and any(current.get(kind, {}).get(k) != v for k, v in entries.items()):
            cycle._fail("D4 lost an existing cycle receipt")
    old_slots = {s["receipt"]["sha256"] for s in old.get("slots", {}).values()}
    for key, slot in current.get("slots", {}).items():
        if key not in old.get("slots", {}) and slot["candidate"] in CLOSED:
            cycle._fail("D4 cannot add a slot to a closed candidate")
    for key, activity in current.get("activities", {}).items():
        if key not in old.get("activities", {}) and activity["slot_receipt_sha256"] in old_slots:
            cycle._fail("D4 cannot restart a closed execution activity")
    return p


def validate_if_present(ledger, book, *, allow_pending=False):
    if AUTHORITY_KEY in ledger:
        return validate_applied(ledger, book)
    if _directory(book).exists() and not allow_pending:
        cycle._fail("D4 publication is incomplete; reconcile the original approval before execution")
    return None


def allocations(ledger):
    _, reference, _ = cycle._parts()
    values = formal.allocations(ledger)
    applied = validate_if_present(ledger, reference.BatchLedger())
    if applied:
        values.update(applied["gate_allocations"])
    return values


def assert_open(ledger, candidate):
    _, reference, _ = cycle._parts()
    applied = validate_if_present(ledger, reference.BatchLedger())
    if applied and candidate in CLOSED:
        cycle._fail("old development candidate is closed to new execution by D4")
    if candidate == f"{cycle.CYCLE_ID}-development-4" and not applied:
        cycle._fail("candidate 4 needs its explicit applied development amendment")


def fourth_authorized(ledger=None):
    _, reference, _ = cycle._parts()
    book = reference.BatchLedger()
    ledger = book.snapshot() if ledger is None else ledger
    if not validate_if_present(ledger, book):
        cycle._fail("candidate 4 needs its explicit applied development amendment")


def apply(*, approval_path=None, approval_sha256=None, execute=False, fault=None):
    """Explicit operator-only application; the model cannot call this helper."""
    if not execute or approval_path is None or not isinstance(approval_sha256, str):
        cycle._fail("D4 needs explicit execute, approval path and exact SHA")
    _, reference, _ = cycle._parts()
    path = Path(approval_path).resolve(strict=True)
    if path.stat().st_size > 256 * 1024:
        cycle._fail("D4 approval exceeds the bounded record size")
    raw = path.read_bytes()
    if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() != approval_sha256:
        cycle._fail("operator-approved D4 record bytes changed")
    value = reference._json(path)
    book = cycle._book()
    with book.ledger._lock():
        current = book._snapshot_unlocked()
        directory = _directory(book.ledger)
        before_path, saved_approval, receipt_path = (directory / n for n in ("before.json", "approval.json", "amendment.json"))
        if AUTHORITY_KEY in current:
            validate_applied(current, book.ledger)
            if current[AUTHORITY_KEY]["approval_sha256"] != approval_sha256:
                cycle._fail("D4 is already bound to another approval")
            return reference._json(receipt_path)
        original = book.ledger.path.read_bytes()
        p = _validate_approval(value, current, hashlib.sha256(original).hexdigest())
        _baseline(current, allow_pending=True)
        from orca_agent.models import utc_now
        from orca_agent.store import atomic_write
        for destination, content in ((before_path, original), (saved_approval, raw)):
            if destination.exists():
                if destination.read_bytes() != content:
                    cycle._fail("interrupted D4 baseline or approval changed; reconcile first")
            else:
                atomic_write(destination, content, immutable=True)
        if fault:
            fault("after_original_snapshot")
        details = {"approval_id": APPROVAL_ID, "approval_sha256": approval_sha256,
                   "before_sha256": p["before_sha256"], "proposal_sha256": cycle._digest(p)}
        if receipt_path.exists():
            receipt = reference._json(receipt_path)
            if {k: v for k, v in receipt.items() if k != "applied_at"} != details:
                cycle._fail("interrupted D4 receipt differs from exact approval")
        else:
            receipt = {**details, "applied_at": utc_now().isoformat()}
            reference._save(receipt_path, receipt, immutable=True)
        if fault:
            fault("after_amendment_receipt")
        after = copy.deepcopy(current)
        after[AUTHORITY_KEY] = {"approval_id": APPROVAL_ID, "approval_sha256": approval_sha256,
                                "receipt_sha256": reference.sha256_file(receipt_path)}
        book._save(after)
        if fault:
            fault("after_ledger_publication")
        validate_applied(book._snapshot_unlocked(), book.ledger)
        return receipt
