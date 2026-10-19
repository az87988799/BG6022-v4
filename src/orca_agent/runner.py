"""Shared initialization, evidence collection and recovery for the single Agent loop."""

import json

from orca_agent.backends import local
from orca_agent.doctor import capture_environment
from orca_agent.models import PermissionSnapshot, utc_now
from orca_agent.store import atomic_write
from orca_agent.structured import prepare_task
from orca_agent.versions import CURRENT_CHECK_VERSION, is_supported_orca_version


def initialize(store, config, spec_path):
    request, plan, budget = prepare_task(spec_path, store)
    environment = capture_environment(config, require_mpi=any(
        step.parameters.cores > 1 for step in plan.steps))
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[request.geometry_artifact_id],
        max_cores=config.resources.max_cores, max_memory_mb=config.resources.total_memory_mb), budget,
        configuration=config.run_configuration())
    atomic_write(store.path(f"runs/{run.id}/environment.json"),
                 (json.dumps(environment, ensure_ascii=False, indent=2) + "\n").encode(),
                 immutable=True)
    return run


def _step_results(store, run):
    candidates = {}
    for attempt in run.attempts:
        if attempt.result_id and attempt.state != "not_started":
            chain = store.result_chain(run, attempt)
            result = chain[-1]
            if result.id != attempt.result_id:
                raise ValueError("unbound replacement result requires explicit resume")
            if result.attempt_id != attempt.id or result.step_id != attempt.step_id:
                raise ValueError("result does not match its bound attempt")
            for artifact_id in result.artifact_ids:
                store.artifact_path(artifact_id)
            candidates.setdefault(attempt.step_id, []).append(result)
    for call in run.calls:
        if call.step_id and call.result_id:
            result = store.load_result(run.id, call.result_id)
            if result.call_id != call.id or result.step_id != call.step_id:
                raise ValueError("result does not match its bound Tool call")
            for artifact_id in result.artifact_ids:
                store.artifact_path(artifact_id)
            candidates.setdefault(call.step_id, []).append(result)
    result_by_step = {}
    for step_id, values in candidates.items():
        selected = run.selected_results.get(step_id)
        if selected:
            values = [value for value in values if value.id == selected]
        if len(values) != 1:
            raise ValueError("multiple results need an explicit consumption binding")
        result_by_step[step_id] = values[0]
    return result_by_step


def _goals(store, run, plan, results):
    from orca_agent.goals import current_goal_evidence
    request = store.load_request(run)
    for goal in request.goals:
        selection = current_goal_evidence(store, run, request, goal, plan, results)
        result = selection["result"]
        output = result.qualified_outputs.get(goal.port) if result else None
        valid = bool(selection["assessment"] and not selection["gaps"]
                     and selection["assessment"]["status"] == "passed")
        if output and not valid:
            mismatch = {"category": "check_rule_mismatch", "goal_id": goal.id,
                        "result_id": result.id, "required_version": goal.minimum_check_version,
                        "observed_versions": sorted({check.rule_version for check in output.checks})}
            if mismatch not in run.diagnostics:
                run.diagnostics.append(mismatch)
        run.goal_status[goal.id] = "satisfied" if valid else "insufficient_evidence"
    satisfied = all(run.goal_status[g.id] == "satisfied" for g in request.goals if g.required)
    run.delivery_status = "complete" if satisfied else "partial"
    return satisfied


