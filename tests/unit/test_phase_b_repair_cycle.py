"""Adopted multi-candidate scope, synthetic accounting and disabled live guards."""

import copy

import pytest

from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import Store, sha256_file
from tests.helpers import phase_b_bounded_package as package
from tests.helpers import phase_b_budget as budget
from tests.helpers import phase_b_model_evaluation as models
from tests.helpers import phase_b_repair_cycle as cycle
from tests.unit.test_phase_b_bounded_package import approved as approved
from tests.unit.test_phase_b_bounded_r3 import r3_approved as r3_approved
from tests.unit.test_phase_b_bounded_renewal import renewal_approved as renewal_approved
from tests.unit.test_phase_b_budget import bind_model, make_run, record, settle_model
from tests.unit.test_phase_b_budget_amendment import legacy as legacy


def test_exact_cycle_scope_matches_adopted_plan_and_original_applied_baseline():
    s = cycle.scope()
    sums = {d: sum(v[d] for v in s["allocations"].values()) for d in cycle.DIMENSIONS}
    assert sums == dict(zip(cycle.DIMENSIONS, (1744, 12864000, 21, 42, 108, 22, 22), strict=True))
    assert 324 + sums["http_requests"] == s["proposed_limits"]["model"]["http_requests"] == 2068
    assert 1018912 + sums["tokens"] == s["proposed_limits"]["model"]["tokens"] == 13882912
    assert s["proposed_limits"] == budget.reference.CYCLE_LIMITS
    assert s["execution_activity_seconds"] == 345600
    assert s["development_candidates"] == 3 and s["formal_candidates"] == 2
    assert tuple(s["model_gates_per_development_candidate"]) == package.MODEL_SLOTS
    assert sha256_file(package.PROJECT / cycle.PLAN_PATH) == cycle.PLAN_SHA256
    approval = package.reference.cycle_approval()
    assert approval["user_statement"] == "按照方案开始修复，并完成刚才还没有完成的任务"
    assert approval["approval_question"] is None and approval["approval_options"] == []
    assert approval["repair_cycle"] == s
    assert approval["previous_approval_id"] == package.reference.R3_APPROVAL_ID


def test_model_operator_uses_unchanged_run_seconds(manager, monkeypatch):
    run, slot = bound(manager)
    store = manager[1]
    observed = {}
    monkeypatch.setattr(cycle, "guard_model_slot", lambda *a, **k: None)
    monkeypatch.setattr(models, "prepare", lambda *a, **k: (store, run, {}, {}))
    monkeypatch.setattr(models, "evaluate", lambda *a, **k: observed.update(active=cycle._ACTIVE.get()) or "offline")
    original = run.model_dump(mode="json")
    assert cycle.model_slot(manager[2], cycle.MODEL_SLOTS[0], execute=True, live=True) == "offline"
    assert observed["active"]
    state = manager[0].snapshot()["repair_cycle"]
    entry = next(iter(state["activities"].values()))
    assert entry["seconds_reserved"] == original["budget"]["run_seconds"] == 1800
    assert entry["slot_receipt_sha256"] == slot["receipt"]["sha256"]
    assert store.load_run(run.id).model_dump(mode="json") == original


@pytest.mark.parametrize("number", [0, 4, True, 1.0, "1"])
def test_candidate_count_has_strict_finite_identity(number):
    with pytest.raises(budget.ReferenceBlocked):
        cycle.candidate_label("development", number)


@pytest.mark.parametrize("label", [package.LABEL, package.RENEWAL_LABEL, package.R3_LABEL, package.R4_LABEL])
def test_all_old_packages_are_closed_even_if_previously_approved(label):
    with pytest.raises(budget.ReferenceBlocked, match="closed"):
        package._assert_open(label)


@pytest.mark.parametrize("entry", ["prepare", "evaluate"])
def test_low_level_new_model_entry_cannot_bypass_unpinned_cycle(tmp_path, monkeypatch, entry):
    monkeypatch.setattr(models, "ROOT", tmp_path / "evaluations")
    monkeypatch.setattr(budget.reference, "CYCLE_APPROVAL_SHA256", None)
    kwargs = {"allow_live": True} if entry == "evaluate" else {}
    with pytest.raises(budget.ReferenceBlocked, match="not pinned"):
        getattr(models, entry)(cycle.MODEL_SLOTS[0], 1, category="development",
            freeze_label=cycle.candidate_label("development", 1), **kwargs)
    assert not list(tmp_path.iterdir())


