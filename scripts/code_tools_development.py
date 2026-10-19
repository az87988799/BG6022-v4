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
DEVELOPMENT_LIMITS_V3 = {"runs": 50, "model_calls": 1200, "model_tokens": 60000000,
                         "orca_starts": 100, "internal_work_units": 20000}
DEVELOPMENT_RUN_LIMITS = {"model_calls": 24, "model_tokens": 1200000,
                        "orca_starts": 2, "internal_work_units": 400}
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


def inspect_seeded_execution(store, run_id):
    """Read-only prelaunch proof; mirror selection without executing or repairing."""
    from orca_agent.agent import _ready
    from orca_agent.model_usage import current_basis, read_model_reply
    from orca_agent.models import fingerprint
    from orca_agent.orca.native import read_pinned_input
    from orca_agent.runner import _step_results

    run = store.load_run(run_id)
    validate_seeded_request(store.load_request(run).original_text)
    if (run.attempts or run.internal_calls or run.usage.orca_starts_reserved
            or run.usage.orca_starts_actual or run.usage.internal_work_units):
        raise ValueError("seeded first execution requires zero prior Attempts and native reservations")
    control = store.read_control(run.id)
    if (store.read_signal(run.id)
            or any(message["id"] not in run.processed_messages for message in control["messages"])
            or set(run.result_ids) - set(run.processed_feedback)):
        raise ValueError("seeded selection requires processed feedback and unchanged user control")
    plan = store.load_plan(run)
    ready = _ready(plan, _step_results(store, run))
    basis = current_basis(store, run)
    pending = [item for item in run.decisions if item.get("action") == "call_tool"
               and item["id"] not in run.applied_decisions]
    proof = {}
    if pending:
        decision = pending[0]
        if (decision.get("basis") != basis or set(decision.get("parameters", {})) != {"step_id"}
                or any(call.consumption.get("_decision_id") == decision["id"] for call in run.calls)):
            raise ValueError("seeded gate requires a current unapplied model-selected execution Step")
        step = next((item for item in ready if item.id == decision["parameters"]["step_id"]), None)
        proof["selection_origin"] = "unapplied_call_tool"
    else:
        decision = run.decisions[-1] if run.decisions else {}
        prior_basis = decision.get("basis", {})
        if (plan is None or decision.get("plan_id") != plan.id
                or decision.get("plan_version") != plan.version
                or decision.get("request_version") != run.request_version
                or not decision.get("record_sha256") or decision.get("user_message_ids")
                or set(prior_basis) != set(basis)
                or any(prior_basis[key] != value for key, value in basis.items() if key != "plan_version")
                or prior_basis["plan_version"] != decision.get("prior_plan_version")
                or plan.version != (prior_basis["plan_version"] or 0) + 1):
            raise ValueError("seeded gate requires the current activated model Plan and version basis")
        # load_plan already checks the activated record hash and exact Plan payload.
        records = [item for item in run.model_records if item.get("id") == decision["id"]]
        if (len(records) != 1 or records[0].get("basis") != prior_basis
                or records[0].get("status") != "known" or not records[0].get("response_record_sha256")):
            raise ValueError("seeded activated Plan lacks its settled model source")
        reply, response_hash = read_model_reply(store, run, records[0])
        expected_action = "initial_plan" if prior_basis["plan_version"] is None else "revise_plan"
        if reply.error_category or not reply.proposal or reply.proposal.get("action") != expected_action:
            raise ValueError("seeded activated Plan source is not an accepted model planning response")
        step = ready[0] if ready else None  # Never skip a ready non-Python predecessor.
        proof.update(selection_origin="activated_model_plan_first_ready",
                     source_model_response_sha256=response_hash,
                     source_decision_record_sha256=decision["record_sha256"],
                     source_decision_basis=prior_basis)
    if step is None or step.tool != "python.execute":
        raise ValueError("seeded first execution must be a ready Python Step")
    code = step.parameters.code
    artifact = store.load_artifact(code.artifact_id)
    writers = [call for call in run.calls if call.id == artifact.source.get("call_id")]
    if (code.artifact_id not in store.verified_own_artifacts(run)
            or artifact.run_id != run.id or artifact.source.get("role") != "python"
            or artifact.sha256 != code.sha256 or len(writers) != 1
            or writers[0].tool != "workspace.write" or writers[0].state != "completed"):
        raise ValueError("seeded code must be the exact verified workspace.write Artifact of this Run")
    data = read_pinned_input(store.artifact_path(code.artifact_id), code.sha256, limit=65536)
    syntax = inspect_seeded_code(data.decode("utf-8"))
    return {**proof, "syntax": syntax, "request_version": run.request_version,
        "plan_version": run.plan_version, "plan_sha256": fingerprint(plan),
        "step_id": step.id, "logical_id": step.logical_id, "code": code.model_dump(mode="json"),
        "code_bytes": len(data), "source_call_id": artifact.source["call_id"],
        "decision_id": decision["id"], "usage_before": run.usage.model_dump(mode="json")}


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
            receipt.update(status="prelaunch_verified", **inspect_seeded_execution(self.store, self.run_id),
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
    if (not isinstance(ledger, dict) or type(ledger.get("version")) is not int
            or ledger["version"] not in (1, 2, 3)):
        raise RuntimeError("invalid development ledger version")
    version = ledger["version"]
    fields = {"version", "limits", "reservations", "separate_native_probe_starts", "meaning"}
    if version >= 2:
        fields.add("revisions")
    limits = {1: DEVELOPMENT_LIMITS_V1, 2: DEVELOPMENT_LIMITS_V2, 3: DEVELOPMENT_LIMITS_V3}[version]
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
        if version == 3:
            _validate_budget(row["budget"], DEVELOPMENT_RUN_LIMITS)
        for key, value in row["budget"].items():
            totals[key] += value
            if totals[key] > limits[key]:
                raise RuntimeError("development batch reservation exhausted: " + key)
    if version >= 2:
        revisions = ledger["revisions"]
        if (not isinstance(revisions, list) or len(revisions) != version - 1
                or any(not isinstance(revision, dict) for revision in revisions)):
            raise RuntimeError("development extension revision history does not match ledger version")
        revision = revisions[-1]
        revision_fields = {"action", "revised_at", "reason", "old_limits", "new_limits",
            "prior_ledger_sha256", "original_reservations_count", "original_reservations_sha256"}
        prior_limits = DEVELOPMENT_LIMITS_V1 if version == 2 else DEVELOPMENT_LIMITS_V2
        original_count = prior_limits["runs"]
        action = "extend_development_budget_once" if version == 2 else "extend_development_budget_to_fifty"
        if version == 3:
            revision_fields.add("prior_revisions_sha256")
        if (set(revision) != revision_fields
                or revision["action"] != action
                or not _same_limits(revision["old_limits"], prior_limits)
                or not _same_limits(revision["new_limits"], limits)
                or type(revision["original_reservations_count"]) is not int
                or revision["original_reservations_count"] != original_count or len(rows) < original_count
                or not isinstance(revision["reason"], str) or not 1 <= len(revision["reason"].strip()) <= 2000
                or not _valid_timestamp(revision["revised_at"])
                or not isinstance(revision["prior_ledger_sha256"], str)
                or len(revision["prior_ledger_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in revision["prior_ledger_sha256"])
                or revision["original_reservations_sha256"] != _canonical_hash(rows[:original_count])
                or (version == 3 and revision["prior_revisions_sha256"] != _canonical_hash(revisions[:-1]))):
            raise RuntimeError("invalid development budget revision or altered original reservations")
        # Recursively validate both historical prefixes under their original contracts.
        original = {key: value for key, value in ledger.items() if key != "revisions"}
        original.update(version=version - 1, limits=prior_limits, reservations=rows[:original_count])
        if version == 3:
            original["revisions"] = revisions[:-1]
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


def extend_development_budget_to_fifty(path, reason):
    """Apply the user's new explicit authorization to a full v2 ledger only."""
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 2000:
        raise RuntimeError("development budget revision requires a concise explicit reason")
    with FileLock(str(path.with_suffix(".lock"))):
        raw, ledger = _read_development_ledger(path)
        if ledger["version"] != 2 or len(ledger["reservations"]) != 8:
            raise RuntimeError("extension requires exactly eight v2 reservations and cannot be repeated")
        revision = {
            "action": "extend_development_budget_to_fifty", "revised_at": utc_now().isoformat(),
            "reason": reason.strip(), "old_limits": dict(DEVELOPMENT_LIMITS_V2),
            "new_limits": dict(DEVELOPMENT_LIMITS_V3), "prior_ledger_sha256": hashlib.sha256(raw).hexdigest(),
            "original_reservations_count": 8,
            "original_reservations_sha256": _canonical_hash(ledger["reservations"]),
            "prior_revisions_sha256": _canonical_hash(ledger["revisions"])}
        ledger.update(version=3, limits=dict(DEVELOPMENT_LIMITS_V3))
        ledger["revisions"].append(revision)
        _validate_development_ledger(ledger)
        atomic_write(path, (json.dumps(ledger, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return revision


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
        if ledger["version"] == 3:
            _validate_budget(budget, DEVELOPMENT_RUN_LIMITS)
        for key, value in budget.items():
            if sum(row["budget"][key] for row in ledger["reservations"]) + value > limits[key]:
                raise RuntimeError("development batch reservation exhausted: " + key)
        ledger["reservations"].append({"evidence": evidence.name, "budget": budget,
                                       "reserved_at": utc_now().isoformat()})
        _validate_development_ledger(ledger)
        atomic_write(path, (json.dumps(ledger, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    revisions = {"extend-development-budget-once": (2, extend_development_budget_once),
                 "extend-development-budget-to-fifty": (3, extend_development_budget_to_fifty)}
    if argv and argv[0] in revisions:
        parser = argparse.ArgumentParser(description="Explicitly revise only the existing development batch ledger.")
        parser.add_argument("--ledger", required=True, type=Path)
        parser.add_argument("--reason", required=True)
        args = parser.parse_args(argv[1:])
        version, revise = revisions[argv[0]]
        revision = revise(args.ledger.resolve(), args.reason)
        print(json.dumps({"version": version, "revision": revision, "run_created": False}), flush=True)
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
    report_error = None
    try:
        report = build_report(store, run)
        write(evidence / "report.json", report)
        (evidence / "report.txt").write_text(render_report(report), encoding="utf-8")
    except Exception as exc:
        report_error = {"type": type(exc).__name__, "message": str(exc)}
    write(evidence / "outcome.json", {"run_id": run.id, "state": run.state,
        "delivery_status": run.delivery_status, "goals": run.goal_status,
        "usage": run.usage.model_dump(mode="json"), "error": error, "report_error": report_error,
        **({"seeded_code_error": gate.outcome()} if gate else {}),
        "source_changed_during_run": changed, "finished_at": utc_now().isoformat()})
    print(json.dumps({"state": run.state, "goals": run.goal_status,
                      "error": error, "report_error": report_error, "changed_sources": changed}), flush=True)
    return 0 if (run.state == "completed" and not error and not report_error and not changed
                 and (gate is None or gate.outcome()["status"] == "prelaunch_verified")) else 2


if __name__ == "__main__":
    raise SystemExit(main())
