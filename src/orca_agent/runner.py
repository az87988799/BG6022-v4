"""A single fixed-Plan coordinator. No model, scientific parsing or tool-name branches."""

import importlib
import json
import os

import psutil

from orca_agent.backends import local
from orca_agent.doctor import diagnose
from orca_agent.models import PermissionSnapshot, utc_now
from orca_agent.store import BudgetExceeded, EnvironmentBusy, atomic_write, sha256_file
from orca_agent.structured import prepare_task
from orca_agent.tools.registry import get_tool
from orca_agent.versions import CURRENT_CHECK_VERSION, is_supported_orca_version


def initialize(store, config, spec_path):
    request, plan, budget = prepare_task(spec_path, store)
    environment = diagnose(config)
    if (environment["orca"]["compatible"] is not True
            or not is_supported_orca_version(environment["orca"].get("version"))):
        raise ValueError("ORCA installation is missing, incompatible or unverified; run doctor")
    if os.name != "nt":
        raise ValueError("only the explicitly selected Windows local environment is supported")
    environment["orca"]["sha256"] = sha256_file(config.orca_path)
    if any(step.parameters.cores > 1 for step in plan.steps):
        if not config.mpi_path or not config.mpi_path.is_file():
            raise ValueError("parallel input requires the explicit MPI executable")
        environment["mpi"]["sha256"] = sha256_file(config.mpi_path)
    run = store.create_run(request, plan, PermissionSnapshot(
        scientific_execution=True, artifact_ids=[request.geometry_artifact_id]), budget)
    atomic_write(store.path(f"runs/{run.id}/environment.json"),
                 (json.dumps(environment, ensure_ascii=False, indent=2) + "\n").encode(),
                 immutable=True)
    return run


def _step_results(store, run):
    result_by_step = {}
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
            if attempt.step_id in result_by_step:
                raise ValueError("multiple results need an explicit consumption binding")
            result_by_step[attempt.step_id] = result
    return result_by_step


