"""Explicit operator approval for only the two existing formal allocations.

There is no built-in approval and no second ledger. A reviewed approval path
and exact SHA are required at application; the immutable applied receipt then
owns that binding. Consequently adoption never requires editing frozen code.
"""

import copy
import hashlib
from pathlib import Path

from tests.helpers import phase_b_repair_cycle as cycle

APPROVAL_ID = "repair-cycle-formal-budget-20261008"
PROPOSAL_PATH = "docs/reviews/2026-10-08-repair-cycle-formal-delta.md"


def proposal():
    """Actual current manifest maxima, never an execution authorization."""
    from tests.helpers.phase_b_repair_cycle_execution import formal_manifest, manifest_budget
    _, reference, _ = cycle._parts()
    allocations, manifests = {}, {}
    for number in (1, 2):
        name = f"formal-{number}"
        manifest = formal_manifest(number)
        maxima = manifest_budget(manifest)[name]
        previous = dict(zip(cycle.DIMENSIONS, cycle.ALLOCATIONS[name], strict=True))
        if any(maxima[d] != previous[d] for d in cycle.DIMENSIONS if d not in {"http_requests", "tokens"}):
            cycle._fail("formal addition cannot change ORCA or input allocations")
        if any(maxima[d] < previous[d] for d in ("http_requests", "tokens")):
            cycle._fail("formal amendment cannot shrink or transfer old allocations")
        allocations[name] = maxima
        manifests[name] = cycle._digest(manifest)
    approved = copy.deepcopy(cycle.LIMITS)
    for dimension in ("http_requests", "tokens"):
        index = cycle.DIMENSIONS.index(dimension)
        approved["model"][dimension] += sum(v[dimension] - cycle.ALLOCATIONS[k][index] for k, v in allocations.items())
    path = reference.PROJECT / PROPOSAL_PATH
    return {"schema_version": 1, "approval_id": APPROVAL_ID, "cycle_id": cycle.CYCLE_ID,
        "status": "proposal_only" if path.is_file() else "doc_pending",
        "decision_document": PROPOSAL_PATH,
        "decision_document_sha256": reference.sha256_file(path) if path.is_file() else None,
        "original_cycle_approval_sha256": reference.CYCLE_APPROVAL_SHA256,
        "original_cycle_scope_sha256": cycle._digest(cycle.scope()),
        "previous_limits": copy.deepcopy(cycle.LIMITS), "approved_limits": approved,
        "formal_allocations": allocations, "formal_manifest_sha256": manifests,
        "unchanged": ["original_scope", "original_approval", "development_allocations", "formal_e2e_allocations",
                      "usd", "orca_starts", "identity_queries", "structure_preparations", "execution_activity_seconds",
                      "single_run_limits", "failure_policy", "history"]}


