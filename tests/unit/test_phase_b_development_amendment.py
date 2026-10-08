"""D4 gate transfers on isolated synthetic ledgers; no live execution."""

import copy
import json

import pytest

from orca_agent.models import BudgetLimits, Goal, PermissionSnapshot, Request
from orca_agent.store import sha256_file
from tests.helpers import phase_b_budget as budget
from tests.helpers import phase_b_cycle_formal_amendment as formal
from tests.helpers import phase_b_development_amendment as amendment
from tests.helpers import phase_b_repair_cycle as cycle
from tests.helpers import phase_b_repair_cycle_execution as execution
from tests.unit.test_phase_b_budget import bind_model, record, settle_model
from tests.unit.test_phase_b_cycle_formal_amendment import formal_approved as formal_approved
from tests.unit.test_phase_b_repair_cycle import approved as approved
from tests.unit.test_phase_b_repair_cycle import cycle_approved as cycle_approved
from tests.unit.test_phase_b_repair_cycle import legacy as legacy
from tests.unit.test_phase_b_repair_cycle import r3_approved as r3_approved
from tests.unit.test_phase_b_repair_cycle import renewal_approved as renewal_approved


def run_for(store, row):
    run = store.create_run(Request(goals=[Goal(id="g", port="energy", minimum_check_version="orca-hf-2")]),
        None, PermissionSnapshot(), BudgetLimits(model_calls=row["declared"]["http_requests"],
            model_tokens=row["declared"]["tokens"], orca_starts=0, extra_orca_starts=0))
    run.batch_category = "development"
    store._write_json(f"runs/{run.id}/run.json", run)
    return run


@pytest.fixture
def prepared(formal_approved, cycle_approved, tmp_path, monkeypatch, request):
    book, formal_path, _ = formal_approved
    _, store, old_run, _, unknown = cycle_approved
    formal.apply(approval_path=formal_path, approval_sha256=sha256_file(formal_path), execute=True)
    # Settle the fixture's old unknown through real accounting, without an HTTP.
    settle_model(book, store, old_run, unknown)
    monkeypatch.setattr(cycle, "_book", lambda: book)
    actual_candidate = cycle._candidate
    monkeypatch.setattr(cycle, "_candidate", lambda state, label, **kw:
                        actual_candidate(state, label, verify_source=False))
    document = tmp_path / "synthetic-d4-proposal.md"
    document.write_text("Synthetic D4 approval test only; not real user authority.", encoding="utf-8")
    monkeypatch.setattr(amendment, "PROPOSAL_PATH", str(document))
    for number in (1, 2, 3):
        label = cycle.candidate_label("development", number)
        manifest = execution.development_manifest(number)
        cycle._publish(book, book.snapshot(), "candidates", label,
            {"label": label, "kind": "development", "number": number, "commit": str(number) * 40,
             "approval_sha256": budget.reference.CYCLE_APPROVAL_SHA256,
             "manifest": manifest, "source_files": {"synthetic": "no live runtime"}})
        variants = cycle.MODEL_SLOTS[:3 if number == 3 else 2]
        for index, variant in enumerate(variants):
            row = manifest["slots"][f"gates-{number}/{variant.replace('/', '__')}"]
            run = run_for(store, row)
            slot = cycle.bind_model_run(label, variant, store, run)
            with cycle.activity(slot, seconds=1800):
                pass
            evidence = tmp_path / f"synthetic-grade-{number}-{index}.json"
            evidence.write_text('{"offline_synthetic":true}', encoding="utf-8")
            failed = index == len(variants) - 1
            if not (getattr(request, "param", None) == "pending_review" and number == 3 and failed):
                cycle.record_outcome(slot, status="failed" if failed else "passed",
                    failure_kind="ordinary" if failed else None,
                    affected_dependencies=["protocol"] if failed else [],
                    evidence=[{"path": str(evidence), "sha256": sha256_file(evidence)}])
    scenario = getattr(request, "param", None)
    if scenario == "pending_activity":
        cycle._publish(book, book.snapshot(), "activities", "synthetic-interrupted-activity",
            {"slot_receipt_sha256": slot["receipt"]["sha256"], "seconds_reserved": 1800,
             "segment": "complete", "started_at": "2026-10-08T00:00:00Z"})
    elif scenario == "unknown":
        # Inject a settled unknown HTTP receipt offline. Bypassing admission is
        # limited to setup; the amendment must reject this unresolved cost.
        run = run_for(store, row)
        item = record(499)
        with monkeypatch.context() as injection:
            injection.setattr(cycle, "guard_budget_reservation", lambda *a, **kw: {})
            book.reserve_model(run, item)
        bind_model(store, run, item)
        settle_model(book, store, run, item, known=False)
    proposed = (amendment._proposal(sha256_file(book.ledger.path), book.snapshot()["limit_authority"])
                if scenario else amendment.proposal())
    value = {"schema_version": 1, "approval_id": amendment.APPROVAL_ID, "status": "user_approved",
        "user_statement": "synthetic test approval only", "question_text": "synthetic fixed D4 question",
        "proposal": proposed}
    path = tmp_path / "synthetic-d4-approval.json"
    budget.reference._save(path, value)
    return book, store, path, value


