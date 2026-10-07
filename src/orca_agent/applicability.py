"""Current scientific use of frozen evidence; no parser or new lifecycle.

Qualification belongs to the original Result. These checks only decide whether
that unchanged evidence can answer the current Request, including its geometry.
"""

from __future__ import annotations

from orca_agent.versions import CURRENT_CHECK_VERSION

CONDITION_FIELDS = ("method", "basis", "charge", "multiplicity", "electronic_state", "environment")
PROFILE = dict(method="HF", basis="STO-3G", charge=0, multiplicity=1,
               electronic_state="RHF", environment="gas_phase")
PURPOSE_VERSION = "current-purpose-1"
IDENTITY_RULE_VERSION = "named-target-1"


def canonical_condition(name, value):
    # JSON booleans/floats must not satisfy integer electronic conditions via
    # Python's False == 0 / True == 1 / 0.0 == 0 equality.
    if name in {"charge", "multiplicity"} and type(value) is not int:
        return None
    if isinstance(value, str):
        if value.strip().lower() in {"unknown", "unresolved", "not_applicable", ""}:
            return None
        if name == "method" and value.upper() in {"HF", "RHF"}:
            return "HF"
        if name == "electronic_state" and value.upper() in {"RHF", "UHF"}:
            return value.upper()
        if name == "environment" and value.lower() in {"gas", "gas_phase", "gas phase"}:
            return "gas_phase"
    return value


def effective_conditions(request, goal=None, system_id=None):
    """Resolve scoped conditions without overwriting unknowns or Goal conflicts.

System declarations are scoped overrides of general Request settings. A Goal is
an additional constraint, never an override of the applicable system setting.
Only absent state/environment inherit the existing fixed calculation profile.
"""
    reasons, origins, values = [], {}, {}
    system = next((s for s in request.systems if s.id == system_id), None)
    if system_id is not None and system is None:
        reasons.append("unknown_system:" + system_id)
    for name in CONDITION_FIELDS:
        if name in request.conditions:
            value, origin = request.conditions[name], "request.conditions"
            if (hasattr(request, name) and getattr(request, name) is not None
                    and canonical_condition(name, value) != canonical_condition(name, getattr(request, name))):
                reasons.append("conflicting_request_condition:" + name)
        elif hasattr(request, name):
            value, origin = getattr(request, name), "request." + request.conditions_source.get(name, "unknown")
        else:
            value, origin = PROFILE[name], "default:fixed-rhf-profile-1"
        if request.conditions_source.get(name) in {"unknown", "not_applicable", "inferred"}:
            value, origin = None, "request." + request.conditions_source[name]
        if system and name in system.conditions:
            value, origin = system.conditions[name], "system:" + system.id
            if system.conditions_source.get(name) in {"unknown", "not_applicable", "inferred"}:
                value = None
        # RHF is a method spelling with an explicit restricted-state meaning.
        method_value = (system.conditions.get("method") if system and "method" in system.conditions
                        else request.conditions.get("method", request.method))
        if name == "electronic_state" and method_value == "RHF" and canonical_condition(name, value) != "RHF":
            reasons.append("conflicting_condition:method/electronic_state")
        value = canonical_condition(name, value)
        if goal and name in goal.conditions:
            constraint = canonical_condition(name, goal.conditions[name])
            if value is None or constraint is None:
                reasons.append("unknown_condition:" + name)
            elif constraint != value:
                reasons.append("conflicting_goal_condition:" + name)
        values[name], origins[name] = value, origin
        if value is None:
            reasons.append("unknown_condition:" + name)
        elif value != PROFILE[name]:
            reasons.append("unsupported_condition:" + name)
    if request.unresolved or (goal and goal.unresolved):
        reasons.append("unresolved_request_or_goal")
    return {"version": PURPOSE_VERSION, "conditions": values, "sources": origins,
            "reasons": list(dict.fromkeys(reasons)), "status": "unresolved" if reasons else "passed"}


def current_conditions(request, goal, actual, system_id=None):
    assessment = effective_conditions(request, goal, system_id)
    for key, expected in assessment["conditions"].items():
        value = canonical_condition(key, actual.get(key))
        if value is None:
            assessment["reasons"].append("source_condition_unknown:" + key)
        elif expected != value:
            assessment["reasons"].append("source_condition_mismatch:" + key)
    assessment["status"] = "unresolved" if assessment["reasons"] else "passed"
    return assessment


