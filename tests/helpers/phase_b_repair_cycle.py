"""Fixed repair-cycle accounting on the original acceptance ledger.

This is an acceptance operator, never a production domain or alternate ledger.
No real operation is available without its separately pinned adoption record.
"""

from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import re
import subprocess
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

CYCLE_ID = "repair-cycle-20261008"
PLAN_PATH = "docs/reviews/2026-10-08-best-repair-plan.md"
PLAN_SHA256 = "595bd302f2e0f5affc4f29e01f82de1b07c255d872dd82944550c04108655706"
BASELINE_SHA256 = "b5b0efbfb7b8a805a904b08fbf3d35e7a325809c897bab3c7c68ede2ae039b13"
LIMITS = {
    "orca_starts": {"reference": 37, "development": 68, "formal": 108, "total": 213},
    "model": {"http_requests": 2068, "tokens": 13_882_912, "usd": 20},
}
MODEL_SLOTS = (
    "N-06/raw-unsupported-system", "V-06/insufficient-additional-budget",
    "V-07/array-location", "V-09/different-method", "V-09/missing-electron-state",
    "N-03/raw-electron-state-clarification", "N-03/raw-ambiguous-reference",
    "N-04/raw-authorized-defaults", "N-04/raw-unique-inheritance",
    "N-04/raw-unconfirmed-inference", "N-07/raw-read-only-window",
    "N-09/raw-user-goal-replacement", "N-09/raw-preserve-goal",
)
ALLOCATIONS = {
    "gates-1": (64, 512000, 0, 0, 0, 0, 0),
    "gates-2": (64, 512000, 0, 0, 0, 0, 0),
    "gates-3": (64, 512000, 0, 0, 0, 0, 0),
    "layered": (48, 288000, 1, 6, 0, 2, 2),
    "legacy-c": (48, 288000, 0, 16, 0, 0, 0),
    "conditional-d": (16, 96000, 0, 6, 0, 0, 0),
    "e2e-development": (48, 288000, 6, 6, 0, 6, 6),
    "targeted-repair": (48, 384000, 2, 8, 0, 2, 2),
    "formal-1": (624, 4704000, 0, 0, 48, 0, 0),
    "formal-e2e-1": (48, 288000, 6, 0, 6, 6, 6),
    "formal-2": (624, 4704000, 0, 0, 48, 0, 0),
    "formal-e2e-2": (48, 288000, 6, 0, 6, 6, 6),
}
DIMENSIONS = ("http_requests", "tokens", "reference", "development", "formal",
              "identity_queries", "structure_preparations")
_ACTIVE = ContextVar("repair_cycle_activity", default=None)
DEPENDENCIES = {"protocol", "request_semantics", "query", "applicability", "terminal_delivery",
                "budget", "structure", "science", "recovery"}
HARD_FAILURES = {"source_hash_conflict", "unknown_http_cost", "unknown_process",
                 "unauthorized_execution", "resource_limit", "shared_gate_failure"}


def scope():
    """The exact adopted finite allocation; no active ledger or runtime reads."""
    return {
        "schema_version": 1, "cycle_id": CYCLE_ID, "model_profile": "disabled",
        "decision_document": PLAN_PATH, "decision_document_sha256": PLAN_SHA256,
        "baseline_ledger_sha256": BASELINE_SHA256,
        "baseline_usage": {"http_requests": 324, "tokens": 1018912, "usd": "0.4065609",
                           "reference": 16, "development": 26, "formal": 0},
        "proposed_limits": copy.deepcopy(LIMITS),
        "allocations": {name: dict(zip(DIMENSIONS, values, strict=True))
                        for name, values in ALLOCATIONS.items()},
        "development_candidates": 3, "formal_candidates": 2,
        "model_gates_per_development_candidate": list(MODEL_SLOTS),
        "structure_totals": {"identity_queries": 22, "structure_preparations": 22,
                             "retries": 0},
        "execution_activity_seconds": 96 * 3600,
        "single_run_limits": {"model_calls": 8, "model_tokens": 48000,
                              "decision_rounds": 12, "seconds": 1800,
                              "corrections_per_proposal": 1},
        "resources": {"cores": 4, "memory_mb": 1024, "maxcore_mb": 192,
                      "environment_parallel_processes": 1, "prepare_seconds": 30,
                      "reference_seconds": 120, "science_seconds": 300},
        "input_boundary": {"response_bytes": 262144, "response_deadline_seconds": 20,
                           "http_read_timeout_seconds": 10, "http_connect_timeout_seconds": 5},
        "closed_packages": ["bounded-20261008", "bounded-20261008-r2",
                            "bounded-20261008-r3", "bounded-20261008-r4"],
        "continuation_policy": {
            "ordinary_development_failure": "retain Run; only independent zero-ORCA diagnostics may continue",
            "failure_dependencies_unknown": "block until supported classification",
            "science": "require applicable independent gates in the same candidate",
            "repeat": "new committed patch, root cause, counterexample and offline evidence required",
            "hard_stop": ["source_hash_conflict", "unknown_http_cost", "unknown_process",
                          "unauthorized_execution", "resource_limit", "shared_gate_failure"],
            "formal": "any required failure rejects whole round; second repaired candidate reruns full manifest",
            "cross_candidate_pass_reuse": False, "cross_allocation_borrowing": False,
        },
    }


def _parts():
    from orca_agent.store import Store
    from tests.helpers import phase_b_budget as budget
    return budget, budget.reference, Store


def _fail(message):
    _, reference, _ = _parts()
    raise reference.ReferenceBlocked(message)


def _integer(value, name):
    if type(value) is not int or value < 0:
        _fail(f"invalid cycle {name}")
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _evidence(item):
    _, reference, _ = _parts()
    if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
        _fail("cycle evidence needs an exact path/hash pair")
    path = Path(item["path"])
    path = path if path.is_absolute() else reference.PROJECT / path
    if not path.is_file() or reference.sha256_file(path) != item["sha256"]:
        _fail("cycle evidence source hash changed or missing")
    return path