def _goals(store, run, plan, results):
    request = store.load_request(run)
    for goal in request.goals:
        binding = plan.goal_map.get(goal.id)
        result = results.get(binding.step_id) if binding else None
        output = result.qualified_outputs.get(binding.port) if result and binding else None
        valid = bool(output and all(c.rule_version == goal.minimum_check_version
                                   for c in output.checks))
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
    if outcome["state"] == "unknown" or outcome.get("reconciliation"):
        run.usage.resource_usage_complete = False
    detected_postprocess = sum((_outcome(store, item) or {}).get("postprocess_starts_detected", 0)
                               for item in run.attempts)
    run.usage.postprocess_starts = max(run.usage.postprocess_starts, detected_postprocess)
    store.finish_attempt(
        run, attempt.id, state=("not_started" if outcome.get("not_started") and outcome.get("control_action") == "pause"
               else outcome["state"]), result_id=result.id,
        elapsed_seconds=max(attempt.elapsed_seconds, usage.get("wall_seconds", 0)),
        cpu_seconds=max(attempt.cpu_seconds, usage.get("user_cpu_seconds", 0) + usage.get("kernel_cpu_seconds", 0)),
        started=bool(outcome.get("handle")),
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


def _recover(store, config, run, plan):
    from orca_agent.tools.calculation import collect_result

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
            handle = (json.loads(handle_path.read_text(encoding="utf-8")) if handle_path.exists()
                      else attempt.execution_handle or {})
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
            step = next(step for step in plan.steps if step.id == attempt.step_id)
            result = collect_result(store, run, step, attempt, outcome)
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
                receipt = json.loads(store.artifact_path(proof["artifact_id"]).read_text())
                run.diagnostics.append({"category": "termination_reconciled", "attempt_id": attempt.id,
                                        "receipt_artifact_id": proof["artifact_id"], **receipt})
        if attempt.finished_at:
            # Old versions could finish an attempt while retaining an empty
            # unknown Result. Supplement its evidence without charging it twice.
            attempt.result_id = result.id
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
    if any(goal.minimum_check_version != CURRENT_CHECK_VERSION for goal in request.goals):
        raise ValueError("check_rule_revalidation_required: historical Run may be inspected and recovered; "
                         "scientific continuation requires explicit rule revalidation")
    environment = json.loads(store.path(f"runs/{run.id}/environment.json").read_text())
    if not is_supported_orca_version(environment.get("orca", {}).get("version")):
        raise ValueError("frozen ORCA version is not enabled for scientific execution")


def execute(store, config, run_id, *, resume=False, fault=None):
    """Advance only while holding this Run's coordinator lock; signal writes remain available."""
    with store.run_lock(run_id):
        run = store.load_run(run_id)
        plan = store.load_plan(run)
        if resume:
            control_path = store.path(f"runs/{run.id}/control.json")
            control_before = control_path.read_bytes() if control_path.exists() else None
            if not _recover(store, config, run, plan):
                return run
            with store.control_lock(run.id):
                control_after = control_path.read_bytes() if control_path.exists() else None
                if control_before == control_after and store.read_signal(run.id) == "pause":
                    store.signal(run.id, None)
        elif any(a.state in ("intent", "running", "unknown") for a in run.attempts):
            raise ValueError("unfinished attempts require explicit resume and reconciliation")
        results = _step_results(store, run)
        if run.state in ("completed", "cancelled"):
            _goals(store, run, plan, results)
            store.save_run(run)
            return run
        if store.read_signal(run.id) == "cancel":
            run.state = "cancelled"
            store.save_run(run)
            return run
        if _goals(store, run, plan, results):
            run.state = "completed"
            store.save_run(run)
            return run
        # A fixed plan cannot retry failed inputs without an explicitly validated change.
        if any(a.finished_at and a.state not in ("completed", "not_started") for a in run.attempts):
            run.state = "failed"
            store.record_diagnostic(run, "stopped", "Prior failed attempt retained; no automatic retry")
            return run
        run.state = "running"
        store.save_run(run)
        pending = [step for step in plan.steps if step.id not in results]
        try:
            while pending:
                signal = store.read_signal(run.id)
                if signal:
                    run.state = "paused" if signal == "pause" else "cancelled"
                    break
                if utc_now() >= run.deadline:
                    raise BudgetExceeded("run deadline exhausted")
                step = next((s for s in pending if all(dep in results for dep in s.depends_on)), None)
                if step is None:
                    raise ValueError("no dependency-ready step")
                reference = step.geometry
                if reference.artifact_id:
                    geometry_id = reference.artifact_id
                else:
                    output = results[reference.producer_step_id].qualified_outputs.get(reference.port)
                    if output is None or not output.artifact_id:
                        raise ValueError("required producer geometry did not pass its scientific checks")
                    if any(check.rule_version != CURRENT_CHECK_VERSION for check in output.checks):
                        raise ValueError("check_rule_revalidation_required: producer output uses historical checks")
                    geometry_id = output.artifact_id
                _validate_execution_rules(store, run)
                attempt = store.reserve_attempt(run, step, geometry_id)
                attempt.execution_handle = {"job_name": local.new_job_name(),
                    "coordinator_pid": os.getpid(), "coordinator_create_time": psutil.Process().create_time()}
                store.update_lease_handle(run.id, attempt.id, attempt.execution_handle)
                store.save_run(run)
                if fault:
                    fault("after_intent_saved")
                tool = get_tool(step.tool)
                module, name = tool.implementation.rsplit(".", 1)
                implementation = getattr(importlib.import_module(module), name)
                result, outcome = implementation(store, run, step, attempt, config, fault)
                store.save_result(result)
                if fault:
                    fault("after_result_saved")
                _settle(store, run, attempt, result, outcome)
                if fault:
                    fault("after_run_updated")
                if not outcome.get("not_started"):
                    results[step.id] = result
                    pending.remove(step)
                # Every result updates goals and scientific prerequisites before another launch.
                if _goals(store, run, plan, results):
                    run.state = "completed"
                    break
                if outcome["state"] != "completed":
                    signal = store.read_signal(run.id)
                    run.state = ("paused" if signal == "pause" else "cancelled" if signal == "cancel"
                                 else outcome["state"] if outcome["state"] in ("unknown", "cancelled") else "failed")
                    break
                store.save_run(run)
            else:
                run.state = "completed" if _goals(store, run, plan, results) else "failed"
        except KeyboardInterrupt:
            run.state = "unknown"
            run.diagnostics.append({"category": "interrupted", "message": "Explicit resume required"})
        except (ValueError, OSError, RuntimeError) as exc:
            unfinished = any(a.finished_at is None for a in run.attempts)
            run.state = ("unknown" if unfinished else
                         "budget_exhausted" if isinstance(exc, BudgetExceeded) else "failed")
            run.diagnostics.append({"category": type(exc).__name__, "message": str(exc),
                                    "environment_occupied": isinstance(exc, EnvironmentBusy)})
        _goals(store, run, plan, results)
        if run.state == "unknown":
            run.usage.resource_usage_complete = False
        store.save_run(run)
        return run