def scientific_goal_system(request, goal):
    """Scalar outputs have exactly one target; old multi-target Goals stay unmet."""
    if len(goal.system_ids) != len(set(goal.system_ids)):
        raise ValueError("duplicate_goal_system_ids")
    if any(identifier not in {s.id for s in request.systems} for identifier in goal.system_ids):
        raise ValueError("unknown_goal_system_id")
    if len(goal.system_ids) > 1 or (not goal.system_ids and len(request.systems) > 1):
        raise ValueError("multi_system_scalar_goal_requires_explicit_per_system_goals")
    identifier = goal.system_ids[0] if goal.system_ids else (
        request.systems[0].id if len(request.systems) == 1 else None)
    return next((s for s in request.systems if s.id == identifier), None)


def _goal_identity_names(goal):
    """Only canonical identity constraints affect use; provenance is not a condition."""
    names = goal.identity.get("canonical_names", [])
    if (not isinstance(names, list) or any(not isinstance(name, str) for name in names)
            or len(set(names)) != len(names)):
        raise ValueError("invalid_goal_identity_constraint")
    return sorted(names)


def validate_goal_identity(store, request, goal, artifact_id=None):
    """Check new named scalar targets against the existing bounded geometry reader.

    This is the finite water/methane contract, not a general molecular identity
    algorithm. Historical Goals without canonical identity retain their rules.
    """
    names = _goal_identity_names(goal)
    if not names:
        return
    from collections import Counter

    from orca_agent.models import CalculationParameters
    from orca_agent.tools.registry import (
        SCIENCE_COMPOSITIONS,
        SCIENCE_IDENTITIES,
        validate_geometry,
    )

    if len(names) != 1 or names[0] not in SCIENCE_IDENTITIES:
        raise ValueError("unsupported_goal_identity_constraint")
    system = scientific_goal_system(request, goal)
    wanted = artifact_id or (system.geometry_artifact_id if system else request.geometry_artifact_id)
    if not wanted:
        raise ValueError("named_target_geometry_missing")
    path = store.artifact_path(wanted)
    if path.stat().st_size > 65536:
        raise ValueError("named_target_geometry_exceeds_bound")
    atoms = validate_geometry(path.read_text(encoding="utf-8"), CalculationParameters())
    if Counter(atom[0] for atom in atoms) != SCIENCE_COMPOSITIONS[SCIENCE_IDENTITIES[names[0]]]:
        raise ValueError("named_target_geometry_identity_mismatch")


def _validate_step_identities(store, request, step, artifact_id):
    system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
    for goal in request.goals:
        if (goal.port not in {"energy", "optimized_geometry"}
                or (goal.system_ids and system_id not in goal.system_ids)):
            continue
        validate_goal_identity(store, request, goal)
        validate_goal_identity(store, request, goal, artifact_id)


def validate_scientific_plan(request, plan):
    from orca_agent.tools.registry import get_tool

    steps = {s.id: s for s in plan.steps}
    for goal in request.goals:
        binding = plan.goal_map.get(goal.id)
        if goal.port in {"energy", "optimized_geometry"} and binding and binding.step_id:
            system = scientific_goal_system(request, goal)
            producer = steps[binding.step_id]
            if system and producer.system_id not in {None, system.id}:
                raise ValueError("scientific goal is bound to a different requested system")
    for step in plan.steps:
        if "execute_orca" not in get_tool(step.tool).effects:
            continue
        if len(request.systems) > 1 and step.system_id is None:
            raise ValueError("scientific Step requires its requested system identity")
        system_id = step.system_id or (request.systems[0].id if len(request.systems) == 1 else None)
        applicable = [goal for goal in request.goals if (
            (step.system_id in goal.system_ids) or
            (not goal.system_ids) or
            (plan.goal_map.get(goal.id) and plan.goal_map[goal.id].step_id == step.id))]
        for goal in applicable or [None]:
            assessment = current_conditions(request, goal, {**PROFILE, **step.parameters.model_dump()}, system_id)
            if assessment["reasons"]:
                raise ValueError("step changes request condition: " + ",".join(assessment["reasons"]))
        if step.geometry.producer_step_id:
            producer = steps[step.geometry.producer_step_id]
            if producer.system_id != step.system_id:
                raise ValueError("cross-system optimized geometry dependency is unsupported")