def _book():
    budget, reference, Store = _parts()
    reference.cycle_approval()
    return budget.AcceptanceBudget(Store(reference.BATCH_ROOT / "reference"))


def _state(ledger, *, allow_development_pending=False):
    _, reference, _ = _parts()
    if not reference.is_cycle_ledger(ledger):
        _fail("cycle limits have not been applied")
    if ledger.get("limit_authority", {}).get("approval_id") == reference.CYCLE_FORMAL_APPROVAL_ID:
        from tests.helpers.phase_b_cycle_formal_amendment import validate_applied
        validate_applied(ledger, reference.BatchLedger())
    from tests.helpers.phase_b_development_amendment import validate_if_present
    validate_if_present(ledger, reference.BatchLedger(), allow_pending=allow_development_pending)
    value = ledger.get("repair_cycle")
    initial = {"scope_sha256": _digest(scope()), "candidates": {}, "slots": {},
               "activities": {}, "activity_settlements": {}, "outcomes": {}, "reconciliations": {}}
    if value is None:
        value = initial
    if set(value) != set(initial) or value["scope_sha256"] != initial["scope_sha256"]:
        _fail("cycle management scope changed")
    for kind in initial.keys() - {"scope_sha256"}:
        if not isinstance(value[kind], dict):
            _fail("invalid cycle management entries")
        directory = reference.BATCH_ROOT / CYCLE_ID / kind
        if {p.stem for p in directory.glob("*.json")} != set(value[kind]):
            _fail("cycle receipt publication is incomplete; reconcile without a new identity")
        for item in value[kind].values():
            path = _evidence(item["receipt"])
            if reference._json(path) != {k: v for k, v in item.items() if k != "receipt"}:
                _fail("cycle entry differs from its immutable receipt")
            for evidence in item.get("evidence", []):
                _evidence(evidence)
    return value


def _publish(book, ledger, kind, key, value):
    _, reference, _ = _parts()
    state = _state(ledger)
    if key in state[kind]:
        if {k: v for k, v in state[kind][key].items() if k != "receipt"} != value:
            _fail("cycle stable identity cannot be rebound")
        return state[kind][key]
    path = reference.BATCH_ROOT / CYCLE_ID / kind / f"{key}.json"
    # Interrupted receipt publication must match exactly; it never creates a
    # second identity or refunds occupancy under a new name.
    reference._save(path, value, immutable=True)
    saved = {**value, "receipt": {"path": str(path.resolve()), "sha256": reference.sha256_file(path)}}
    state[kind][key] = saved
    ledger["repair_cycle"] = state
    book._save(ledger)
    return saved


def candidate_label(kind, number):
    # A representable label is not execution authority; admission below checks
    # the separately applied D4 extension before any freeze or reservation.
    limit = 4 if kind == "development" else 2 if kind == "formal" else 0
    if type(number) is not int or not 1 <= number <= limit:
        _fail("candidate outside adopted finite cycle")
    return f"{CYCLE_ID}-{kind}-{number}"


def parse_candidate(label):
    match = re.fullmatch(re.escape(CYCLE_ID) + r"-(development|formal)-([1-4])", label)
    if not match or candidate_label(match[1], int(match[2])) != label:
        _fail("unknown repair-cycle candidate label")
    return match[1], int(match[2])


def _repair_evidence(value, previous, current):
    if not isinstance(value, dict) or not value.get("root_cause") or not value.get("patch_paths"):
        _fail("next candidate requires a located root cause and actual patch")
    _evidence(value.get("offline_validation"))
    _, reference, _ = _parts()
    paths = subprocess.check_output(["git", "diff", "--name-only", previous, current],
                                    cwd=reference.PROJECT, text=True).splitlines()
    if (previous == current or any(p not in paths for p in value["patch_paths"])
            or not any(p.startswith(("src/", "tests/")) for p in value["patch_paths"])
            or not value.get("counterexamples")):
        _fail("repetition requires a new committed repair and counterexamples")


def _allowed_slot(allocation, slot_id):
    if allocation.startswith("gates-"):
        allowed = {v.replace("/", "__") for v in MODEL_SLOTS}
    elif allocation == "layered":
        allowed = {"water-input", "methane-input", "methane-reference"}
        allowed |= {f"{system}-{i}" for system in ("water", "methane") for i in (1, 2, 3)}
    elif allocation == "legacy-c":
        allowed = {"sampling_left", "sampling_right", "sampling_stop", "repair_exhaustion",
                   "repair_success", "methane_opt_control"}
    elif allocation == "conditional-d":
        allowed = {"sampling", "repair"}
    elif allocation == "targeted-repair":
        allowed = {f"repair-{i}" for i in range(1, 9)} | {"reference-1", "reference-2"}
    elif allocation == "e2e-development" or allocation.startswith("formal-e2e-"):
        allowed = {f"{s}-{i}{suffix}" for s in ("water", "methane") for i in (1, 2, 3)
                   for suffix in ("", "-reference")}
    elif allocation in {"formal-1", "formal-2"}:
        # Exact membership is checked against the complete frozen manifest by
        # bind_slot. Avoid rebuilding its hundreds of case specs for every row.
        if not re.fullmatch(r"(?:model|offline|joint)-[a-zA-Z0-9_-]+-[1-3]", slot_id):
            _fail("unknown formal manifest slot identity")
        return
    else:
        _fail("unknown fixed allocation")
    if slot_id not in allowed:
        _fail("slot outside the fixed purpose allocation")