def apply(path, **kwargs):
    return amendment.apply(approval_path=path, approval_sha256=sha256_file(path), execute=True, **kwargs)


def test_no_implicit_approval_or_unapplied_fourth_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "absent")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "none")
    with pytest.raises(budget.ReferenceBlocked, match="explicit execute"):
        amendment.apply()
    label = cycle.candidate_label("development", 4)
    assert cycle.parse_candidate(label) == ("development", 4)
    with pytest.raises(budget.ReferenceBlocked, match="explicit applied"):
        amendment.assert_open({}, label)
    for number in (5, True, 4.0, "4"):
        with pytest.raises(budget.ReferenceBlocked):
            cycle.candidate_label("development", number)
    assert not list(tmp_path.iterdir())


def test_unapplied_fourth_freeze_and_bind_reject_before_external_probes(prepared, monkeypatch):
    book, store, _, _ = prepared
    before = book.ledger.path.read_bytes()
    label = cycle.candidate_label("development", 4)
    manifest = cycle.model_manifest(4)
    monkeypatch.setattr(cycle.subprocess, "check_output",
                        lambda *a, **kw: pytest.fail("unapproved freeze reached an external probe"))
    with pytest.raises(budget.ReferenceBlocked, match="explicit applied"):
        cycle.freeze_candidate("development", 4, manifest=manifest)
    row = next(iter(manifest["slots"].values()))
    run = run_for(store, row)
    with pytest.raises(budget.ReferenceBlocked, match="explicit applied"):
        cycle.bind_model_run(label, cycle.MODEL_SLOTS[0], store, run)
    assert book.ledger.path.read_bytes() == before


@pytest.mark.parametrize("prepared", ["unknown", "pending_activity", "pending_review"], indirect=True)
def test_application_rejects_unknown_cost_or_pending_work(prepared):
    book, _, path, _ = prepared
    before = book.ledger.path.read_bytes()
    with pytest.raises(budget.ReferenceBlocked, match="unknown|unresolved|pending independent review"):
        apply(path)
    assert book.ledger.path.read_bytes() == before
    assert not (book.ledger.root / "budget-amendments" / amendment.APPROVAL_ID).exists()


@pytest.mark.parametrize("point", [None, "after_original_snapshot", "after_amendment_receipt", "after_ledger_publication"])
def test_exact_transfer_is_idempotent_and_preserves_original_bytes(prepared, point):
    book, _, path, value = prepared
    before = book.ledger.path.read_bytes()
    old_files = {p: p.read_bytes() for p in book.ledger.root.rglob("*.json") if p != book.ledger.path}
    original_scope = cycle.scope()
    def fault(stage):
        if stage == point:
            raise OSError("synthetic D4 interruption")
    if point:
        with pytest.raises(OSError, match="synthetic"):
            apply(path, fault=fault)
        if point != "after_ledger_publication":
            with pytest.raises(budget.ReferenceBlocked, match="publication is incomplete"):
                cycle._state(book.snapshot())
    receipt = apply(path)
    after = book.snapshot()
    assert {k: v for k, v in after.items() if k != amendment.AUTHORITY_KEY} == json.loads(before)
    assert cycle.scope() == original_scope
    assert all(p.read_bytes() == content for p, content in old_files.items())
    saved = book.ledger.root / "budget-amendments" / amendment.APPROVAL_ID
    assert (saved / "before.json").read_bytes() == before
    assert (saved / "approval.json").read_bytes() == path.read_bytes()
    assert apply(path) == receipt and amendment.proposal() == value["proposal"]
    actual = amendment.allocations(after)
    original = formal.allocations(after)
    assert {k: v for k, v in actual.items() if not k.startswith("gates-")} == {
        k: v for k, v in original.items() if not k.startswith("gates-")}
    assert sum(v["http_requests"] for k, v in actual.items() if k.startswith("gates-")) == 192
    assert sum(v["tokens"] for k, v in actual.items() if k.startswith("gates-")) == 1536000
    assert cycle.candidate_label("development", 4).endswith("development-4")
    with pytest.raises(budget.ReferenceBlocked):
        cycle.candidate_label("development", 5)


@pytest.mark.parametrize("change", ["sha", "user", "usd", "tokens", "transfer", "science", "candidate5",
                                   "old_scope", "document", "manifest", "baseline", "float", "extra"])