def _outcome(store, attempt):
    path = store.path(f"{attempt.directory}/execution.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _settle(store, run, attempt, result, outcome):
    usage = outcome.get("resource_usage", {})
    handle = outcome.get("handle") or {}
    started = bool(handle) and not outcome.get("not_started", False)
    if handle.get("backend") == "linux-container" and attempt.execution_kind == "orca":
        started = bool(handle.get("actual_start_evidence")) and handle.get("launch_phase") == "executed"
    if (outcome["state"] == "unknown" or outcome.get("reconciliation")
            or usage.get("complete") is False):
        run.usage.resource_usage_complete = False
    detected_postprocess = sum((_outcome(store, item) or {}).get("postprocess_starts_detected", 0)
                               for item in run.attempts)
    run.usage.postprocess_starts = max(run.usage.postprocess_starts, detected_postprocess)
    store.finish_attempt(
        run, attempt.id, state=("not_started" if outcome.get("not_started") and outcome.get("control_action") == "pause"
               else outcome["state"]), result_id=result.id,
        elapsed_seconds=max(attempt.elapsed_seconds, usage.get("wall_seconds", 0)),
        cpu_seconds=max(attempt.cpu_seconds, usage.get("user_cpu_seconds", 0) + usage.get("kernel_cpu_seconds", 0)),
        started=started,
        termination_confirmed=outcome["state"] != "unknown",
    )


def _record_reconciliation(store, run, attempt, reconciliation, observed_usage=None):
    """Publish termination proof before collecting a recovery Result; reuse after a crash."""
    relative = f"{attempt.directory}/reconciled.json"
    path = store.path(relative)
    if path.exists():
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if receipt.get("attempt_id") != attempt.id or receipt.get("reconciliation", {}).get("state") != "terminated":
            raise ValueError("invalid durable termination receipt")
        return receipt
    observed_usage = observed_usage or {}
    receipt = {"attempt_id": attempt.id, "reconciliation": reconciliation,
               "observed_at": utc_now().isoformat(),
               "cost": {
                   "elapsed_seconds_lower_bound": max(
                       attempt.elapsed_seconds, observed_usage.get("wall_seconds", 0)),
                   "cpu_seconds_lower_bound": max(
                       attempt.cpu_seconds, observed_usage.get("user_cpu_seconds", 0)
                       + observed_usage.get("kernel_cpu_seconds", 0)),
                   "additional_usage": "unknown after coordinator loss",
               }}
    atomic_write(path, (json.dumps(receipt, indent=2) + "\n").encode(), immutable=True)
    return receipt


def _recover_internal_calls(store, run, attempt):
    """Reconcile durable child identities and collect; never restart task code."""
    from pathlib import Path

    from orca_agent.tools.managed import _archive, _collection_limits

    for call in run.internal_calls:
        if call.parent_attempt_id != attempt.id or call.state not in {"reserved", "running", "unknown"}:
            continue
        handle = call.execution_handle
        if not handle:
            store.record_diagnostic(run, "internal_reconciliation_not_implemented",
                "Internal launch identity is incomplete; raw files, reservation and lease retained. No task replay.")
            return False
        backend = local
        if handle.get("backend") == "linux-container":
            from orca_agent.backends import container
            backend = container
        reconciliation = backend.reconcile(handle)
        if reconciliation["state"] != "terminated":
            store.record_diagnostic(run, "internal_reconciliation_required", reconciliation["reason"])
            return False
        root = store.path(f"{attempt.directory}/internal/{call.request_key}")
        receipt_path, prepared_path = root / "execution.json", root / "prepared.json"
        execution = json.loads(receipt_path.read_text(encoding="utf-8")) if receipt_path.exists() else {}
        prepared = json.loads(prepared_path.read_text(encoding="utf-8")) if prepared_path.exists() else {}
        task = Path(prepared["input_path"]).parent if prepared.get("input_path") else root / "task"
        if not task.resolve().is_relative_to(store.path(attempt.directory).resolve()):
            raise ValueError("internal recovery task directory escapes its frozen Attempt")
        files = {}
        try:
            if backend is not local:
                boundary = backend.boundary_from_handle(handle, local_root=root)
                backend.collect(boundary, "orca", task, limits=_collection_limits(store, run))
                collection_state = backend.reconcile(handle)
                if collection_state["state"] != "terminated":
                    store.record_diagnostic(run, "internal_collection_reconciliation_required",
                                             collection_state["reason"])
                    return False
            if task.exists():
                _archive(store, run, attempt, task, "", files)
            proof_path = root / "reconciled.json"
            if proof_path.exists():
                proof = json.loads(proof_path.read_text(encoding="utf-8"))
                if (proof.get("request_key") != call.request_key or proof.get("attempt_id") != attempt.id
                        or proof.get("reconciliation", {}).get("state") != "terminated"):
                    raise ValueError("internal durable reconciliation identity differs")
            else:
                store._write_json(f"{attempt.directory}/internal/{call.request_key}/reconciled.json",
                    {"attempt_id": attempt.id, "request_key": call.request_key,
                     "reconciliation": reconciliation, "additional_usage": "unknown after coordinator loss"},
                    immutable=True)
            for name in ("intent.json", "started.json", "actual-start.json", "prepared.json", "execution.json", "reconciled.json"):
                path = root / name
                if path.exists():
                    artifact = store.import_artifact(path, "execution_record", run_id=run.id, attempt_id=attempt.id)
                    files["record/" + name] = {"artifact_id": artifact.id, "sha256": artifact.sha256}
        except (ValueError, OSError, RuntimeError) as exc:
            store.record_diagnostic(run, "internal_collection_recovery_required", str(exc)[:1000])
            return False
        state = execution.get("state")
        complete = state in {"completed", "failed", "cancelled", "timed_out"}
        state = state if complete else "failed"
        conditions = dict(prepared.get("actual_conditions", {}))
        if not complete:
            conditions["termination_basis"] = "coordinator_lost; exact internal boundary terminated"
            run.usage.resource_usage_complete = False
        usage = execution.get("resource_usage", {}) if complete else {"complete": False}
        response = {"request_key": call.request_key, "input_fingerprint": call.input_fingerprint,
            "operation_status": state, "scientific_validation": "unverified",
            "actual_conditions": conditions,
            "files": {name: reference for name, reference in files.items() if not name.startswith("record/")}}
        # Unknown cost is a durable fact before the guarded child settlement;
        # that guard compares the exact Run snapshot, including usage flags.
        store.save_run(run)
        store.finish_internal_call(run, attempt, call.request_key, state=state,
            artifact_ids=[entry["artifact_id"] for entry in files.values()],
            actual_conditions=conditions, resource_usage=usage, response=response)
    return True


def _recover_task_checkpoints(store, run, attempt):
    """Reattach a bounded saved checkpoint after its Run publication was interrupted."""
    from orca_agent.backends.task_files import collect_paths, regular_path
    from orca_agent.models import TaskCheckpoint

    if attempt.execution_kind != "python" or attempt.frozen_step is None:
        return
    directory = store.path(f"{attempt.directory}/checkpoints")
    if not directory.exists():
        return
    regular_path(directory, store.path(attempt.directory))
    complete = {item.request_key: item for item in run.internal_calls if item.state == "completed"}
    known = {(item.source_attempt_id, item.name): item for item in run.task_checkpoints}
    additions = []
    for path in sorted(collect_paths(directory, count=256, depth=0)):
        if path.suffix != ".json" or path.stat().st_size > 256 * 1024:
            raise ValueError("checkpoint recovery requires bounded JSON records")
        checkpoint = TaskCheckpoint.model_validate_json(path.read_bytes())
        if (checkpoint.source_attempt_id != attempt.id or path.stem != checkpoint.name
                or checkpoint.code_sha256 != attempt.frozen_step.parameters.code.sha256
                or not set(checkpoint.completed_request_keys) <= complete.keys()):
            raise ValueError("checkpoint recovery differs from its original Attempt or completed receipts")
        for key in checkpoint.completed_request_keys:
            for artifact_id in complete[key].artifact_ids:
                store.artifact_path(artifact_id)
        previous = known.get((attempt.id, checkpoint.name))
        if previous and previous != checkpoint:
            raise ValueError("checkpoint recovery cannot rewrite published task state")
        if previous is None:
            additions.append(checkpoint)
    if additions:
        if len(run.task_checkpoints) + len(additions) > 256:
            raise ValueError("retained checkpoint recovery limit exhausted")
        run.task_checkpoints.extend(additions)
        store.save_run(run)


def _recover(store, config, run, plan):
    import importlib

    from orca_agent.tools.registry import get_tool

    for attempt in run.attempts:
        chain = store.result_chain(run, attempt)
        current = chain[-1] if chain else None
        outcome = _outcome(store, attempt)
        lease = store.environment_lease()
        owns_lease = bool(lease and (lease["run_id"], lease["attempt_id"]) == (run.id, attempt.id))
        needs_collection = current is None or current.operation_status == "unknown"
        # A settled attempt can still have a replacement saved before its Run reference.
        if (attempt.finished_at and not needs_collection and not owns_lease
                and current.id == attempt.result_id):
            _recover_task_checkpoints(store, run, attempt)
            continue
        if not attempt.finished_at and attempt.state not in ("intent", "running", "unknown"):
            continue
        # A saved recovery result is reattached before any new archive is generated.
        # Its explicit chain and all archived hashes have already been checked.
        recovered_outcome = current.source.get("execution", {}) if current else {}
        if recovered_outcome.get("reconciliation", {}).get("state") == "terminated":
            outcome = recovered_outcome
        uncertain_receipt = not outcome or outcome["state"] == "unknown"
        if uncertain_receipt or (owns_lease and recovered_outcome.get("reconciliation")):
            handle_path = store.path(f"{attempt.directory}/started.json")
            actual_path = store.path(f"{attempt.directory}/actual-start.json")
            handle = (json.loads(handle_path.read_text(encoding="utf-8")) if handle_path.exists()
                      else attempt.execution_handle or {})
            if actual_path.exists():
                actual = json.loads(actual_path.read_text(encoding="utf-8"))
                proof = actual.get("actual_start_evidence", {}) if isinstance(actual, dict) else {}
                identity = handle.get("workload_identity", {})
                actual_identity = actual.get("workload_identity", {}) if isinstance(actual, dict) else {}
                if (attempt.execution_kind != "orca" or not handle_path.exists()
                        or handle.get("backend") != "linux-container"
                        or not isinstance(actual, dict) or actual.get("backend") != "linux-container"
                        or actual.get("launch_phase") != "executed"
                        or not isinstance(proof, dict) or not isinstance(actual_identity, dict)
                        or proof.get("executable") != "/opt/orca/orca"
                        or proof.get("method") != "proc_exe_inode_and_pid_start_ticks"
                        or any(not handle.get(key) or handle.get(key) != actual.get(key) for key in (
                            "container_id", "launch_id", "image_id", "manifest_sha256",
                            "engine_id", "run_id", "attempt_id"))
                        or actual.get("execution") != handle.get("execution")
                        or any(identity.get(key) is None or identity.get(key) != actual_identity.get(key)
                               or identity.get(key) != proof.get(key) for key in ("pid", "start_ticks"))):
                    raise ValueError("actual-start receipt differs from its persisted gated ORCA identity")
                handle = actual
            if handle.get("backend") == "linux-container":
                from orca_agent.backends import container
                reconciliation = container.reconcile(handle)
            else:
                reconciliation = local.reconcile(handle)
            if reconciliation["state"] != "terminated":
                if not attempt.finished_at:
                    attempt.state = "unknown"
                run.state = "unknown"
                run.usage.resource_usage_complete = False
                store.record_diagnostic(run, "reconciliation_required", reconciliation["reason"])
                return False
            if uncertain_receipt:
                previous = outcome or {}
                outcome = {**previous, "state": "failed", "exit_code": None, "handle": handle,
                           "resource_usage": previous.get("resource_usage", {}),
                           "reason": "coordinator_lost; termination confirmed",
                           "reconciliation": reconciliation}
                _record_reconciliation(store, run, attempt, reconciliation, outcome["resource_usage"])
        if needs_collection:
            if attempt.execution_kind == "python" and not _recover_internal_calls(store, run, attempt):
                run.state = "unknown"
                run.usage.resource_usage_complete = False
                return False
            _recover_task_checkpoints(store, run, attempt)
            if attempt.frozen_step:
                step = attempt.frozen_step
            else:
                # Legacy Runs have no revision permission. Their original immutable
                # Plan is the only admissible source; never use a new current Plan.
                version = attempt.plan_version or 1
                historical = store.load_plan_revision(run, version)
                step = next(s for s in historical.steps if s.id == attempt.step_id)
            collector = get_tool(step.tool).check_contract.get(
                "collector", "orca_agent.tools.calculation.collect_result")
            handle = outcome.get("handle") or attempt.execution_handle or {}
            if handle.get("backend") == "linux-container":
                from orca_agent.backends import container
                from orca_agent.tools.managed import _collection_limits
                recorddir = store.path(attempt.directory)
                boundary = container.boundary_from_handle(handle, local_root=recorddir)
                try:
                    container.collect(boundary, "python" if attempt.execution_kind == "python" else "orca",
                                      recorddir / "task", limits=_collection_limits(store, run))
                except Exception as exc:
                    outcome["collection_error"] = f"container recovery collection: {exc}"
                collection_state = container.reconcile(handle)
                if collection_state["state"] != "terminated":
                    run.state = "unknown"
                    run.usage.resource_usage_complete = False
                    store.record_diagnostic(run, "collection_reconciliation_required",
                                             collection_state["reason"])
                    return False
            module, name = collector.rsplit(".", 1)
            result = getattr(importlib.import_module(module), name)(store, run, step, attempt, outcome)
            result.supersedes_result_id = current.id if current else None
            store.save_result(result)
            chain.append(result)
        else:
            result = current
        # Keep the complete chain before the atomic Run publication. An orphan
        # Result from a crash is selected by relation and identity, not timestamp.
        for item in chain:
            if item.id not in run.result_ids:
                run.result_ids.append(item.id)
        if outcome.get("reconciliation"):
            run.usage.resource_usage_complete = False
            proof = result.source.get("files", {}).get("reconciled.json")
            if proof and not any(item.get("category") == "termination_reconciled"
                                 and item.get("attempt_id") == attempt.id for item in run.diagnostics):
                receipt = json.loads(store.artifact_path(proof["artifact_id"]).read_text(encoding="utf-8"))
                run.diagnostics.append({"category": "termination_reconciled", "attempt_id": attempt.id,
                                        "receipt_artifact_id": proof["artifact_id"], **receipt})
        if attempt.finished_at:
            # Old versions could finish an attempt while retaining an empty
            # unknown Result. Supplement its evidence without charging it twice.
            attempt.result_id = result.id
            run.selected_results[attempt.step_id] = result.id
            store.save_run(run)
            if owns_lease:
                store.release_environment(run.id, attempt.id, termination_confirmed=True)
        else:
            _settle(store, run, attempt, result, outcome)
    lease = store.environment_lease()
    if lease and lease["run_id"] == run.id:
        run.state = "unknown"
        store.record_diagnostic(run, "orphan_launch_intent", "Unconfirmed lease retained; no restart")
        return False
    return True


def _validate_execution_rules(store, run):
    request = store.load_request(run)
    if any(goal.minimum_check_version != CURRENT_CHECK_VERSION for goal in request.goals
           if goal.port in ("energy", "optimized_geometry", "dipole_moment")):
        raise ValueError("check_rule_revalidation_required: historical Run may be inspected and recovered; "
                         "scientific continuation requires explicit rule revalidation")
    environment = json.loads(store.path(f"runs/{run.id}/environment.json").read_text(encoding="utf-8"))
    if not is_supported_orca_version(environment.get("orca", {}).get("version")):
        raise ValueError("frozen ORCA version is not enabled for scientific execution")


def execute(store, config, run_id, *, resume=False, fault=None, transport=None, batch=None,
            stop_event=None):
    """Compatibility entry into the one feedback loop, including fixed structured plans."""
    from orca_agent.agent import execute as run_agent
    return run_agent(store, config, run_id, resume=resume, fault=fault,
                     transport=transport, batch=batch, stop_event=stop_event)
