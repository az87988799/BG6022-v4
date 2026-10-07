"""Shared, fixed acceptance accounting; this helper never sends or executes.

All Stores and Runs use the original B-01 ledger. Immutable reservation and
settlement receipts protect new entries; an interrupted cross-file publication
keeps its occupancy and blocks further reservations until reconciled. This is
acceptance support, not a new production billing or batch domain object.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path

from orca_agent.model_usage import INPUT_USD_PER_MILLION, OUTPUT_USD_PER_MILLION
from orca_agent.models import Attempt, Run, fingerprint, utc_now
from orca_agent.store import controlled_path, sha256_file
from orca_agent.tools.registry import get_tool

_SPEC = importlib.util.spec_from_file_location("phase_b_budget_reference", Path(__file__).with_name("phase_b_reference.py"))
reference = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reference)
LIMITS = reference.ACTIVE_LIMITS
ReferenceBlocked = reference.ReferenceBlocked


def _integer(value, name):
    if type(value) is not int or not 0 <= value <= 1_000_000_000:
        raise ReferenceBlocked(f"invalid {name} accounting value")
    return value


def _money(value):
    if not isinstance(value, str):
        raise ReferenceBlocked("money must be an exact Decimal string")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise ReferenceBlocked("invalid Decimal money") from None
    if not amount.is_finite() or amount < 0:
        raise ReferenceBlocked("invalid Decimal money")
    return amount


def _price(prompt, completion):
    return (prompt * INPUT_USD_PER_MILLION + completion * OUTPUT_USD_PER_MILLION) / Decimal(1_000_000)


class AcceptanceBudget:
    def __init__(self, store):
        self.store = store
        self.ledger = reference.BatchLedger()
        self._run_cache = {}

    @staticmethod
    def _run_entry(run, store):
        if run.batch_category not in {"formal", "development"}:
            raise ReferenceBlocked("acceptance Run requires a frozen formal/development category")
        persisted = store.load_run(run.id)
        if persisted.batch_category != run.batch_category:
            raise ReferenceBlocked("acceptance category differs from the persisted Run")
        return {"run_id": run.id, "store_root": str(store.root.resolve()), "category": run.batch_category}

    def _directory(self, kind, ticket):
        return self.ledger.root / "agent-budget" / kind / reference._identifier(ticket)

    def _load_run(self, entry):
        key = (entry["store_root"], entry["run_id"])
        if key in self._run_cache:
            return self._run_cache[key]
        root = Path(entry["store_root"])
        if not root.is_absolute():
            raise ReferenceBlocked("batch Run root is not absolute")
        path = controlled_path(root, f"runs/{entry['run_id']}/run.json")
        if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
            raise ReferenceBlocked("batch reservation has no readable Run binding")
        run = Run.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if run.id != entry["run_id"] or run.batch_category != entry["category"]:
            raise ReferenceBlocked("batch Run identity/category binding changed")
        self._run_cache[key] = run
        return run

    @staticmethod
    def _model_basis(record):
        names = ("id", "logical_id", "request_hash", "basis", "input_reserved", "output_reserved",
                 "cost_reserved_usd", "prompt_version", "sdk_version", "model", "token_bound_version")
        saved = {name: record.get(name) for name in names}
        # Keep exact legacy receipt shape. New reservations must also bind the
        # selected mode; production request validation proves old missing modes
        # are the original disabled body, without rewriting their receipts.
        if "model_profile" in record:
            if type(record["model_profile"]) is not str or record["model_profile"] not in {"disabled", "thinking_low"}:
                raise ReferenceBlocked("invalid reserved model profile")
            saved["model_profile"] = record["model_profile"]
        return saved

    def _bound(self, kind, entry):
        try:
            run = self._load_run(entry)
        except (ValueError, OSError):
            return False
        if kind == "model_records":
            found = [r for r in run.model_records if r.get("id") == entry["record"]["id"]]
            return bool(len(found) == 1
                        and self._model_basis(found[0]) == self._model_basis(entry["record"])
                        and (entry["state"] != "known" or all(
                            found[0].get(k) == v for k, v in entry["settled_record"].items())))
        found = [a for a in run.attempts if a.logical_id == entry["logical_id"]
                 and a.number == entry["attempt_number"]]
        if entry.get("prelaunch_proof_sha256"):
            if len(found) != 1:
                return False
            proof = controlled_path(Path(entry["store_root"]), f"{found[0].directory}/prelaunch-aborted.json")
            if not proof.is_file() or sha256_file(proof) != entry["prelaunch_proof_sha256"]:
                return False
        return bool(len(found) == 1 and found[0].input_fingerprint == entry["input_fingerprint"]
                    and found[0].geometry_artifact_id == entry["geometry_artifact_id"]
                    and found[0].step_id == entry["step_id"])

    def _validate_receipts(self, ledger, kind):
        entries = ledger.get(kind, {})
        limits = ledger["limits"]  # Validated against its immutable applied approval.
        maximum = limits["model"]["http_requests"] if kind == "model_records" else limits["orca_starts"]["total"]
        if not isinstance(entries, dict) or len(entries) > maximum:
            raise ReferenceBlocked("invalid bounded acceptance entry collection")
        root = self.ledger.root / "agent-budget" / kind
        directories = {p.name for p in root.iterdir() if p.is_dir()} if root.exists() else set()
        if directories != set(entries):
            raise ReferenceBlocked("uncommitted or missing batch reservation; reconcile without resend")
        for ticket, entry in entries.items():
            path = self._directory(kind, ticket) / "reservation.json"
            if not path.is_file() or sha256_file(path) != entry.get("reservation_sha256"):
                raise ReferenceBlocked("batch reservation receipt changed")
            reservation = json.loads(path.read_text(encoding="utf-8"))
            mutable = {"reservation_sha256", "settlements", "state", "orca_starts_actual",
                       "attempt_id", "execution_uncertain", "settled_record", "prelaunch_proof_sha256"}
            if {k: v for k, v in entry.items() if k not in mutable} != reservation:
                raise ReferenceBlocked("batch reservation identity/classification changed")
            if entry.get("state") not in {"reserved", "known", "unknown"}:
                raise ReferenceBlocked("invalid batch settlement state")
            receipts = entry.get("settlements", [])
            settlement_root = self._directory(kind, ticket) / "settlements"
            filenames = {p.name for p in settlement_root.glob("*.json")} if settlement_root.exists() else set()
            if len(receipts) > 8 or filenames != {r["sha256"] + ".json" for r in receipts}:
                raise ReferenceBlocked("uncommitted or missing batch settlement; reconcile without resend")
            previous = None
            for item in receipts:
                receipt_path = settlement_root / (item["sha256"] + ".json")
                if sha256_file(receipt_path) != item["sha256"]:
                    raise ReferenceBlocked("batch settlement receipt changed")
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt.get("ticket") != ticket or receipt.get("previous") != previous:
                    raise ReferenceBlocked("batch settlement receipt binding changed")
                previous = item["sha256"]
            if receipts:
                latest = json.loads((settlement_root / (receipts[-1]["sha256"] + ".json")).read_text(encoding="utf-8"))
                if latest["settlement"] != {k: entry[k] for k in latest["settlement"]}:
                    raise ReferenceBlocked("batch settled amounts differ from immutable receipt")
            elif entry["state"] != "reserved":
                raise ReferenceBlocked("settled batch entry has no immutable receipt")
            if entry["state"] != "reserved" and not self._bound(kind, entry):
                raise ReferenceBlocked("settled batch entry lost its Run binding")

    @staticmethod
    def _model_totals(ledger):
        known_tokens = unknown_tokens = 0
        known_money = unknown_money = Decimal("0")
        entries = ledger.get("model_records", {})
        for entry in entries.values():
            record = entry["record"]
            reserved_input = _integer(record["input_reserved"], "input reservation")
            reserved_output = _integer(record["output_reserved"], "output reservation")
            reserved_money = _money(record["cost_reserved_usd"])
            if reserved_money != _price(reserved_input, reserved_output):
                raise ReferenceBlocked("model reservation price differs from the frozen profile")
            if entry["state"] == "known":
                actual = entry["settled_record"]
                input_tokens = _integer(actual["input_tokens"], "known input tokens")
                output_tokens = _integer(actual["output_tokens"], "known output tokens")
                if actual["total_tokens"] != input_tokens + output_tokens:
                    raise ReferenceBlocked("known token accounting is inconsistent")
                cost = _money(actual["cost_known_usd"])
                if cost != _price(input_tokens, output_tokens):
                    raise ReferenceBlocked("known price differs from the frozen profile")
                known_tokens += input_tokens + output_tokens
                known_money += cost
            else:
                unknown_tokens += reserved_input + reserved_output
                unknown_money += reserved_money
        return {"http_requests": len(entries), "tokens": known_tokens + unknown_tokens,
                "usd": str(known_money + unknown_money), "known_tokens": known_tokens,
                "unknown_tokens": unknown_tokens, "known_usd": str(known_money),
                "unknown_usd": str(unknown_money)}

    def snapshot(self):
        """Verify immutable evidence and return a read-only accounting snapshot."""
        # A writer publishes its immutable receipt before the ledger reference.
        # Hold the same short lock across both reads so an in-flight publication
        # is not mistaken for an orphan. This creates only the coordination lock,
        # never an accounting record or reservation.
        with self.ledger._lock():
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self, *, allow_legacy_limits=False):
        """Verify the accounting files while the caller holds the batch lock."""
        self._run_cache = {}
        ledger = (self.ledger.snapshot(allow_legacy_limits=True) if allow_legacy_limits
                  else self.ledger.snapshot())
        if reference.DELIVERED_SNAPSHOT.exists():
            delivered = reference._json(reference.DELIVERED_SNAPSHOT)
            for ticket, old in delivered.get("entries", {}).items():
                if ledger.get("entries", {}).get(ticket) != old:
                    raise ReferenceBlocked("delivered reference reservation changed or disappeared")
        for kind in ("model_records", "agent_science"):
            self._validate_receipts(ledger, kind)
        totals = self._model_totals(ledger)
        stored = ledger.get("model_usage", {})
        if (stored.get("http_requests", 0) != totals["http_requests"]
                or stored.get("tokens", 0) != totals["tokens"]
                or _money(str(stored.get("usd", 0))) != Decimal(totals["usd"])
                or ledger.get("model_records") and stored != totals):
            raise ReferenceBlocked("batch model totals differ from immutable reservations")
        return ledger

    def _before_reserve(self, ledger, run_entry):
        for kind in ("model_records", "agent_science"):
            for entry in ledger.get(kind, {}).values():
                if entry["run_id"] == run_entry["run_id"] and (
                    entry["store_root"] != run_entry["store_root"] or entry["category"] != run_entry["category"]
                ):
                    raise ReferenceBlocked("Run identity cannot move root or change budget classification")
                if entry["state"] == "reserved" and not self._bound(kind, entry):
                    raise ReferenceBlocked("unresolved cross-file reservation has no Run binding; reconcile first")

    def _save(self, ledger):
        ledger["model_usage"] = self._model_totals(ledger)
        ledger["model_accounting"] = "durable HTTP reservations; known plus conservative unknown occupancy"
        reference._save(self.ledger.path, ledger)

    def _reserve(self, ledger, kind, ticket, immutable):
        path = self._directory(kind, ticket) / "reservation.json"
        reference._save(path, immutable, immutable=True)
        ledger.setdefault(kind, {})[ticket] = {**immutable, "state": "reserved",
                                               "reservation_sha256": sha256_file(path), "settlements": []}
        self._save(ledger)

    def reserve_model(self, run, record):
        if record.get("status") != "reserved":
            raise ReferenceBlocked("only a fresh HTTP reservation may be admitted")
        ticket = reference._identifier(record["id"])
        owner = self._run_entry(run, self.store)
        # Persist only the fixed non-sensitive accounting fields, never a prompt,
        # key, HTTP headers, raw exception, or model proposal.
        saved = self._model_basis(record)
        with self.ledger._lock():
            ledger = self._snapshot_unlocked()
            self._before_reserve(ledger, owner)
            if ticket in ledger.get("model_records", {}):
                raise ReferenceBlocked("HTTP identity already reserved; reconciliation must never resend")
            proposed = {**owner, "id": ticket, "record": saved, "reserved_at": utc_now().isoformat()}
            trial = {**ledger, "model_records": {**ledger.get("model_records", {}),
                     ticket: {**proposed, "state": "reserved"}}}
            totals = self._model_totals(trial)
            limits = ledger["limits"]
            if (totals["http_requests"] > limits["model"]["http_requests"]
                    or totals["tokens"] > limits["model"]["tokens"]
                    or Decimal(totals["usd"]) > Decimal(str(limits["model"]["usd"]))):
                raise ReferenceBlocked("frozen batch model HTTP/token/USD limit exhausted")
            self._reserve(ledger, "model_records", ticket, proposed)

    def _settle(self, ledger, kind, ticket, settlement):
        entry = ledger[kind][ticket]
        if entry.get("settlements") and all(entry.get(k) == v for k, v in settlement.items()):
            return
        if entry["state"] == "known":
            raise ReferenceBlocked("known batch accounting cannot be changed or refunded")
        if len(entry.get("settlements", [])) >= 8:
            raise ReferenceBlocked("bounded batch reconciliation exhausted")
        previous = entry["settlements"][-1]["sha256"] if entry["settlements"] else None
        value = {"ticket": ticket, "previous": previous, "settlement": settlement}
        # The filename is content-addressed; interrupted publication remains an
        # orphan and cannot be mistaken for a free or fresh send.
        payload = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
        digest = hashlib.sha256(payload).hexdigest()
        path = self._directory(kind, ticket) / "settlements" / (digest + ".json")
        reference._save(path, value, immutable=True)
        entry.update(settlement)
        entry["settlements"].append({"sha256": digest})
        self._save(ledger)

    def settle_model(self, run, record):
        owner = self._run_entry(run, self.store)
        ticket = reference._identifier(record["id"])
        if record.get("status") not in {"known", "unknown"}:
            raise ReferenceBlocked("HTTP settlement requires known or unknown usage")
        with self.ledger._lock():
            ledger = self._snapshot_unlocked()
            entry = ledger.get("model_records", {}).get(ticket)
            if not entry or any(entry[k] != v for k, v in owner.items()) or self._model_basis(record) != entry["record"]:
                raise ReferenceBlocked("HTTP settlement differs from its frozen reservation")
            persisted = self.store.load_run(run.id)
            matches = [r for r in persisted.model_records if r.get("id") == ticket]
            if len(matches) != 1 or matches[0] != record:
                raise ReferenceBlocked("HTTP settlement lacks the exact durable Run record")
            receipt_hash = record.get("response_record_sha256")
            if receipt_hash:
                path = self.store.path(f"runs/{run.id}/model/{ticket}.response.json")
                if not path.is_file() or sha256_file(path) != receipt_hash:
                    raise ReferenceBlocked("model settlement response receipt changed or missing")
            elif record["status"] == "known":
                raise ReferenceBlocked("known model usage needs a verified response receipt")
            fields = ("status", "input_tokens", "output_tokens", "total_tokens", "cost_known_usd",
                      "response_record_sha256", "error_category")
            settled = {k: record[k] for k in fields if k in record}
            trial = {**ledger, "model_records": {**ledger["model_records"], ticket: {
                **entry, "state": record["status"], "settled_record": settled}}}
            self._model_totals(trial)  # Validate arithmetic before publishing a receipt.
            self._settle(ledger, "model_records", ticket, {"state": record["status"], "settled_record": settled})

    def reserve_science(self, run, step, geometry_id, *, draft_attempt=None):
        owner = self._run_entry(run, self.store)
        if "execute_orca" not in get_tool(step.tool).effects:
            raise ReferenceBlocked("only actual ORCA steps consume the scientific batch quota")
        self.store.artifact_path(geometry_id)
        geometry = self.store.load_artifact(geometry_id)
        number = run.usage.logical_attempts.get(step.logical_id, 0) + 1
        ticket = "science_" + fingerprint([run.id, step.logical_id, number])[:48]
        input_hash = fingerprint({"tool": step.tool, "parameters": step.parameters.model_dump(),
                                  "geometry_hash": geometry.sha256})
        immutable = {**owner, "id": ticket, "step_id": step.id, "logical_id": step.logical_id,
                     "attempt_number": number, "geometry_artifact_id": geometry_id,
                     "geometry_sha256": geometry.sha256, "input_fingerprint": input_hash,
                     "request_version": run.request_version, "plan_version": run.plan_version,
                     "permission_version": run.permission.version, "orca_starts_reserved": 1,
                     "reserved_at": utc_now().isoformat()}
        if draft_attempt is not None:
            if (draft_attempt.number != number or draft_attempt.step_id != step.id
                    or draft_attempt.input_fingerprint != input_hash or draft_attempt.frozen_step != step
                    or draft_attempt.geometry_artifact_id != geometry_id):
                raise ReferenceBlocked("prelaunch Attempt draft differs from reservation")
            immutable.update(reservation_phase="before_persisted_attempt",
                prelaunch_attempt=draft_attempt.model_dump(mode="json"),
                run_before={"orca_starts_reserved": run.usage.orca_starts_reserved,
                            "extra_orca_starts_reserved": run.usage.extra_orca_starts_reserved,
                            "fingerprint_attempts": run.usage.fingerprint_attempts.get(input_hash, 0),
                            "attempt_ids": [a.id for a in run.attempts]})
        with self.ledger._lock():
            ledger = self._snapshot_unlocked()
            self._before_reserve(ledger, owner)
            if ticket in ledger.get("agent_science", {}):
                raise ReferenceBlocked("scientific identity already reserved; reconcile without relaunch")
            entries = [*ledger["entries"].values(), *ledger.get("agent_science", {}).values()]
            limits = ledger["limits"]
            if (len(entries) >= limits["orca_starts"]["total"]
                    or sum(e["category"] == owner["category"] for e in entries)
                    >= limits["orca_starts"][owner["category"]]):
                raise ReferenceBlocked("frozen batch ORCA reservation limit exhausted")
            self._reserve(ledger, "agent_science", ticket, immutable)
        return ticket

    def settle_science(self, run, ticket, attempt):
        owner = self._run_entry(run, self.store)
        with self.ledger._lock():
            ledger = self._snapshot_unlocked()
            entry = ledger.get("agent_science", {}).get(reference._identifier(ticket))
            if not entry or any(entry[k] != v for k, v in owner.items()):
                raise ReferenceBlocked("scientific settlement differs from its reservation owner")
            actual = next((a for a in self.store.load_run(run.id).attempts if a.id == attempt.id), None)
            if actual is None or actual.model_dump(mode="json") != attempt.model_dump(mode="json"):
                raise ReferenceBlocked("scientific settlement has no exact durable Attempt")
            expected = {"step_id": attempt.step_id, "logical_id": attempt.logical_id,
                        "attempt_number": attempt.number, "geometry_artifact_id": attempt.geometry_artifact_id,
                        "input_fingerprint": attempt.input_fingerprint}
            if any(entry[key] != value for key, value in expected.items()):
                raise ReferenceBlocked("scientific Attempt identity/input differs from reservation")
            unknown = attempt.finished_at is None or attempt.state in {"intent", "running", "unknown"}
            settlement = {"state": "unknown" if unknown else "known", "attempt_id": attempt.id,
                          "orca_starts_actual": int(attempt.started), "execution_uncertain": unknown}
            proof = self.store.path(f"{attempt.directory}/prelaunch-aborted.json")
            if entry.get("reservation_phase") == "before_persisted_attempt" and proof.exists():
                settlement["prelaunch_proof_sha256"] = sha256_file(proof)
            self._settle(ledger, "agent_science", ticket, settlement)

    def reconcile_science(self, run):
        """Settle existing batch reservations from durable Attempts, never relaunch.

        Legacy orphans stay unknown. New pre-publication reservations retain an
        exact Attempt draft; Store can prove/record not_started only while every
        persisted source still lies before execution preparation. No quota refunds.
        """
        owner = self._run_entry(run, self.store)
        persisted = self.store.load_run(run.id)
        pending, prelaunch = [], []
        with self.ledger._lock():
            ledger = self._snapshot_unlocked()
            for ticket, entry in ledger.get("agent_science", {}).items():
                if entry["run_id"] != run.id:
                    continue
                if any(entry[key] != value for key, value in owner.items()):
                    raise ReferenceBlocked("scientific reconciliation owner/category changed")
                attempts = [a for a in persisted.attempts if a.logical_id == entry["logical_id"]
                            and a.number == entry["attempt_number"]]
                if (entry.get("reservation_phase") == "before_persisted_attempt"
                        and (not attempts or len(attempts) == 1 and attempts[0].state == "intent"
                             and not attempts[0].execution_handle)):
                    prelaunch.append((ticket, entry))
                    continue
                if len(attempts) != 1:
                    raise ReferenceBlocked("scientific reservation has no unique Attempt; reconcile before execution")
                attempt = attempts[0]
                if (attempt.step_id != entry["step_id"]
                        or attempt.input_fingerprint != entry["input_fingerprint"]
                        or attempt.geometry_artifact_id != entry["geometry_artifact_id"]):
                    raise ReferenceBlocked("scientific reconciliation Attempt identity/input changed")
                pending.append((ticket, attempt))
        # Never acquire the environment lock while holding the batch lock:
        # reserve_attempt uses coordinator -> environment -> batch ordering.
        for ticket, entry in prelaunch:
            attempt = self.store.recover_unstarted_reservation(run,
                Attempt.model_validate(entry["prelaunch_attempt"]), before=entry["run_before"],
                reservation_sha256=entry["reservation_sha256"])
            pending.append((ticket, attempt))
        for ticket, attempt in pending:
            self.settle_science(run, ticket, attempt)
        return {ticket: "unknown" if a.finished_at is None or a.state in {"intent", "running", "unknown"}
                else "known" for ticket, a in pending}
