"""Offline staging/launcher-path replay only. Never execute task code or a process."""
import hashlib
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from orca_agent.backends.container_runtime import helper
from orca_agent.store import Store
from orca_agent.tools.managed import _prepare_python_inputs


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


root = Path(__file__).resolve().parents[2]
data = root / "data/live-repair/attempts/a025/data"
run_id = "run_9d9ed6dbc81245f486a47498af1c8d00"
run_path = data / "runs" / run_id / "run.json"
with tempfile.TemporaryDirectory() as temporary:
    temporary = Path(temporary)
    store = Store(data, environment_root=temporary / "environment")
    run = store.load_run(run_id)
    attempt = next(item for item in run.attempts if item.id == "attempt_77b7ab3180494693a98857e34da1fe36")
    step = attempt.frozen_step
    assert step.parameters.sdk_orca is False
    entry, = step.parameters.inputs.entries
    assert entry.target_path == "stdout.out"
    source = store.artifact_path(entry.artifact_id)
    paths = [run_path, source, store.artifact_path(step.parameters.code.artifact_id),
             store.path(attempt.directory) / "execution.json"]
    before = {str(path.relative_to(root)): sha(path) for path in paths}
    area = temporary / "attempt"
    task, logs = area / "task", area / "python-runtime"
    task.mkdir(parents=True)
    logs.mkdir()
    _prepare_python_inputs(store, run, step, area, task)
    assert sha(task / "stdout.out") == entry.sha256
    calls = []

    def gated(folder, logical_area, timeout):
        assert folder == logs
        calls.append("simulated_gate")
        (folder / ".launch.json").write_text('{"synthetic":true}', encoding="utf-8")

    def no_exec(*args):
        calls.append("would_exec_only_no_process_started")

    with patch.object(helper, "ROOT", area), patch.object(helper, "require_identity", lambda *_: None), \
            patch.object(helper.os, "chdir", lambda folder: calls.append("same_task_cwd" if folder == task else "wrong")), \
            patch.object(helper, "gated_child", gated), patch.object(helper.os, "dup2", lambda *_: None), \
            patch.object(helper.os, "execve", no_exec):
        helper.launch("python", 10, "python", "isolated-1")
    assert calls == ["same_task_cwd", "simulated_gate", "would_exec_only_no_process_started"]
    assert sha(task / "stdout.out") == entry.sha256
    assert (logs / "stdout.out").read_bytes() == b""
    assert (logs / "stderr.txt").read_bytes() == b""
    after = {str(path.relative_to(root)): sha(path) for path in paths}
    assert after == before
    record = {"scope": "Offline original a025 hash-pinned input staging and fixed launcher path; gate/dup2/execve are stubs. No task code, container, model or ORCA execution.",
              "run_id": run.id, "attempt_id": attempt.id, "input_artifact_id": entry.artifact_id,
              "input_target_path": entry.target_path, "input_sha256": entry.sha256,
              "original_files": before, "originals_unchanged": True,
              "source_layout": None, "counterfactual_layout": "isolated-1",
              "task_input_preserved": True, "runtime_logs_created_separately": True,
              "calls": calls, "task_or_scientific_success_claimed": False,
              "helper_sha256": sha(root / "src/orca_agent/backends/container_runtime/helper.py")}
output = root / "data/workflow-root-repair/a025-python-runtime-layout-replay.json"
output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"output": str(output), "originals_unchanged": True, "input_preserved": True}))
