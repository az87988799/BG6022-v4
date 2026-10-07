"""Shared acceptance quota/cross-file accounting tests; no model or ORCA sends."""

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from orca_agent.models import (
    BudgetLimits,
    Goal,
    InputRef,
    OutputBinding,
    PermissionSnapshot,
    Plan,
    Request,
    Step,
)
from orca_agent.store import Store, sha256_file

SCRIPT = Path(__file__).resolve().parents[1] / "helpers" / "phase_b_budget.py"
SPEC = importlib.util.spec_from_file_location("phase_b_budget", SCRIPT)
budget = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(budget)
WATER = "3\nwater\nO 0 0 0\nH 0 .757 .587\nH 0 -.757 .587\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(budget.reference, "BATCH_ROOT", tmp_path / "batch")
    monkeypatch.setattr(budget.reference, "DELIVERED_SNAPSHOT", tmp_path / "delivered.json")
    store = Store(tmp_path / "store", environment_root=tmp_path / "environment")
    return budget.AcceptanceBudget(store), store


def make_run(store, category="formal", *, science=False):
    request = Request(goals=[Goal(id="g", port="energy", minimum_check_version="orca-hf-2")])
    step = plan = artifact = None
    if science:
        source = store.root.parent / (store.root.name + "-geometry.xyz")
        source.write_text(WATER)
        artifact = store.import_artifact(source, "initial_geometry")
        request.geometry_artifact_id = artifact.id
        step = Step(id="sp", logical_id="sp", tool="orca.sp", geometry=InputRef(artifact_id=artifact.id))
        plan = Plan(request_id=request.id, steps=[step], goal_map={"g": OutputBinding(step_id="sp", port="energy")})
    run = store.create_run(request, plan, PermissionSnapshot(scientific_execution=science,
                           artifact_ids=[artifact.id] if artifact else []), BudgetLimits())
    # Test initialization establishes the frozen classification before any spend.
    run.batch_category = category
    store._write_json(f"runs/{run.id}/run.json", run)
    return run, step, artifact


def record(index=0, *, prompt=100, completion=50):
    return {"id": f"model_{index}", "logical_id": f"decision_{index}", "request_hash": "a" * 64,
            "basis": {"request_version": 1, "plan_version": None, "permission_version": 1, "control_generation": 0},
            "input_reserved": prompt, "output_reserved": completion,
            "cost_reserved_usd": str(budget._price(prompt, completion)), "status": "reserved",
            "prompt_version": "fixture-1", "sdk_version": "fixture", "model": "fixture",
            "token_bound_version": "fixture-1"}


def bind_model(store, run, item):
    run.model_records.append(item)
    store._write_json(f"runs/{run.id}/run.json", run)


def settle_model(book, store, run, item, *, known=True, prompt=40, completion=10):
    item["status"] = "known" if known else "unknown"
    if known:
        path = store.path(f"runs/{run.id}/model/{item['id']}.response.json")
        budget.reference._save(path, {"offline_test_receipt": True}, immutable=True)
        item.update(input_tokens=prompt, output_tokens=completion, total_tokens=prompt + completion,
                    cost_known_usd=str(budget._price(prompt, completion)), response_record_sha256=sha256_file(path))
    store._write_json(f"runs/{run.id}/run.json", run)
    book.settle_model(run, item)


def test_constructor_and_empty_snapshot_are_readonly(setup):
    book, _ = setup
    assert not book.ledger.root.exists()
    assert book.snapshot()["limits"] == budget.LIMITS
    assert not book.ledger.path.exists()
    assert {path.name for path in book.ledger.root.iterdir()} <= {"batch-ledger.lock"}


