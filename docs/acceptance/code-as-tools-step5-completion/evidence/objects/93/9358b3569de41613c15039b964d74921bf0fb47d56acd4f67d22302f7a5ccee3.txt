"""Explicit development continuation; retains the existing Run and its budget."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from orca_agent.config import Config
from orca_agent.entrypoints import execute
from orca_agent.models import utc_now
from orca_agent.report import build_report, render_report
from orca_agent.store import Store, atomic_write
from scripts.code_tools_development import snapshot

root = Path.cwd()
batch = root / "data/code-as-tools-step5"
evidence = batch / "live-005-continuation-2"
evidence.mkdir(exist_ok=False)
config = Config.model_validate_json((batch / "config-final.json").read_bytes())
store = Store(config.data_root, configuration=config.run_configuration())
run_id = "run_27b25c07ada4468e83e2c76571139fb1"
run = store.load_run(run_id)
text = "继续完成当前已授权任务；保持原目标、条件、最低证据、权限与累计预算。已有脚本和证据继续使用，允许按必要反馈修复。"

def write(name, value):
    atomic_write(evidence / name, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode(), immutable=True)

write("run-before.json", run.model_dump(mode="json"))
frozen = {"created_at": utc_now().isoformat(),
          "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
          "source_files": snapshot(root), "configuration": config.model_dump(mode="json"),
          "run_id": run_id, "text": text, "scope": "same Run after deterministic incremental Plan repair; not a fresh frozen-candidate pass",
          "original_outcome_sha256": hashlib.sha256((batch / "live-005/outcome.json").read_bytes()).hexdigest(),
          "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
          "limits": {"runs": 5, "model_calls": 72, "model_tokens": 3600000, "orca_starts": 12}}
write("freeze.json", frozen)
write("run-location.json", {"run_id": run_id, "data_root": str(store.root), "environment_root": str(store.environment_root)})
write("pending-control.json", store.read_control(run_id))
print(json.dumps({"run_id": run_id, "resume": True, "usage_before": run.usage.model_dump(mode="json"), "deadline": run.deadline.isoformat()}), flush=True)
error = None
try:
    run = execute(store, config, run_id, resume=True)
except Exception as exc:
    error = {"type": type(exc).__name__, "message": str(exc)}
    run = store.load_run(run_id)
changed = sorted(k for k, v in snapshot(root).items() if frozen["source_files"].get(k) != v)
report = build_report(store, run)
write("report.json", report)
atomic_write(evidence / "report.txt", render_report(report).encode("utf-8"), immutable=True)
write("outcome.json", {"state": run.state, "goals": run.goal_status, "delivery_status": run.delivery_status,
                      "usage": run.usage.model_dump(mode="json"), "error": error,
                      "source_changed_during_run": changed, "finished_at": utc_now().isoformat()})
print(json.dumps({"state": run.state, "goals": run.goal_status, "error": error, "changed": changed}), flush=True)
