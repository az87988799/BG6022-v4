"""One production responsibility chain for registered local scientific tools."""

import json
import time

from orca_agent.models import Check, PermissionSnapshot, QualifiedOutput, Result, utc_now
from orca_agent.orca.adapter import prepare_input, read_outputs
from orca_agent.store import ControlChanged, atomic_write, sha256_file
from orca_agent.versions import CURRENT_CHECK_VERSION


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
    permission = json.loads(store.path(f"runs/{run.id}/permission.json").read_text(encoding="utf-8"))
    if PermissionSnapshot.model_validate(permission) != run.permission:
        raise ValueError("permission changed after validation")
    if not run.permission.scientific_execution or step.tool not in run.permission.allowed_tools:
        raise ValueError("execution is not authorized")
    signal = store.read_signal(run.id)
    if signal:
        raise _PrelaunchControl(signal)
    try:
        store.check_execution_control(run)
    except ControlChanged as exc:
        raise _PrelaunchControl("message") from exc
    if any(goal.minimum_check_version != CURRENT_CHECK_VERSION
           for goal in store.load_request(run).goals
           if goal.port in ("energy", "optimized_geometry", "dipole_moment")):
        raise ValueError("check_rule_revalidation_required before scientific execution")
    if utc_now() >= run.deadline:
        raise ValueError("run deadline exhausted before execution")
    from orca_agent.doctor import validate_environment
    validate_environment(store, config, run, require_mpi=step.parameters.cores > 1)


def execute_calculation(store, run, step, attempt, config, fault=None):
    from orca_agent.tools.managed import launch_orca, record_actual_start

    recorddir = store.path(attempt.directory)
    workdir = recorddir / "task"
    workdir.mkdir()
    startup_control = None

    def hook(point):
        if fault:
            fault(point)

    def on_started(handle):
        nonlocal startup_control
        attempt.execution_handle = handle
        attempt.state = "running"
        _save_json(recorddir / "started.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)
        # The suspended process exists. Preserve its identity even when the
        # final check rejects resuming its thread; backend cleanup still applies.
        try:
            revalidate(store, run, step, config)
        except _PrelaunchControl as exc:
            startup_control = exc.action
            raise

    def on_prepared(handle):
        attempt.execution_handle = handle
        _save_json(recorddir / "boundary.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)

    invoked = False
    try:
        # This entire boundary precedes entry into the backend. An ordinary
        # preparation exception proves no process was started; hard crashes do not.
        geometry = store.artifact_path(attempt.geometry_artifact_id)
        prepared = prepare_input(workdir, geometry, step.parameters, step.tool)
        _save_json(recorddir / "prepared.json", prepared)
        if fault:
            fault("after_input_prepared")
        revalidate(store, run, step, config)
        invoked = True
        outcome = launch_orca(store, run, step, config, prepared, workdir, recorddir,
                              on_started=on_started, job_name=attempt.execution_handle.get("job_name"),
                              execution_handle=attempt.execution_handle,
                              on_actual_started=lambda handle: record_actual_start(store, run, attempt, handle),
                              on_prepared=on_prepared, fault=hook)
        if startup_control and outcome["state"] != "unknown":
            outcome.update(state="cancelled", control_action=startup_control,
                           reason="control_changed_before_suspended_thread_resumed")
    except Exception as exc:
        if invoked:
            raise
        signal = exc.action if isinstance(exc, _PrelaunchControl) else None
        outcome = {"state": "cancelled" if signal else "failed",
                   "reason": f"prelaunch_rejected: {exc}", "not_started": True,
                   "control_action": signal,
                   "handle": None, "resource_usage": {}, "exit_code": None}
    converters = {p["pid"] for p in outcome.get("resource_usage", {}).get("observed_processes", [])
                  if p.get("executable", "").lower().endswith(("orca_2json.exe", "/orca_2json"))}
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
    _save_json(recorddir / "execution.json", outcome)
    if fault:
        fault("after_execution_saved")
    return collect_result(store, run, step, attempt, outcome), outcome


def collect_result(store, run, step, attempt, outcome):
    """Archive completed files before publishing result references; never run conversion."""
    start = time.monotonic()
    recorddir = store.path(attempt.directory)
    workdir = recorddir / "task" if (recorddir / "task").is_dir() else recorddir
    # These identities belong to the startup reservation. A later Request or
    # Plan must never relabel old execution; None explicitly records legacy unknowns.
    versions = {name: getattr(attempt, name, None) for name in
                ("request_version", "plan_version", "permission_version")}
    if outcome["state"] == "unknown":
        return Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                      operation_status="unknown", diagnostics=[{"category": "execution_unknown",
                      "reason": outcome.get("reason")}, *outcome.get("budget_violations", [])],
                      source={"execution": outcome, **versions})
    artifacts = {}
    collection_error = outcome.get("collection_error")
    try:
        from orca_agent.backends.task_files import collect_paths
        files = sorted(collect_paths(workdir, count=run.budget.artifact_count))
        if workdir != recorddir:
            files += sorted(path for path in recorddir.glob("*.json") if path.is_file())
        usage = store.artifact_budget_usage(run)
        if not store.cumulative_artifact_budget(run):
            # Original budgets bounded each collection, without a Run-wide quota.
            usage = {"reserved_count": 0, "reserved_bytes": 0}
        if (len(files) + usage["reserved_count"] > run.budget.artifact_count
                or sum(p.stat().st_size for p in files) + usage["reserved_bytes"]
                > run.budget.artifact_total_bytes
                or any(p.stat().st_size > run.budget.artifact_bytes for p in files)):
            raise ValueError("artifact collection exceeds the frozen count/size budget")
        for path in files:
            if time.monotonic() - start > store.run_configuration(run).resources.collection_timeout_seconds:
                raise TimeoutError("artifact collection exceeded its configured time budget")
            artifacts[path.name] = store.import_artifact(path, role="raw_evidence", run_id=run.id,
                                                         attempt_id=attempt.id)
    except Exception as exc:
        collection_error = str(exc)
    try:
        if collection_error:
            raise ValueError(collection_error)
        environment_path = store.path(f"runs/{run.id}/environment.json")
        environment = json.loads(environment_path.read_text(encoding="utf-8")) if environment_path.exists() else {}
        parsed = read_outputs(workdir, step.parameters, step.tool,
                              expected_orca_version=environment.get("orca", {}).get("version"))
    except Exception as exc:
        parsed = {"checks": {}, "qualified_outputs": {}, "observations": {},
                  "diagnostics": [{"category": "collection_error" if collection_error else "parse_error",
                                   "detail": str(exc)}]}
    try:
        current_paths = {p.name: p for p in collect_paths(workdir, count=run.budget.artifact_count)}
        if workdir != recorddir:
            current_paths.update({p.name: p for p in recorddir.glob("*.json") if p.is_file()})
        current_files = set(current_paths)
        changed = current_files != set(artifacts) or any(
            not current_paths[name].is_file() or sha256_file(current_paths[name]) != artifact.sha256
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
        source={**versions, "input_fingerprint": attempt.input_fingerprint,
                "geometry_artifact_id": attempt.geometry_artifact_id,
                "files": {name: {"artifact_id": a.id, "sha256": a.sha256}
                          for name, a in artifacts.items()}, "execution": outcome,
                "conditions": {**step.parameters.model_dump(mode="json"),
                               "electronic_state": "RHF", "environment": "gas_phase",
                               "profile": "fixed-rhf-profile-1"}},
    )
