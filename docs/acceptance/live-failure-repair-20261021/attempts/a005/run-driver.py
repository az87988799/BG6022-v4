"""Run the original task against one captured product, never inject a solution."""
import hashlib
import json
import os
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ATTEMPT = Path(sys.argv[1]).resolve()
META = json.loads((ATTEMPT / "attempt.json").read_bytes())
SNAPSHOT = Path(META["snapshot"])
os.environ["LOCALAPPDATA"] = str(Path(META["environment_root"]).parent.parent)
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONIOENCODING"] = "utf-8"


def save(name, value):
    with (ATTEMPT / name).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def verify():
    for name, value in META["source_files"].items():
        assert hashlib.sha256((SNAPSHOT / name).read_bytes()).hexdigest() == value["sha256"], name
    assert hashlib.sha256((ATTEMPT / "configuration.json").read_bytes()).hexdigest() == META["configuration_sha256"]
    assert hashlib.sha256((ATTEMPT / "original.txt").read_bytes()).hexdigest() == META["original_sha256"]
    if "driver" in META:
        assert hashlib.sha256(Path(META["driver"]).read_bytes()).hexdigest() == META["driver_sha256"]


def main():
    import orca_agent
    from orca_agent.config import Config
    from orca_agent.entrypoints import execute
    from orca_agent.llm import DeepSeekTransport
    from orca_agent.natural import initialize_text
    from orca_agent.report import build_report, render_report
    from orca_agent.store import Store, default_environment_root

    assert Path(orca_agent.__file__).resolve().is_relative_to(SNAPSHOT)
    assert not (ATTEMPT / "start.json").exists(), "Never launch the same attempt twice"
    verify()
    config = Config.model_validate_json((ATTEMPT / "configuration.json").read_bytes())
    assert default_environment_root().resolve() == Path(META["environment_root"]).resolve()
    store = Store(config.data_root, environment_root=default_environment_root(),
                  configuration=config.run_configuration())
    run = initialize_text(store, config, (ATTEMPT / "original.txt").read_text(encoding="utf-8"))
    save("start.json", {"run_id": run.id, "task": META["task"], "candidate": META["candidate"],
                        "started_at_utc": datetime.now(timezone.utc).isoformat(),
                        "source_manifest_sha256": META["source_manifest_sha256"]})
    print(json.dumps({"event": "started", "task": META["task"], "run_id": run.id}), flush=True)
    started = time.monotonic()
    error = None
    class ObservedTransport(DeepSeekTransport):
        @staticmethod
        async def _exchange(client, request):
            try:
                return await DeepSeekTransport._exchange(client, request)
            except Exception as exc:
                chain, cause = [], exc
                for _ in range(8):
                    if cause is None:
                        break
                    chain.append(type(cause).__name__)
                    cause = cause.__cause__
                frames = [{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                          for frame in traceback.extract_tb(exc.__traceback__)[-8:]]
                name = getattr(exc, "name", None)
                save("transport-diagnostic-" + request.request_hash[:16] + ".json", {
                    "request_hash": request.request_hash, "exception_types": chain, "frames": frames,
                    "python_name": name if isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else None,
                    "exception_messages_or_headers_recorded": False,
                })
                raise
    try:
        run = execute(store, config, run.id, transport=ObservedTransport())
    except Exception as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        run = store.load_run(run.id)
    elapsed = time.monotonic() - started
    verify()
    usage = run.usage.model_dump(mode="json")
    unsettled = [a.id for a in run.attempts if a.state in {"intent", "running", "unknown"}]
    unsettled += [a.request_key for a in run.internal_calls if a.state in {"reserved", "running", "unknown"}]
    unknown = []
    if error or unsettled or run.state == "unknown":
        unknown.extend(("orca_starts", "internal_work_units"))
    if usage["model_tokens_unknown"]:
        unknown.append("model_tokens")
    unconfirmed = []
    for attempt in run.attempts:
        if attempt.execution_kind != "orca" or not attempt.result_id:
            continue
        result = store.load_result(run.id, attempt.result_id)
        execution = result.source.get("execution", {})
        if execution.get("actual_start_status") != "unconfirmed":
            continue
        terminated = (attempt.state in {"failed", "timed_out", "cancelled", "completed"}
            and attempt.finished_at is not None
            and execution.get("resource_usage", {}).get("active_processes") == 0
            and execution.get("state") == attempt.state)
        unconfirmed.append({"attempt_id": attempt.id, "result_id": result.id,
            "execution_terminated": terminated, "start_count_interval": [0, 1]})
        if not terminated:
            unsettled.append(attempt.id)
    if unconfirmed and "orca_starts" not in unknown:
        unknown.append("orca_starts")
    known = {"model_calls": usage["model_calls"], "model_tokens": usage["model_tokens_used"],
             "orca_starts": usage["orca_starts_actual"], "internal_work_units": usage["internal_work_units"],
             "wall_seconds": elapsed}
    assert not any(known[k] > META["reservation"][k] for k in known), "Actual campaign reservation exceeded"
    report = build_report(store, run)
    save("report.json", report)
    (ATTEMPT / "report.txt").write_text(render_report(report), encoding="utf-8", newline="\n")
    outcome = {"finished_at_utc": datetime.now(timezone.utc).isoformat(), "run_id": run.id,
        "task": META["task"], "candidate": META["candidate"], "state": run.state,
        "goal_status": run.goal_status, "delivery_status": run.delivery_status,
        "usage": usage, "known_usage": known, "unknown_fields": unknown, "error": error,
        "unconfirmed_start_accounting": unconfirmed,
        "unknown_usage_reservations": {"orca_starts": len(unconfirmed)},
        "unsettled_execution": unsettled, "source_unchanged": True,
        "independent_reference": "not_run", "control_execution": "not_run"}
    save("outcome.json", outcome)
    print(json.dumps({"event": "finished", **outcome}), flush=True)
    raise SystemExit(2 if unknown or error else 0)


if __name__ == "__main__":
    main()
