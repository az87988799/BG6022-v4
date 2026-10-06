"""Explicit, resumable driver for the frozen 25 x 3 fixed-evidence evaluations.

Without --execute --live-model this CLI never sends a model request. Every real
send uses the original shared acceptance ledger; scientific permission is always
false. Expected assertions, review files, and slot labels never enter the prompt.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
from pathlib import Path

from filelock import FileLock

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.store import Store, StoreError, atomic_write, sha256_file

PROJECT = Path(__file__).resolve().parents[2]
ROOT = PROJECT / "data/phase-b/model-evaluations"
STORE_ROOT = PROJECT / "data/phase-b/reference"


def _module(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cases = _module("phase_b_fixed_model_cases", "phase_b_model_cases.py")
budget = _module("phase_b_fixed_model_budget", "phase_b_budget.py")


def _write(path, data, *, immutable=False):
    content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if immutable and path.exists():
        if path.read_bytes() != content:
            raise StoreError("immutable evaluation record changed")
        return
    atomic_write(path, content, immutable=immutable)


def _read(path):
    if path.stat().st_size > 256 * 1024:
        raise StoreError("evaluation metadata/review exceeds 256 KiB")
    return json.loads(path.read_text(encoding="utf-8"))


def _slot(variant_id, repetition, category, freeze_label):
    cases.variant_spec(variant_id)
    if type(repetition) is not int or repetition not in (1, 2, 3):
        raise ValueError("repetition must be 1, 2, or 3")
    if category not in {"formal", "development"}:
        raise ValueError("unknown evaluation category")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", freeze_label):
        raise ValueError("freeze label must be a path-safe lowercase identifier")
    return ROOT / category / freeze_label / variant_id.replace("/", "__") / str(repetition)


def _formal_anchor(freeze_label, frozen):
    """Bind each batch to exact freeze bytes, retaining every previous batch.

    Slot allocation describes evaluations, not a spending allowance. All batches
    continue using the same AcceptanceBudget ledger under STORE_ROOT.
    """
    active = PROJECT / "docs/acceptance/phase-b/formal-freeze.json"
    digest = sha256_file(active)
    if frozen.get("freeze_sha256") != digest:
        raise StoreError("active formal freeze changed after validation")
    record = _read(active)
    if record.get("freeze_label") != freeze_label or record.get("code_commit") != frozen.get("code_commit"):
        raise StoreError("active formal freeze identity changed after validation")
    archive = ROOT / "formal-freezes"
    identity = {"schema_version": 1, "freeze_label": freeze_label, "freeze_sha256": digest,
                "code_commit": frozen["code_commit"], "spec_sha256": sha256_file(cases.CASES),
                "variant_ids": list(cases.fixed_variant_ids()), "repetitions": [1, 2, 3]}
    anchor = archive / f"{freeze_label}.anchor.json"
    snapshot = archive / f"{freeze_label}.freeze.json"
    if anchor.exists() and _read(anchor) != identity:
        raise StoreError("formal label is already bound to a different immutable freeze")
    for metadata_path in (ROOT / "formal" / freeze_label).glob("*/*/metadata.json"):
        if _read(metadata_path).get("freeze_sha256") != digest:
            raise StoreError("existing formal slot is bound to a different freeze")
    for prior_batch in (ROOT / "formal").glob("*"):
        if prior_batch.is_dir() and prior_batch.name != freeze_label and not (
                archive / f"{prior_batch.name}.anchor.json").is_file():
            raise StoreError("prior formal batch has no preserved freeze anchor")
    for prior in archive.glob("*.anchor.json"):
        saved = _read(prior)
        label = saved.get("freeze_label", "")
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", label) or prior.name != f"{label}.anchor.json":
            raise StoreError("prior formal freeze anchor identity differs")
        original = archive / f"{label}.freeze.json"
        if not original.is_file() or sha256_file(original) != saved.get("freeze_sha256"):
            raise StoreError("prior formal freeze snapshot is missing or changed")
        original_record = _read(original)
        if (original_record.get("freeze_label") != label
                or original_record.get("code_commit") != saved.get("code_commit")):
            raise StoreError("prior formal freeze snapshot identity differs")
    legacy = ROOT / "formal-freeze.json"
    if legacy.exists():
        old = _read(legacy)
        old_label = old.get("freeze_label")
        if old_label == freeze_label:
            if old != {key: identity[key] for key in ("freeze_label", "spec_sha256", "variant_ids", "repetitions")}:
                raise StoreError("legacy formal allocation differs from its immutable identity")
        elif not isinstance(old_label, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", old_label):
            raise StoreError("legacy formal freeze identity is invalid")
        elif not (archive / f"{old_label}.anchor.json").is_file():
            raise StoreError("prior legacy formal freeze snapshot must be preserved before a new batch")
        else:
            archived = _read(archive / f"{old_label}.anchor.json")
            if old != {key: archived[key] for key in (
                    "freeze_label", "spec_sha256", "variant_ids", "repetitions")}:
                raise StoreError("legacy formal anchor differs from its preserved snapshot binding")
    content = active.read_bytes()
    if hashlib.sha256(content).hexdigest() != digest or sha256_file(active) != digest:
        raise StoreError("active formal freeze changed while preparing its snapshot")
    if snapshot.exists():
        if snapshot.read_bytes() != content:
            raise StoreError("immutable formal freeze snapshot changed")
    else:
        atomic_write(snapshot, content, immutable=True)
    _write(anchor, identity, immutable=True)
    return digest


def prepare(variant_id, repetition, *, category="formal", freeze_label="formal-v1"):
    """Reserve one immutable identity before preparing; never replace a used Run."""
    directory = _slot(variant_id, repetition, category, freeze_label)
    frozen = None
    if category == "formal":
        freeze = _module("phase_b_fixed_model_freeze", "phase_b_freeze.py")
        frozen = freeze.validate_freeze(freeze_label)
    ROOT.mkdir(parents=True, exist_ok=True)
    with FileLock(str(ROOT / "slots.lock"), timeout=30):
        if category == "formal":
            frozen_digest = _formal_anchor(freeze_label, frozen)
        ready = directory / "ready.json"
        metadata_path = directory / "metadata.json"
        if ready.exists():
            binding = _read(ready)
            if sha256_file(metadata_path) != binding["metadata_sha256"]:
                raise StoreError("frozen evaluation metadata changed")
            metadata = _read(metadata_path)
            if (metadata["variant_id"] != variant_id or metadata["repetition"] != repetition
                    or metadata["category"] != category or metadata["freeze_label"] != freeze_label):
                raise StoreError("evaluation slot identity differs from its frozen metadata")
            if category == "formal" and metadata.get("freeze_sha256") != frozen_digest:
                raise StoreError("evaluation slot differs from the active formal freeze")
            store = Store(STORE_ROOT)
            run = store.load_run(binding["run_id"])
            if run.id != metadata["run_id"] or run.batch_category != category:
                raise StoreError("evaluation Run identity/category changed")
            return store, run, metadata, directory
        reservation = directory / "reservation.json"
        if reservation.exists():
            raise StoreError("evaluation preparation was interrupted; reconcile its existing Run before retrying")
        _write(reservation, {"variant_id": variant_id, "repetition": repetition, "category": category,
                             "freeze_label": freeze_label, "spec_sha256": sha256_file(cases.CASES)}, immutable=True)
        store = Store(STORE_ROOT)
        run, metadata = cases.create_request(store, variant_id, repetition, category=category,
                                             freeze_label=freeze_label)
        if frozen is not None:
            metadata["formal_freeze"] = frozen
            metadata["code_commit"] = frozen.get("code_commit", frozen.get("commit"))
            metadata["freeze_sha256"] = frozen_digest
        _write(metadata_path, metadata, immutable=True)
        _write(ready, {"run_id": run.id, "metadata_sha256": sha256_file(metadata_path)}, immutable=True)
        return store, run, metadata, directory


def review_template(metadata):
    entry = {"passed": None, "quote": "", "rationale": ""}
    return {"variant_id": metadata["variant_id"], "repetition": metadata["repetition"],
            "run_id": metadata["run_id"], "spec_sha256": metadata["spec_sha256"],
            "all_proposal_facts_passed": None, "semantic_review_passed": None,
            "instructions": "Review actual persisted model text independently; quote exact text. Do not infer a pass from fixture expectations.",
            "behavior": {item["metric"]: dict(entry) for item in metadata["expected"]
                         if item["metric"] in cases.BEHAVIOR_METRICS},
            "explanation": {axis: dict(entry) for axis in cases.EXPLANATION_AXES}}


def _review(directory, metadata, review_path=None):
    path = Path(review_path) if review_path is not None else directory / "review.json"
    if not path.exists():
        return None
    value = _read(path)
    if any(value.get(key) != metadata[key] for key in ("variant_id", "repetition", "run_id", "spec_sha256")):
        raise StoreError("independent review targets a different evaluation")
    return value


def evaluate(variant_id, repetition, *, allow_live=False, resume=False, category="formal",
             freeze_label="formal-v1", review_path=None):
    """Explicit execution gate plus immutable slot reuse; no retries by new Run ID."""
    if not allow_live:
        raise StoreError("real model execution requires the explicit --live-model gate")
    store, run, metadata, directory = prepare(variant_id, repetition, category=category,
                                              freeze_label=freeze_label)
    with FileLock(str(directory / "evaluation.lock"), timeout=30):
        run = store.load_run(run.id)
        if (run.permission.scientific_execution or run.budget.orca_starts or run.budget.extra_orca_starts
                or run.attempts or run.usage.orca_starts_actual or run.usage.postprocess_starts):
            raise StoreError("fixed-evidence evaluation must have zero scientific permission and starts")
        finished = directory / "execution.json"
        if not metadata["fixture_gaps"] and not finished.exists():
            if run.model_records and not resume:
                raise StoreError("existing model trajectory needs explicit --resume; never create another Run")
            batch = budget.AcceptanceBudget(store)
            batch.snapshot()  # Fail before HTTP when accounting has changed/missing evidence.
            config = Config(data_root=store.root, orca_path=None, mpi_path=None)
            if resume and run.state == "waiting_user" and metadata["continuation_messages"]:
                request = store.load_request(run)
                if len(request.messages) < len(metadata["initial_request"]["messages"]) + len(metadata["continuation_messages"]):
                    run = cases.advance_user_turn(store, run, metadata)
            run = agent.execute(store, config, run.id, resume=resume, batch=batch)
            while metadata["continuation_messages"] and run.state == "waiting_user":
                request = store.load_request(run)
                remaining = len(metadata["initial_request"]["messages"]) + len(metadata["continuation_messages"]) - len(request.messages)
                if remaining <= 0:
                    break
                run = cases.advance_user_turn(store, run, metadata)
                run = agent.execute(store, config, run.id, batch=batch)
            state = {"run_id": run.id, "state": run.state, "http_requests": run.usage.model_calls,
                     "model_record_ids": [record["id"] for record in run.model_records]}
            if run.state in {"completed", "failed", "cancelled", "budget_exhausted", "waiting_user"}:
                _write(finished, state, immutable=True)
            else:
                _write(directory / "interrupted.json", state)
        report = cases.evaluate_response(store, run, metadata, review=_review(directory, metadata, review_path))
        _write(directory / "grade.json", report)
        template = directory / "review-template.json"
        if not template.exists():
            _write(template, review_template(metadata), immutable=True)
        return report


def regrade(variant_id, repetition, *, category="formal", freeze_label="formal-v1", review_path=None):
    """Review an existing trajectory without sending any further model request."""
    directory = _slot(variant_id, repetition, category, freeze_label)
    ready, metadata_path = directory / "ready.json", directory / "metadata.json"
    if not ready.is_file() or not metadata_path.is_file():
        raise StoreError("review requires an already prepared evaluation")
    binding, metadata = _read(ready), _read(metadata_path)
    if sha256_file(metadata_path) != binding["metadata_sha256"]:
        raise StoreError("frozen evaluation metadata changed")
    expected = {"variant_id": variant_id, "repetition": repetition, "category": category,
                "freeze_label": freeze_label, "run_id": binding["run_id"]}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise StoreError("review slot identity differs from frozen metadata")
    store = Store(STORE_ROOT)
    run = store.load_run(binding["run_id"])
    if run.batch_category != category:
        raise StoreError("review Run category changed")
    report = cases.evaluate_response(store, run, metadata, review=_review(directory, metadata, review_path))
    _write(directory / "grade.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=cases.fixed_variant_ids())
    parser.add_argument("--repetition", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--category", choices=("formal", "development"), default="formal")
    parser.add_argument("--freeze-label", default="formal-v1")
    parser.add_argument("--prepare", action="store_true", help="prepare immutable input only; no HTTP")
    parser.add_argument("--execute", action="store_true", help="execute one slot, requires --live-model")
    parser.add_argument("--live-model", action="store_true", help="explicitly enable bounded DeepSeek HTTPS")
    parser.add_argument("--resume", action="store_true", help="reconcile and continue the same interrupted Run")
    parser.add_argument("--review", type=Path, help="independent review JSON; regrade without HTTP unless --execute")
    parser.add_argument("--regrade", action="store_true", help="grade an existing slot without any HTTP or new Run")
    args = parser.parse_args(argv)
    if args.variant is None:
        print(json.dumps({"variants": list(cases.fixed_variant_ids()), "repetitions": 3,
                          "http_executed": False}, ensure_ascii=False, indent=2))
        return 0
    options = {"category": args.category, "freeze_label": args.freeze_label}
    if args.execute:
        report = evaluate(args.variant, args.repetition, allow_live=args.live_model, resume=args.resume,
                          review_path=args.review, **options)
    elif args.review or args.regrade:
        report = regrade(args.variant, args.repetition, review_path=args.review, **options)
    elif args.prepare:
        _, run, metadata, directory = prepare(args.variant, args.repetition, **options)
        report = {"run_id": run.id, "metadata_path": str(directory / "metadata.json"),
                  "fixture_gaps": metadata["fixture_gaps"], "http_executed": False}
    else:
        parser.error("select --prepare, --execute --live-model, or --review; no request has been sent")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