def _validate_approval(value):
    _, reference, _ = cycle._parts()
    required = {"schema_version", "approval_id", "status", "user_statement", "question_text", "proposal"}
    if (set(value) != required or value.get("schema_version") != 1 or value.get("approval_id") != APPROVAL_ID
            or value.get("status") != "user_approved" or any(not isinstance(value.get(k), str) or not value[k].strip()
                                                          for k in ("user_statement", "question_text"))):
        cycle._fail("formal amendment needs an explicit actual-user approval transcript and exact schema")
    p = value["proposal"]
    fields = {"schema_version", "approval_id", "cycle_id", "status", "decision_document", "decision_document_sha256",
              "original_cycle_approval_sha256", "original_cycle_scope_sha256", "previous_limits", "approved_limits",
              "formal_allocations", "formal_manifest_sha256", "unchanged"}
    if (not isinstance(p, dict) or set(p) != fields or p.get("schema_version") != 1
            or p.get("approval_id") != APPROVAL_ID or p.get("cycle_id") != cycle.CYCLE_ID
            or p.get("status") != "proposal_only" or p.get("previous_limits") != cycle.LIMITS
            or p.get("original_cycle_scope_sha256") != cycle._digest(cycle.scope())
            or p.get("original_cycle_approval_sha256") != reference.CYCLE_APPROVAL_SHA256
            or p.get("decision_document") != PROPOSAL_PATH or not p.get("decision_document_sha256")):
        cycle._fail("formal amendment does not bind the original adopted cycle")
    cycle._evidence({"path": p["decision_document"], "sha256": p["decision_document_sha256"]})
    if set(p.get("formal_allocations", {})) != {"formal-1", "formal-2"}:
        cycle._fail("formal amendment can only add to both existing formal allocations")
    approved = copy.deepcopy(cycle.LIMITS)
    for name, values in p["formal_allocations"].items():
        previous = dict(zip(cycle.DIMENSIONS, cycle.ALLOCATIONS[name], strict=True))
        if set(values) != set(previous):
            cycle._fail("formal allocation dimensions changed")
        for d in cycle.DIMENSIONS:
            cycle._integer(values[d], d)
            if d not in {"http_requests", "tokens"} and values[d] != previous[d]:
                cycle._fail("formal amendment cannot change science/input allowances")
        for d in ("http_requests", "tokens"):
            if values[d] < previous[d]:
                cycle._fail("formal amendment cannot reduce or transfer allowance")
            approved["model"][d] += values[d] - previous[d]
    if p.get("approved_limits") != approved or approved == cycle.LIMITS:
        cycle._fail("formal cumulative limits do not match their exact positive additions")
    return p


def validate_applied(ledger, book):
    """Audit immutable authority recursively; no current-file approval guess."""
    _, reference, _ = cycle._parts()
    authority = ledger.get("limit_authority", {})
    directory = book.root / "budget-amendments" / APPROVAL_ID
    receipt_path, before_path, approval_path = (directory / name for name in ("amendment.json", "before.json", "approval.json"))
    if (set(authority) != {"approval_id", "approval_sha256", "origin", "receipt_sha256"}
            or authority.get("approval_id") != APPROVAL_ID or authority.get("origin") != "amendment"
            or not receipt_path.is_file() or not before_path.is_file() or not approval_path.is_file()
            or reference.sha256_file(receipt_path) != authority["receipt_sha256"]
            or reference.sha256_file(approval_path) != authority["approval_sha256"]):
        cycle._fail("formal allowance has no immutable applied approval and receipt")
    approval = reference._json(approval_path)
    p = _validate_approval(approval)
    before, receipt = reference._json(before_path), reference._json(receipt_path)
    required = {"approval_id": APPROVAL_ID, "approval_sha256": authority["approval_sha256"],
        "previous_limits": cycle.LIMITS, "approved_limits": p["approved_limits"],
        "before_sha256": reference.sha256_file(before_path), "previous_limit_authority": before.get("limit_authority"),
        "preserved_model_usage": before.get("model_usage"),
        "preserved_entry_counts": {k: len(before.get(k, {})) for k in ("entries", "model_records", "agent_science")}}
    if ({k: v for k, v in receipt.items() if k != "applied_at"} != required
            or before.get("limits") != cycle.LIMITS or ledger.get("limits") != p["approved_limits"]):
        cycle._fail("formal amendment changed its approved limits or baseline")
    book._validate_bounded_limit_authority(before, approval_id=reference.CYCLE_APPROVAL_ID)
    book._validate_preserved_costs(before, ledger)
    prior_state = before.get("repair_cycle", {})
    current_state = ledger.get("repair_cycle", {})
    if any(current_state.get(k) != v for k, v in prior_state.items() if k == "scope_sha256"):
        cycle._fail("formal amendment changed original cycle scope")
    for kind, entries in prior_state.items():
        if isinstance(entries, dict) and any(current_state.get(kind, {}).get(k) != v for k, v in entries.items()):
            cycle._fail("formal amendment lost existing cycle receipts")
    return p


