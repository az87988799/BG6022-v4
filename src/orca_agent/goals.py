"""Current-purpose checks over previously qualified outputs; no scientific parser."""

from orca_agent.applicability import direct_applicability, purpose_snapshot
from orca_agent.minimum_evidence import assess_minimum_evidence
from orca_agent.models import OutputBinding


def _bound_goal_evidence(store, run, request, goal, binding, results):
    """Resolve one declared binding and assess its present purpose, without writes."""
    selected = None
    gaps = []
    try:
        if binding.port != goal.port:
            gaps.append("goal_physical_quantity_binding_mismatch")
        elif binding.gap:
            gaps.append(binding.gap)
        elif binding.evidence:
            reference = binding.evidence
            if (reference.run_id != run.id
                    and reference.result_id not in run.permission.result_ids):
                gaps.append("goal_evidence_outside_permission_snapshot")
            elif reference.port != goal.port:
                gaps.append("goal_physical_quantity_binding_mismatch")
            elif reference.rule_version not in {None, goal.minimum_check_version}:
                gaps.append("goal_evidence_check_version_mismatch")
            elif reference.sha256 and not reference.artifact_id:
                gaps.append("explicit_artifact_hash_without_identity")
            elif reference.run_id == run.id and reference.result_id not in run.result_ids:
                gaps.append("selected_result_not_bound_to_run")
            else:
                selected = store.load_result(reference.run_id, reference.result_id)
                if reference.attempt_id and selected.attempt_id != reference.attempt_id:
                    gaps.append("explicit_attempt_binding_mismatch")
                if reference.artifact_id:
                    artifact = store.load_artifact(reference.artifact_id)
                    store.artifact_path(artifact.id)
                    if (artifact.id not in selected.artifact_ids
                            or reference.sha256 not in {None, artifact.sha256}):
                        gaps.append("explicit_artifact_binding_mismatch")
        elif binding.step_id:
            selected_id = run.selected_results.get(binding.step_id)
            if selected_id:
                if selected_id not in run.result_ids:
                    gaps.append("selected_result_not_bound_to_run")
                else:
                    selected = store.load_result(run.id, selected_id)
            elif results is not None:
                selected = results.get(binding.step_id)
            elif len(run.result_ids) > 128:
                gaps.append("result_count_exceeds_selection_bound")
            else:
                # Legacy records have no selected_results. Only a unique result
                # for the declared Step is admissible; timestamps have no role.
                candidates = [store.load_result(run.id, identifier) for identifier in run.result_ids]
                candidates = [item for item in candidates if item.step_id == binding.step_id]
                if len(candidates) == 1:
                    selected = candidates[0]
                elif len(candidates) > 1:
                    gaps.append("ambiguous_result_binding")
            if selected and selected.step_id != binding.step_id:
                gaps.append("selected_result_step_mismatch")
            if selected and (selected.run_id != run.id or selected.id not in run.result_ids):
                gaps.append("selected_result_not_bound_to_run")
    except (KeyError, ValueError, OSError, RuntimeError, TypeError):
        selected = None
        gaps.append("goal_evidence_unavailable")
    assessment = goal_evidence_assessment(store, run, request, goal, selected) if selected else None
    return {"binding": binding, "result": selected, "assessment": assessment, "gaps": gaps}


def current_goal_evidence(store, run, request, goal, plan=None, results=None):
    """Select applicable explicit direct/Plan evidence, never the newest Result.

    Legacy stale direct selections remain readable, but cannot hide an applicable
    active Plan binding. A failed selection is returned for honest diagnostics.
    """
    bindings = []
    direct = run.goal_evidence.get(goal.id)
    if direct:
        bindings.append(OutputBinding(port=direct.port, evidence=direct))
    binding = plan.goal_map.get(goal.id) if plan else None
    if binding:
        bindings.append(binding)
    candidates = []
    for binding in bindings:
        selected = _bound_goal_evidence(store, run, request, goal, binding, results)
        if (not selected["gaps"] and selected["assessment"]
                and selected["assessment"]["status"] == "passed"):
            return selected
        candidates.append(selected)
    return next((item for item in candidates if item["result"] is not None),
                candidates[-1] if candidates else {
                    "binding": None, "result": None, "assessment": None,
                    "gaps": ["goal_has_no_evidence_binding"]})