def _qualified(result, port):
    from orca_agent.orca.checks import OPTIMIZATION_STAGE_RULE, check_outputs

    output = result.qualified_outputs.get(port)
    required = {c.name for c in check_outputs({}, "orca.opt")[port]}
    if (output is None or output.checks != result.checks.get(port)
            or not required.issubset({c.name for c in output.checks})
            or any(c.status != "passed" or c.rule_version != CURRENT_CHECK_VERSION for c in output.checks)):
        raise ValueError("qualified_" + port + "_checks_missing_or_failed")
    if port == "optimized_geometry" and not any(
            c.name == "optimization_stage_binding" and c.source.get("rule_version") == OPTIMIZATION_STAGE_RULE
            for c in output.checks):
        raise ValueError("qualified_optimized_geometry_final_stage_rule_missing")
    return output


def verified_source(store, result):
    """Check exact existing Attempt and manifest; do not parse scientific values."""
    run = store.load_run(result.run_id)
    attempts = [a for a in run.attempts if a.id == result.attempt_id]
    if (result.id not in run.result_ids or len(attempts) != 1
            or attempts[0].step_id != result.step_id or result.operation_status != "completed"):
        raise ValueError("source_has_no_exact_completed_attempt")
    attempt = attempts[0]
    if getattr(attempt, "result_id", None) not in {None, result.id}:
        raise ValueError("source_result_differs_from_attempt_selection")
    frozen = getattr(attempt, "frozen_step", None)
    if frozen and (frozen.id != attempt.step_id or frozen.tool != attempt.tool):
        raise ValueError("source_frozen_step_mismatch")
    if (result.source.get("input_fingerprint") != attempt.input_fingerprint
            or result.source.get("geometry_artifact_id") != attempt.geometry_artifact_id):
        raise ValueError("source_input_binding_mismatch")
    files = result.source.get("files", {})
    if not {"stdout.out", "job.inp", "geometry.xyz"}.issubset(files):
        raise ValueError("source_manifest_missing")
    for source in files.values():
        artifact = store.load_artifact(source["artifact_id"])
        store.artifact_path(artifact.id)
        if (artifact.sha256 != source["sha256"] or artifact.id not in result.artifact_ids
                or (artifact.run_id, artifact.attempt_id) != (result.run_id, attempt.id)):
            raise ValueError("source_artifact_binding_mismatch")
    if set(result.artifact_ids) != {entry["artifact_id"] for entry in files.values()}:
        raise ValueError("source_manifest_incomplete")
    return run, attempt, files


def source_conditions(store, result, *, port="energy"):
    """Read checked conditions, restoring only the proven historical fixed profile."""
    _, attempt, files = verified_source(store, result)
    _qualified(result, port)
    actual = {name: canonical_condition(name, result.source.get("conditions", {}).get(name))
              for name in CONDITION_FIELDS}
    basis = {}
    for name in CONDITION_FIELDS[:4]:
        if actual[name] != PROFILE[name]:
            raise ValueError("source_conditions_missing_or_outside_profile:" + name)
    frozen = getattr(attempt, "frozen_step", None)
    if frozen and any(canonical_condition(name, getattr(frozen.parameters, name)) != actual[name]
                      for name in CONDITION_FIELDS[:4]):
        raise ValueError("source_conditions_differ_from_frozen_input")
    # Existing production inputs are immutable and checked by input_integrity.
    # Recover only two omitted historical labels; explicit unknown stays unknown.
    original = store.artifact_path(files["job.inp"]["artifact_id"]).read_text(encoding="utf-8").upper()
    tokens = original.replace("!", " ").split()
    profile_proven = ("RHF" in tokens and "STO-3G" in tokens
                      and not any(word in original for word in ("CPCM", "SMD", "COSMO", "UHF")))
    for name in CONDITION_FIELDS[4:]:
        if name not in result.source.get("conditions", {}) and profile_proven:
            actual[name] = PROFILE[name]
            basis[name] = {"rule": "fixed-rhf-profile-1", "input": files["job.inp"],
                           "checks": ["input_integrity", "method_and_electronic_state"]}
        elif actual[name] != PROFILE[name]:
            raise ValueError("source_condition_unknown_or_unsupported:" + name)
    return actual, basis


