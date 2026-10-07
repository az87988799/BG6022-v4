"""B-01 acceptance-only independent references; never imported by the product.

The CLI is read-only unless --execute is explicit. Scientific execution reuses
the production runner's permission, attempt budget, environment lease and Job
Object. Only input construction is replaced by immutable, prewritten bytes.
Expected values below never come from OPI or a product Result.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
from pathlib import Path
from unittest.mock import patch

from filelock import FileLock

from orca_agent import runner
from orca_agent.config import Config
from orca_agent.models import CalculationParameters, utc_now
from orca_agent.store import Store, atomic_write, sha256_file
from orca_agent.tools.registry import validate_geometry

PROJECT = Path(__file__).resolve().parents[2]
BATCH_ROOT = PROJECT / "data" / "phase-b"
DELIVERED_SNAPSHOT = PROJECT / "docs" / "acceptance" / "phase-b" / "batch-ledger-snapshot.json"
ORIGINAL_LIMITS = {
    "orca_starts": {"reference": 16, "formal": 48, "development": 32, "total": 96},
    "model": {"http_requests": 600, "tokens": 6_000_000, "usd": 10},
}
LIMITS = {
    "orca_starts": {"reference": 16, "formal": 48, "development": 48, "total": 112},
    "model": {"http_requests": 1004, "tokens": 6_500_000, "usd": 10},
}
LIMIT_APPROVAL_ID = "repair-budget-20261007"
LIMIT_APPROVAL = PROJECT / "docs/acceptance/phase-b/budget-approval-20261007.json"
LIMIT_APPROVAL_SHA256 = "ba9b02a45989fb9b84338c27171e54988b9bfacb1aece99881204faeb510ae95"
ACTIVE_LIMITS = {
    "orca_starts": {"reference": 16, "formal": 48, "development": 48, "total": 112},
    "model": {"http_requests": 1050, "tokens": 6_530_000, "usd": 10},
}
SUPPLEMENT_APPROVAL_ID = "repair-budget-supplement-20261007"
SUPPLEMENT_APPROVAL = PROJECT / "docs/acceptance/phase-b/budget-approval-supplement-20261007.json"
SUPPLEMENT_APPROVAL_SHA256 = "1b6b7dd295361920b04e913fe8e4d9d87df3ff58e4bc7844b65bae100da885a3"


class ReferenceBlocked(ValueError):
    """An existing reservation or frozen limit prevents another execution."""


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save(path: Path, value: dict, *, immutable: bool = False) -> None:
    atomic_write(path, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False)
                       + "\n").encode(), immutable=immutable)


def limit_approval() -> dict:
    if not LIMIT_APPROVAL.is_file() or sha256_file(LIMIT_APPROVAL) != LIMIT_APPROVAL_SHA256:
        raise ReferenceBlocked("approved budget record is missing or changed")
    value = _json(LIMIT_APPROVAL)
    if (value.get("approval_id") != LIMIT_APPROVAL_ID or value.get("status") != "user_approved"
            or value.get("previous_limits") != ORIGINAL_LIMITS or value.get("approved_limits") != LIMITS):
        raise ReferenceBlocked("approved budget record differs from the exact authorized limits")
    return value


def initial_limit_authority() -> dict:
    limit_approval()
    return {"approval_id": LIMIT_APPROVAL_ID, "approval_sha256": LIMIT_APPROVAL_SHA256,
            "origin": "initial_empty_ledger"}


def supplement_approval() -> dict:
    limit_approval()
    if not SUPPLEMENT_APPROVAL.is_file() or sha256_file(SUPPLEMENT_APPROVAL) != SUPPLEMENT_APPROVAL_SHA256:
        raise ReferenceBlocked("approved budget supplement is missing or changed")
    value = _json(SUPPLEMENT_APPROVAL)
    if (value.get("approval_id") != SUPPLEMENT_APPROVAL_ID or value.get("status") != "user_approved"
            or value.get("previous_limits") != LIMITS or value.get("approved_limits") != ACTIVE_LIMITS
            or value.get("previous_approval_id") != LIMIT_APPROVAL_ID
            or value.get("previous_approval_sha256") != LIMIT_APPROVAL_SHA256):
        raise ReferenceBlocked("approved budget supplement differs from the exact authorized limits")
    return value


def active_limit_authority() -> dict:
    supplement_approval()
    return {"approval_id": SUPPLEMENT_APPROVAL_ID, "approval_sha256": SUPPLEMENT_APPROVAL_SHA256,
            "origin": "initial_empty_ledger"}


def _identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value):
        raise ValueError("reference id must be a stable, path-safe identifier")
    if value.upper().split(".")[0] in {"CON", "PRN", "AUX", "NUL", *(
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))}:
        raise ValueError("reference id is a reserved Windows device name")
    return value


def reference_input(scf_maxiter: int) -> str:
    """Reviewed SP-only profile; the actual execution copies the supplied file."""
    return ("! RHF STO-3G TightSCF NORI NoAutoStart\n"
            "%pal nprocs 4 end\n%maxcore 192\n%scf\n"
            f"  MaxIter {scf_maxiter}\n  ConvForced 1\nend\n"
            "* xyzfile 0 1 geometry.xyz\n")


def reviewed_sources(geometry: Path, input_path: Path, scf_maxiter: int) -> dict:
    parameters = CalculationParameters(scf_maxiter=scf_maxiter, timeout_seconds=120)
    geometry, input_path = geometry.resolve(strict=True), input_path.resolve(strict=True)
    if geometry.stat().st_size > 65536 or input_path.stat().st_size > 65536:
        raise ValueError("reference source exceeds 64 KiB")
    # Whitespace/case can differ, but arbitrary input, hidden extra blocks and
    # implicit JSON/postprocessing controls cannot enter this acceptance helper.
    if input_path.read_text(encoding="utf-8").upper().split() != reference_input(
            scf_maxiter).upper().split():
        raise ValueError("reference input differs from the frozen SP-only profile")
    atoms = validate_geometry(geometry.read_text(encoding="utf-8"), parameters)
    if [atom[0] for atom in atoms] != ["O", "H", "H"]:
        raise ValueError("B-01 reference candidates require the frozen O,H,H atom mapping")
    return {"input_path": str(input_path), "input_sha256": sha256_file(input_path),
            "geometry_path": str(geometry), "geometry_sha256": sha256_file(geometry),
            "parameters": parameters.model_dump(mode="json"), "atom_mapping": ["O", "H", "H"],
            "coordinate_unit": "angstrom"}


def _fingerprint(sources: dict) -> str:
    # ID, category and source path are deliberately excluded: none may erase
    # unknown cost or permit a second launch of the same frozen bytes.
    data = [sources["input_sha256"], sources["geometry_sha256"]]
    return hashlib.sha256(json.dumps(data).encode()).hexdigest()


class BatchLedger:
    """One fixed acceptance manifest, not a new production domain lifecycle."""

    def __init__(self):
        self.root = BATCH_ROOT
        self.path = self.root / "batch-ledger.json"

    def _guard_missing_ledger(self) -> None:
        if not self.path.exists() and (DELIVERED_SNAPSHOT.exists() or (self.root / "budget-amendments").exists()):
            raise ReferenceBlocked(
                "durable batch ledger is missing but a delivered snapshot exists; "
                "restore the original batch archive before use; budgets cannot restart")

    def snapshot(self, *, allow_legacy_limits: bool = False) -> dict:
        self._guard_missing_ledger()
        if not self.path.exists():
            return {"schema_version": 1, "limits": ACTIVE_LIMITS, "entries": {},
                    "limit_authority": active_limit_authority(),
                    "model_usage": {"http_requests": 0, "tokens": 0, "usd": 0},
                    "model_accounting": "frozen only; transport and charging implemented in B-04"}
        ledger = _json(self.path)
        legacy = allow_legacy_limits and ledger.get("limits") == LIMITS
        if ledger.get("schema_version") != 1 or (ledger.get("limits") != ACTIVE_LIMITS and not legacy):
            raise ReferenceBlocked("batch ledger schema/limits differ; explicit approved migration required")
        if legacy:
            self._validate_first_limit_authority(ledger)
        else:
            self._validate_limit_authority(ledger)
        for reference_id, entry in ledger["entries"].items():
            if entry.get("receipt_sha256") is not None:
                if entry.get("id") != reference_id:
                    raise ReferenceBlocked("receipt reservation identity differs from ledger key")
                self._validated_receipt(entry)
        return ledger

    def _validate_limit_authority(self, ledger: dict) -> None:
        initial = active_limit_authority()
        authority = ledger.get("limit_authority")
        directory = self.root / "budget-amendments" / SUPPLEMENT_APPROVAL_ID
        if authority == initial:
            if ((self.root / "budget-amendments").exists()
                    or (DELIVERED_SNAPSHOT.exists()
                        and _json(DELIVERED_SNAPSHOT).get("limits") != ACTIVE_LIMITS)):
                raise ReferenceBlocked("historical ledger requires its approved migration receipt")
            return
        required = {**initial, "origin": "amendment"}
        if (not isinstance(authority, dict) or set(authority) != {*required, "receipt_sha256"}
                or any(authority.get(k) != v for k, v in required.items())):
            raise ReferenceBlocked("ledger has no exact approved supplement authority")
        path, before_path = directory / "amendment.json", directory / "before.json"
        if (not path.is_file() or sha256_file(path) != authority["receipt_sha256"]
                or not before_path.is_file()):
            raise ReferenceBlocked("budget supplement receipt or previous ledger is missing or changed")
        receipt = _json(path)
        if (receipt.get("approval_id") != SUPPLEMENT_APPROVAL_ID
                or receipt.get("approval_sha256") != SUPPLEMENT_APPROVAL_SHA256
                or receipt.get("previous_limits") != LIMITS or receipt.get("approved_limits") != ACTIVE_LIMITS
                or sha256_file(before_path) != receipt.get("before_sha256")):
            raise ReferenceBlocked("budget supplement binding or previous ledger hash changed")
        before = _json(before_path)
        if (before.get("limits") != LIMITS
                or before.get("limit_authority", {}).get("origin") != "amendment"
                or receipt.get("previous_limit_authority") != before.get("limit_authority")):
            raise ReferenceBlocked("budget supplement baseline lacks the first applied approval")
        self._validate_first_limit_authority(before)
        self._validate_preserved_costs(before, ledger)

    def _validate_first_limit_authority(self, ledger: dict) -> None:
        """Audit the first immutable approval without granting current execution."""
        if ledger.get("limits") != LIMITS:
            raise ReferenceBlocked("first approved limit profile changed")
        initial = initial_limit_authority()
        authority = ledger.get("limit_authority")
        directory = self.root / "budget-amendments" / LIMIT_APPROVAL_ID
        if authority == initial:
            delivered_legacy = (DELIVERED_SNAPSHOT.exists()
                                and _json(DELIVERED_SNAPSHOT).get("limits") == ORIGINAL_LIMITS)
            if directory.exists() or delivered_legacy:
                raise ReferenceBlocked("historical ledger requires its approved migration receipt")
            return
        required = {**initial, "origin": "amendment"}
        if (not isinstance(authority, dict) or set(authority) != {*required, "receipt_sha256"}
                or any(authority.get(k) != v for k, v in required.items())):
            raise ReferenceBlocked("ledger has no exact approved limit authority")
        path, before_path = directory / "amendment.json", directory / "before.json"
        if (not path.is_file() or sha256_file(path) != authority["receipt_sha256"]
                or not before_path.is_file()):
            raise ReferenceBlocked("budget amendment receipt or original ledger is missing or changed")
        receipt = _json(path)
        if (receipt.get("approval_id") != LIMIT_APPROVAL_ID
                or receipt.get("approval_sha256") != LIMIT_APPROVAL_SHA256
                or receipt.get("previous_limits") != ORIGINAL_LIMITS or receipt.get("approved_limits") != LIMITS
                or sha256_file(before_path) != receipt.get("before_sha256")):
            raise ReferenceBlocked("budget amendment binding or original ledger hash changed")
        before = _json(before_path)
        if before.get("limits") != ORIGINAL_LIMITS or "limit_authority" in before:
            raise ReferenceBlocked("budget amendment baseline is not the original ledger")
        self._validate_preserved_costs(before, ledger)

    @staticmethod
    def _validate_preserved_costs(before: dict, ledger: dict) -> None:
        # The baseline remains an append-only audit anchor. Previously settled
        # costs cannot disappear; unknown reservations may settle normally.
        mutable = {"state", "settlements", "orca_starts_actual", "attempt_id", "execution_uncertain",
                   "settled_record", "prelaunch_proof_sha256", "receipt_path", "receipt_sha256"}
        for kind in ("entries", "model_records", "agent_science"):
            current = ledger.get(kind, {})
            for ticket, old in before.get(kind, {}).items():
                new = current.get(ticket)
                allowed = mutable | ({"run_id"} if kind == "entries" and old.get("run_id") is None else set())
                if (new is None or any(new.get(k) != v for k, v in old.items() if k not in allowed)
                        or (old.get("state") in {"known", "finished"} and new != old)
                        or new.get("settlements", [])[:len(old.get("settlements", []))] != old.get("settlements", [])):
                    raise ReferenceBlocked("pre-amendment reservation or cumulative cost changed or disappeared")

    def _lock(self):
        self._guard_missing_ledger()
        self.root.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.root / "batch-ledger.lock"), timeout=10)

    def receipt_path(self, reference_id: str) -> Path:
        return self.root / "receipts" / f"{_identifier(reference_id)}.json"

    def _validated_receipt(self, entry: dict, *, check_evidence: bool = False) -> dict:
        path = self.receipt_path(entry["id"])
        if not path.exists():
            raise ReferenceBlocked("reservation has no receipt; reconcile without automatic resend")
        payload = path.read_bytes()
        committed_hash = entry.get("receipt_sha256")
        if committed_hash is not None:
            if (entry.get("receipt_path") != str(path)
                    or hashlib.sha256(payload).hexdigest() != committed_hash):
                raise ReferenceBlocked("reference receipt changed after publication")
        receipt = json.loads(payload.decode("utf-8"))
        self._validate_receipt_binding(entry, receipt)
        if (committed_hash is not None
                and receipt["execution_uncertain"] != entry["execution_uncertain"]):
            raise ReferenceBlocked("receipt uncertainty differs from the committed reservation")
        if check_evidence:
            for item in receipt.get("evidence_files", []):
                if sha256_file(Path(item["path"])) != item["sha256"]:
                    raise ReferenceBlocked("reference evidence changed after receipt")
        if committed_hash is None:
            # A receipt saved before ledger publication has no committed hash.
            # It may be inspected, but cannot clear unknown cost or grant a new
            # execution under another identity, even if its claimed outcome is known.
            return {**receipt, "execution_uncertain": True, "reference_verified": False,
                    "receipt_publication": "uncommitted_read_only"}
        return receipt

    @staticmethod
    def _validate_receipt_binding(entry: dict, receipt: dict) -> None:
        if (any(field not in receipt for field in
                ("id", "run_id", "fingerprint", "category", "execution_uncertain"))
                or receipt["id"] != entry["id"]
                or receipt["run_id"] != entry["run_id"]
                or receipt["fingerprint"] != entry["fingerprint"]
                or receipt["category"] != entry["category"]
                or type(receipt["execution_uncertain"]) is not bool):
            raise ReferenceBlocked("receipt does not match reservation identity/run binding")

    def read(self, reference_id: str | None = None) -> dict:
        ledger = self.snapshot()
        if reference_id is None:
            return ledger
        entry = ledger["entries"].get(_identifier(reference_id))
        receipt = self.receipt_path(reference_id)
        if receipt.exists() and entry is None:
            raise ReferenceBlocked("receipt has no matching reservation")
        return {"entry": entry, "receipt": self._validated_receipt(entry, check_evidence=True)
                if receipt.exists() else None}

    def reserve(self, reference_id: str, category: str, sources: dict) -> tuple[dict, bool]:
        _identifier(reference_id)
        if category not in ("reference", "development"):
            raise ValueError("this helper only executes reference/development calculations")
        with self._lock():
            ledger = self.snapshot()
            fingerprint = _fingerprint(sources)
            existing = ledger["entries"].get(reference_id)
            if existing is not None:
                if (existing["category"] != category or existing["fingerprint"] != fingerprint
                        or existing["sources"]["parameters"] != sources["parameters"]):
                    raise ReferenceBlocked("stable id cannot change category, input, geometry or parameters")
                return self._validated_receipt(existing, check_evidence=True), False
            for other_id, entry in ledger["entries"].items():
                if entry["fingerprint"] != fingerprint:
                    continue
                other_receipt = self.receipt_path(other_id)
                if (not other_receipt.exists() or entry.get("receipt_sha256") is None
                        or entry.get("execution_uncertain", True)):
                    raise ReferenceBlocked("same input/geometry has an unresolved reservation under another id")
                self._validated_receipt(entry, check_evidence=True)
            # Agent evaluations use the same frozen ledger and cannot create a
            # second quota through a different execution helper.
            agent_entries = list(ledger.get("agent_science", {}).values())
            if any(item.get("state") == "reserved" and not item.get("attempt_id")
                   for item in agent_entries):
                raise ReferenceBlocked("Agent scientific reservation is unresolved; reconcile first")
            entries = [*ledger["entries"].values(), *agent_entries]
            counts = {name: sum(item["category"] == name for item in entries)
                      for name in ("reference", "formal", "development")}
            if (counts[category] >= ACTIVE_LIMITS["orca_starts"][category]
                    or len(entries) >= ACTIVE_LIMITS["orca_starts"]["total"]):
                raise ReferenceBlocked("frozen batch ORCA reservation limit exhausted")
            entry = {"id": reference_id, "category": category, "fingerprint": fingerprint,
                     "sources": sources, "reserved_at": utc_now().isoformat(),
                     "state": "reserved", "run_id": None, "orca_starts_reserved": 1,
                     "orca_starts_actual": None, "execution_uncertain": True}
            ledger["entries"][reference_id] = entry
            _save(self.path, ledger)
            return entry, True

    def bind_run(self, reference_id: str, run_id: str) -> None:
        with self._lock():
            ledger = self.snapshot()
            entry = ledger["entries"][_identifier(reference_id)]
            if entry["state"] != "reserved" or entry["run_id"] is not None:
                raise ReferenceBlocked("run binding is immutable and may occur only once")
            entry.update(run_id=run_id, state="initialized")
            _save(self.path, ledger)

    def finish(self, reference_id: str, receipt: dict) -> None:
        with self._lock():
            ledger = self.snapshot()
            entry = ledger["entries"][_identifier(reference_id)]
            self._validate_receipt_binding(entry, receipt)
            # Receipt first: a crash before ledger publication still allows only
            # read-only replay. Reservations never disappear and never refund.
            _save(self.receipt_path(reference_id), receipt, immutable=True)
            entry.update(state="finished" if not receipt["execution_uncertain"] else "unknown",
                         execution_uncertain=receipt["execution_uncertain"],
                         orca_starts_actual=receipt.get("usage", {}).get("orca_starts_actual"),
                         receipt_path=str(self.receipt_path(reference_id)),
                         receipt_sha256=sha256_file(self.receipt_path(reference_id)))
            _save(self.path, ledger)


def independent_output(path: Path) -> dict:
    """Raw stdout checks, deliberately independent of the production parser."""
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("reference stdout exceeds bounded 16 MiB read")
    text = path.read_bytes().decode("utf-8")

    def line(match):
        # Leading \s* may span blank LF records (including Windows CRCRLF).
        # Locate the actual first token, not the beginning of that whitespace.
        token = re.search(r"\S", match.group(0))
        return text[:match.start() + token.start()].count("\n") + 1

    conditions = (re.search(r"Hartree-Fock type\s+HFTyp\s*\.+\s*RHF", text)
                  and "Your calculation utilizes the basis: STO-3G" in text
                  and re.search(r"Total Charge\s+Charge\s*\.+\s+0\s*$", text, re.M)
                  and re.search(r"Multiplicity\s+Mult\s*\.+\s+1\s*$", text, re.M))
    result = {"path": str(path.resolve()), "sha256": sha256_file(path),
              "status": "unverified", "uses_product_parser": False,
              "line_numbering": "1-based LF-delimited raw UTF-8 records; CR retained",
              "conditions_confirmed": bool(conditions)}
    failures = list(re.finditer(r"SCF NOT CONVERGED AFTER\s+(\d+)\s+CYCLES", text))
    iterations = list(re.finditer(
        r"^\s*(\d+)\s+([-+]?\d+\.\d+)\s+[-+0-9.eE]+\s+[-+0-9.eE]+"
        r"\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s+[-+0-9.eE]+\s*$", text, re.M))
    energies = list(re.finditer(
        r"^\s*FINAL SINGLE POINT ENERGY\s+([-+]?\d+\.\d+(?:[Ee][-+]?\d+)?)\s*$", text, re.M))
    converged = list(re.finditer(r"^\s*\**\s*SCF CONVERGED AFTER\s+\d+\s+CYCLES", text, re.M))
    if conditions and failures and iterations and not converged and not energies:
        result.update(status="scf_not_converged", failure_line=line(failures[-1]),
                      reported_cycles=int(failures[-1][1]),
                      iterations=[{"number": int(m[1]), "line": line(m)} for m in iterations],
                      energy_eh=None)
        return result
    if not conditions or not energies or not converged or failures:
        return result
    last = energies[-1]
    energy = float(last[1])
    if (not math.isfinite(energy) or converged[-1].start() >= last.start()
            or "ORCA TERMINATED NORMALLY" not in text[last.end():]):
        return result
    mantissa, _, exponent = last[1].lower().partition("e")
    quantum = 10.0 ** (int(exponent or "0") - len(mantissa.partition(".")[2]))
    result.update(status="converged", energy_eh=energy, energy_line=line(last),
                  scf_converged_line=line(converged[-1]),
                  printed_quantum_eh=quantum, print_rounding_eh=quantum / 2 + 8 * math.ulp(energy))
    return result


def _frozen_input(sources: dict):
    def prepare(workdir, geometry_path, parameters, tool_name):
        if tool_name != "orca.sp" or parameters.model_dump(mode="json") != sources["parameters"]:
            raise ValueError("reference parameters changed after reservation")
        geometry_bytes = Path(geometry_path).read_bytes()
        input_bytes = Path(sources["input_path"]).read_bytes()
        if (hashlib.sha256(geometry_bytes).hexdigest() != sources["geometry_sha256"]
                or hashlib.sha256(input_bytes).hexdigest() != sources["input_sha256"]):
            raise ValueError("reference source bytes changed after reservation")
        directory = Path(workdir)
        atomic_write(directory / "job.inp", input_bytes, immutable=True)
        atomic_write(directory / "geometry.xyz", geometry_bytes, immutable=True)
        manifest = {"tool": tool_name, "parameters": sources["parameters"],
                    "input_sha256": sources["input_sha256"],
                    "geometry_sha256": sources["geometry_sha256"],
                    "input_file": "job.inp", "geometry_file": "geometry.xyz"}
        _save(directory / "input-manifest.json", manifest, immutable=True)
        return {**manifest, "input_path": str(directory / "job.inp"),
                "geometry_path": str(directory / "geometry.xyz")}
    return prepare


def execute_reference(reference_id: str, category: str, geometry: Path,
                      input_path: Path, scf_maxiter: int) -> dict:
    sources = reviewed_sources(geometry, input_path, scf_maxiter)
    ledger = BatchLedger()
    entry, fresh = ledger.reserve(reference_id, category, sources)
    if not fresh:
        return entry
    receipt = {"id": reference_id, "category": category, "fingerprint": entry["fingerprint"],
               "sources": sources, "run_id": None, "evidence_files": [],
               "execution_uncertain": True, "reference_verified": False,
               "reference_authorship": "prewritten independent input and raw-output reader",
               "human_expert_approval": False,
               "limitations": "same ORCA engine/backend; not cross-engine or experimental accuracy"}
    run, store, stage = None, None, "preparation"
    try:
        spec_dir = BATCH_ROOT / "specs" / reference_id
        spec_dir.mkdir(parents=True, exist_ok=False)
        geometry_bytes = Path(sources["geometry_path"]).read_bytes()
        if hashlib.sha256(geometry_bytes).hexdigest() != sources["geometry_sha256"]:
            raise ValueError("geometry changed after reservation")
        atomic_write(spec_dir / "geometry.xyz", geometry_bytes, immutable=True)
        spec = {"description": f"B-01 independent reference {reference_id}; acceptance only",
                "geometry": "geometry.xyz",
                "steps": [{"name": "reference", "tool": "orca.sp", "parameters": sources["parameters"]}],
                "goals": [{"name": "energy", "step": "reference", "port": "energy"}],
                "budget": {"attempts_per_step": 1, "orca_starts": 1, "extra_orca_starts": 0,
                           "postprocess_starts": 0, "run_seconds": 180}}
        _save(spec_dir / "task.json", spec, immutable=True)
        # No environment_root override: references share the production admission slot.
        store = Store(BATCH_ROOT / "reference")
        config = Config(
            orca_path=Path(os.environ.get("ORCA_AGENT_ORCA", "E:/orca/orca.exe")).resolve(),
            mpi_path=Path(os.environ.get("ORCA_AGENT_MPI", "C:/Program Files/Microsoft MPI/Bin/mpiexec.exe")).resolve(),
            data_root=store.root,
        )
        stage = "initialization"
        run = runner.initialize(store, config, spec_dir / "task.json")
        receipt["run_id"] = run.id
        ledger.bind_run(reference_id, run.id)
        stage = "execution"
        with patch("orca_agent.tools.calculation.prepare_input", _frozen_input(sources)):
            run = runner.execute(store, config, run.id)
        stage = "independent_review"
        if len(run.attempts) != 1:
            raise ValueError("reference must preserve exactly one scientific attempt")
        directory = store.path(run.attempts[0].directory)
        outcome = _json(directory / "execution.json")
        receipt["execution"] = outcome
        receipt["execution_uncertain"] = outcome["state"] == "unknown"
        if (directory / "job.2jsonout").exists() or outcome.get("postprocess_starts_detected", 0):
            raise ValueError("reference unexpectedly invoked forbidden JSON postprocessing")
        independent = independent_output(directory / "stdout.out")
        receipt["independent_output"] = independent
        usage = outcome.get("resource_usage", {})
        resources_passed = (usage.get("cores") == 4 and usage.get("job_commit_limit_bytes") == 1073741824
                            and usage.get("active_processes") == 0
                            and usage.get("peak_job_commit_bytes", math.inf) <= 1073741824)
        receipt["resources_passed"] = resources_passed
        receipt["reference_verified"] = bool(resources_passed and (
            (outcome["state"] == "completed" and independent["status"] == "converged")
            or (outcome["state"] == "failed" and outcome.get("reason") == "nonzero_exit_code"
                and independent["status"] == "scf_not_converged")))
    except Exception as exc:
        # Do not copy arbitrary exception strings/environment content into evidence.
        receipt["exception"] = {"type": type(exc).__name__, "stage": stage}
        if stage in ("preparation", "initialization"):
            receipt["execution_uncertain"] = False
    finally:
        receipt["recorded_at"] = utc_now().isoformat()
        if run is not None and store is not None:
            try:
                latest = store.load_run(run.id)
                receipt["usage"] = latest.usage.model_dump(mode="json")
                receipt["run_state"] = latest.state
                receipt["result_ids"] = latest.result_ids
                environment = store.path(f"runs/{run.id}/environment.json")
                receipt["environment"] = _json(environment)
                paths = [environment, store.path(f"runs/{run.id}/run.json")]
                for attempt in latest.attempts:
                    paths.extend(p for p in store.path(attempt.directory).iterdir() if p.is_file())
                for path in sorted(paths):
                    receipt["evidence_files"].append({"path": str(path), "sha256": sha256_file(path),
                                                      "size_bytes": path.stat().st_size})
            except Exception as exc:
                receipt["collection_exception"] = {"type": type(exc).__name__}
                receipt["execution_uncertain"] = True
                receipt["reference_verified"] = False
        else:
            receipt["usage"] = {"orca_starts_actual": 0 if not receipt["execution_uncertain"] else None}
        ledger.finish(reference_id, receipt)
    return receipt


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="explicitly enable one managed ORCA reference")
    parser.add_argument("--category", choices=("development", "reference"))
    parser.add_argument("--id", dest="reference_id")
    parser.add_argument("--geometry", type=Path)
    parser.add_argument("--input", type=Path, dest="input_path")
    parser.add_argument("--scf-maxiter", type=int)
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps(BatchLedger().read(args.reference_id), indent=2, ensure_ascii=False))
        return 0
    if any(getattr(args, field) is None for field in (
            "category", "reference_id", "geometry", "input_path", "scf_maxiter")):
        parser.error("--execute requires --category --id --geometry --input --scf-maxiter")
    receipt = execute_reference(args.reference_id, args.category, args.geometry,
                                args.input_path, args.scf_maxiter)
    print(json.dumps({"id": receipt["id"], "run_id": receipt["run_id"],
                      "reference_verified": receipt["reference_verified"],
                      "independent_output": receipt.get("independent_output"),
                      "exception": receipt.get("exception"),
                      "receipt": str(BatchLedger().receipt_path(args.reference_id))}, indent=2))
    return 0 if receipt["reference_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