@pytest.fixture
def cycle_approved(r3_approved, tmp_path, monkeypatch):
    book, store, run, known, unknown, _ = r3_approved
    package.apply_limits(execute=True, package=package.R3_LABEL)
    value = {"approval_id": package.reference.CYCLE_APPROVAL_ID, "status": "user_approved",
             "previous_limits": package.reference.R3_LIMITS, "approved_limits": cycle.LIMITS,
             "previous_approval_id": package.reference.R3_APPROVAL_ID,
             "previous_approval_sha256": package.reference.R3_APPROVAL_SHA256,
             "decision_document": cycle.PLAN_PATH, "decision_document_sha256": cycle.PLAN_SHA256,
             "repair_cycle": cycle.scope(), "approval_baseline": {"ledger_sha256": sha256_file(book.ledger.path)}}
    path = tmp_path / "synthetic-cycle-approval.json"
    package.reference._save(path, value)
    monkeypatch.setattr(package.reference, "CYCLE_APPROVAL", path)
    monkeypatch.setattr(package.reference, "CYCLE_APPROVAL_SHA256", sha256_file(path))
    return book, store, run, known, unknown


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_cycle_migration_retains_every_cost_and_skips_unapproved_r4(cycle_approved, point):
    book, *_ = cycle_approved
    before = book.snapshot()
    old_files = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    def fault(where):
        if where == point:
            raise OSError("synthetic migration interruption")
    if point:
        with pytest.raises(OSError, match="synthetic"):
            package.apply_limits(execute=True, package=cycle.CYCLE_ID, fault=fault)
    receipt = package.apply_limits(execute=True, package=cycle.CYCLE_ID)
    after = book.snapshot()
    assert after["limits"] == cycle.LIMITS
    assert {k: v for k, v in before.items() if k not in {"limits", "limit_authority"}} == {
        k: v for k, v in after.items() if k not in {"limits", "limit_authority"}}
    assert after["model_usage"]["unknown_tokens"] == 150
    assert receipt["previous_limit_authority"]["approval_id"] == package.reference.R3_APPROVAL_ID
    assert all(p.read_bytes() == value for p, value in old_files.items())
    assert not (book.ledger.root / "budget-amendments" / package.reference.R4_APPROVAL_ID).exists()
    assert package.apply_limits(execute=True, package=cycle.CYCLE_ID) == receipt
    with pytest.raises(budget.ReferenceBlocked, match="unknown"):
        cycle._no_unknown(after, cycle._state(after))


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """Real JSON/locks/receipt validation, isolated from the historical ledger."""
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "absent")
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    book = budget.AcceptanceBudget(store)
    book._save({"schema_version": 1, "limits": copy.deepcopy(cycle.LIMITS), "entries": {}})
    # This fixture isolates scope accounting from approval migration, covered
    # separately above; production snapshot never bypasses its authority chain.
    monkeypatch.setattr(book.ledger, "snapshot", lambda **_: budget.reference._json(book.ledger.path))
    monkeypatch.setattr(cycle, "_book", lambda: book)
    real_candidate = cycle._candidate
    monkeypatch.setattr(cycle, "_candidate", lambda state, label: real_candidate(state, label, verify_source=False))
    monkeypatch.setattr(budget.reference, "CYCLE_APPROVAL_SHA256", "synthetic-pin")
    label = cycle.candidate_label("development", 1)
    from tests.helpers.phase_b_repair_cycle_execution import development_manifest
    manifest = development_manifest(1)
    ledger = book.snapshot()
    cycle._publish(book, ledger, "candidates", label, {"label": label,
        "approval_sha256": "synthetic-pin", "manifest": manifest, "source_files": {"synthetic": "never executed"}})
    return book, store, label, manifest


def bound(manager, variant=None):
    book, store, label, manifest = manager
    variant = variant or cycle.MODEL_SLOTS[0]
    row = manifest["slots"][f"gates-1/{variant.replace('/', '__')}"]
    limits = BudgetLimits(model_calls=row["declared"]["http_requests"],
        model_tokens=row["declared"]["tokens"], orca_starts=0, extra_orca_starts=0,
        identity_queries=0, structure_preparations=0)
    run = store.create_run(Request(goals=[Goal(id="g", port="energy", minimum_check_version="orca-hf-2")]),
                           None, PermissionSnapshot(), limits)
    run.batch_category = "development"
    store._write_json(f"runs/{run.id}/run.json", run)
    return run, cycle.bind_model_run(label, variant, store, run)


