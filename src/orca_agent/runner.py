"""Shared initialization, evidence collection and recovery for the single Agent loop."""

import json
import os

from orca_agent.backends import local
from orca_agent.doctor import diagnose
from orca_agent.models import PermissionSnapshot, utc_now
from orca_agent.store import atomic_write, sha256_file
from orca_agent.structured import prepare_task
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
    from orca_agent.goals import validate_goal_evidence
    request = store.load_request(run)
    for goal in request.goals:
        binding = plan.goal_map.get(goal.id) if plan else None
        result = results.get(binding.step_id) if binding else None
        direct = run.goal_evidence.get(goal.id)
        reference = direct or (binding.evidence if binding else None)
        if reference:
            if reference.run_id != run.id and reference.result_id not in run.permission.result_ids:
                raise ValueError("goal evidence is outside the permission snapshot")
            result = store.load_result(reference.run_id, reference.result_id)
            if reference.attempt_id and result.attempt_id != reference.attempt_id:
                raise ValueError("goal evidence Attempt differs")
        output = result.qualified_outputs.get(goal.port) if result else None
        valid = bool(result and validate_goal_evidence(store, run, request, goal, result)
                     and not (binding and binding.gap and not direct))
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
            if attempt.frozen_step:
                step = attempt.frozen_step
            else:
                # Legacy Runs have no revision permission. Their original immutable
                # Plan is the only admissible source; never use a new current Plan.
                version = attempt.plan_version or 1
                historical = store.load_plan_revision(run, version)
                step = next(s for s in historical.steps if s.id == attempt.step_id)
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
           if goal.port in ("energy", "optimized_geometry")):
        raise ValueError("check_rule_revalidation_required: historical Run may be inspected and recovered; "
                         "scientific continuation requires explicit rule revalidation")
    environment = json.loads(store.path(f"runs/{run.id}/environment.json").read_text())
    if not is_supported_orca_version(environment.get("orca", {}).get("version")):
        raise ValueError("frozen ORCA version is not enabled for scientific execution")


def execute(store, config, run_id, *, resume=False, fault=None, transport=None, batch=None):
    """Compatibility entry into the one feedback loop, including fixed structured plans."""
    from orca_agent.agent import execute as run_agent
    return run_agent(store, config, run_id, resume=resume, fault=fault,
                     transport=transport, batch=batch)