def apply(*, approval_path=None, approval_sha256=None, execute=False, fault=None):
    """Root/operator only: explicit reviewed bytes; never called by the model."""
    if not execute or approval_path is None or not isinstance(approval_sha256, str):
        cycle._fail("formal addition needs explicit execute, approval path and exact SHA")
    budget, reference, Store = cycle._parts()
    reference.cycle_approval()
    path = Path(approval_path).resolve(strict=True)
    if path.stat().st_size > 256 * 1024:
        cycle._fail("formal approval exceeds the bounded record size")
    raw = path.read_bytes()
    if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() != approval_sha256:
        cycle._fail("operator-approved formal record bytes changed")
    value = reference._json(path)
    p = _validate_approval(value)
    if p != proposal():
        cycle._fail("formal addition differs from current exact manifest/proposal; no execution")
    book = budget.AcceptanceBudget(Store(reference.BATCH_ROOT / "reference"))
    with book.ledger._lock():
        current = book._snapshot_unlocked()
        directory = reference.BATCH_ROOT / "budget-amendments" / APPROVAL_ID
        before_path, receipt_path, saved_approval = (directory / name for name in ("before.json", "amendment.json", "approval.json"))
        if current.get("limit_authority", {}).get("approval_id") == APPROVAL_ID:
            validate_applied(current, book.ledger)
            if current["limit_authority"]["approval_sha256"] != approval_sha256:
                cycle._fail("formal amendment is already bound to another approval")
            return reference._json(receipt_path)
        if current["limits"] != cycle.LIMITS:
            cycle._fail("formal addition must follow the original applied cycle authority")
        # Known development consumption may advance while approval is reviewed.
        # The exact application-time ledger is captured and never reset.
        original = book.ledger.path.read_bytes()
        if before_path.exists() and before_path.read_bytes() != original:
            cycle._fail("interrupted formal migration baseline changed; reconcile first")
        from orca_agent.models import utc_now
        from orca_agent.store import atomic_write
        for destination, content in ((before_path, original), (saved_approval, raw)):
            if not destination.exists():
                atomic_write(destination, content, immutable=True)
            elif destination.read_bytes() != content:
                cycle._fail("immutable formal amendment evidence changed")
        if fault:
            fault("after_original_snapshot")
        details = {"approval_id": APPROVAL_ID, "approval_sha256": approval_sha256,
            "previous_limits": cycle.LIMITS, "approved_limits": p["approved_limits"],
            "before_sha256": reference.sha256_file(before_path), "previous_limit_authority": current["limit_authority"],
            "preserved_model_usage": current.get("model_usage"),
            "preserved_entry_counts": {k: len(current.get(k, {})) for k in ("entries", "model_records", "agent_science")}}
        if receipt_path.exists():
            receipt = reference._json(receipt_path)
            if {k: v for k, v in receipt.items() if k != "applied_at"} != details:
                cycle._fail("interrupted formal receipt differs from exact approval")
        else:
            receipt = {**details, "applied_at": utc_now().isoformat()}
            reference._save(receipt_path, receipt, immutable=True)
        if fault:
            fault("after_amendment_receipt")
        after = copy.deepcopy(current)
        after["limits"] = p["approved_limits"]
        after["limit_authority"] = {"approval_id": APPROVAL_ID, "approval_sha256": approval_sha256,
            "origin": "amendment", "receipt_sha256": reference.sha256_file(receipt_path)}
        book._save(after)
        if fault:
            fault("after_ledger_publication")
        book._snapshot_unlocked()
        return receipt


def allocations(ledger):
    _, reference, _ = cycle._parts()
    values = {k: dict(zip(cycle.DIMENSIONS, v, strict=True)) for k, v in cycle.ALLOCATIONS.items()}
    if ledger.get("limit_authority", {}).get("approval_id") == APPROVAL_ID:
        p = validate_applied(ledger, reference.BatchLedger())
        values.update(p["formal_allocations"])
    return values