def test_rejects_unapproved_or_changed_record_without_publication(prepared, change):
    book, _, path, value = prepared
    before = book.ledger.path.read_bytes()
    altered = copy.deepcopy(value)
    p = altered["proposal"]
    if change == "user":
        altered["user_statement"] = ""
    elif change == "usd":
        p["unchanged_limits"]["model"]["usd"] += 1
    elif change == "tokens":
        p["gate_allocations"]["gates-4"]["tokens"] += 1
    elif change == "transfer":
        p["transfers"][0]["from"] = "formal-1"
    elif change == "science":
        p["gate_allocations"]["gates-4"]["development"] = 1
    elif change == "candidate5":
        p["development_candidates"] = 5
    elif change == "old_scope":
        p["original_cycle_scope_sha256"] = "a" * 64
    elif change == "document":
        p["decision_document_sha256"] = "b" * 64
    elif change == "manifest":
        p["gate_manifest_sha256"] = "c" * 64
    elif change == "baseline":
        p["before_sha256"] = "d" * 64
    elif change == "float":
        p["development_candidates"] = 4.0
    elif change == "extra":
        altered["execute"] = True
    budget.reference._save(path, altered)
    with pytest.raises(budget.ReferenceBlocked):
        amendment.apply(approval_path=path, approval_sha256="0" * 64 if change == "sha" else sha256_file(path), execute=True)
    assert book.ledger.path.read_bytes() == before
    assert not (book.ledger.root / "budget-amendments" / amendment.APPROVAL_ID).exists()


def publish_fourth(book):
    label = cycle.candidate_label("development", 4)
    manifest = execution.development_manifest(4)
    cycle._publish(book, book.snapshot(), "candidates", label,
        {"label": label, "approval_sha256": budget.reference.CYCLE_APPROVAL_SHA256,
         "manifest": manifest, "source_files": {"synthetic": "no live execution"}})
    return label, manifest


def test_all_old_execution_paths_close_but_new_gate_reserves_and_settles(prepared):
    book, store, path, _ = prepared
    apply(path)
    before = book.snapshot()
    old = next(iter(before["repair_cycle"]["slots"].values()))
    old_run = store.load_run(old["run_id"])
    for operation in (lambda: cycle.guard_model_slot(cycle.MODEL_SLOTS[0], 1, category="development",
                         candidate=old["candidate"], model_profile="disabled"),
                      lambda: cycle.bind_model_run(old["candidate"], cycle.MODEL_SLOTS[0], store, old_run),
                      lambda: cycle._slot_guard(book.snapshot(), owner={"run_id": old_run.id, "store_root": str(store.root.resolve())}),
                      lambda: execution._open(old["candidate"], "layered", "water-input", execute=True, live=True)):
        with pytest.raises(budget.ReferenceBlocked, match="closed"):
            operation()
    with pytest.raises(budget.ReferenceBlocked, match="closed"):
        with cycle.activity(old, seconds=1800):
            pytest.fail("closed candidate executed")
    assert book.snapshot() == before
    label, manifest = publish_fourth(book)
    assert execution.manifest_budget(manifest)["gates-4"]["http_requests"] == 64
    old_manifest = execution.development_manifest(3)
    assert {k: v for k, v in manifest["slots"].items() if not k.startswith("gates-")} == {
        k: v for k, v in old_manifest["slots"].items() if not k.startswith("gates-")}
    row = manifest["slots"]["gates-4/" + cycle.MODEL_SLOTS[0].replace("/", "__")]
    run = run_for(store, row)
    slot = cycle.bind_model_run(label, cycle.MODEL_SLOTS[0], store, run)
    with cycle.activity(slot, seconds=1800):
        item = record(404)
        book.reserve_model(run, item)
        bind_model(store, run, item)
        settle_model(book, store, run, item)
    after = book.snapshot()
    assert after["model_usage"]["http_requests"] == before["model_usage"]["http_requests"] + 1
    assert after["limits"] == before["limits"]
    with pytest.raises(budget.ReferenceBlocked, match="all current-candidate"):
        cycle._dependencies(cycle._state(after), label, ["science"], scientific=True)


@pytest.mark.parametrize("change", ["approval", "receipt", "before", "authority_removed", "limits", "old_slot"])
def test_applied_overlay_tampering_blocks_before_new_execution(prepared, change):
    book, _, path, _ = prepared
    apply(path)
    ledger = book.snapshot()
    directory = book.ledger.root / "budget-amendments" / amendment.APPROVAL_ID
    if change in {"approval", "receipt", "before"}:
        name = {"receipt": "amendment", "approval": "approval", "before": "before"}[change]
        (directory / f"{name}.json").write_bytes(b'{}\n')
    elif change == "authority_removed":
        del ledger[amendment.AUTHORITY_KEY]
    elif change == "limits":
        ledger['limits']['model']['tokens'] += 1
    else:
        old = next(iter(ledger['repair_cycle']['slots'].values()))
        old['declared']['http_requests'] -= 1
    with pytest.raises(budget.ReferenceBlocked):
        cycle._state(ledger)