def _validate_manifest(kind, number, manifest, *, ledger=None):
    from tests.helpers.phase_b_cycle_formal_amendment import allocations as original_allocations
    from tests.helpers.phase_b_development_amendment import allocations
    maxima = allocations(ledger) if ledger is not None else original_allocations({})
    if kind == "development" and number == 4:
        from tests.helpers.phase_b_development_amendment import fourth_authorized
        fourth_authorized(ledger)
        if ledger is None:
            _, reference, _ = _parts()
            maxima = allocations(reference.BatchLedger().snapshot())
    if not isinstance(manifest, dict) or not manifest.get("slots"):
        _fail("cycle freeze requires its generated nonempty manifest")
    if kind == "formal":
        from tests.helpers.phase_b_repair_cycle_execution import formal_manifest
        if manifest != formal_manifest(number):
            _fail("formal cycle freeze requires its exact complete matrix adapter manifest")
    for key, row in manifest["slots"].items():
        allocation, slot_id = key.split("/", 1)
        if allocation not in maxima or (kind == "formal") != allocation.startswith("formal-"):
            _fail("manifest contains wrong candidate allocation")
        _allowed_slot(allocation, slot_id)
        if allocation.startswith("gates-") and allocation != f"gates-{number}":
            _fail("manifest borrows another development candidate")
        if (set(row) != {"declared", "dependencies"} or set(row["declared"]) != set(DIMENSIONS)
                or not row["dependencies"] or not set(row["dependencies"]) <= DEPENDENCIES):
            _fail("invalid manifest slot contract")
        for dimension in DIMENSIONS:
            _integer(row["declared"][dimension], dimension)
    for key, row in (model_manifest(number)["slots"].items() if kind == "development" else []):
        if manifest["slots"].get(key) != row:
            _fail("candidate manifest must contain all exact current model gates")
    from tests.helpers.phase_b_cycle_formal_amendment import validate_applied
    if kind == "formal" and ledger and ledger.get("limit_authority", {}).get("approval_id") == "repair-cycle-formal-budget-20261008":
        _, reference, _ = _parts()
        approved = validate_applied(ledger, reference.BatchLedger())
        if approved["formal_manifest_sha256"].get(f"formal-{number}") != _digest(manifest):
            _fail("formal manifest differs from the explicit applied addition")
    for allocation, values in maxima.items():
        for dimension, maximum in values.items():
            declared = sum(row["declared"][dimension] for key, row in manifest["slots"].items()
                           if key.startswith(allocation + "/"))
            if declared > maximum:
                _fail("candidate manifest exceeds a purpose allocation")


def freeze_candidate(kind, number, *, manifest, repair_evidence=None):
    """Freeze only; actual callers must separately bind every executable slot."""
    from tests.helpers import phase_b_bounded_package as package
    from tests.helpers import phase_b_freeze as freeze
    label = candidate_label(kind, number)
    book = _book()
    _, reference, _ = _parts()
    # Admission precedes source/runtime probes as well as any publication.
    from tests.helpers.phase_b_development_amendment import assert_open
    assert_open(book.snapshot(), label)
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=reference.PROJECT, text=True).strip():
        _fail("cycle freeze needs a clean committed candidate")
    _validate_manifest(kind, number, manifest, ledger=book.snapshot() if kind == "formal" else None)
    if kind == "formal":
        formal_freeze_requirements(label, repair_evidence=repair_evidence)
    config = freeze.evaluation_config(science=True, model_profile="disabled")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=reference.PROJECT, text=True).strip()
    value = {"label": label, "kind": kind, "number": number, "commit": commit,
             "source_files": package._source_files(), "runtime": freeze.runtime_environment(),
             "configuration": config.model_dump(mode="json"), "manifest": manifest,
             "approval_sha256": reference.CYCLE_APPROVAL_SHA256,
             "scope_sha256": _digest(scope()), "repair_evidence": repair_evidence}
    if kind == "formal":
        path = reference.PROJECT / "docs/acceptance/phase-b/coverage-repair-cycle-20261008.json"
        value["formal_coverage"] = {"path": str(path), "sha256": reference.sha256_file(path)}
        frozen = freeze.validate_freeze(label, config=freeze.evaluation_config(model_profile="disabled"))
        value["formal_freeze"] = {"path": str(freeze.FREEZE), "sha256": reference.sha256_file(freeze.FREEZE)}
        if frozen["code_commit"] != commit:
            _fail("formal source freeze targets another commit")
    value["runtime"]["rdkit_version"] = importlib.metadata.version("rdkit")
    from orca_agent.model_usage import INPUT_USD_PER_MILLION, OUTPUT_USD_PER_MILLION
    value["price_basis"] = {"input_usd_per_million": str(INPUT_USD_PER_MILLION),
                             "output_usd_per_million": str(OUTPUT_USD_PER_MILLION)}
    value["binaries"] = {name: {"path": str(path), "sha256": reference.sha256_file(path)}
                         for name, path in (("orca", config.orca_path), ("mpi", config.mpi_path)) if path}
    if set(value["binaries"]) != {"orca", "mpi"}:
        _fail("cycle candidate requires frozen ORCA/MPI identities")
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        from tests.helpers.phase_b_development_amendment import assert_open
        assert_open(ledger, label)
        _no_unknown(ledger, state)
        if label in state["candidates"]:
            saved = _candidate(state, label)
            if saved["manifest"] != manifest or saved["repair_evidence"] != repair_evidence:
                _fail("candidate identity cannot be refrozen with another manifest")
            return saved
        value["budget_authority"] = copy.deepcopy(ledger["limit_authority"])
        value["ledger_at_freeze_sha256"] = reference.sha256_file(book.ledger.path)
        value["usage_at_freeze"] = copy.deepcopy(ledger.get("model_usage", {}))
        if number > 1:
            previous = state["candidates"].get(candidate_label(kind, number - 1))
            if not previous:
                _fail("prior candidate is required; cannot skip a cycle allocation")
            failures = [o for o in state["outcomes"].values()
                        if o["candidate"] == previous["label"] and o["status"] == "failed"]
            if not failures:
                _fail("conditional candidate requires an actual prior failure")
            _repair_evidence(repair_evidence, previous["commit"], commit)
        return _publish(book, ledger, "candidates", label, value)