def _validate_goal_evidence(store, run, request, goal, result):
    if request.unresolved or goal.unresolved or result.operation_status != "completed":
        return False
    if goal.port != "artifact_metadata":
        for artifact_id in result.artifact_ids:
            store.artifact_path(artifact_id)
    if goal.minimum_check_version == "evidence-read-1":
        checks = result.checks.get(goal.port, [])
        observation = result.observations.get(goal.port)
        if not (checks and isinstance(observation, dict)
                and all(c.status == "passed" and c.rule_version == "evidence-read-1" for c in checks)):
            return False
        if observation.get("status") in {"missing", "missing_json"}:
            return False
        from orca_agent.tools.evidence import observation_complete
        if (not observation_complete(observation)
                and goal.conditions.get("accept_partial_observations") is not True):
            return False
        if goal.conditions.get("require_nonempty_matches") is True and not observation.get("matches"):
            return False
        call = next((c for c in run.calls if c.id == result.call_id), None)
        imported_source = goal.conditions.get("imported_source_id")
        if imported_source:
            if not call or not call.parameters.get("artifact_id"):
                return False
            artifact = store.load_artifact(call.parameters["artifact_id"])
            store.artifact_path(artifact.id)
            if (artifact.source.get("source_id") != imported_source
                    or artifact.source.get("kind") != "user_registered_import"):
                return False
        return bool(call and all(call.parameters.get(k) == v
                                for k, v in goal.conditions.get("query", {}).items()))
    output = result.qualified_outputs.get(goal.port)
    if not output or output.checks != result.checks.get(goal.port) or any(
            c.rule_version != goal.minimum_check_version or c.status != "passed" for c in output.checks):
        return False
    if goal.port in ("energy", "optimized_geometry"):
        return direct_applicability(store, request, goal, result)["status"] == "passed"
    else:
        current_purpose = purpose_snapshot(request, goal, include_requirements=False)
        if any(system["effective"]["status"] != "passed" for system in current_purpose["systems"].values()):
            return False
        source_run = store.load_run(result.run_id)
        call = next((c for c in source_run.calls if c.id == result.call_id), None)
        analysis_goal_id = goal.conditions.get("analysis_goal_id") if goal.port == "member_table" else goal.id
        if not call or call.parameters.get("goal_id") != analysis_goal_id:
            return False
        historical = store.load_request_revision(source_run, call.request_version)
        old_goal = next((g for g in historical.goals if g.id == goal.id), None)
        if old_goal is None or purpose_snapshot(historical, old_goal, include_requirements=False) != current_purpose:
            return False
        frozen_purpose = call.consumption.get("_current_purpose")
        analysis_goal = next((g for g in historical.goals if g.id == analysis_goal_id), None)
        if (frozen_purpose is not None and (analysis_goal is None
                or frozen_purpose != purpose_snapshot(historical, analysis_goal))):
            return False
        if goal.port == "member_table":
            old_analysis = next((g for g in historical.goals if g.id == analysis_goal_id), None)
            current_analysis = next((g for g in request.goals if g.id == analysis_goal_id), None)
            if (old_analysis is None or current_analysis is None
                    or purpose_snapshot(historical, old_analysis, include_requirements=False) != purpose_snapshot(request, current_analysis, include_requirements=False)
                    or old_analysis.port not in result.qualified_outputs):
                return False
        for source in call.consumption.values():
            if not isinstance(source, dict):
                continue
            if source.get("result_fingerprint"):
                from orca_agent.models import fingerprint
                original = store.load_result(source["run_id"], source["result_id"])
                if fingerprint(original) != source["result_fingerprint"]:
                    return False
            for artifact_id, digest in source.get("artifact_hashes", {}).items():
                store.artifact_path(artifact_id)
                if store.load_artifact(artifact_id).sha256 != digest:
                    return False
    return True


def goal_evidence_assessment(store, run, request, goal, result):
    reasons = []
    current_use = None
    try:
        if goal.port in {"energy", "optimized_geometry"}:
            output = result.qualified_outputs.get(goal.port)
            if request.unresolved or goal.unresolved or result.operation_status != "completed":
                passed = False
                reasons.append("unresolved_purpose_or_incomplete_source_operation")
            elif (not output or not output.checks or output.checks != result.checks.get(goal.port)
                  or any(c.status != "passed" or c.rule_version != goal.minimum_check_version for c in output.checks)):
                passed = False
                reasons.append("required_output_checks_missing_failed_or_wrong_version")
            else:
                current_use = direct_applicability(store, request, goal, result)
                passed = current_use["status"] == "passed"
                reasons.extend(current_use["reasons"])
        else:
            passed = _validate_goal_evidence(store, run, request, goal, result)
            if not passed and goal.port in {"energy_difference", "sampling", "member_table"}:
                for system in purpose_snapshot(request, goal)["systems"].values():
                    reasons.extend(system["effective"]["reasons"])
    except (ValueError, KeyError, OSError, RuntimeError, TypeError) as error:
        passed = False
        reasons.append(str(error))
    requirements = assess_minimum_evidence(goal, result, purpose_passed=passed)
    reasons.extend(entry["reason"] + ":" + entry["requested"] for entry in requirements if entry["status"] != "passed")
    if not passed and not reasons:
        reasons.append("current_goal_evidence_not_applicable")
    return {"status": "passed" if passed and not reasons else "unresolved",
            "reasons": list(dict.fromkeys(reasons)), "minimum_evidence": requirements,
            "current_use": current_use}


def validate_goal_evidence(store, run, request, goal, result):
    return goal_evidence_assessment(store, run, request, goal, result)["status"] == "passed"