def test_low_level_accounting_rejects_unbound_run_and_missing_activity(manager):
    book, store, *_ = manager
    run, _, _ = make_run(store, "development")
    with pytest.raises(budget.ReferenceBlocked, match="exactly one frozen slot"):
        book.reserve_model(run, record(10))
    run, _ = bound(manager)
    with pytest.raises(budget.ReferenceBlocked, match="reserved activity"):
        book.reserve_model(run, record(11))
    assert not book.snapshot().get("model_records")


def test_slot_activity_settles_real_costs_once_and_does_not_reset_allowance(manager):
    book, store, *_ = manager
    run, slot = bound(manager)
    with cycle.activity(slot, seconds=1800):
        item = record(12)
        book.reserve_model(run, item)
        bind_model(store, run, item)
        settle_model(book, store, run, item)
    ledger = book.snapshot()
    assert ledger["model_usage"]["http_requests"] == 1
    assert ledger["model_records"][item["id"]]["cycle_allocation"] == "gates-1"
    assert len(ledger["repair_cycle"]["activities"]) == len(ledger["repair_cycle"]["activity_settlements"]) == 1
    with pytest.raises(budget.ReferenceBlocked, match="already consumed"):
        with cycle.activity(slot, seconds=1800):
            pytest.fail("no execution")
    assert book.snapshot() == ledger


def test_interrupted_activity_blocks_other_slots_without_refund(manager):
    book, *_ = manager
    _, slot = bound(manager)
    with pytest.raises(OSError):
        with cycle.activity(slot, seconds=1800):
            raise OSError("synthetic interrupted execution")
    state = book.snapshot()["repair_cycle"]
    assert len(state["activities"]) == 1 and not state["activity_settlements"]
    with pytest.raises(budget.ReferenceBlocked, match="unresolved execution activity"):
        bound(manager, cycle.MODEL_SLOTS[1])


def test_run_limits_cannot_grow_after_cycle_binding(manager):
    book, store, *_ = manager
    run, slot = bound(manager)
    run.budget = run.budget.model_copy(update={"identity_queries": 1})
    # Deliberate durable-file tampering; save_run itself already rejects this.
    store._write_json(f"runs/{run.id}/run.json", run)
    with pytest.raises(budget.ReferenceBlocked, match="Run budget changed"):
        with cycle.activity(slot, seconds=1800):
            book.reserve_model(run, record(13))
    assert not book.snapshot().get("model_records")


def test_ordinary_failure_only_allows_independent_zero_orca_diagnostics(manager, tmp_path):
    book, _, label, _ = manager
    _, slot = bound(manager, cycle.MODEL_SLOTS[1])
    evidence = tmp_path / "actual-offline-grade.json"
    evidence.write_text('{"status":"failed","synthetic":true}')
    cycle.record_outcome(slot, status="failed", failure_kind="ordinary",
        affected_dependencies=["terminal_delivery"], evidence=[{"path": str(evidence), "sha256": sha256_file(evidence)}])
    state = cycle._state(book.snapshot())
    cycle._dependencies(state, label, ["request_semantics", "protocol"])
    with pytest.raises(budget.ReferenceBlocked, match="related failure"):
        cycle._dependencies(state, label, ["query", "terminal_delivery"])
    with pytest.raises(budget.ReferenceBlocked, match="related failure"):
        cycle._dependencies(state, label, ["science"], scientific=True)


def test_science_requires_all_current_candidate_gates_not_old_passes(manager):
    book, _, label, _ = manager
    with pytest.raises(budget.ReferenceBlocked, match="all current-candidate"):
        cycle._dependencies(cycle._state(book.snapshot()), label, ["science"], scientific=True)