def _candidate(state, label, *, verify_source=True):
    from tests.helpers import phase_b_bounded_package as package
    from tests.helpers import phase_b_freeze as freeze
    _, reference, _ = _parts()
    parse_candidate(label)
    value = state["candidates"].get(label)
    if not value or value["approval_sha256"] != reference.CYCLE_APPROVAL_SHA256:
        _fail("candidate is not frozen under the adopted cycle")
    if verify_source and value["source_files"] != package._source_files():
        _fail("cycle candidate source changed")
    if verify_source:
        config = freeze.evaluation_config(science=True, model_profile="disabled")
        runtime = freeze.runtime_environment()
        runtime["rdkit_version"] = importlib.metadata.version("rdkit")
        if value["configuration"] != config.model_dump(mode="json") or value["runtime"] != runtime:
            _fail("cycle candidate runtime or configuration changed")
        for item in value["binaries"].values():
            _evidence(item)
        if "formal_coverage" in value:
            _evidence(value["formal_coverage"])
            _evidence(value["formal_freeze"])
    return value


def formal_freeze_requirements(candidate, *, repair_evidence=None):
    """Read-only admission for the existing formal freeze and its full matrix."""
    from tests.helpers.phase_b_bounded_package import _source_files
    from tests.helpers.phase_b_repair_cycle_execution import formal_manifest
    kind, number = parse_candidate(candidate)
    if kind != "formal":
        _fail("formal freeze requires a formal candidate")
    manifest = formal_manifest(number)
    book = _book()
    _, reference, _ = _parts()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        _validate_manifest(kind, number, manifest, ledger=ledger)
        state = _state(ledger)
        _no_unknown(ledger, state)
        if number == 1:
            candidates = [c for c in state["candidates"].values() if parse_candidate(c["label"])[0] == "development"]
            if not candidates:
                _fail("formal freeze requires current development evidence")
            latest = max(candidates, key=lambda c: parse_candidate(c["label"])[1])
            if latest["source_files"] != _source_files():
                _fail("development evidence differs from formal candidate sources")
            required = {key for key in latest["manifest"]["slots"]
                        if not key.startswith(("conditional-d/", "targeted-repair/"))}
            passed = {f"{s['allocation']}/{s['slot_id']}" for s in state["slots"].values()
                      if s["candidate"] == latest["label"] and state["outcomes"].get(s["receipt"]["sha256"], {}).get("status") == "passed"}
            if not required <= passed or any(o["candidate"] == latest["label"] and o["status"] == "failed"
                                             for o in state["outcomes"].values()):
                _fail("formal freeze requires every current development gate, input and scientific trajectory to pass")
        else:
            previous = state["candidates"].get(candidate_label("formal", 1))
            if not previous or not any(o["candidate"] == previous["label"] and o["status"] == "failed"
                                       for o in state["outcomes"].values()):
                _fail("second formal candidate requires actual first-round failure")
            current = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=reference.PROJECT, text=True).strip()
            _repair_evidence(repair_evidence, previous["commit"], current)
    return manifest


def guard_formal_offline(candidate, repetition):
    kind, number = parse_candidate(candidate)
    if kind != "formal" or type(repetition) is not int or repetition not in (1, 2, 3):
        _fail("unknown cycle formal offline repetition")
    book = _book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        frozen = _candidate(state, candidate)
        _no_unknown(ledger, state)
        _dependencies(state, candidate, sorted(DEPENDENCIES))
        required = {key for key in frozen["manifest"]["slots"]
                    if key.startswith(f"formal-{number}/offline-") and key.endswith(f"-{repetition}")}
        if not required or any(f"{s['allocation']}/{s['slot_id']}" in required for s in state["slots"].values()
                               if s["candidate"] == candidate):
            _fail("formal offline repetition is absent or already consumed")
        return frozen, required


def record_formal_offline_receipt(candidate, repetition, receipt_path):
    """Consume actual existing formal pytest receipts; never turn skipped into pass."""
    import xml.etree.ElementTree as ET
    frozen, required = guard_formal_offline(candidate, repetition)
    _, reference, _ = _parts()
    receipt_path = Path(receipt_path)
    receipt = reference._json(receipt_path)
    manifest = frozen["manifest"]
    nodes = sorted({node for item in manifest["coverage_bindings"].values() if item["slot"] in required
                    for node in item["pytest_nodeids"]})
    if (receipt.get("freeze_label") != candidate or receipt.get("repetition") != repetition
            or receipt.get("code_commit") != frozen["commit"] or receipt.get("pytest_nodeids") != nodes
            or receipt.get("coverage_sha256") != manifest["coverage_sha256"]
            or receipt.get("freeze_sha256") != frozen["formal_freeze"]["sha256"]
            or receipt.get("live_model") is not False or receipt.get("live_orca") is not False):
        _fail("formal offline receipt differs from frozen exact coverage")
    evidence = [{"path": str(receipt_path), "sha256": reference.sha256_file(receipt_path)}]
    for name in ("junit", "log"):
        path = receipt_path.parent / receipt[f"{name}_path"]
        if not path.resolve().is_relative_to(receipt_path.parent.resolve()):
            _fail("offline evidence escaped its invocation directory")
        item = {"path": str(path), "sha256": receipt[f"{name}_sha256"]}
        _evidence(item)
        evidence.append(item)
    xml = ET.parse(_evidence(evidence[1])).getroot()
    tests = list(xml.iter("testcase"))
    passed = (receipt.get("pytest_returncode") == 0 and len(tests) >= len(nodes)
              and not any(list(xml.iter(tag)) for tag in ("failure", "error", "skipped")))
    book = _book()
    for key in sorted(required):
        allocation, slot_id = key.split("/", 1)
        with book.ledger._lock():
            ledger = book._snapshot_unlocked()
            row = manifest["slots"][key]
            slot = _publish(book, ledger, "slots", _digest([candidate, key]),
                {"candidate": candidate, "allocation": allocation, "slot_id": slot_id, **row,
                 "category": "formal_offline", "evidence": evidence})
        record_outcome(slot, status="passed" if passed else "failed", failure_kind=None if passed else "ordinary",
                       affected_dependencies=[] if passed else sorted(DEPENDENCIES), evidence=evidence)
    return {"passed": passed, "required_slots": len(required)}


