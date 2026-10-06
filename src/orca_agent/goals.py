"""Current-purpose checks over previously qualified outputs; no scientific parser."""


def validate_goal_evidence(store, run, request, goal, result):
    if request.unresolved or goal.unresolved or result.operation_status != "completed":
        return False
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
        if (observation.get("status") == "partial"
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
            c.rule_version != goal.minimum_check_version for c in output.checks):
        return False
    if goal.port in ("energy", "optimized_geometry"):
        conditions = result.source.get("conditions", {})
        system = next((s for s in request.systems if s.id in goal.system_ids), None)
        for name in ("method", "basis", "charge", "multiplicity"):
            expected = system.conditions.get(name, getattr(request, name)) if system else getattr(request, name)
            if expected is None or conditions.get(name) != expected:
                return False
        attempt = next((a for a in store.load_run(result.run_id).attempts
                        if a.id == result.attempt_id), None)
        if not attempt:
            return False
        if goal.port == "energy" and goal.conditions.get("geometry_relation") == "fixed_initial":
            from orca_agent.tools.registry import get_tool
            if "optimized_geometry" in get_tool(attempt.tool).output_ports:
                return False
        wanted = system.geometry_artifact_id if system else request.geometry_artifact_id
        if wanted and store.load_artifact(wanted).sha256 != store.load_artifact(
                attempt.geometry_artifact_id).sha256:
            if goal.conditions.get("geometry_relation") == "fixed_initial":
                return False
            # Explicit optimized-geometry dependency belongs to this current Plan.
            plan = store.load_plan(run)
            step = next((s for s in plan.steps if s.id == result.step_id), None) if plan else None
            if not step or not step.geometry or not step.geometry.producer_step_id:
                return False
    else:
        call = next((c for c in run.calls if c.id == result.call_id), None)
        analysis_goal_id = goal.conditions.get("analysis_goal_id") if goal.port == "member_table" else goal.id
        if not call or call.parameters.get("goal_id") != analysis_goal_id:
            return False
        historical = store.load_request_revision(run, call.request_version)
        old_goal = next((g for g in historical.goals if g.id == goal.id), None)
        if old_goal != goal:
            return False
        if goal.port == "member_table":
            old_analysis = next((g for g in historical.goals if g.id == analysis_goal_id), None)
            current_analysis = next((g for g in request.goals if g.id == analysis_goal_id), None)
            if (old_analysis is None or old_analysis != current_analysis
                    or old_analysis.port not in result.qualified_outputs):
                return False
        for source in call.consumption.values():
            if not isinstance(source, dict):
                continue
            for artifact_id, digest in source.get("artifact_hashes", {}).items():
                store.artifact_path(artifact_id)
                if store.load_artifact(artifact_id).sha256 != digest:
                    return False
    return True