@pytest.mark.parametrize("publication", ["reservation", "settlement"])
def test_snapshot_waits_for_receipt_and_ledger_to_be_published_together(setup, monkeypatch, publication):
    """Two independent lock clients exercise the real publication gap offline."""
    book, store = setup
    reader = budget.AcceptanceBudget(store)
    run, _, _ = make_run(store)
    item = record()
    if publication == "settlement":
        book.reserve_model(run, item)
        bind_model(store, run, item)
    receipt_written, release_writer, reader_started, read_files = (Event() for _ in range(4))
    save = budget.reference._save
    snapshot = reader.ledger.snapshot

    def paused_publication(path, value, **kwargs):
        result = save(path, value, **kwargs)
        is_receipt = (path.name == "reservation.json" if publication == "reservation"
                      else path.parent.name == "settlements")
        if is_receipt:
            receipt_written.set()
            assert release_writer.wait(5), "offline test writer was not released"
        return result

    def inspected_files():
        read_files.set()
        return snapshot()

    def read_snapshot():
        reader_started.set()
        return reader.snapshot()

    monkeypatch.setattr(budget.reference, "_save", paused_publication)
    monkeypatch.setattr(reader.ledger, "snapshot", inspected_files)
    with ThreadPoolExecutor(max_workers=2) as executor:
        writer = executor.submit(book.reserve_model, run, item) if publication == "reservation" else (
            executor.submit(settle_model, book, store, run, item))
        try:
            assert receipt_written.wait(5)
            observed = executor.submit(read_snapshot)
            assert reader_started.wait(5)
            assert not read_files.wait(0.15), "snapshot read uncommitted files without the writer lock"
            assert not observed.done()
        finally:
            release_writer.set()
        writer.result(timeout=5)
        value = observed.result(timeout=5)
    expected_state = "reserved" if publication == "reservation" else "known"
    assert value["model_records"][item["id"]]["state"] == expected_state
    assert value["model_usage"]["http_requests"] == 1
    assert value["model_usage"]["tokens"] == (150 if publication == "reservation" else 50)


def test_locked_mutations_do_not_reenter_public_snapshot(setup, monkeypatch):
    book, store = setup
    run, _, _ = make_run(store)

    def forbidden():
        raise AssertionError("already-locked mutation must not acquire another FileLock")

    monkeypatch.setattr(book, "snapshot", forbidden)
    item = record()
    book.reserve_model(run, item)
    bind_model(store, run, item)
    settle_model(book, store, run, item)
    assert budget.AcceptanceBudget(store).snapshot()["model_usage"]["known_tokens"] == 50


def test_missing_ledger_with_delivered_snapshot_never_resets_reference_or_model_quota(setup):
    book, store = setup
    budget.reference._save(budget.reference.DELIVERED_SNAPSHOT, {"entries": {"original": {"category": "reference"}}})
    run, _, _ = make_run(store)
    with pytest.raises(budget.ReferenceBlocked, match="budgets cannot restart"):
        book.reserve_model(run, record())
    assert not book.ledger.path.exists()


def test_reserved_http_is_unknown_until_known_settlement_and_counts_never_refund(setup):
    book, store = setup
    run, _, _ = make_run(store)
    item = record()
    book.reserve_model(run, item)
    assert book.snapshot()["model_usage"] == {"http_requests": 1, "tokens": 150, "usd": "0.00009",
                                               "known_tokens": 0, "unknown_tokens": 150,
                                               "known_usd": "0", "unknown_usd": "0.00009"}
    bind_model(store, run, item)
    settle_model(book, store, run, item)
    settled = book.snapshot()["model_usage"]
    assert settled["http_requests"] == 1 and settled["known_tokens"] == 50 and settled["unknown_tokens"] == 0
    assert settled["known_usd"] == "0.000024"
    before = book.ledger.path.read_bytes()
    book.settle_model(run, item)
    assert book.ledger.path.read_bytes() == before
    with pytest.raises(budget.ReferenceBlocked, match="already reserved"):
        book.reserve_model(run, {**item, "status": "reserved"})


def test_unknown_usage_keeps_full_token_and_decimal_money_occupancy(setup):
    book, store = setup
    run, _, _ = make_run(store)
    item = record()
    book.reserve_model(run, item)
    bind_model(store, run, item)
    settle_model(book, store, run, item, known=False)
    totals = book.snapshot()["model_usage"]
    assert totals["tokens"] == totals["unknown_tokens"] == 150
    assert totals["usd"] == "0.00009" and totals["known_tokens"] == 0