def _no_unknown(ledger, state):
    active = _ACTIVE.get()
    active = active[0] if active else None
    for key, value in state["activities"].items():
        if key not in state["activity_settlements"] and key != active:
            _fail("cycle has an unresolved execution activity; reconcile without restart")
    _costs_known(ledger, state)
    if any(o["failure_kind"] in HARD_FAILURES for o in state["outcomes"].values()
           if o["status"] == "failed" and o["receipt"]["sha256"] not in state["reconciliations"]):
        _fail("cycle hard failure requires reconciliation before new execution")


def _costs_known(ledger, state):
    for kind in ("model_records", "agent_science"):
        if any(e.get("state") != "known" for e in ledger.get(kind, {}).values()):
            _fail("cycle has unknown HTTP cost or scientific process; reconcile first")
    if any(e.get("execution_uncertain", True) for e in ledger.get("entries", {}).values()):
        _fail("cycle has unknown reference execution; reconcile first")
    _, _, Store = _parts()
    for slot in state["slots"].values():
        if "run_id" not in slot:
            continue
        run = Store(Path(slot["store_root"])).load_run(slot["run_id"])
        if (run.state == "unknown" or any(c.state in {"reserved", "unknown"} for c in run.calls)
                or any(a.state in {"intent", "prepared", "running", "unknown"} for a in run.attempts)):
            _fail("cycle Run has unresolved Tool/process state; reconcile first")


def _dependencies(state, candidate, dependencies, *, scientific=False):
    if not dependencies or not set(dependencies) <= DEPENDENCIES:
        _fail("slot needs explicit supported dependencies")
    failed = [o for o in state["outcomes"].values()
              if o["candidate"] == candidate and o["status"] == "failed"]
    if any(scientific or parse_candidate(candidate)[0] == "formal" or not o["affected_dependencies"]
           or set(dependencies) & set(o["affected_dependencies"]) for o in failed):
        _fail("related failure blocks this slot; independent zero-ORCA diagnosis only")
    if scientific:
        kind, number = parse_candidate(candidate)
        required = ({f"gates-{number}/{v.replace('/', '__')}" for v in MODEL_SLOTS} if kind == "development" else
                    {key for key, row in state["candidates"][candidate]["manifest"]["slots"].items()
                     if key.startswith(f"formal-{number}/") and not row["declared"]["formal"]})
        passed = {f"{s['allocation']}/{s['slot_id']}" for s in state["slots"].values()
                  if s["candidate"] == candidate and state["outcomes"].get(s["receipt"]["sha256"], {}).get("status") == "passed"}
        if not required <= passed:
            _fail("scientific cycle slots require all current-candidate independent model gates")


def _pending_review(state, candidate, allocation, slot_id):
    """Known cost is not a classification of whether later work is independent."""
    executed = {a["slot_receipt_sha256"] for a in state["activities"].values()}
    for previous in state["slots"].values():
        digest = previous["receipt"]["sha256"]
        if digest not in executed or digest in state["outcomes"]:
            continue
        same = (previous["candidate"], previous["allocation"], previous["slot_id"]) == (candidate, allocation, slot_id)
        reference_pause = (previous["candidate"] == candidate and previous["allocation"] == allocation
                           and previous["slot_id"] + "-reference" == slot_id
                           and f"{digest}-before_reference" in state["activity_settlements"]
                           and f"{digest}-after_reference" not in state["activities"])
        layered_resolution = (previous["candidate"] == candidate and previous["allocation"] == allocation == "layered"
                              and previous["slot_id"].endswith("-input") and slot_id.endswith("-input")
                              and f"{digest}-resolve" in state["activity_settlements"]
                              and f"{digest}-prepare" not in state["activities"])
        if not same and not reference_pause and not layered_resolution:
            _fail("completed execution awaits actual review/classification before another slot")