def test_manifest_cannot_borrow_another_candidate_or_omit_required_gate():
    manifest = cycle.model_manifest(1)
    cycle._validate_manifest("development", 1, manifest)
    with pytest.raises(budget.ReferenceBlocked, match="another development"):
        cycle._validate_manifest("development", 2, manifest)
    manifest["slots"].pop(next(iter(manifest["slots"])))
    with pytest.raises(budget.ReferenceBlocked, match="all exact current model gates"):
        cycle._validate_manifest("development", 1, manifest)


def test_invalid_reference_and_second_formal_round_cannot_bypass_unfinished_adapter(manager):
    book, *_ = manager
    with pytest.raises(budget.ReferenceBlocked, match="exactly one frozen slot"):
        cycle.guard_reference_reservation(book.snapshot(), "unbound-reference", "reference", {})
    with pytest.raises(budget.ReferenceBlocked, match="formal"):
        cycle._validate_manifest("formal", 2, {"slots": {"fake": {}}})


def test_orphan_cycle_receipt_cannot_be_ignored_for_new_identity(manager):
    book, *_ = manager
    path = book.ledger.root / cycle.CYCLE_ID / "slots" / "orphan.json"
    budget.reference._save(path, {"synthetic": "interrupted before ledger publication"}, immutable=True)
    with pytest.raises(budget.ReferenceBlocked, match="publication is incomplete"):
        bound(manager)


def test_activity_96_hour_total_is_checked_before_side_effect(manager):
    book, *_ = manager
    _, slot = bound(manager)
    ledger = book.snapshot()
    cycle._publish(book, ledger, "activities", "prior-activities-total", {
        "slot_receipt_sha256": "historical-synthetic-slot", "seconds_reserved": 345599, "segment": "complete"})
    ledger = book.snapshot()
    cycle._publish(book, ledger, "activity_settlements", "prior-activities-total", {
        "slot_receipt_sha256": "historical-synthetic-slot", "seconds": 345599, "segment": "complete"})
    with pytest.raises(budget.ReferenceBlocked, match="activity time exhausted"):
        with cycle.activity(slot, seconds=2):
            pytest.fail("no activity after cap")


def test_unknown_activity_reconciliation_preserves_full_reserved_time(manager, tmp_path):
    book, *_ = manager
    _, slot = bound(manager)
    with pytest.raises(OSError):
        with cycle.activity(slot, seconds=10):
            raise OSError("synthetic interruption")
    key = next(iter(book.snapshot()["repair_cycle"]["activities"]))
    audit = tmp_path / "known-empty-run-audit.json"
    audit.write_text('{"synthetic":true,"no_execution_began":true}')
    cycle.reconcile_activity(key, evidence=[{"path": str(audit), "sha256": sha256_file(audit)}])
    settled = book.snapshot()["repair_cycle"]["activity_settlements"][key]
    assert settled["seconds"] == 10
    assert settled["time_accounting"] == "conservative_full_reservation"
    with pytest.raises(budget.ReferenceBlocked, match="already consumed"):
        with cycle.activity(slot, seconds=10):
            pytest.fail("reconciliation cannot refund execution opportunity")


def test_known_activity_overrun_retains_actual_time_and_requires_review(manager, monkeypatch):
    book, *_ = manager
    _, slot = bound(manager)
    clock = {"now": 100.0}
    monkeypatch.setattr(cycle.time, "monotonic", lambda: clock["now"])
    with pytest.raises(budget.ReferenceBlocked, match="actual time retained"):
        with cycle.activity(slot, seconds=1):
            clock["now"] = 103.0
    settled = next(iter(book.snapshot()["repair_cycle"]["activity_settlements"].values()))
    assert settled["seconds"] == 3 and settled["exceeded_reservation"] is True
    with pytest.raises(budget.ReferenceBlocked, match="awaits actual review"):
        bound(manager, cycle.MODEL_SLOTS[1])


def test_changed_outcome_evidence_stops_readmission(manager, tmp_path):
    book, *_ = manager
    _, slot = bound(manager)
    proof = tmp_path / "grade.json"
    proof.write_text('{"synthetic":true,"status":"failed"}')
    cycle.record_outcome(slot, status="failed", failure_kind="ordinary", affected_dependencies=["terminal_delivery"],
        evidence=[{"path": str(proof), "sha256": sha256_file(proof)}])
    proof.write_text('{"synthetic":true,"status":"passed"}')
    with pytest.raises(budget.ReferenceBlocked, match="source hash changed"):
        cycle._state(book.snapshot())