def test_unbound_cross_file_reservation_blocks_another_run_and_root(setup, tmp_path):
    book, store = setup
    run, _, _ = make_run(store)
    book.reserve_model(run, record())
    other = Store(tmp_path / "other", environment_root=tmp_path / "other-environment")
    second, _, _ = make_run(other, "development")
    with pytest.raises(budget.ReferenceBlocked, match="no Run binding"):
        budget.AcceptanceBudget(other).reserve_model(second, record(1))
    assert book.snapshot()["model_usage"]["http_requests"] == 1


def test_cross_root_bound_runs_share_actual_http_and_token_totals(setup, tmp_path):
    book, store = setup
    run, _, _ = make_run(store)
    first = record()
    book.reserve_model(run, first)
    bind_model(store, run, first)
    settle_model(book, store, run, first)
    other = Store(tmp_path / "other", environment_root=tmp_path / "other-environment")
    second, _, _ = make_run(other, "development")
    other_book = budget.AcceptanceBudget(other)
    second_record = record(1)
    other_book.reserve_model(second, second_record)
    bind_model(other, second, second_record)
    assert other_book.snapshot()["model_usage"]["http_requests"] == 2
    assert other_book.snapshot()["model_usage"]["tokens"] == 200


def test_reservation_receipt_before_ledger_crash_blocks_resend(setup, monkeypatch):
    book, store = setup
    run, _, _ = make_run(store)
    original = budget.reference._save

    def crash(path, value, **kwargs):
        if path == book.ledger.path:
            raise OSError("offline crash after immutable reservation")
        return original(path, value, **kwargs)

    monkeypatch.setattr(budget.reference, "_save", crash)
    with pytest.raises(OSError):
        book.reserve_model(run, record())
    monkeypatch.setattr(budget.reference, "_save", original)
    with pytest.raises(budget.ReferenceBlocked, match="uncommitted or missing batch reservation"):
        book.reserve_model(run, record(1))


@pytest.mark.parametrize("change", ["category", "reservation", "totals", "delete_entry", "settlement"])
def test_snapshot_rejects_tampered_accounting_without_writing(setup, change):
    book, store = setup
    run, _, _ = make_run(store)
    item = record()
    book.reserve_model(run, item)
    bind_model(store, run, item)
    settle_model(book, store, run, item)
    data = json.loads(book.ledger.path.read_text(encoding="utf-8"))
    entry = data["model_records"][item["id"]]
    if change == "category":
        entry["category"] = "development"
    elif change == "reservation":
        path = book._directory("model_records", item["id"]) / "reservation.json"
        path.write_text("changed")
    elif change == "totals":
        data["model_usage"]["known_usd"] = "0"
    elif change == "delete_entry":
        data["model_records"] = {}
    else:
        path = book._directory("model_records", item["id"]) / "settlements" / (entry["settlements"][0]["sha256"] + ".json")
        path.write_text("changed")
    budget.reference._save(book.ledger.path, data)
    before = book.ledger.path.read_bytes()
    with pytest.raises(budget.ReferenceBlocked):
        book.snapshot()
    assert book.ledger.path.read_bytes() == before


def test_model_batch_token_limit_uses_reservations_not_only_successes(setup):
    book, store = setup
    run, _, _ = make_run(store)
    item = record(prompt=budget.LIMITS["model"]["tokens"], completion=0)
    book.reserve_model(run, item)
    bind_model(store, run, item)
    with pytest.raises(budget.ReferenceBlocked, match="HTTP/token/USD limit"):
        book.reserve_model(run, record(1))
    assert book.snapshot()["model_usage"]["http_requests"] == 1