def bind_slot(candidate, allocation, slot_id, *, declared, dependencies,
              run=None, store=None, reference_id=None, reference_sources=None):
    """Reserve full slot allowances once; originals retain all model/ORCA costs."""
    book = _book()
    _, reference, _ = _parts()
    if allocation not in {*ALLOCATIONS, "gates-4"} or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", slot_id):
        _fail("unknown cycle allocation or unsafe slot identity")
    _allowed_slot(allocation, slot_id)
    if set(declared) != set(DIMENSIONS):
        _fail("slot must declare every allocation dimension")
    declared = {k: _integer(v, k) for k, v in declared.items()}
    if bool(run is not None) == bool(reference_id):
        _fail("slot needs exactly one Run or reference binding")
    owner = {}
    if run is not None:
        if store is None or store.load_run(run.id).model_dump() != run.model_dump():
            _fail("slot needs an exact durable Run")
        expected = {"http_requests": run.budget.model_calls, "tokens": run.budget.model_tokens,
                    "identity_queries": run.budget.identity_queries,
                    "structure_preparations": run.budget.structure_preparations}
        if any(expected[k] > declared[k] for k in expected):
            _fail("Run exceeds cycle slot model/input declaration")
        if run.batch_category not in {"development", "formal"} or run.budget.orca_starts > declared[run.batch_category]:
            _fail("Run science category/limit exceeds cycle slot")
        owner = {"run_id": run.id, "store_root": str(store.root.resolve()), "category": run.batch_category,
                 "run_limits": run.budget.model_dump(mode="json")}
    else:
        reference._identifier(reference_id)
        if declared["reference"] != 1 or any(v for k, v in declared.items() if k != "reference"):
            _fail("reference slot must reserve exactly one independent reference")
        if not reference_sources:
            _fail("reference slot must freeze actual input/geometry sources")
        owner = {"reference_id": reference_id, "category": "reference", "sources": reference_sources}
    value = {"candidate": candidate, "allocation": allocation, "slot_id": slot_id,
             "declared": declared, "dependencies": sorted(set(dependencies)), **owner}
    key = hashlib.sha256(f"{candidate}/{allocation}/{slot_id}".encode()).hexdigest()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        from tests.helpers.phase_b_development_amendment import assert_open
        assert_open(ledger, candidate)
        frozen = _candidate(state, candidate)
        if allocation in {"conditional-d", "targeted-repair"} and not frozen.get("repair_evidence"):
            _fail("conditional repair allocation requires an actual repaired candidate")
        kind, number = parse_candidate(candidate)
        if ((allocation.startswith("gates-") and allocation != f"gates-{number}")
                or (kind == "formal") != allocation.startswith("formal-")):
            _fail("candidate cannot consume another round or execution category")
        _no_unknown(ledger, state)
        _pending_review(state, candidate, allocation, slot_id)
        _dependencies(state, candidate, dependencies, scientific=bool(sum(declared[k] for k in ("reference", "development", "formal"))))
        if frozen["manifest"]["slots"].get(f"{allocation}/{slot_id}") != {"declared": declared, "dependencies": sorted(set(dependencies))}:
            _fail("slot differs from candidate generated manifest")
        if key not in state["slots"]:
            if any((run is not None and s.get("run_id") == run.id)
                   or (reference_id and s.get("reference_id") == reference_id) for s in state["slots"].values()):
                _fail("existing Run/reference cannot be moved or repeated across cycle slots")
            from tests.helpers.phase_b_development_amendment import allocations
            ceiling = allocations(ledger)[allocation]
            for dimension, maximum in ceiling.items():
                used = sum(s["declared"][dimension] for s in state["slots"].values() if s["allocation"] == allocation)
                if used + declared[dimension] > maximum:
                    _fail(f"cycle allocation {allocation}/{dimension} exhausted; borrowing forbidden")
        return _publish(book, ledger, "slots", key, value)


def _slot_guard(ledger, owner=None, reference_id=None):
    state = _state(ledger)
    _no_unknown(ledger, state)
    matches = [s for s in state["slots"].values() if
               (owner is not None and s.get("run_id") == owner["run_id"] and s.get("store_root") == owner["store_root"])
               or (reference_id is not None and s.get("reference_id") == reference_id)]
    if len(matches) != 1:
        _fail("new cycle execution needs exactly one frozen slot; unbound low-level call rejected")
    slot = matches[0]
    from tests.helpers.phase_b_development_amendment import assert_open
    assert_open(ledger, slot["candidate"])
    _candidate(state, slot["candidate"])
    _dependencies(state, slot["candidate"], slot["dependencies"],
                  scientific=bool(sum(slot["declared"][k] for k in ("reference", "development", "formal"))))
    current = _ACTIVE.get()
    active = state["activities"].get(current[0] if current else None)
    if not active or active["slot_receipt_sha256"] != slot["receipt"]["sha256"]:
        _fail("new cycle execution requires its reserved activity")
    if current[0] in state["activity_settlements"]:
        _fail("completed cycle activity cannot execute again")
    if time.monotonic() - current[1] >= active["seconds_reserved"]:
        _fail("cycle activity deadline reached; no new reservation")
    if owner is not None:
        _, _, Store = _parts()
        run = Store(Path(owner["store_root"])).load_run(owner["run_id"])
        if run.budget.model_dump(mode="json") != slot["run_limits"]:
            _fail("Run budget changed from the admitted cycle slot")
    return slot


def guard_budget_reservation(ledger, owner, kind, proposed):
    slot = _slot_guard(ledger, owner=owner)
    if slot["category"] != owner["category"]:
        _fail("cycle Run category changed")
    if kind == "science" and slot["declared"][owner["category"]] == 0:
        _fail("zero-ORCA diagnostic cannot consume scientific budget")
    if kind == "model" and slot["declared"]["http_requests"] == 0:
        _fail("this cycle slot has no model allocation")
    if kind == "model":
        records = [e for e in ledger.get("model_records", {}).values() if e["run_id"] == owner["run_id"]]
        tokens = sum(e["settled_record"]["total_tokens"] if e["state"] == "known"
                     else e["record"]["input_reserved"] + e["record"]["output_reserved"] for e in records)
        reserved = _integer(proposed.get("input_reserved"), "input tokens") + _integer(proposed.get("output_reserved"), "output tokens")
        if len(records) + 1 > slot["declared"]["http_requests"] or tokens + reserved > slot["declared"]["tokens"]:
            _fail("cycle slot model allocation exhausted")
    elif kind == "science":
        used = sum(e["run_id"] == owner["run_id"] for e in ledger.get("agent_science", {}).values())
        if used + 1 > slot["declared"][owner["category"]]:
            _fail("cycle slot scientific allocation exhausted")
    else:
        _fail("unknown cycle budget reservation kind")
    return {"cycle_id": CYCLE_ID, "cycle_slot_sha256": slot["receipt"]["sha256"],
            "cycle_allocation": slot["allocation"]}


def guard_reference_reservation(ledger, reference_id, category, sources):
    slot = _slot_guard(ledger, reference_id=reference_id)
    if category != "reference" or slot["sources"] != sources:
        _fail("independent reference differs from its bound cycle input")
    return {"cycle_id": CYCLE_ID, "cycle_slot_sha256": slot["receipt"]["sha256"],
            "cycle_allocation": slot["allocation"]}


