"""Drive one explicitly authorized natural-language development run; no solver content."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from filelock import FileLock

from orca_agent.config import load_config
from orca_agent.entrypoints import create_text, execute
from orca_agent.models import utc_now
from orca_agent.report import build_report, render_report
from orca_agent.store import Store, atomic_write

SEEDED_MARKER = "CODE_TOOLS_DEVELOPMENT_FAULT_V1"
SEEDED_DECLARATION = "DEVELOPMENT_FAULT_SEEDED: " + SEEDED_MARKER
DEVELOPMENT_LIMITS_V1 = {"runs": 5, "model_calls": 120, "model_tokens": 6000000,
                         "orca_starts": 10, "internal_work_units": 2000}
DEVELOPMENT_LIMITS_V2 = {"runs": 8, "model_calls": 192, "model_tokens": 9600000,
                         "orca_starts": 16, "internal_work_units": 3200}
DEVELOPMENT_MEANING = "worst-case development reservations; failures/unknowns never reclaim a slot"


def validate_seeded_request(text):
    """Require an explicit test declaration; never alter the original request."""
    if SEEDED_DECLARATION not in {line.strip() for line in text.splitlines()}:
        raise ValueError("seeded-code-error requires the explicit declaration in the original test request")


def inspect_seeded_code(source):
    """Compile/inspect only: the first executable statement must raise NameError."""
    tree = ast.parse(source, filename="model-generated-task.py")
    compile(tree, "model-generated-task.py", "exec", dont_inherit=True)
    body = list(tree.body)
    if (body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body.pop(0)
    while body and isinstance(body[0], ast.ImportFrom) and body[0].module == "__future__":
        if any((alias.asname or alias.name) == SEEDED_MARKER for alias in body[0].names):
            raise ValueError("future import must not bind the seeded marker")
        body.pop(0)
    if (not body or not isinstance(body[0], ast.Expr)
            or not isinstance(body[0].value, ast.Name) or body[0].value.id != SEEDED_MARKER):
        raise ValueError("first executable statement must be the standalone undefined seeded marker")
    return {"marker": SEEDED_MARKER, "line": body[0].lineno,
            "ast_kind": "Expr(Name(Load))", "compiled_without_execution": True}


class SeededCodeErrorGate:
    """Test-only observer; it never changes a proposal, Artifact, Plan or budget."""

    def __init__(self, store, run_id, evidence):
        self.store, self.run_id, self.evidence = store, run_id, evidence
        self.receipt = None

    def __call__(self, point):
        if point != "before_science_reservation":
            return
        if self.receipt is not None:
            if self.receipt["status"] != "prelaunch_verified":
                raise RuntimeError("seeded-code-error not_established; gate remains closed")
            return
        receipt = {"version": 1, "scope": "development_fault_seeded", "run_id": self.run_id,
                   "marker": SEEDED_MARKER, "point": point, "status": "not_established",
                   "checked_at": utc_now().isoformat(), "mutated_task_content": False}
        try:
            from orca_agent.models import fingerprint
            from orca_agent.orca.native import read_pinned_input

            run = self.store.load_run(self.run_id)
            validate_seeded_request(self.store.load_request(run).original_text)
            if (run.attempts or run.internal_calls or run.usage.orca_starts_reserved
                    or run.usage.orca_starts_actual or run.usage.internal_work_units):
                raise ValueError("seeded first execution requires zero prior Attempts and native reservations")
            plan = self.store.load_plan(run)
            decision = run.decisions[-1] if run.decisions else {}
            if (decision.get("action") != "call_tool"
                    or set(decision.get("parameters", {})) != {"step_id"}):
                raise ValueError("seeded gate requires the current model-selected execution Step")
            steps = [step for step in plan.steps if step.id == decision["parameters"]["step_id"]] if plan else []
            if len(steps) != 1 or steps[0].tool != "python.execute":
                raise ValueError("seeded first execution must be a Python Step")
            step = steps[0]
            code = step.parameters.code
            artifact = self.store.load_artifact(code.artifact_id)
            if (code.artifact_id not in self.store.verified_own_artifacts(run)
                    or artifact.run_id != run.id or artifact.source.get("role") != "python"
                    or not artifact.source.get("call_id") or artifact.sha256 != code.sha256):
                raise ValueError("seeded code must be the exact verified workspace.write Artifact of this Run")
            data = read_pinned_input(self.store.artifact_path(code.artifact_id), code.sha256, limit=65536)
            syntax = inspect_seeded_code(data.decode("utf-8"))
            receipt.update(status="prelaunch_verified", syntax=syntax,
                request_version=run.request_version, plan_version=run.plan_version,
                plan_sha256=fingerprint(plan), step_id=step.id, logical_id=step.logical_id,
                code=code.model_dump(mode="json"), code_bytes=len(data),
                source_call_id=artifact.source["call_id"], decision_id=decision["id"],
                usage_before=run.usage.model_dump(mode="json"),
                limitations=["Explicitly prompted generic defect; not a spontaneous model error.",
                    "Prelaunch verification only; actual failure, feedback and model repair require separate audit."])
        except Exception as exc:
            receipt["reason"] = str(exc)[:1000]
            self._save(receipt)
            raise RuntimeError("seeded-code-error not_established: " + receipt["reason"]) from exc
        self._save(receipt)

    def _save(self, receipt):
        atomic_write(self.evidence / "seeded-code-error-prelaunch.json",
                     (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), immutable=True)
        self.receipt = receipt

    def outcome(self):
        return self.receipt or {"scope": "development_fault_seeded", "status": "not_established",
            "reason": "before_science_reservation was not reached", "marker": SEEDED_MARKER}


def snapshot(root):
    paths = sorted((root / "src").rglob("*.py"))
    paths += [root / "uv.lock", root / "pyproject.toml"]
    return {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _canonical_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False).encode("utf-8")).hexdigest()


def _valid_timestamp(value):
    try:
        return isinstance(value, str) and datetime.fromisoformat(value).utcoffset() is not None
    except ValueError:
        return False


def _validate_budget(budget, limits):
    if (not isinstance(budget, dict) or set(budget) != set(limits) - {"runs"}
            or any(type(value) is not int or not 0 <= value <= limits[key] for key, value in budget.items())):
        raise RuntimeError("invalid development reservation budget")


def _same_limits(actual, expected):
    return (isinstance(actual, dict) and actual == expected
            and all(type(value) is int for value in actual.values()))


def _validate_development_ledger(ledger):
    if not isinstance(ledger, dict) or type(ledger.get("version")) is not int or ledger["version"] not in (1, 2):
        raise RuntimeError("invalid development ledger version")
    version = ledger["version"]
    fields = {"version", "limits", "reservations", "separate_native_probe_starts", "meaning"}
    if version == 2:
        fields.add("revisions")
    limits = DEVELOPMENT_LIMITS_V1 if version == 1 else DEVELOPMENT_LIMITS_V2
    if (set(ledger) != fields or not _same_limits(ledger["limits"], limits)
            or type(ledger["separate_native_probe_starts"]) is not int or ledger["separate_native_probe_starts"] != 1
            or ledger["meaning"] != DEVELOPMENT_MEANING):
        raise RuntimeError("development ledger fixed limits or metadata differ")
    rows = ledger["reservations"]
    if not isinstance(rows, list) or len(rows) > limits["runs"]:
        raise RuntimeError("invalid development reservation count")
    names, totals = set(), dict.fromkeys(set(limits) - {"runs"}, 0)
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"evidence", "budget", "reserved_at"}:
            raise RuntimeError("invalid development reservation record")
        name = row["evidence"]
        if (not isinstance(name, str) or not name.strip() or name in {".", ".."}
                or any(char in name for char in "/\\:\x00") or name.casefold() in names
                or not _valid_timestamp(row["reserved_at"])):
            raise RuntimeError("invalid or duplicate development reservation identity")
        names.add(name.casefold())
        _validate_budget(row["budget"], limits)
        for key, value in row["budget"].items():
            totals[key] += value
            if totals[key] > limits[key]:
                raise RuntimeError("development batch reservation exhausted: " + key)
    if version == 2:
        revisions = ledger["revisions"]
        if not isinstance(revisions, list) or len(revisions) != 1 or not isinstance(revisions[0], dict):
            raise RuntimeError("development extension requires exactly one revision")
        revision = revisions[0]
        if (set(revision) != {"action", "revised_at", "reason", "old_limits", "new_limits",
                "prior_ledger_sha256", "original_reservations_count", "original_reservations_sha256"}
                or revision["action"] != "extend_development_budget_once"
                or not _same_limits(revision["old_limits"], DEVELOPMENT_LIMITS_V1)
                or not _same_limits(revision["new_limits"], limits)
                or type(revision["original_reservations_count"]) is not int
                or revision["original_reservations_count"] != 5 or len(rows) < 5
                or not isinstance(revision["reason"], str) or not 1 <= len(revision["reason"].strip()) <= 2000
                or not _valid_timestamp(revision["revised_at"])
                or not isinstance(revision["prior_ledger_sha256"], str)
                or len(revision["prior_ledger_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in revision["prior_ledger_sha256"])
                or revision["original_reservations_sha256"] != _canonical_hash(rows[:5])):
            raise RuntimeError("invalid development budget revision or altered original reservations")
        # The original prefix must still be a legal, fully reserved v1 batch.
        original = {key: value for key, value in ledger.items() if key != "revisions"}
        original.update(version=1, limits=DEVELOPMENT_LIMITS_V1, reservations=rows[:5])
        _validate_development_ledger(original)
    return limits


def _read_development_ledger(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise RuntimeError("duplicate development ledger JSON key: " + key)
            result[key] = value
        return result
    raw = path.read_bytes()
    if len(raw) > 1048576:
        raise RuntimeError("development ledger exceeds size limit")
    ledger = json.loads(raw, object_pairs_hook=unique_object)
    _validate_development_ledger(ledger)
    return raw, ledger


def extend_development_budget_once(path, reason):
    """Explicit development-only revision; never called by automatic reservation."""
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise RuntimeError("development budget revision requires a concise explicit reason")
    with FileLock(str(path.with_suffix(".lock"))):
        raw, ledger = _read_development_ledger(path)  # Missing or invalid history is never replaced.
        if ledger["version"] != 1 or len(ledger["reservations"]) != 5:
            raise RuntimeError("extension requires exactly five v1 reservations and cannot be repeated")
        ledger.update(version=2, limits=dict(DEVELOPMENT_LIMITS_V2), revisions=[{
            "action": "extend_development_budget_once", "revised_at": utc_now().isoformat(),
            "reason": reason.strip(), "old_limits": dict(DEVELOPMENT_LIMITS_V1),
            "new_limits": dict(DEVELOPMENT_LIMITS_V2), "prior_ledger_sha256": hashlib.sha256(raw).hexdigest(),
            "original_reservations_count": 5,
            "original_reservations_sha256": _canonical_hash(ledger["reservations"])}])
        _validate_development_ledger(ledger)
        atomic_write(path, (json.dumps(ledger, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return ledger["revisions"][0]


def reserve_development(evidence, config):
    """Reserve worst-case Run envelopes once; new IDs cannot reset this test batch."""
    path = evidence.parent / "development-ledger.json"
    with FileLock(str(path.with_suffix(".lock"))):
        ledger = _read_development_ledger(path)[1] if path.exists() else {
            "version": 1, "limits": dict(DEVELOPMENT_LIMITS_V1), "reservations": [],
            "separate_native_probe_starts": 1,
            "meaning": DEVELOPMENT_MEANING}
        limits = _validate_development_ledger(ledger)
        if len(ledger["reservations"]) >= limits["runs"]:
            raise RuntimeError("development batch is exhausted; automatic reservation cannot extend it")
        budget = {key: getattr(config.text.budget, key) for key in limits if key != "runs"}
        _validate_budget(budget, limits)
        for key, value in budget.items():
            if sum(row["budget"][key] for row in ledger["reservations"]) + value > limits[key]:
                raise RuntimeError("development batch reservation exhausted: " + key)
        ledger["reservations"].append({"evidence": evidence.name, "budget": budget,
                                       "reserved_at": utc_now().isoformat()})
        _validate_development_ledger(ledger)
        atomic_write(path, (json.dumps(ledger, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["extend-development-budget-once"]:
        parser = argparse.ArgumentParser(description="Explicitly revise only the existing development batch ledger.")
        parser.add_argument("--ledger", required=True, type=Path)
        parser.add_argument("--reason", required=True)
        args = parser.parse_args(argv[1:])
        revision = extend_development_budget_once(args.ledger.resolve(), args.reason)
        print(json.dumps({"version": 2, "revision": revision, "run_created": False}), flush=True)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--text-file", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--live-model", action="store_true")
    parser.add_argument("--live-orca", action="store_true")
    parser.add_argument("--seeded-code-error", action="store_true",
        help="test-only: verify an explicitly requested first-code NameError; never modify model code")
    args = parser.parse_args(argv)
    if not args.live_model or not args.live_orca:
        parser.error("both explicit live switches are required")
    text = args.text_file.read_text(encoding="utf-8").strip()
    if args.seeded_code_error:
        try:
            validate_seeded_request(text)
        except ValueError as exc:
            parser.error(str(exc))
    root = Path(__file__).resolve().parents[1]
    evidence = args.evidence.resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    config = load_config(args.config)
    reserve_development(evidence, config)
    frozen = {"created_at": utc_now().isoformat(), "source_files": snapshot(root),
        "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip(),
        "configuration": config.model_dump(mode="json"), "text": text,
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "scope": "development_fault_seeded" if args.seeded_code_error else
                 "one natural-language development run; no injected Plan/input/script"}
    write(evidence / "freeze.json", frozen)
    store = Store(config.data_root, configuration=config.run_configuration())
    run, _ = create_text(store, config, text)
    write(evidence / "run-location.json", {"run_id": run.id, "data_root": str(store.root),
        "environment_root": str(store.environment_root)})
    print(json.dumps({"run_id": run.id, "evidence": str(evidence)}), flush=True)
    gate = SeededCodeErrorGate(store, run.id, evidence) if args.seeded_code_error else None
    error = None
    try:
        run = execute(store, config, run.id, **({"fault": gate} if gate else {}))
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        run = store.load_run(run.id)
    final = snapshot(root)
    changed = sorted(key for key in set(final) | set(frozen["source_files"])
                     if final.get(key) != frozen["source_files"].get(key))
    report = build_report(store, run)
    write(evidence / "report.json", report)
    (evidence / "report.txt").write_text(render_report(report), encoding="utf-8")
    write(evidence / "outcome.json", {"run_id": run.id, "state": run.state,
        "delivery_status": run.delivery_status, "goals": run.goal_status,
        "usage": run.usage.model_dump(mode="json"), "error": error,
        **({"seeded_code_error": gate.outcome()} if gate else {}),
        "source_changed_during_run": changed, "finished_at": utc_now().isoformat()})
    print(json.dumps({"state": run.state, "goals": run.goal_status,
                      "error": error, "changed_sources": changed}), flush=True)
    return 0 if (run.state == "completed" and not error and not changed
                 and (gate is None or gate.outcome()["status"] == "prelaunch_verified")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