def test_http_limit_counts_all_runs_before_send(setup):
    book, store = setup
    run, _, _ = make_run(store)
    ledger = book.snapshot()
    # Seed every allowed zero-token offline reservation, including those above
    # the old 700-record validation bound. All immutable receipts exist.
    maximum = budget.LIMITS["model"]["http_requests"]
    for index in range(maximum):
        item = record(index, prompt=0, completion=0)
        immutable = {**book._run_entry(run, store), "id": item["id"], "record": book._model_basis(item), "reserved_at": "fixture"}
        path = book._directory("model_records", item["id"]) / "reservation.json"
        budget.reference._save(path, immutable, immutable=True)
        ledger.setdefault("model_records", {})[item["id"]] = {**immutable, "state": "reserved",
            "reservation_sha256": sha256_file(path), "settlements": []}
        run.model_records.append(item)
    store._write_json(f"runs/{run.id}/run.json", run)
    book._save(ledger)
    with pytest.raises(budget.ReferenceBlocked, match="HTTP/token/USD limit"):
        book.reserve_model(run, record(maximum + 1))
    assert book.snapshot()["model_usage"]["http_requests"] == maximum


@pytest.mark.parametrize("cost", [0.00009, "NaN", "Infinity", "-1", "0"])
def test_money_is_exact_finite_conservative_decimal_string(setup, cost):
    book, store = setup
    run, _, _ = make_run(store)
    item = {**record(), "cost_reserved_usd": cost}
    with pytest.raises(budget.ReferenceBlocked):
        book.reserve_model(run, item)
    assert not book.ledger.path.exists()


def test_science_reserves_stable_identity_and_preserves_known_zero_launch(setup):
    book, store = setup
    run, step, geometry = make_run(store, science=True)
    ticket = book.reserve_science(run, step, geometry.id)
    with pytest.raises(budget.ReferenceBlocked, match="no Run binding"):
        book.reserve_science(run, step, geometry.id)
    attempt = store.reserve_attempt(run, step, geometry.id)
    store.finish_attempt(run, attempt.id, state="not_started", started=False, termination_confirmed=True)
    book.settle_science(run, ticket, attempt)
    entry = book.snapshot()["agent_science"][ticket]
    assert entry["state"] == "known" and entry["orca_starts_actual"] == 0
    assert entry["orca_starts_reserved"] == 1
    before = book.ledger.path.read_bytes()
    book.settle_science(run, ticket, attempt)
    assert book.ledger.path.read_bytes() == before


def test_unknown_science_occupies_quota_until_same_attempt_is_reconciled(setup):
    book, store = setup
    run, step, geometry = make_run(store, science=True)
    ticket = book.reserve_science(run, step, geometry.id)
    attempt = store.reserve_attempt(run, step, geometry.id)
    attempt.state = "unknown"
    store.save_run(run)
    book.settle_science(run, ticket, attempt)
    assert book.snapshot()["agent_science"][ticket]["execution_uncertain"]
    store.finish_attempt(run, attempt.id, state="failed", started=True, termination_confirmed=True)
    book.settle_science(run, ticket, attempt)
    entry = book.snapshot()["agent_science"][ticket]
    assert entry["state"] == "known" and entry["orca_starts_actual"] == 1
    assert len(entry["settlements"]) == 2


def test_science_reconciliation_recovers_after_attempt_settlement_and_is_idempotent(setup):
    book, store = setup
    run, step, geometry = make_run(store, science=True)
    ticket = book.reserve_science(run, step, geometry.id)
    attempt = store.reserve_attempt(run, step, geometry.id)
    store.finish_attempt(run, attempt.id, state="failed", started=True, termination_confirmed=True)
    assert book.snapshot()["agent_science"][ticket]["state"] == "reserved"
    assert book.reconcile_science(run) == {ticket: "known"}
    before = book.ledger.path.read_bytes()
    assert book.reconcile_science(run) == {ticket: "known"}
    assert book.ledger.path.read_bytes() == before
    entry = book.snapshot()["agent_science"][ticket]
    assert entry["orca_starts_reserved"] == entry["orca_starts_actual"] == 1
    assert len(entry["settlements"]) == 1