@contextmanager
def activity(slot, *, seconds, segment="complete"):
    """Time only a real execution interval; interrupted intervals retain reserve."""
    book = _book()
    if (type(seconds) not in (int, float) or not math.isfinite(seconds)
            or not 0 < seconds <= 1800 or _ACTIVE.get() is not None):
        _fail("invalid or nested cycle execution activity")
    allowed = {"complete"}
    if slot["allocation"] == "e2e-development" or slot["allocation"].startswith("formal-e2e-"):
        allowed |= {"before_reference", "after_reference"}
    if slot["allocation"] == "layered" and slot["slot_id"].endswith("-input"):
        allowed = {"resolve", "prepare"}
    if segment not in allowed or slot.get("reference_id") and segment != "complete":
        _fail("unknown fixed execution activity segment")
    slot_sha = slot["receipt"]["sha256"]
    key = f"{slot_sha}-{segment}"
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        from tests.helpers.phase_b_development_amendment import assert_open
        assert_open(ledger, slot["candidate"])
        _no_unknown(ledger, state)
        if key in state["activities"]:
            _fail("slot activity already consumed; reconcile rather than repeat")
        if segment == "after_reference" and f"{slot_sha}-before_reference" not in state["activity_settlements"]:
            _fail("E2E continuation requires the settled pre-reference segment")
        if segment == "prepare" and f"{slot_sha}-resolve" not in state["activity_settlements"]:
            _fail("layered preparation requires settled identity resolution")
        if any(a["slot_receipt_sha256"] == slot_sha and a["segment"] != segment
               and "complete" in {segment, a["segment"]} for a in state["activities"].values()):
            _fail("cannot restart an E2E Run with another activity shape")
        if not any(s == slot for s in state["slots"].values()):
            _fail("activity slot is not bound to this ledger")
        used = sum(state["activity_settlements"].get(k, {}).get("seconds", a["seconds_reserved"])
                   for k, a in state["activities"].items())
        if used + seconds > scope()["execution_activity_seconds"]:
            _fail("repair-cycle execution activity time exhausted")
        _publish(book, ledger, "activities", key,
                 {"slot_receipt_sha256": slot_sha, "seconds_reserved": seconds, "segment": segment,
                  "started_at": datetime.now(timezone.utc).isoformat()})
    start = time.monotonic()
    token = _ACTIVE.set((key, start))
    try:
        yield
    except BaseException:
        # Unknown outcome stays conservatively reserved until explicit review.
        raise
    else:
        elapsed = max(0.0, time.monotonic() - start)
        with book.ledger._lock():
            ledger = book._snapshot_unlocked()
            _publish(book, ledger, "activity_settlements", key,
                     {"slot_receipt_sha256": slot_sha, "seconds": elapsed, "segment": segment,
                      "exceeded_reservation": elapsed > seconds,
                      "finished_at": datetime.now(timezone.utc).isoformat()})
        if elapsed > seconds:
            _fail("activity exceeded reserved execution time; actual time retained and execution stopped")
    finally:
        _ACTIVE.reset(token)


def record_outcome(slot, *, status, evidence, failure_kind=None, affected_dependencies=()):
    """Save operator classification bound to actual immutable grading evidence."""
    if status not in {"passed", "failed"} or not evidence:
        _fail("cycle outcome requires actual grading evidence")
    if status == "failed" and failure_kind not in {"ordinary", *HARD_FAILURES}:
        _fail("failure classification must be explicit")
    if status == "passed" and (failure_kind is not None or affected_dependencies):
        _fail("passed cycle outcome cannot conceal failure classification")
    if not set(affected_dependencies) <= DEPENDENCIES:
        _fail("unknown failure dependency")
    for item in evidence:
        _evidence(item)
    value = {"candidate": slot["candidate"], "slot_receipt_sha256": slot["receipt"]["sha256"],
             "status": status, "evidence": evidence, "failure_kind": failure_kind,
             "affected_dependencies": sorted(set(affected_dependencies))}
    book = _book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        if not any(s == slot for s in state["slots"].values()):
            _fail("outcome slot is not bound to this cycle")
        return _publish(book, ledger, "outcomes", slot["receipt"]["sha256"], value)


def reconcile_activity(activity_id, *, evidence):
    """After actual reconciliation, retain the full time reservation, not zero."""
    if not evidence:
        _fail("activity reconciliation needs actual execution evidence")
    for item in evidence:
        _evidence(item)
    book = _book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        _costs_known(ledger, state)
        pending = state["activities"].get(activity_id)
        if not pending or activity_id in state["activity_settlements"]:
            _fail("activity is absent or already settled")
        return _publish(book, ledger, "activity_settlements", activity_id,
            {"slot_receipt_sha256": pending["slot_receipt_sha256"], "segment": pending["segment"],
             "seconds": pending["seconds_reserved"], "time_accounting": "conservative_full_reservation",
             "evidence": evidence})


def reconcile_hard_failure(outcome_sha256, *, evidence):
    """Preserve the failure and reviewed reconciliation as separate receipts."""
    if not evidence:
        _fail("hard failure reconciliation needs actual evidence")
    for item in evidence:
        _evidence(item)
    book = _book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        _costs_known(ledger, state)
        matches = [o for o in state["outcomes"].values() if o["receipt"]["sha256"] == outcome_sha256
                   and o["status"] == "failed" and o["failure_kind"] in HARD_FAILURES]
        if len(matches) != 1:
            _fail("no unique hard failure to reconcile")
        return _publish(book, ledger, "reconciliations", outcome_sha256,
            {"outcome_sha256": outcome_sha256, "evidence": evidence,
             "permission": "repaired candidate may proceed; old failure never becomes a pass"})


def model_dependencies(variant):
    if variant not in MODEL_SLOTS:
        _fail("model variant outside the fixed cycle")
    if variant.startswith("V-06/"):
        return ["budget", "protocol", "terminal_delivery"]
    if variant.startswith("V-07/"):
        return ["protocol", "query", "terminal_delivery"]
    if variant.startswith("V-09/"):
        return ["applicability", "protocol", "terminal_delivery"]
    if variant.startswith("N-07/"):
        return ["protocol", "query", "request_semantics"]
    return ["protocol", "request_semantics"]


