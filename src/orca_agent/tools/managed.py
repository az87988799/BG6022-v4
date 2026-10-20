"""Task-code execution and bounded SDK calls in the existing coordinator.

All task content is untrusted. The mailbox transports bytes, never a host path,
permission, command line, scientific success assertion or execution handle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from contextlib import ExitStack
from pathlib import Path

from orca_agent.backends import local
from orca_agent.backends.isolation import AppContainerSession, clean_environment
from orca_agent.backends.task_files import collect_paths, read_mailbox
from orca_agent.models import (
    CodeRef,
    InputManifest,
    InputManifestEntry,
    NativeOrcaParameters,
    Result,
    fingerprint,
    utc_now,
)
from orca_agent.orca.native import prepare_native_input, read_pinned_input
from orca_agent.task_sdk import MAX_REQUEST_BYTES
from orca_agent.tools.calculation import _save_json
from orca_agent.tools.registry import get_tool


def _plan_step_matches(store, run, step, *, request=None, plan=None, allow_pending=False):
    from orca_agent.file_references import resolve_step_files

    plan = plan if plan is not None else store.load_plan(run)
    declared = next((item for item in plan.steps if item.id == step.id), None) if plan else None
    if declared is None:
        return False
    if declared == step:
        return True
    return resolve_step_files(store, run, declared, request=request, plan=plan,
                              allow_pending=allow_pending) == step


def _control(store, run, step):
    from orca_agent.models import PermissionSnapshot

    if store.load_run(run.id) != run or not _plan_step_matches(store, run, step):
        raise ValueError("Run or Plan changed before managed execution")
    snapshot = json.loads(store.path(f"runs/{run.id}/permission.json").read_text("utf-8"))
    if PermissionSnapshot.model_validate(snapshot) != run.permission:
        raise ValueError("immutable permission changed before managed execution")
    store.validate_budget_snapshot(run)
    store.check_execution_control(run)
    if store.read_signal(run.id) or utc_now() >= run.deadline:
        raise ValueError("execution control/deadline prevents launch")


class _MissingInitialGeometry(ValueError):
    """Only an absent anchor can be deferred at Plan admission, never bad evidence."""


def _initial_geometry(store, run, request, system_id):
    """Read the trusted initial anchor; task-authored copies cannot establish it."""
    from orca_agent.input_bindings import prepared_geometry

    system = next((item for item in request.systems if item.id == system_id), None)
    if system and system.geometry_source == "prepare":
        geometry_id = prepared_geometry(store, run, request, system_id)
    else:
        geometry_id = system.geometry_artifact_id if system else request.geometry_artifact_id
        if geometry_id is not None and geometry_id not in run.permission.artifact_ids:
            raise ValueError("registered native geometry is outside the frozen permission")
    if geometry_id is None:
        raise _MissingInitialGeometry(
            "native execution requires current prepared or registered geometry: "
            "complete identity resolution and geometry preparation, or bind an authorized "
            "registered initial geometry, before executing sdk_orca=true. "
            "Plan approval may instead declare a direct pending same-system geometry preparation "
            "dependency; task-generated XYZ "
            "files cannot replace this initial identity/geometry anchor.")
    geometry = store.load_artifact(geometry_id)
    geometry_bytes = read_pinned_input(store.artifact_path(geometry_id), geometry.sha256, limit=65536)
    return geometry_id, geometry, geometry_bytes


def _pending_initial_geometry(run, request, plan, step):
    """A future producer is a dependency, not an initial-geometry qualification."""
    system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
    system = next((item for item in request.systems if item.id == system_id), None)
    if system is None or system.geometry_source != "prepare":
        return False
    for producer in plan.steps:
        tool = get_tool(producer.tool)
        if (producer.id in step.depends_on and "prepare_geometry" in tool.effects
                and "prepared_geometry" in tool.output_ports
                and producer.system_id == system_id
                and getattr(producer.parameters, "system_id", None) == system_id
                and producer.tool in run.permission.allowed_tools
                and run.permission.geometry_preparation
                and not any(call.step_id == producer.id for call in run.calls)):
            return True
    return False


def validate_sdk_orca_preconditions(store, run, step, *, request=None, plan=None,
                                    allow_future_geometry=False):
    """Read-only preflight before any Python reservation or boundary startup."""
    if not run.permission.scientific_execution or "orca.run" not in run.permission.allowed_tools:
        raise ValueError("sdk_orca=true requires authorized managed ORCA execution")
    request = request if request is not None else store.load_request(run)
    plan = plan if plan is not None else store.load_plan(run)
    if plan is None or not _plan_step_matches(
            store, run, step, request=request, plan=plan, allow_pending=allow_future_geometry):
        raise ValueError("SDK preflight requires the current or candidate frozen Step")
    if len(request.systems) > 1 and step.system_id is None:
        raise ValueError("sdk_orca=true requires an explicit requested system")
    try:
        binding = native_input_binding(store, run, step, request=request, plan=plan)
        if binding is None:  # Historical profiles still require the declared initial anchor.
            system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
            _initial_geometry(store, run, request, system_id)
    except _MissingInitialGeometry:
        if allow_future_geometry and _pending_initial_geometry(run, request, plan, step):
            return None
        raise
    return binding


def native_input_binding(store, run, step, *, request=None, plan=None):
    """Consume current trusted geometry/conditions, never task-authored claims."""
    from orca_agent.applicability import CONDITION_FIELDS, step_condition_goals
    from orca_agent.orca.native_profile import canonical_native_condition
    from orca_agent.task_observations import _native_current_conditions

    policy = store.run_configuration(run).execution.native_input_policy
    if policy is None:
        return None
    def canonical(name, value):
        return canonical_native_condition(name, value, policy=policy)
    request = request if request is not None else store.load_request(run)
    plan = plan if plan is not None else store.load_plan(run)
    if not _plan_step_matches(store, run, step, request=request, plan=plan):
        raise ValueError("native input binding requires the current frozen Step")
    if len(request.systems) > 1 and step.system_id is None:
        raise ValueError("native execution requires an explicit requested system")
    system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
    system = next((item for item in request.systems if item.id == system_id), None)
    simplified = store.run_configuration(run).workflow_policy == "simplified-1"
    related_goals = [goal for goal in request.goals if (
        (plan.goal_map.get(goal.id) and plan.goal_map[goal.id].step_id == step.id)
        or system_id in goal.system_ids or not goal.system_ids)]
    selected_goals = step_condition_goals(request, plan, step) if simplified else related_goals
    # Resolve each intended evaluation before requiring a complete setup.
    # Goal-specific values must never be lost merely because no shared value
    # exists (for example a gas/water comparison).
    projected = request
    if simplified:
        projected = request.model_copy(update={"condition_evidence": {
            **request.condition_evidence, "workflow_policy": {"version": "simplified-1"}}})
    assessments = [_native_current_conditions(projected, goal, system_id, policy=policy)
                   for goal in selected_goals or [None]]
    for assessment in assessments:
        if assessment["reasons"]:
            raise ValueError("native conditions unresolved: " + ",".join(assessment["reasons"]))
    conditions = dict(assessments[0]["conditions"])
    condition_sources = dict(assessments[0]["sources"])
    environments = {assessment["conditions"]["environment"] for assessment in assessments}
    for name in CONDITION_FIELDS:
        variants = {assessment["conditions"][name] for assessment in assessments}
        if None in variants:
            raise ValueError("current Request condition is unsupported by the frozen native execution profile")
        if len(variants) > 1 and not (simplified and name == "environment"):
            raise ValueError("native Step requires separate evaluations for differing Goal condition: " + name)
    if simplified and any("environment" in goal.conditions for goal in selected_goals):
        condition_sources["environment"] = "goal:" + ",".join(
            goal.id for goal in selected_goals if "environment" in goal.conditions)
    for name, value in step.parameters.conditions.items():
        if simplified and name == "environment":
            selected = canonical(name, value)
            if selected not in environments:
                raise ValueError("Step environment is outside the requested conditions")
            conditions[name] = selected
            condition_sources[name] = "step:" + step.id
            environments = {selected}
            continue
        if name in CONDITION_FIELDS and canonical(name, value) != conditions[name]:
            raise ValueError("Step condition conflicts with current Request: " + name)
    geometry_id, geometry, geometry_bytes = _initial_geometry(store, run, request, system_id)
    policy = store.run_configuration(run).execution.native_input_policy
    relations = [scope.get("geometry_relation") for scope in (
        request.conditions, system.conditions if system else {}, step.parameters.conditions,
        *(goal.conditions for goal in related_goals))]
    candidate_binding = {}
    if (policy.allow_task_geometry and step.tool == "python.execute"
            and not any(relation is not None for relation in relations)):
        from orca_agent.orca.native import _xyz_file

        count = _xyz_file(geometry_bytes)
        candidate_binding = {"allow_task_geometry": True,
            "initial_atom_symbols": [line.split()[0] for line in
                geometry_bytes.decode("ascii").splitlines()[2:2 + count]]}
    workflow = ({"workflow_policy": "simplified-1"}
                if simplified else {})
    if simplified and len(environments) > 1:
        workflow["allowed_condition_values"] = {"environment": sorted(environments)}
    return {"request_id": request.id, "request_version": request.version,
            "step_id": step.id, "system_id": system_id, "conditions": conditions,
            "condition_sources": condition_sources, "geometry_artifact_id": geometry_id,
            "geometry_sha256": geometry.sha256, **candidate_binding, **workflow}


def launch_orca(store, run, step, config, prepared, workdir, root, *,
                on_started, job_name=None, parent_job_name=None, fault=None, deadline=None,
                boundary=None, execution_handle=None, on_prepared=None, parent_handle=None,
                on_actual_started=None):
    """The sole ORCA process-launch path for OPI and native input adapters."""
    if store.run_configuration(run).execution.backend == "linux-container":
        return _launch_container_orca(
            store, run, step, config, prepared, workdir, root, on_started=on_started,
            execution_handle=execution_handle, on_prepared=on_prepared,
            parent_handle=parent_handle, boundary=boundary, deadline=deadline, fault=fault,
            on_actual_started=on_actual_started)
    from orca_agent.natural import ensure_scientific_environment
    from orca_agent.store import sha256_file

    owned_boundary = boundary is None
    session = None
    try:
        resources = prepared["resources"]
        contract = get_tool("orca.run").check_contract
        if (resources["cores"] > 1
                and store.run_configuration(run).execution.backend not in contract["parallel_execution"]):
            raise ValueError("parallel_execution_unavailable: " + contract["parallel_execution_reason"])
        ensure_scientific_environment(store, config, run, require_mpi=resources["cores"] > 1)
        environment = json.loads(store.path(f"runs/{run.id}/environment.json").read_text("utf-8"))
        if (str(config.orca_path) != environment["orca"]["path"]
                or sha256_file(config.orca_path) != environment["orca"]["sha256"]):
            raise ValueError("ORCA installation differs from the frozen environment")
        boundary = boundary or _prepare_native_boundary(config, root, workdir)
        session, staged = boundary["session"], boundary["staged"]
        if boundary["workdir"] != workdir:
            raise ValueError("native work directory differs from its pre-staged boundary")
        if staged["manifest"]["files"]["orca.exe"]["sha256"] != environment["orca"]["sha256"]:
            raise ValueError("staged ORCA differs from the frozen executable")
        if resources["cores"] > 1:
            mpi = staged["manifest"]["files"].get("mpiexec.exe")
            if (mpi is None or mpi["sha256"] != environment.get("mpi", {}).get("sha256")
                    or str(config.mpi_path) != environment.get("mpi", {}).get("path")):
                raise ValueError("parallel runtime differs from the frozen MPI executable")
        _save_json(root / "runtime-manifest.json", staged["manifest"])
        child_env = clean_environment(staged["executable"].parent, workdir)
        _control(store, run, step)
    except Exception as exc:
        if session and owned_boundary:
            session.close()
        return {"state": "failed", "reason": f"prelaunch_rejected: {exc}",
                "not_started": True, "handle": None, "exit_code": None, "resource_usage": {}}
    control_lock = store.control_lock(run.id)
    locked = False

    def hook(point):
        nonlocal locked
        if point == "after_resumed" and locked:
            control_lock.release()
            locked = False
        if fault:
            fault(point)

    def started(handle):
        on_started(handle)
        _control(store, run, step)

    try:
        control_lock.acquire()
        locked = True
        _control(store, run, step)
        outcome = local.run_managed(
            staged["executable"], [Path(prepared["input_path"]).name], workdir,
            cores=resources["cores"], total_memory_mb=resources["memory_mb"],
            timeout_s=min(resources["timeout_seconds"], (run.deadline - utc_now()).total_seconds(),
                          deadline - time.monotonic() if deadline is not None else float("inf")),
            on_started=started, job_name=job_name, parent_job_name=parent_job_name,
            argv0=os.path.relpath(staged["executable"], workdir),
            cancel_requested=lambda: store.read_signal(run.id) == "cancel",
            environment=child_env, isolation=session.policy(allow_children=True), fault=hook,
        )
    finally:
        if locked:
            control_lock.release()
        if owned_boundary:
            session.close()
    return outcome


def _collection_limits(store, run):
    return {"count": run.budget.artifact_count, "file_bytes": run.budget.artifact_bytes,
            "total_bytes": run.budget.artifact_total_bytes,
            "seconds": store.run_configuration(run).resources.collection_timeout_seconds}


def record_actual_start(store, run, attempt, handle):
    """Persist positive exec evidence without rewriting the earlier gated identity."""
    previous = attempt.execution_handle or {}
    proof = handle.get("actual_start_evidence", {})
    if (handle.get("backend") != "linux-container" or handle.get("launch_phase") != "executed"
            or proof.get("executable") != "/opt/orca/orca"
            or any(previous.get(key) != handle.get(key) for key in ("container_id", "launch_id"))
            or any(previous.get("workload_identity", {}).get(key) != proof.get(key)
                   for key in ("pid", "start_ticks"))):
        raise ValueError("actual start differs from the persisted gated workload")
    _save_json(store.path(attempt.directory) / "actual-start.json", handle)
    attempt.execution_handle = handle
    store.update_lease_handle(run.id, attempt.id, handle)
    store.save_run(run)


def _retain_input_copies(workdir):
    """Keep staging bytes separate from subsequently collected mutable outputs."""
    source = workdir.resolve(strict=True)
    parent = source.parent.resolve(strict=True)
    target = parent / (source.name + "-inputs")
    if source.parent != parent or target.resolve().parent != parent or target.exists():
        raise ValueError("input snapshot destination is not a fresh sibling")
    source.rename(target)
    source.mkdir()


def _launch_container_orca(store, run, step, config, prepared, workdir, root, *,
                           on_started, execution_handle, on_prepared, parent_handle,
                           boundary, deadline, fault, on_actual_started):
    """The Linux execution adapter; input admission and budgets remain above it."""
    from orca_agent.backends import container
    from orca_agent.natural import ensure_scientific_environment

    configuration = store.run_configuration(run)
    invoked = False
    try:
        ensure_scientific_environment(store, config, run)
        _control(store, run, step)
        timeout = min(prepared["resources"]["timeout_seconds"],
                      (run.deadline - utc_now()).total_seconds(),
                      deadline - time.monotonic() if deadline is not None else float("inf"))
        if boundary is None:
            invoked = True
            boundary = container.prepare_boundary(
                configuration.execution, execution_handle, local_root=root,
                resources=configuration.resources, timeout_s=timeout,
                on_prepared=on_prepared, fault=fault)
        def checkpoint():
            container.check_active(boundary)
        container.stage(boundary, "orca", {
            path.relative_to(workdir).as_posix(): path
            for path in collect_paths(workdir, count=run.budget.artifact_count)},
            checkpoint=checkpoint)
        _retain_input_copies(workdir)
        lock = store.control_lock(run.id)
        locked = False

        def hook(point):
            nonlocal locked
            if point == "after_resumed" and locked:
                lock.release()
                locked = False
            if fault:
                fault(point)

        def started(handle):
            on_started(handle)
            _control(store, run, step)

        try:
            lock.acquire()
            locked = True
            _control(store, run, step)
            invoked = True
            outcome = container.run_managed(
                boundary, kind="orca", local_workdir=workdir, timeout_s=timeout,
                on_started=started, cancel_requested=lambda: store.read_signal(run.id) == "cancel",
                on_actual_started=on_actual_started,
                parent_handle=parent_handle, fault=hook)
        finally:
            if locked:
                lock.release()
        if outcome["state"] != "unknown":
            try:
                container.collect(boundary, "orca", workdir,
                                  limits=_collection_limits(store, run),
                                  checkpoint=checkpoint if parent_handle and (
                                      configuration.workflow_policy != "simplified-1"
                                      or boundary["running"]) else None)
            except Exception as exc:
                outcome.update(state="failed", reason=f"container_collection_error: {exc}")
                outcome["collection_error"] = str(exc)
            if not parent_handle:
                reconciliation = container.reconcile(boundary["handle"])
                if reconciliation["state"] != "terminated":
                    outcome.update(state="unknown", reason="container_collection_termination_unconfirmed")
                    outcome["collection_reconciliation"] = reconciliation
        return outcome
    except Exception as exc:
        # A missing create/exec receipt does not prove absence of a workload.
        return {"state": "unknown" if invoked else "failed", "reason": str(exc),
                "not_started": not invoked, "handle": execution_handle if invoked else None,
                "exit_code": None, "resource_usage": {}}


def _prepare_native_boundary(config, root, workdir):
    """Prepare before task Python starts so expensive copies cannot stall its monitor."""
    from orca_agent.backends.runtime_files import stage_orca_runtime

    workdir.mkdir(parents=True, exist_ok=True)
    staged = stage_orca_runtime(root / "runtime", config.orca_path, mpi_path=config.mpi_path)
    session = AppContainerSession(root)
    try:
        session.grant_read(root / "runtime")
        session.grant_write(workdir)
    except Exception:
        session.close()
        raise
    return {"staged": staged, "session": session, "workdir": workdir}


def _archive(store, run, attempt, folder, prefix, artifacts=None, *, checkpoint=None):
    artifacts = {} if artifacts is None else artifacts
    seconds = store.archive_timeout(run)
    if store.run_configuration(run).workflow_policy != "simplified-1":
        seconds = min(seconds, max(0, (run.deadline - utc_now()).total_seconds()))
    deadline = time.monotonic() + seconds

    def archive_checkpoint():
        if checkpoint:
            checkpoint()
        if time.monotonic() >= deadline:
            raise TimeoutError("task artifact collection deadline exhausted")

    for path in collect_paths(folder, count=run.budget.artifact_count):
        archive_checkpoint()
        name = prefix + path.relative_to(folder).as_posix()
        artifact = store.import_artifact(path, "raw_evidence", run_id=run.id,
                                         attempt_id=attempt.id, source={"task_path": name},
                                         checkpoint=archive_checkpoint)
        artifacts[name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
    return artifacts


def _collect_sdk_errors(store, run, attempt, root, files, diagnostics):
    """Expose only bounded error data from the coordinator's response mailbox."""
    from orca_agent.llm import _has_credential
    from orca_agent.task_sdk import MAX_RESPONSE_BYTES

    responses = root / "responses"
    if not responses.is_dir():
        return
    paths = sorted(responses.glob("response-*.json"))
    legacy = responses / "response.json"
    if legacy.is_file():
        paths.insert(0, legacy)
    retained, rejected, unreadable = 0, 0, 0
    for path in paths[:32]:
        if not path.resolve().is_relative_to(root.resolve()):
            unreadable += 1
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("SDK response exceeds its transport bound")
            response = json.loads(raw)
        except (ValueError, OSError):
            unreadable += 1
            continue
        error = response.get("error") if isinstance(response, dict) else None
        if not isinstance(error, str) or not error.strip():
            continue
        rejected += 1
        if retained >= 8:
            continue
        detail = "[redacted credential-bearing SDK error]" if _has_credential(error) else error
        encoded = detail.encode("utf-8")
        truncated = len(encoded) > 2048
        if truncated:
            detail = encoded[:2037].decode("utf-8", errors="ignore") + "[truncated]"
        projection = {"error": detail, "detail_truncated": truncated,
                      "raw_response_sha256": hashlib.sha256(raw).hexdigest(),
                      "classification": "bounded_error_only_projection"}
        target = root / "sdk_diagnostics" / path.name
        if target.exists():
            if json.loads(target.read_bytes()) != projection:
                raise ValueError("Archived SDK error projection changed")
        else:
            _save_json(target, projection)
        artifact = store.import_artifact(target, "execution_record", run_id=run.id, attempt_id=attempt.id,
            source={"sdk_response_name": path.name, "raw_response_sha256": projection["raw_response_sha256"],
                    "projection": "bounded_error_only"})
        files["sdk_responses/" + path.name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        diagnostics.append({"category": "managed_sdk_request_rejected", "detail": detail,
            "response_name": path.name, "artifact_id": artifact.id, "sha256": artifact.sha256,
            "raw_response_sha256": projection["raw_response_sha256"], "detail_truncated": truncated})
        retained += 1
    if rejected > retained or len(paths) > 32 or unreadable:
        diagnostics.append({"category": "managed_sdk_diagnostics_bounded", "retained_errors": retained,
            "additional_scanned_errors": rejected - retained, "unreadable_responses": unreadable,
            "unscanned_response_files": max(0, len(paths) - 32)})


def _collect_internal_failures(store, run, attempt, diagnostics):
    """Project settled child failures from registered evidence, never task assertions."""
    from orca_agent.llm import _has_credential

    failed = [receipt for receipt in run.internal_calls
              if receipt.parent_attempt_id == attempt.id
              and receipt.state in {"failed", "timed_out", "cancelled"}]
    for receipt in reversed(failed[-4:]):
        diagnostic = {"category": "internal_orca_failed", "request_key": receipt.request_key,
                      "operation_status": receipt.state, "evidence": []}
        for artifact_id in receipt.artifact_ids:
            artifact = store.load_artifact(artifact_id)
            name = artifact.source.get("task_path")
            if name not in {"stdout.out", "stderr.txt"}:
                continue
            if artifact.run_id != run.id or artifact.attempt_id != attempt.id:
                diagnostic["evidence_error"] = "internal log is not owned by this Attempt"
                continue
            reference = {"filename": name, "artifact_id": artifact.id, "sha256": artifact.sha256}
            try:
                # The archive remains authoritative. Only its verified bounded
                # tail enters feedback, alongside the directly readable identity.
                path = store.artifact_path(artifact.id)
                with path.open("rb") as stream:
                    offset = max(0, artifact.size - 2048)
                    stream.seek(offset)
                    raw = stream.read(2048)
                text = raw.decode("utf-8", errors="replace")
                if _has_credential(text):
                    text = "[redacted credential-bearing internal error]"
                reference.update({"text": text, "byte_start": offset,
                                  "truncated": offset > 0, "total_bytes": artifact.size})
            except (ValueError, OSError) as exc:
                # Keep the verified identity discoverable without claiming its
                # damaged/missing content was read successfully.
                reference["read_error"] = type(exc).__name__
            diagnostic["evidence"].append(reference)
            if len(diagnostic["evidence"]) == 2:
                break
        diagnostics.append(diagnostic)
    if len(failed) > 4:
        diagnostics.append({"category": "internal_orca_failures_bounded",
                            "shown": 4, "total": len(failed),
                            "omitted": len(failed) - 4})


def collect_result(store, run, step, attempt, outcome):
    """Keep actual files; no fixed JSON requirement or implicit scientific parser."""
    root = store.path(attempt.directory)
    files, diagnostics = {}, []
    if outcome.get("collection_error"):
        diagnostics.append({"category": "collection_error", "detail": outcome["collection_error"]})
    if outcome["state"] != "unknown":
        try:
            _collect_sdk_errors(store, run, attempt, root, files, diagnostics)
            for folder in ("sealed", "task", "checkpoints"):
                if (root / folder).exists():
                    _archive(store, run, attempt, root / folder, folder + "/", files)
            # Coordinator records are outside every task-writable ACL tree.
            for path in root.glob("*.json"):
                artifact = store.import_artifact(path, "execution_record", run_id=run.id,
                                                 attempt_id=attempt.id)
                files[path.name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
            for receipt in run.internal_calls:
                if receipt.parent_attempt_id == attempt.id:
                    internal = root / "internal" / receipt.request_key
                    prepared_path = internal / "prepared.json"
                    prepared = json.loads(prepared_path.read_bytes()) if prepared_path.exists() else {}
                    raw_task = (Path(prepared["input_path"]).parent
                                if prepared.get("input_path") else internal / "task")
                    if not raw_task.resolve().is_relative_to(root.resolve()):
                        raise ValueError("internal raw evidence escapes its frozen Attempt")
                    if not receipt.artifact_ids and raw_task.is_dir():
                        _archive(store, run, attempt, raw_task,
                                 f"internal/{receipt.request_key}/task/", files)
                        for path in internal.glob("*.json"):
                            artifact = store.import_artifact(path, "execution_record", run_id=run.id,
                                                             attempt_id=attempt.id)
                            files[f"internal/{receipt.request_key}/{path.name}"] = {
                                "artifact_id": artifact.id, "sha256": artifact.sha256}
                    for artifact_id in receipt.artifact_ids:
                        artifact = store.load_artifact(artifact_id)
                        # Keep common original log/input names discoverable.
                        # The Artifact identity and hash remain authoritative;
                        # coordinator records and other names retain their IDs.
                        name = artifact.source.get("task_path")
                        key = f"internal/{receipt.request_key}/{artifact_id}"
                        if name in {"stdout.out", "stderr.txt", "job.inp"}:
                            key = f"internal/{receipt.request_key}/task/{name}"
                            if key in files and files[key]["artifact_id"] != artifact_id:
                                key = f"internal/{receipt.request_key}/{artifact_id}"
                        files[key] = {
                            "artifact_id": artifact_id, "sha256": artifact.sha256}
            _collect_internal_failures(store, run, attempt, diagnostics)
        except Exception as exc:
            diagnostics.append({"category": "collection_error", "detail": str(exc)})
    else:
        diagnostics.append({"category": "execution_unknown", "detail": outcome.get("reason")})
    if outcome["state"] != "completed":
        diagnostics.append({"category": outcome["state"], "detail": outcome.get("reason")})
    reference = getattr(step.parameters, "code", None) or getattr(step.parameters, "input", None)
    observation_records = []
    if getattr(step.parameters, "code", None) and outcome["state"] != "unknown":
        from orca_agent.task_observations import collect
        try:
            observation_records = collect(store, run, step, attempt, files, diagnostics=diagnostics)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
            diagnostics.append({"category": "observation_manifest_rejected", "detail": str(exc)})
    return Result(run_id=run.id, step_id=step.id, attempt_id=attempt.id,
                  operation_status=outcome["state"], artifact_ids=[v["artifact_id"] for v in files.values()],
                  diagnostics=diagnostics, observation_records=observation_records,
                  observations={"execution_completed": outcome["state"] == "completed",
                                "evidence_collection_complete": not any(
                                    item["category"] in {"collection_error", "execution_unknown"}
                                    for item in diagnostics),
                                "scientific_validation": "unverified"},
                  source={"files": files, "execution": outcome,
                          "code_sha256": reference.sha256 if reference else None,
                          "request_version": attempt.request_version,
                          "plan_version": attempt.plan_version,
                          "permission_version": attempt.permission_version})


class _Broker:
    """Sequential mailbox handling in the existing coordinator, never a child Agent."""

    def __init__(self, store, run, step, attempt, config, session, requests, responses, deadline,
                 native_boundary=None, container_boundary=None):
        self.store, self.run, self.step, self.attempt = store, run, step, attempt
        self.config, self.session = config, session
        self.requests, self.responses = requests, responses
        self.sequence = 1
        self.legacy_handled = False
        self.deadline = deadline
        self.native_boundary = native_boundary
        self.container_boundary = container_boundary

    def poll(self, parent_handle):
        from orca_agent.task_sdk import MAX_EXCHANGES

        if self.legacy_handled or self.sequence > MAX_EXCHANGES:
            return
        sequence = self.sequence
        name = f"request-{sequence:06d}.json"
        legacy = False

        def read(name):
            if self.container_boundary:
                from orca_agent.backends import container
                return container.read_request(self.container_boundary, max_bytes=MAX_REQUEST_BYTES, name=name)
            target = self.requests / name
            return read_mailbox(target, MAX_REQUEST_BYTES) if target.exists() else None

        try:
            data = read(name)
            if data is None and sequence == 1:
                data = read("request.json")
                legacy = data is not None
            if data is None:
                return
            # Claim this transport slot before admission. A rejected message cannot be replayed.
            self.sequence += 1
            self.legacy_handled = legacy
            value = json.loads(data)
            if (legacy and value.get("version") != 1) or (not legacy and value.get("version") != 2):
                raise ValueError("SDK protocol does not match its mailbox")
            response = self.execute(value, parent_handle)
        except Exception as exc:
            if self.sequence == sequence:
                self.sequence += 1
            response = {"error": f"managed_sdk_request_rejected: {exc}"}
        response_name = "response.json" if legacy else f"response-{sequence:06d}.json"
        _save_json(self.responses / response_name, response)
        if self.container_boundary:
            from orca_agent.backends import container
            container.publish_response(self.container_boundary, response,
                {name: Path(path) for name, path in response.get("files", {}).items()},
                sequence=None if legacy else sequence)

    def _checkpoint(self, value):
        if (set(value) != {"version", "kind", "request_key", "state", "requires_decision", "reason"}
                or type(value["requires_decision"]) is not bool
                or not isinstance(value["state"], dict)
                or (value["reason"] is not None and not isinstance(value["reason"], str))):
            raise ValueError("invalid bounded task checkpoint")
        checkpoint = self.store.save_task_checkpoint(
            self.run, self.attempt, value["request_key"], value["state"],
            completed_request_keys=[item.request_key for item in self.run.internal_calls
                                    if item.state == "completed"],
            requires_decision=value["requires_decision"], reason=value["reason"])
        return {"request_key": value["request_key"], "input_fingerprint": fingerprint(value),
                "checkpoint_sha256": checkpoint.sha256,
                "completed_request_keys": checkpoint.completed_request_keys,
                "requires_decision": checkpoint.requires_decision}

    def _reply(self, receipt, parent_handle):
        from orca_agent.store import sha256_file

        if not receipt.response:
            raise ValueError("completed historical receipt has no pinned replay response")
        response = dict(receipt.response)
        output = self.responses / f"files-{self.sequence:06d}"
        output.mkdir()
        files = {}
        for name, reference in response.get("files", {}).items():
            # Saved names passed archive's portable path checks; validate again on consumption.
            from orca_agent.backends.container import _relative
            _relative(name)
            source = self.store.artifact_path(reference["artifact_id"])
            if sha256_file(source) != reference["sha256"]:
                raise ValueError("completed receipt raw evidence hash changed")
            target = output / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                while block := reader.read(1024 * 1024):
                    self._active(parent_handle)
                    writer.write(block)
            if sha256_file(target) != reference["sha256"]:
                raise ValueError("receipt copy differs from original evidence")
            files[name] = str(target)
        if self.session is not None:
            self.session.grant_read(output)
        return {**response, "files": files, "source_attempt_id": receipt.parent_attempt_id}

    def _active(self, parent_handle):
        if self.container_boundary:
            from orca_agent.backends import container
            container.check_active(self.container_boundary)
        else:
            local.check_active_job(parent_handle["job_name"])

    def execute(self, value, parent_handle):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("script deadline exhausted before SDK admission")
        if not isinstance(value, dict) or type(value.get("version")) is not int:
            raise ValueError("invalid bounded SDK request")
        if value["version"] == 2 and value.get("kind") == "checkpoint":
            return self._checkpoint(value)
        if getattr(self.step.parameters, "sdk_orca", None) is False:
            raise ValueError("sdk_orca=false declares analysis only; revise the Plan with "
                             "sdk_orca=true and a current initial geometry before requesting ORCA")
        old_keys = {"version", "request_key", "input", "dependencies"}
        new_keys = old_keys | {"kind", "purpose", "retry_of"}
        if (not ((value["version"] == 1 and set(value) == old_keys)
                 or (value["version"] == 2 and set(value) == new_keys and value["kind"] == "orca"))
                or not isinstance(value["input"], str) or not isinstance(value["dependencies"], list)
                or len(value["dependencies"]) > 32):
            raise ValueError("invalid bounded SDK request")
        if self.native_boundary is None:
            raise ValueError("ORCA SDK boundary was not authorized/prepared before task execution")
        if any(item.source_attempt_id == self.attempt.id and item.requires_decision
               for item in self.run.task_checkpoints):
            raise ValueError("task checkpoint requires a coordinator decision before more computation")
        from orca_agent.models import InternalCall
        purpose, retry_of = value.get("purpose", "new_candidate"), value.get("retry_of")
        digest = fingerprint(value)
        InternalCall(request_key=value["request_key"], input_fingerprint=digest, state="reserved",
                     purpose=purpose, retry_of=retry_of)
        _control(self.store, self.run, self.step)
        self._active(parent_handle)
        existing = next((item for item in self.run.internal_calls
                         if item.request_key == value["request_key"]), None)
        if existing:
            if existing.input_fingerprint != digest:
                raise ValueError("internal request key was reused with changed input")
            if existing.state != "completed":
                raise ValueError("internal request already exists; reconcile or inspect its receipt")
            return self._reply(existing, parent_handle)

        def checkpoint():
            self._active(parent_handle)

        root = self.store.path(self.attempt.directory)
        sealed = root / "sealed" / "sdk" / f"{value['request_key']}-{self.sequence:06d}"
        sealed.mkdir(parents=True)
        source = sealed / "native.inp"
        source.write_text(value["input"], encoding="utf-8")
        code = self.store.import_artifact(source, "native_input", run_id=self.run.id,
                                          attempt_id=self.attempt.id, checkpoint=checkpoint)
        entries = []
        for index, dependency in enumerate(value["dependencies"]):
            if not isinstance(dependency, dict) or set(dependency) != {"target_path", "content_base64"}:
                raise ValueError("invalid dependency transport")
            content = base64.b64decode(dependency["content_base64"], validate=True)
            dep = sealed / f"dependency-{index}"
            dep.write_bytes(content)
            artifact = self.store.import_artifact(dep, "native_dependency", run_id=self.run.id,
                                                  attempt_id=self.attempt.id, checkpoint=checkpoint)
            entries.append(InputManifestEntry(artifact_id=artifact.id, sha256=artifact.sha256,
                                               target_path=dependency["target_path"], purpose="native_dependency"))
        parameters = NativeOrcaParameters(input=CodeRef(artifact_id=code.id, sha256=code.sha256,
                                                        language="orca"),
                                          inputs=InputManifest(entries=entries))
        internal_root = root / "internal" / value["request_key"]
        # Prepare separately from the durable launch intent directory.
        prepare_root = (root / "native" / "task" / value["request_key"]
                        if not self.container_boundary else root / "native" / value["request_key"] / "task")
        configuration = self.store.run_configuration(self.run)
        prepared = prepare_native_input(prepare_root, parameters, self.store.artifact_path,
            configuration.resources, policy=configuration.execution.native_input_policy,
            binding=native_input_binding(self.store, self.run, self.step))
        _control(self.store, self.run, self.step)
        request = self.store.load_request(self.run)
        receipt = self.store.reserve_internal_call(
            self.run, self.attempt, value["request_key"], digest,
            code=parameters.input, inputs=parameters.inputs, purpose=purpose, retry_of=retry_of,
            goal={"request_id": request.id, "request_version": request.version,
                  "step_id": self.step.id, "goals": [goal.model_dump(mode="json") for goal in request.goals]},
            resources=prepared["resources"],
            conditions={"admission_version": prepared["admission_version"],
                        "execution_impact": prepared["execution_impact"],
                        "input_binding": prepared.get("input_binding")},
            work_units=prepared["execution_impact"]["internal_work_units"])
        task = prepare_root
        _save_json(internal_root / "prepared.json", prepared)
        if self.container_boundary:
            self.container_boundary["orca_scope"] = receipt.request_key
        native = {**self.native_boundary, "workdir": task}

        def started(handle):
            self.store.mark_internal_call_started(self.run, self.attempt, receipt.request_key, handle)

        outcome = launch_orca(self.store, self.run, self.step, self.config, prepared, task,
                              internal_root, on_started=started,
                              on_actual_started=started,
                              parent_job_name=parent_handle.get("job_name"), deadline=self.deadline,
                              parent_handle=parent_handle if self.container_boundary else None,
                              execution_handle=parent_handle,
                              boundary=self.container_boundary or native)
        _save_json(internal_root / "execution.json", outcome)
        files = {}
        collection_error = outcome.get("collection_error")
        collection_checkpoint = checkpoint
        if (configuration.workflow_policy == "simplified-1" and self.container_boundary
                and not self.container_boundary["running"] and outcome["state"] != "unknown"):
            # run_managed confirmed termination. Read-only evidence recovery has
            # its own finite deadline, independent of the expired computation.
            collection_deadline = time.monotonic() + self.store.archive_timeout(self.run)

            def collection_checkpoint():
                if time.monotonic() >= collection_deadline:
                    raise TimeoutError("settled internal evidence collection deadline exhausted")
        try:
            if outcome["state"] != "unknown":
                _archive(self.store, self.run, self.attempt, task, "", files,
                         checkpoint=collection_checkpoint)
                for name in ("intent.json", "started.json", "actual-start.json", "prepared.json", "execution.json", "runtime-manifest.json"):
                    path = internal_root / name
                    if path.exists():
                        artifact = self.store.import_artifact(path, "execution_record", run_id=self.run.id,
                                                              attempt_id=self.attempt.id,
                                                              checkpoint=collection_checkpoint)
                        files["record/" + name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        except Exception as exc:
            collection_error = str(exc)
        response = {"request_key": receipt.request_key, "input_fingerprint": receipt.input_fingerprint,
                    "operation_status": outcome["state"], "scientific_validation": "unverified",
                    "actual_conditions": prepared["actual_conditions"],
                    "files": {name: ref for name, ref in files.items() if not name.startswith("record/")}}
        receipt = self.store.finish_internal_call(self.run, self.attempt, receipt.request_key,
            state="failed" if collection_error else outcome["state"],
            artifact_ids=[item["artifact_id"] for item in files.values()],
            actual_conditions={**prepared["actual_conditions"], "collection_error": collection_error},
            resource_usage=outcome["resource_usage"], response=response if not collection_error else {})
        if collection_error:
            raise ValueError(f"ORCA evidence collection incomplete: {collection_error}")
        if outcome["state"] == "unknown":
            raise ValueError("internal ORCA termination is unknown; no raw files exposed")
        return self._reply(receipt, parent_handle)


def _prepare_python_inputs(store, run, step, root, task):
    if getattr(step.parameters, "sdk_orca", None) is True:
        validate_sdk_orca_preconditions(store, run, step)
    if len(step.parameters.inputs.entries) > run.budget.artifact_count:
        raise ValueError("task input count exceeds the frozen artifact budget")
    input_bytes = sum(store.load_artifact(entry.artifact_id).size
                      for entry in step.parameters.inputs.entries)
    if input_bytes > run.budget.artifact_total_bytes:
        raise ValueError("task input copies exceed the frozen byte budget")
    sealed = root / "sealed"
    sealed.mkdir()
    code = read_pinned_input(store.artifact_path(step.parameters.code.artifact_id),
                             step.parameters.code.sha256, limit=MAX_REQUEST_BYTES)
    (sealed / "task.py").write_bytes(code)
    _save_json(sealed / "inputs.json", step.parameters.model_dump(mode="json"))
    for entry in step.parameters.inputs.entries:
        data = read_pinned_input(store.artifact_path(entry.artifact_id), entry.sha256,
                                 limit=run.budget.artifact_bytes)
        target = task / entry.target_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return sealed


def _execute_container_python(store, run, step, attempt, config, root, task, fault):
    from orca_agent.backends import container
    from orca_agent.natural import ensure_scientific_environment

    configuration = store.run_configuration(run)
    invoked = False
    boundary = None

    def prepared(handle):
        attempt.execution_handle = handle
        _save_json(root / "boundary.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)

    def started(handle):
        attempt.execution_handle = handle
        attempt.state = "running"
        _save_json(root / "started.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)
        _control(store, run, step)

    try:
        _control(store, run, step)
        ensure_scientific_environment(store, config, run)
        sealed = _prepare_python_inputs(store, run, step, root, task)
        timeout = min(configuration.resources.script_timeout_seconds,
                      (run.deadline - utc_now()).total_seconds())
        deadline = time.monotonic() + timeout
        invoked = True
        boundary = container.prepare_boundary(
            configuration.execution, attempt.execution_handle, local_root=root,
            resources=configuration.resources, timeout_s=timeout,
            on_prepared=prepared, fault=fault)

        def checkpoint():
            container.check_active(boundary)

        container.stage(boundary, "sealed", {"task.py": sealed / "task.py"}, checkpoint=checkpoint)
        container.stage(boundary, "python", {
            path.relative_to(task).as_posix(): path
            for path in collect_paths(task, count=run.budget.artifact_count)}, checkpoint=checkpoint)
        _retain_input_copies(task)
        requests, responses = root / "requests", root / "responses"
        requests.mkdir()
        responses.mkdir()
        native = None
        if (run.permission.scientific_execution and "orca.run" in run.permission.allowed_tools
                and getattr(step.parameters, "sdk_orca", None) is not False):
            workdir = root / "native" / "task"
            workdir.mkdir(parents=True)
            native = {"workdir": workdir}
        broker = _Broker(store, run, step, attempt, config, None, requests, responses,
                         deadline, native, container_boundary=boundary)
        lock = store.control_lock(run.id)
        locked = False

        def hook(point):
            nonlocal locked
            if point == "after_resumed" and locked:
                lock.release()
                locked = False
            if fault:
                fault(point)

        try:
            lock.acquire()
            locked = True
            _control(store, run, step)
            outcome = container.run_managed(
                boundary, kind="python", local_workdir=task, timeout_s=timeout,
                on_started=started, on_poll=broker.poll,
                cancel_requested=lambda: store.read_signal(run.id) == "cancel", fault=hook)
        finally:
            if locked:
                lock.release()
        if outcome["state"] != "unknown":
            try:
                container.collect(boundary, "python", task, limits=_collection_limits(store, run))
            except Exception as exc:
                outcome.update(state="failed", reason=f"container_collection_error: {exc}",
                               collection_error=str(exc))
            reconciliation = container.reconcile(boundary["handle"])
            if reconciliation["state"] != "terminated":
                outcome.update(state="unknown", reason="container_collection_termination_unconfirmed")
                outcome["collection_reconciliation"] = reconciliation
    except Exception as exc:
        outcome = {"state": "unknown" if invoked else "failed", "reason": str(exc),
                   "not_started": not invoked, "handle": attempt.execution_handle if invoked else None,
                   "exit_code": None, "resource_usage": {}}
    _save_json(root / "execution.json", outcome)
    _settle_interrupted_calls(store, run, attempt, outcome)
    return collect_result(store, run, step, attempt, outcome), outcome


def _settle_interrupted_calls(store, run, attempt, outcome):
    if outcome["state"] != "unknown":
        for receipt in run.internal_calls:
            if receipt.parent_attempt_id == attempt.id and receipt.state in {"reserved", "running", "unknown"}:
                store.finish_internal_call(run, attempt, receipt.request_key, state="failed",
                    actual_conditions={"termination_basis": "parent_boundary_terminated",
                                       "operation_details": "incomplete_broker_receipt"},
                    resource_usage={"complete": False})


def execute(store, run, step, attempt, config, fault=None):
    """Execute generic Tools in the frozen boundary; no host execution fallback."""
    from orca_agent.backends.runtime_files import stage_python_runtime
    from orca_agent.file_references import resolve_step_files

    root = store.path(attempt.directory)
    task = root / "task"
    task.mkdir()
    resources = store.run_configuration(run).resources
    try:
        file_sources = {}
        resolved = resolve_step_files(store, run, step, sources=file_sources)
        if resolved != attempt.frozen_step:
            raise ValueError("execution files differ from the reserved immutable Attempt")
        if file_sources != attempt.consumption.get("_file_sources", {}):
            raise ValueError("execution file provenance changed after reservation")
    except Exception as exc:
        # File resolution is strictly read-only and precedes any boundary start.
        # A stale source therefore has a known not-started outcome, not an
        # unknown computation that strands the reserved environment lease.
        outcome = {"state": "failed", "reason": f"prelaunch_rejected: {exc}",
                   "not_started": True, "handle": None, "exit_code": None, "resource_usage": {}}
        _save_json(root / "execution.json", outcome)
        return collect_result(store, run, attempt.frozen_step, attempt, outcome), outcome
    step = attempt.frozen_step
    if (store.run_configuration(run).execution.backend == "linux-container"
            and "execute_python" in get_tool(step.tool).effects):
        return _execute_container_python(store, run, step, attempt, config, root, task, fault)
    invoked = False
    stack = ExitStack()

    def started(handle):
        attempt.execution_handle = handle
        attempt.state = "running"
        _save_json(root / "started.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)
        _control(store, run, step)

    def prepared_boundary(handle):
        attempt.execution_handle = handle
        _save_json(root / "boundary.json", handle)
        store.update_lease_handle(run.id, attempt.id, handle)
        store.save_run(run)

    try:
        _control(store, run, step)
        if "execute_python" not in get_tool(step.tool).effects:
            prepared = prepare_native_input(task, step.parameters, store.artifact_path, resources,
                policy=store.run_configuration(run).execution.native_input_policy,
                binding=native_input_binding(store, run, step))
            _save_json(root / "prepared.json", prepared)
            if store.run_configuration(run).execution.native_input_policy is not None:
                store.reserve_native_workload(run, attempt, prepared)
            invoked = True
            outcome = launch_orca(store, run, step, config, prepared, task, root,
                                  on_started=started, job_name=attempt.execution_handle.get("job_name"),
                                  execution_handle=attempt.execution_handle,
                                  on_actual_started=lambda handle: record_actual_start(store, run, attempt, handle),
                                  on_prepared=prepared_boundary, fault=fault)
        else:
            sealed = _prepare_python_inputs(store, run, step, root, task)
            native_boundary = None
            if (run.permission.scientific_execution and "orca.run" in run.permission.allowed_tools
                    and getattr(step.parameters, "sdk_orca", None) is not False
                    and config.orca_path is not None):
                native_boundary = _prepare_native_boundary(config, root / "native", root / "native" / "task")
                stack.callback(native_boundary["session"].close)
                _save_json(root / "native-runtime-manifest.json", native_boundary["staged"]["manifest"])
            staged = stage_python_runtime(root / "runtime", Path(__file__).parents[1] / "task_sdk.py")
            _save_json(root / "runtime-manifest.json", staged["manifest"])
            requests, responses = root / "requests", root / "responses"
            requests.mkdir()
            responses.mkdir()
            with AppContainerSession(root) as session:
                for path in (root / "runtime", sealed, responses):
                    session.grant_read(path)
                for path in (task, requests):
                    session.grant_write(path)
                environment = clean_environment(root / "runtime", task)
                timeout = min(resources.script_timeout_seconds, (run.deadline - utc_now()).total_seconds())
                environment.update(ORCA_TASK_REQUESTS=str(requests), ORCA_TASK_RESPONSES=str(responses),
                                   ORCA_TASK_TIMEOUT_SECONDS=str(timeout))
                broker = _Broker(store, run, step, attempt, config, session, requests, responses,
                                 time.monotonic() + timeout, native_boundary)
                lock = store.control_lock(run.id)
                locked = False

                def hook(point):
                    nonlocal locked
                    if point == "after_resumed" and locked:
                        lock.release()
                        locked = False
                    if fault:
                        fault(point)

                try:
                    lock.acquire()
                    locked = True
                    _control(store, run, step)
                    invoked = True
                    outcome = local.run_managed(staged["executable"], ["-I", str(sealed / "task.py")], task,
                        cores=resources.max_cores, total_memory_mb=resources.total_memory_mb,
                        timeout_s=timeout, environment=environment, isolation=session.policy(),
                        on_started=started, on_poll=broker.poll, job_name=attempt.execution_handle["job_name"],
                        cancel_requested=lambda: store.read_signal(run.id) == "cancel", fault=hook)
                finally:
                    if locked:
                        lock.release()
    except Exception as exc:
        # No retry on a lost backend receipt. Reconciliation owns all uncertainty.
        outcome = {"state": "unknown" if invoked else "failed", "reason": str(exc),
                   "not_started": not invoked, "handle": attempt.execution_handle if invoked else None,
                   "exit_code": None, "resource_usage": {}}
    finally:
        stack.close()
    _save_json(root / "execution.json", outcome)
    _settle_interrupted_calls(store, run, attempt, outcome)
    return collect_result(store, run, step, attempt, outcome), outcome