def qualified_geometry(store, result):
    _, attempt, files = verified_source(store, result)
    if attempt.tool != "orca.opt":
        raise ValueError("optimized_geometry_requires_optimization_attempt")
    output = _qualified(result, "optimized_geometry")
    if not output.artifact_id or output.artifact_id != files.get("job.xyz", {}).get("artifact_id"):
        raise ValueError("optimized_geometry_not_bound_to_final_artifact")
    return output.artifact_id


def _geometry_producer(store, artifact_id):
    artifact = store.load_artifact(artifact_id)
    store.artifact_path(artifact_id)
    if not artifact.run_id or not artifact.attempt_id:
        return None
    run = store.load_run(artifact.run_id)
    attempt = next((a for a in run.attempts if a.id == artifact.attempt_id), None)
    if not attempt or not attempt.result_id:
        return None
    result = store.load_result(run.id, attempt.result_id)
    output = result.qualified_outputs.get("optimized_geometry")
    if not output or output.artifact_id != artifact_id:
        return None
    qualified_geometry(store, result)
    return result


def geometry_lineage(store, result, *, seen=None):
    """Return immutable initial root and any qualified consumed Opt producer."""
    seen = set() if seen is None else set(seen)
    if result.id in seen or len(seen) >= 32:
        raise ValueError("cyclic_or_unbounded_geometry_lineage")
    seen.add(result.id)
    _, attempt, files = verified_source(store, result)
    initial = store.load_artifact(attempt.geometry_artifact_id)
    store.artifact_path(initial.id)
    if initial.sha256 != files["geometry.xyz"]["sha256"]:
        raise ValueError("consumed_geometry_differs_from_frozen_input")
    consumption = getattr(attempt, "consumption", {}).get("geometry", {})
    if consumption and (consumption.get("artifact_id") != initial.id
                        or consumption.get("sha256") != initial.sha256):
        raise ValueError("geometry_consumption_hash_mismatch")
    frozen = getattr(attempt, "frozen_step", None)
    producer = _geometry_producer(store, initial.id)
    if frozen and frozen.geometry:
        if frozen.geometry.artifact_id and frozen.geometry.artifact_id != initial.id:
            raise ValueError("frozen_direct_geometry_mismatch")
        if frozen.geometry.producer_step_id:
            if not producer or producer.step_id != frozen.geometry.producer_step_id or producer.run_id != result.run_id:
                raise ValueError("frozen_geometry_producer_mismatch")
            producer_attempt = next(a for a in store.load_run(producer.run_id).attempts
                                    if a.id == producer.attempt_id)
            if not producer_attempt.frozen_step or producer_attempt.frozen_step.system_id != frozen.system_id:
                raise ValueError("cross_system_geometry_lineage")
    if producer:
        root, _ = geometry_lineage(store, producer, seen=seen)
        return root, producer
    return initial.sha256, None


def direct_applicability(store, request, goal, result):
    system = scientific_goal_system(request, goal)
    validate_goal_identity(store, request, goal)
    actual, recovery = source_conditions(store, result, port=goal.port)
    assessment = current_conditions(request, goal, actual, system.id if system else None)
    assessment["source_conditions"] = actual
    assessment["source_condition_evidence"] = recovery
    root, producer = geometry_lineage(store, result)
    _, attempt, _ = verified_source(store, result)
    validate_goal_identity(store, request, goal, attempt.geometry_artifact_id)
    wanted = system.geometry_artifact_id if system else request.geometry_artifact_id
    requested_input = False
    if not wanted:
        assessment["reasons"].append("target_initial_geometry_missing")
    else:
        store.artifact_path(wanted)
        wanted_hash = store.load_artifact(wanted).sha256
        requested_input = wanted_hash == store.load_artifact(attempt.geometry_artifact_id).sha256
        if wanted_hash != root and not requested_input:
            assessment["reasons"].append("target_system_geometry_lineage_mismatch")
        if system and "system" in system.conditions:
            from collections import Counter

            from orca_agent.models import CalculationParameters
            from orca_agent.tools.registry import SCIENCE_COMPOSITIONS, validate_geometry
            counts = Counter(atom[0] for atom in validate_geometry(
                store.artifact_path(wanted).read_text(encoding="utf-8"), CalculationParameters()))
            declared = SCIENCE_COMPOSITIONS.get(system.conditions["system"])
            if declared != counts:
                assessment["reasons"].append("target_system_composition_mismatch")
    relation = goal.conditions.get("geometry_relation")
    if relation not in {None, "fixed_initial", "optimized"}:
        assessment["reasons"].append("unsupported_geometry_relation")
    if goal.port == "optimized_geometry":
        qualified_geometry(store, result)
    elif relation == "fixed_initial" and (attempt.tool != "orca.sp" or not requested_input):
        assessment["reasons"].append("fixed_initial_does_not_allow_optimized_geometry")
    elif relation == "optimized":
        if attempt.tool == "orca.opt":
            qualified_geometry(store, result)
        elif producer is None:
            assessment["reasons"].append("optimized_energy_requires_qualified_geometry_lineage")
    assessment["status"] = "unresolved" if assessment["reasons"] else "passed"
    return assessment