def model_manifest(number):
    """Build current gate budgets from the existing authoritative case specs."""
    from tests.helpers import phase_b_model_cases as cases
    candidate_label("development", number)
    slots = {}
    for variant in MODEL_SLOTS:
        case = cases.variant_spec(variant)
        declared = dict.fromkeys(DIMENSIONS, 0)
        declared.update(http_requests=case["budget"]["model_http_requests"],
                        tokens=case["budget"]["model_tokens_total"])
        slots[f"gates-{number}/{variant.replace('/', '__')}"] = {
            "declared": declared, "dependencies": model_dependencies(variant)}
    maxima = dict(zip(DIMENSIONS, ALLOCATIONS[f"gates-{min(number, 3)}"], strict=True))
    if any(sum(s["declared"][d] for s in slots.values()) > maxima[d] for d in DIMENSIONS):
        _fail("current model manifest exceeds the adopted per-candidate allocation")
    return {"schema_version": 1, "slots": slots}


def guard_model_slot(variant, repetition, *, category, candidate, model_profile, resume=False):
    kind, number = parse_candidate(candidate)
    from tests.helpers import phase_b_model_cases as cases
    if (variant not in (MODEL_SLOTS if kind == "development" else cases.evaluation_variant_ids())
            or type(repetition) is not int or repetition not in ((1,) if kind == "development" else (1, 2, 3))
            or category != kind or model_profile != "disabled" or resume):
        _fail("model slot outside this cycle candidate; no same-slot resend")
    book = _book()
    with book.ledger._lock():
        ledger = book._snapshot_unlocked()
        state = _state(ledger)
        from tests.helpers.phase_b_development_amendment import assert_open
        assert_open(ledger, candidate)
        frozen = _candidate(state, candidate)
        _no_unknown(ledger, state)
        allocation, slot_id = model_slot_identity(candidate, variant, repetition)
        _pending_review(state, candidate, allocation, slot_id)
        _dependencies(state, candidate, model_dependencies(variant) if kind == "development" else sorted(DEPENDENCIES))
        from tests.helpers.phase_b_repair_cycle_execution import formal_manifest
        expected = model_manifest(number) if kind == "development" else formal_manifest(number)
        key = f"{allocation}/{slot_id}"
        if frozen["manifest"]["slots"].get(key) != expected["slots"][key]:
            _fail("model slot differs from current frozen case manifest")
        return frozen["receipt"]["sha256"]


def model_slot_identity(candidate, variant, repetition=1):
    kind, number = parse_candidate(candidate)
    return ((f"gates-{number}", variant.replace('/', '__')) if kind == "development" else
            (f"formal-{number}", "model-" + variant.replace('/', '__') + f"-{repetition}"))


def bind_model_run(candidate, variant, store, run, *, repetition=1):
    kind, number = parse_candidate(candidate)
    allocation, slot_id = model_slot_identity(candidate, variant, repetition)
    from tests.helpers.phase_b_repair_cycle_execution import formal_manifest
    manifest = model_manifest(number) if kind == "development" else formal_manifest(number)
    row = manifest["slots"][f"{allocation}/{slot_id}"]
    return bind_slot(candidate, allocation, slot_id,
                     declared=row["declared"], dependencies=row["dependencies"], run=run, store=store)


def model_slot(candidate, variant, *, repetition=1, execute=False, live=False):
    """One real gate under a timed fixed slot; human review is a separate step."""
    from tests.helpers import phase_b_model_evaluation as models
    if not execute or not live:
        _fail("cycle model execution requires both explicit live switches")
    kind, _ = parse_candidate(candidate)
    guard_model_slot(variant, repetition, category=kind, candidate=candidate, model_profile="disabled")
    store, run, _, _ = models.prepare(variant, repetition, category=kind, freeze_label=candidate)
    slot = bind_model_run(candidate, variant, store, run, repetition=repetition)
    with activity(slot, seconds=run.budget.run_seconds):
        return models.evaluate(variant, repetition, category=kind, freeze_label=candidate, allow_live=True)


def record_model_outcome(candidate, variant, *, repetition=1, failure_kind=None, affected_dependencies=(), review_path=None):
    from tests.helpers import phase_b_model_evaluation as models
    book = _book()
    _, reference, _ = _parts()
    kind, _ = parse_candidate(candidate)
    allocation, slot_id = model_slot_identity(candidate, variant, repetition)
    directory = models._slot(variant, repetition, kind, candidate)
    metadata = reference._json(directory / "metadata.json")
    if metadata.get("terminal_contract_version") != "terminal-delivery-1":
        _fail("cycle review requires the current terminal contract")
    with book.ledger._lock():
        state = _state(book._snapshot_unlocked())
        frozen = _candidate(state, candidate)
        if metadata.get("bounded_candidate_sha256") != frozen["receipt"]["sha256"]:
            _fail("actual model outcome targets another candidate")
        slots = [s for s in state["slots"].values() if s["candidate"] == candidate
                 and s["allocation"] == allocation and s["slot_id"] == slot_id]
        if len(slots) != 1 or slots[0]["run_id"] != metadata["run_id"]:
            _fail("model outcome has no unique bound slot")
    grade = models.regrade(variant, repetition, category=kind, freeze_label=candidate, review_path=review_path)
    from tests.helpers.phase_b_grading import classify_grade
    status = classify_grade(grade)
    if status not in {"passed", "failed"}:
        _fail("actual independent model review remains required; missing is not a failed slot")
    return record_outcome(slots[0], status=status,
        evidence=[{"path": str(directory / "grade.json"), "sha256": reference.sha256_file(directory / "grade.json")}],
        failure_kind=failure_kind if status == "failed" else None, affected_dependencies=affected_dependencies)
