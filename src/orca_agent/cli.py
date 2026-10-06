"""One foreground command line. Merely inspecting status never resumes execution."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from orca_agent.config import load_config
from orca_agent.models import new_id, utc_now


def emit(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _safe_errors(error):
    """Never echo raw request values, file contents, paths or exception reprs."""
    if isinstance(error, ValidationError):
        from orca_agent.config import Config
        from orca_agent.models import BudgetLimits, CalculationParameters
        from orca_agent.structured import GoalInput, StepInput, TaskInput
        from orca_agent.tools.registry import (
            EvidenceFieldParameters,
            EvidenceListParameters,
            EvidenceTextParameters,
        )
        known_fields = set().union(*(model.model_fields for model in (
            Config, BudgetLimits, CalculationParameters, TaskInput, StepInput, GoalInput,
            EvidenceListParameters, EvidenceTextParameters, EvidenceFieldParameters,
        )))
        items = error.errors(include_input=False, include_context=False, include_url=False)
        return [{
            "loc": [part if isinstance(part, int) or part in known_fields else "<field>"
                    for part in item["loc"]],
            "msg": ("Value violates the supported request contract" if item["type"] == "value_error"
                    else "Invalid JSON document" if item["type"] == "json_invalid"
                    else item["msg"]),
        } for item in items[:40]]
    if isinstance(error, FileNotFoundError):
        message = "A required file or registered artifact is missing"
    elif isinstance(error, PermissionError):
        message = "The operation could not access its required files"
    elif isinstance(error, TimeoutError):
        message = "The bounded operation timed out"
    elif isinstance(error, OSError):
        message = "A file-system operation failed"
    elif isinstance(error, ValueError):
        message = "The operation violates a declared input, path, permission, or scientific contract"
    else:
        message = "The operation could not complete within its supported execution boundary"
    return [{"loc": [], "msg": message}]


def _reject_before_run(store, error):
    """An operation diagnostic is a file, not a new runtime domain lifecycle."""
    from orca_agent.store import Store, atomic_write
    diagnostic = {"category": type(error).__name__, "errors": _safe_errors(error),
                  "time": utc_now().isoformat()}
    response = {"error": diagnostic["category"], "errors": diagnostic["errors"]}
    try:
        target_store = store if store is not None else Store(Path("data"))
        rejection_id = new_id("rejection")
        atomic_write(target_store.path(f"rejections/{rejection_id}.json"),
                     (json.dumps(diagnostic, ensure_ascii=False, indent=2) + "\n").encode(),
                     immutable=True)
        response.update(rejection_recorded=True, rejection_id=rejection_id)
    except (ValueError, OSError, RuntimeError) as audit_error:
        response.update(rejection_recorded=False, audit_error=type(audit_error).__name__)
    return response


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Controlled local ORCA calculation foundation")
    parser.add_argument("--config", type=Path, help="Local TOML configuration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Probe installation without submitting science")
    commands.add_parser("tools", help="Print the registered tool contracts")
    run_parser = commands.add_parser("run", help="Execute an explicit structured request")
    run_parser.add_argument("request", type=Path)
    ask_parser = commands.add_parser("ask", help="Start an Agent from natural text and independent user constraints")
    ask_parser.add_argument("request", type=Path, help="User request bundle JSON; no execution Steps")
    message_parser = commands.add_parser("message", help="Queue a user message without taking coordinator ownership")
    message_parser.add_argument("run_id")
    message_parser.add_argument("text")
    message_parser.add_argument("--update-file", type=Path, help="Explicit user Request fields to apply after queuing")
    report_parser = commands.add_parser("report", help="Render existing evidence and gaps without model or execution")
    report_parser.add_argument("run_id")
    for command in ("status", "pause", "cancel", "resume"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("run_id")
    inspect_parser = commands.add_parser("inspect", help="Read registered evidence without execution")
    inspect_parser.add_argument("artifact_id", nargs="?")
    inspect_parser.add_argument("--run-id", help="List a bounded page of artifacts from a Run")
    inspect_parser.add_argument("--field", help="Read a bounded dot-separated JSON object field")
    inspect_parser.add_argument("--start-line", type=int, default=1)
    inspect_parser.add_argument("--lines", type=int, default=40)
    inspect_parser.add_argument("--offset", type=int, default=0)
    inspect_parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)
    store = run = None
    try:
        config = load_config(args.config)
        if args.command == "doctor":
            from orca_agent.doctor import diagnose
            report = diagnose(config)
            emit(report)
            return 0 if not report["issues"] else 2
        if args.command == "tools":
            from orca_agent.tools.registry import catalog
            emit(catalog())
            return 0
        from orca_agent.store import Store
        store = Store(config.data_root)
        if args.command == "inspect":
            from orca_agent.tools.registry import dispatch_evidence
            if args.run_id:
                if args.artifact_id or args.field:
                    raise ValueError("choose Run listing or one Artifact")
                emit(dispatch_evidence(store, "evidence.list", {
                    "run_id": args.run_id, "offset": args.offset, "limit": args.limit,
                }))
            elif not args.artifact_id:
                raise ValueError("supply an artifact id or --run-id")
            elif args.field:
                emit(dispatch_evidence(store, "evidence.field", {
                    "artifact_id": args.artifact_id, "field": args.field,
                }))
            else:
                emit(dispatch_evidence(store, "evidence.text", {
                    "artifact_id": args.artifact_id, "start_line": args.start_line, "lines": args.lines,
                }))
        elif args.command == "status":
            emit(store.load_run(args.run_id))
        elif args.command == "report":
            from orca_agent.report import build_report, render_report
            print(render_report(build_report(store, args.run_id)))
        elif args.command == "message":
            message_id = store.enqueue_message(args.run_id, args.text)
            if args.update_file:
                from orca_agent.natural import apply_user_update
                if args.update_file.stat().st_size > 65536:
                    raise ValueError("user update exceeds 64 KiB")
                changes = json.loads(args.update_file.read_text(encoding="utf-8"))
                apply_user_update(store, args.run_id, message_id, changes)
            emit({"run_id": args.run_id, "message_id": message_id, "status": "queued",
                  "execution": "active coordinator observes the new generation; otherwise explicitly resume"})
        elif args.command in ("pause", "cancel"):
            store.signal(args.run_id, args.command)
            emit({"run_id": args.run_id, "requested": args.command,
                  "confirmation": "inspect status; an active coordinator applies the signal"})
        else:
            from orca_agent.runner import execute, initialize
            if args.command == "ask":
                from orca_agent.natural import initialize_bundle
                run = initialize_bundle(store, config, args.request)
            else:
                run = (initialize(store, config, args.request) if args.command == "run"
                       else store.load_run(args.run_id))
            emit({"run_id": run.id, "action": args.command})
            result = execute(store, config, run.id, resume=args.command == "resume")
            emit(result)
            if result.agent_enabled:
                from orca_agent.report import build_report, render_report
                print(render_report(build_report(store, result)))
            return 0 if result.state == "completed" else 2
    except (ValueError, OSError, RuntimeError) as exc:
        if args.command in {"run", "ask"} and run is None:
            emit(_reject_before_run(store, exc))
        else:
            emit({"error": type(exc).__name__, "errors": _safe_errors(exc)})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