def test_science_reconciliation_keeps_unfinished_unknown_then_refreshes_durable_attempt(setup):
    book, store = setup
    run, step, geometry = make_run(store, science=True)
    ticket = book.reserve_science(run, step, geometry.id)
    attempt = store.reserve_attempt(run, step, geometry.id)
    assert book.reconcile_science(run) == {ticket: "unknown"}
    before = book.ledger.path.read_bytes()
    assert book.reconcile_science(run) == {ticket: "unknown"}
    assert book.ledger.path.read_bytes() == before
    store.finish_attempt(run, attempt.id, state="failed", started=True, termination_confirmed=True)
    assert book.reconcile_science(run) == {ticket: "known"}
    entry = book.snapshot()["agent_science"][ticket]
    assert entry["orca_starts_reserved"] == entry["orca_starts_actual"] == 1
    assert len(entry["settlements"]) == 2


def test_science_reconciliation_rejects_orphan_without_resetting_or_inventing_attempt(setup):
    book, store = setup
    run, step, geometry = make_run(store, science=True)
    ticket = book.reserve_science(run, step, geometry.id)
    before = book.ledger.path.read_bytes()
    for _ in range(2):
        with pytest.raises(budget.ReferenceBlocked, match="no unique Attempt"):
            book.reconcile_science(run)
        assert book.ledger.path.read_bytes() == before
        assert not store.load_run(run.id).attempts
    assert book.snapshot()["agent_science"][ticket]["state"] == "reserved"


@pytest.mark.parametrize("field,value", [("input_fingerprint", "b" * 64), ("step_id", "changed")])
def test_science_reconciliation_rejects_changed_attempt_identity(setup, field, value):
    book, store = setup
    run, step, geometry = make_run(store, science=True)
    book.reserve_science(run, step, geometry.id)
    attempt = store.reserve_attempt(run, step, geometry.id)
    setattr(attempt, field, value)
    store._write_json(f"runs/{run.id}/run.json", run)
    before = book.ledger.path.read_bytes()
    with pytest.raises(budget.ReferenceBlocked, match="identity/input changed"):
        book.reconcile_science(run)
    assert book.ledger.path.read_bytes() == before


def test_reference_and_agent_helpers_cannot_allocate_separate_development_quotas(setup):
    book, store = setup
    run, step, geometry = make_run(store, "development", science=True)
    ticket = book.reserve_science(run, step, geometry.id)
    attempt = store.reserve_attempt(run, step, geometry.id)
    store.finish_attempt(run, attempt.id, state="not_started", started=False, termination_confirmed=True)
    book.settle_science(run, ticket, attempt)
    ledger = book.snapshot()
    ledger["entries"] = {f"development-{i}": {"category": "development", "fingerprint": f"other-{i}"}
                         for i in range(budget.LIMITS["orca_starts"]["development"] - 1)}
    book._save(ledger)
    with pytest.raises(budget.ReferenceBlocked, match="limit exhausted"):
        book.ledger.reserve("new-old-helper", "development", {
            "input_sha256": "new", "geometry_sha256": "new", "parameters": {}})
    with pytest.raises(budget.ReferenceBlocked, match="limit exhausted"):
        book.reserve_science(run, step, geometry.id)


@pytest.mark.parametrize("category,amount", [("formal", 48), ("development", 48)])
def test_existing_reference_ledger_counts_against_agent_science_subcaps(setup, category, amount):
    book, store = setup
    ledger = book.snapshot()
    ledger["entries"] = {f"old-{i}": {"category": category, "fingerprint": f"other-{i}"} for i in range(amount)}
    book._save(ledger)
    run, step, geometry = make_run(store, category, science=True)
    with pytest.raises(budget.ReferenceBlocked, match="limit exhausted"):
        book.reserve_science(run, step, geometry.id)


def test_delivered_reference_entries_cannot_disappear_even_with_an_existing_ledger(setup):
    book, _ = setup
    ledger = book.snapshot()
    old = {"category": "reference", "fingerprint": "original"}
    ledger["entries"] = {"reference-original": old}
    book._save(ledger)
    budget.reference._save(budget.reference.DELIVERED_SNAPSHOT, {
        "limits": budget.LIMITS, "entries": {"reference-original": old}})
    ledger["entries"] = {}
    book._save(ledger)
    with pytest.raises(budget.ReferenceBlocked, match="reference reservation changed"):
        book.snapshot()