def validate_geometry_consumption(store, run, step, artifact_id):
    """Final Store boundary, including the producer's frozen original system."""
    request = store.load_request(run)
    system = next((s for s in request.systems if s.id == step.system_id), None)
    wanted = system.geometry_artifact_id if system else request.geometry_artifact_id
    producer = _geometry_producer(store, artifact_id)
    if step.geometry.producer_step_id:
        _validate_step_identities(store, request, step, artifact_id)
        if not producer or producer.run_id != run.id or producer.step_id != step.geometry.producer_step_id:
            raise ValueError("future_geometry_producer_binding_mismatch")
        source_run, attempt, _ = verified_source(store, producer)
        selected = source_run.selected_results.get(step.geometry.producer_step_id)
        if selected and selected != producer.id:
            raise ValueError("future_geometry_selection_changed")
        if not attempt.frozen_step or attempt.frozen_step.system_id != step.system_id:
            raise ValueError("cross_system_geometry_consumption")
        root, _ = geometry_lineage(store, producer)
    else:
        return validate_direct_geometry(store, request, step)
    if not wanted or root != store.load_artifact(wanted).sha256:
        raise ValueError("geometry_root_differs_from_requested_system")


def validate_direct_geometry(store, request, step):
    """Concrete registered Opt artifacts may originate in another Run."""
    if not step.geometry or not step.geometry.artifact_id:
        return
    _validate_step_identities(store, request, step, step.geometry.artifact_id)
    system = next((s for s in request.systems if s.id == step.system_id), None)
    if system is None and len(request.systems) == 1:
        system = request.systems[0]
    wanted = system.geometry_artifact_id if system else request.geometry_artifact_id
    if not wanted:
        raise ValueError("target_initial_geometry_missing")
    artifact_id = step.geometry.artifact_id
    store.artifact_path(artifact_id)
    store.artifact_path(wanted)
    if store.load_artifact(artifact_id).sha256 == store.load_artifact(wanted).sha256:
        return
    producer = _geometry_producer(store, artifact_id)
    if producer is None:
        raise ValueError("direct_geometry_has_no_qualified_optimized_lineage")
    root, _ = geometry_lineage(store, producer)
    if root != store.load_artifact(wanted).sha256:
        raise ValueError("geometry_root_differs_from_requested_system")


def purpose_snapshot(request, goal, *, include_requirements=True):
    """Relevant constraints only: unrelated wording/version changes are harmless."""
    ids = list(dict.fromkeys(goal.system_ids + [s.id for s in request.systems])) or [None]
    snapshot = {"version": PURPOSE_VERSION, "port": goal.port,
            "systems": {identifier or "_single": {
                "effective": effective_conditions(request, goal, identifier),
                "geometry_artifact_id": next((s.geometry_artifact_id for s in request.systems if s.id == identifier),
                                             request.geometry_artifact_id),
                "atom_mapping": next((s.atom_mapping for s in request.systems if s.id == identifier), []),
            } for identifier in ids}, "goal_conditions": {
                key: value for key, value in goal.conditions.items()
                if key in {*CONDITION_FIELDS, "comparison", "sampling", "candidates", "members",
                           "geometry_relation", "analysis_goal_id"}},
            "minimum_evidence": goal.minimum_evidence, "minimum_check_version": goal.minimum_check_version,
            "system_ids": goal.system_ids}
    if not include_requirements:
        snapshot.pop("minimum_evidence")
    if names := _goal_identity_names(goal):
        snapshot["goal_identity"] = {"rule_version": IDENTITY_RULE_VERSION, "canonical_names": names}
    return snapshot
