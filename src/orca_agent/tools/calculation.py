"""One production responsibility chain for registered local scientific tools."""

import json
import os
import time

from orca_agent.backends import local
from orca_agent.models import Check, QualifiedOutput, Result, utc_now
from orca_agent.orca.adapter import prepare_input, read_outputs
from orca_agent.store import atomic_write, sha256_file
from orca_agent.versions import CURRENT_CHECK_VERSION, is_supported_orca_version


class _PrelaunchControl(ValueError):
    def __init__(self, action):
        super().__init__("control signal received before execution")
        self.action = action


def _save_json(path, value):
    atomic_write(path, (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode(),
                 immutable=True)


def revalidate(store, run, step, config):
    current = store.load_run(run.id)
    if current != run:
        raise ValueError("run changed after validation")
    plan = store.load_plan(current)
    if step not in plan.steps:
        raise ValueError("step is no longer in the validated plan")
    permission = json.loads(store.path(f"runs/{run.id}/permission.json").read_text())
    if permission != run.permission.model_dump(mode="json"):
        raise ValueError("permission changed after validation")
    if not run.permission.scientific_execution or step.tool not in run.permission.allowed_tools:
        raise ValueError("execution is not authorized")
    signal = store.read_signal(run.id)
    if signal:
        raise _PrelaunchControl(signal)
    if any(goal.minimum_check_version != CURRENT_CHECK_VERSION
           for goal in store.load_request(run).goals):
        raise ValueError("check_rule_revalidation_required before scientific execution")
    if utc_now() >= run.deadline:
        raise ValueError("run deadline exhausted before execution")
    environment = json.loads(store.path(f"runs/{run.id}/environment.json").read_text())
    if not is_supported_orca_version(environment["orca"].get("version")):
        raise ValueError("frozen ORCA version is not enabled")
    if str(config.orca_path) != environment["orca"]["path"]:
        raise ValueError("ORCA executable differs from the frozen environment")
    if sha256_file(config.orca_path) != environment["orca"]["sha256"]:
        raise ValueError("ORCA executable changed after environment validation")
    if step.parameters.cores > 1:
        mpi = environment["mpi"]
        if str(config.mpi_path) != mpi["path"] or sha256_file(config.mpi_path) != mpi["sha256"]:
            raise ValueError("MPI executable changed after environment validation")


def execute_calculation(store, run, step, attempt, config, fault=None):
    workdir = store.path(attempt.directory)
    control_lock = store.control_lock(run.id)
    locked = False

    def hook(point):
        nonlocal locked
        if point == "after_resumed" and locked:
            control_lock.release()
            locked = False
        if fault:
            fault(point)

    def on_started(handle):
        revalidate(store, run, step, config)
        attempt.execution_handle = handle
        attempt.state = "running"
        _save_json(workdir / "started.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)

    invoked = False
    try:
        # This entire boundary precedes entry into the backend. An ordinary
        # preparation exception proves no process was started; hard crashes do not.
        geometry = store.artifact_path(attempt.geometry_artifact_id)
        prepared = prepare_input(workdir, geometry, step.parameters, step.tool)
        _save_json(workdir / "prepared.json", prepared)
        if fault:
            fault("after_input_prepared")
        control_lock.acquire()
        locked = True
        revalidate(store, run, step, config)
        remaining = (run.deadline - utc_now()).total_seconds()
        child_environment = {key.upper(): value for key, value in os.environ.items()}
        directories = [str(config.orca_path.parent)]
        if config.mpi_path:
            directories.append(str(config.mpi_path.parent))
        child_environment["PATH"] = os.pathsep.join(directories + [child_environment.get("PATH", "")])
        child_environment["OMP_NUM_THREADS"] = "1"
        child_environment["MKL_NUM_THREADS"] = "1"
        child_environment["OPENBLAS_NUM_THREADS"] = "1"
        invoked = True
        outcome = local.run_managed(
            config.orca_path, [str(prepared["input_path"])], workdir,
            cores=step.parameters.cores, total_memory_mb=step.parameters.memory_mb,
            timeout_s=min(step.parameters.timeout_seconds, remaining),
            cancel_requested=lambda: store.read_signal(run.id) == "cancel",
            on_started=on_started, job_name=attempt.execution_handle["job_name"], fault=hook,
            environment=child_environment,
        )
    except Exception as exc:
        if invoked:
            raise
        signal = exc.action if isinstance(exc, _PrelaunchControl) else None
        outcome = {"state": "cancelled" if signal else "failed",
                   "reason": f"prelaunch_rejected: {exc}", "not_started": True,
                   "control_action": signal,
                   "handle": None, "resource_usage": {}, "exit_code": None}
    finally:
        if locked:
            control_lock.release()
    converters = {p["pid"] for p in outcome.get("resource_usage", {}).get("observed_processes", [])
                  if p.get("executable", "").lower().endswith("orca_2json.exe")}
    outcome["postprocess_starts_detected"] = max(len(converters), int((workdir / "job.2jsonout").exists()))
    if outcome["postprocess_starts_detected"]:
        outcome.setdefault("budget_violations", []).append({
            "category": "postprocess_budget_exceeded",
            "allowed_starts": run.budget.postprocess_starts,
            "detected_starts": outcome["postprocess_starts_detected"],
        })
        # A budget failure cannot establish that a still-unknown process tree
        # stopped. Keep its lease and defer archival until termination is proven.
        if outcome["state"] != "unknown":
            outcome.update(state="failed", reason="unexpected_internal_postprocess_exceeded_zero_budget")
    _save_json(workdir / "execution.json", outcome)
    if fault:
        fault("after_execution_saved")
    return collect_result(store, run, step, attempt, outcome), outcome


def collect_result(store, run, step, attempt, outcome):
    """Archive completed files before publishing result references; never run conversion."""
    start = time.monotonic()
    workdir = store.path(attempt.directory)
    if outcome["state"] == "unknown":
        return Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                      operation_status="unknown", diagnostics=[{"category": "execution_unknown",
                      "reason": outcome.get("reason")}, *outcome.get("budget_violations", [])],
                      source={"execution": outcome})
    artifacts = {}
    collection_error = None
    try:
        files = sorted(path for path in workdir.iterdir() if path.is_file())
        if len(files) > 200 or sum(p.stat().st_size for p in files) > 256 * 1024 * 1024:
            raise ValueError("artifact collection exceeds the small-system 256 MiB/200-file limit")
        for path in files:
            if time.monotonic() - start > 30:
                raise TimeoutError("artifact collection exceeded its 30 second budget")
            artifacts[path.name] = store.import_artifact(path, role="raw_evidence", run_id=run.id,
                                                         attempt_id=attempt.id)
    except Exception as exc:
        collection_error = str(exc)
    try:
        if collection_error:
            raise ValueError(collection_error)
        environment_path = store.path(f"runs/{run.id}/environment.json")
        environment = json.loads(environment_path.read_text()) if environment_path.exists() else {}
        parsed = read_outputs(workdir, step.parameters, step.tool,
                              expected_orca_version=environment.get("orca", {}).get("version"))
    except Exception as exc:
        parsed = {"checks": {}, "qualified_outputs": {}, "observations": {},
                  "diagnostics": [{"category": "collection_error" if collection_error else "parse_error",
                                   "detail": str(exc)}]}
    try:
        current_files = {p.name for p in workdir.iterdir() if p.is_file()}
        changed = current_files != set(artifacts) or any(
            not (workdir / name).is_file() or sha256_file(workdir / name) != artifact.sha256
            for name, artifact in artifacts.items())
    except Exception as exc:
        changed = True
        parsed["diagnostics"].append({"category": "collection_error", "detail": str(exc)})
    if changed:
        parsed["qualified_outputs"] = {}
        parsed["diagnostics"].append({"category": "source_conflict",
                                      "detail": "evidence changed between archival and parsing"})
        for items in parsed["checks"].values():
            items.append(Check(name="archived_source_integrity", status="failed",
                               rule_version=CURRENT_CHECK_VERSION,
                               detail="archive hashes no longer match the parsed evidence"))
    checks = {port: [Check.model_validate(check) for check in items]
              for port, items in parsed["checks"].items()}
    outputs = {}
    if outcome["state"] == "completed":
        for port, value in parsed["qualified_outputs"].items():
            values = {"checks": checks[port], "source": value.get("source", {})}
            geometry_file = value.get("geometry_file")
            if geometry_file:
                name = str(geometry_file).replace("\\", "/").split("/")[-1]
                if name not in artifacts:
                    raise ValueError("qualified geometry lacks an archived source")
                values["artifact_id"] = artifacts[name].id
            else:
                values.update(value=value["value"], unit=value["unit"])
            outputs[port] = QualifiedOutput(**values)
    diagnostics = [*parsed["diagnostics"], *outcome.get("budget_violations", [])]
    if outcome["state"] != "completed":
        diagnostics.append({"category": outcome["state"], "reason": outcome.get("reason")})
    return Result(
        run_id=run.id, step_id=step.id, attempt_id=attempt.id,
        operation_status=outcome["state"], checks=checks, qualified_outputs=outputs,
        observations=parsed["observations"], diagnostics=diagnostics,
        artifact_ids=[a.id for a in artifacts.values()],
        source={"input_fingerprint": attempt.input_fingerprint,
                "geometry_artifact_id": attempt.geometry_artifact_id,
                "files": {name: {"artifact_id": a.id, "sha256": a.sha256}
                          for name, a in artifacts.items()}, "execution": outcome,
                "conditions": step.parameters.model_dump(mode="json")},
    )
