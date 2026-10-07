"""Registered Tool execution with exact, immutable consumption bindings."""

import importlib
import json

from pydantic import Field, TypeAdapter

from orca_agent.models import Check, Identifier, QualifiedOutput, Record, Result, fingerprint
from orca_agent.store import ControlChanged, StoreError, atomic_write
from orca_agent.tools import analysis
from orca_agent.tools.registry import get_tool


class _MemberRequirement(Record):
    id: Identifier
    required: bool = Field(default=True, strict=True)


def _source_hashes(store, source_run, result):
    """Revalidate provenance even when the source has no qualified number."""
    source = store.load_run(source_run)
    if result.run_id != source_run or result.id not in source.result_ids:
        raise StoreError("source Result is not bound to its Run")
    if result.attempt_id is not None:
        attempts = [a for a in source.attempts if a.id == result.attempt_id]
        if len(attempts) != 1 or attempts[0].step_id != result.step_id:
            raise StoreError("source Result has no exact Attempt/Step provenance")
        attempt = attempts[0]
        for key, expected in (("input_fingerprint", attempt.input_fingerprint),
                              ("geometry_artifact_id", attempt.geometry_artifact_id)):
            if key in result.source and result.source[key] != expected:
                raise StoreError("source Result input/geometry provenance differs from its Attempt")
    else:
        calls = [c for c in source.calls if c.id == result.call_id]
        if len(calls) != 1 or calls[0].step_id != result.step_id:
            raise StoreError("source Result has no exact Tool call provenance")
    hashes = {}
    for artifact_id in result.artifact_ids:
        store.artifact_path(artifact_id)
        artifact = store.load_artifact(artifact_id)
        if result.attempt_id is not None and (artifact.run_id, artifact.attempt_id) != (
                source_run, result.attempt_id):
            raise StoreError("source Artifact Run/Attempt provenance differs")
        hashes[artifact_id] = artifact.sha256
    files = result.source.get("files", {})
    if not isinstance(files, dict):
        raise StoreError("source file manifest is not a provenance mapping")
    for entry in files.values():
        if (not isinstance(entry, dict) or not isinstance(entry.get("artifact_id"), str)
                or hashes.get(entry["artifact_id"]) != entry.get("sha256")
                or entry.get("sha256") is None):
            raise StoreError("source file manifest hash/provenance differs")
    return hashes


def bind_inputs(store, run, step, results):
    bound = {}
    for member_id, reference in step.inputs.items():
        if member_id.startswith("_"):
            raise StoreError("input member cannot use a reserved metadata identity")
        if reference.producer_step_id:
            selected = results.get(reference.producer_step_id)
            if selected is None:
                raise StoreError("required producer has no explicitly selected Result")
            source_run, result_id = run.id, selected.id
            result = store.load_result(source_run, result_id)
            if (result.model_dump(mode="json") != selected.model_dump(mode="json")
                    or result.step_id != reference.producer_step_id):
                raise StoreError("producer Result differs from its persisted Step binding")
        else:
            source_run, result_id = reference.run_id, reference.result_id
            if source_run != run.id and result_id not in run.permission.result_ids:
                raise StoreError("external Result is not authorized")
            result = store.load_result(source_run, result_id)
        hashes = _source_hashes(store, source_run, result)
        if reference.attempt_id and result.attempt_id != reference.attempt_id:
            raise StoreError("source Attempt differs from its consumption binding")
        required = get_tool(step.tool).required_input_checks.get(reference.port)
        output = result.qualified_outputs.get(reference.port)
        if (not required
                or reference.rule_version is not None and reference.rule_version != required):
            raise StoreError("source output does not satisfy the consumer rule")
        unavailable = None
        if result.operation_status != "completed":
            unavailable = "source_operation_" + result.operation_status
        elif output is None:
            unavailable = "qualified_source_output_missing"
        elif (output.checks != result.checks.get(reference.port)
              or any(c.rule_version != required or c.status != "passed" for c in output.checks)):
            unavailable = "source_checks_do_not_satisfy_consumer_rule"
        if reference.artifact_id and hashes.get(reference.artifact_id) != reference.sha256:
            raise StoreError("source Artifact differs from the explicit binding")
        bound[member_id] = {"run_id": source_run, "result_id": result_id,
                            "attempt_id": result.attempt_id, "port": reference.port,
                            "artifact_hashes": hashes, "rule_version": required,
                            "result_fingerprint": fingerprint(result),
                            "status": "unavailable" if unavailable else "qualified",
                            "unavailable_reason": unavailable}
    return bound


def _goal(store, run, call, port):
    request = store.load_request(run)
    goal = next((g for g in request.goals if g.id == call.parameters["goal_id"]), None)
    if not goal or goal.port != port:
        raise StoreError("analysis must serve the unchanged requested physical quantity")
    return goal


def _members(store, call, required, all_ids=()):
    members = []
    bound_ids = [key for key in call.consumption if not key.startswith("_")]
    for member_id in dict.fromkeys([*required, *all_ids, *bound_ids]):
        binding = call.consumption.get(member_id)
        evidence = None
        missing_reason = "required member not bound" if member_id in required else "optional member not bound"
        unavailable_source = None
        if binding:
            for artifact_id, digest in binding["artifact_hashes"].items():
                store.artifact_path(artifact_id)
                if store.load_artifact(artifact_id).sha256 != digest:
                    raise StoreError("consumed artifact changed after reservation")
            result = store.load_result(binding["run_id"], binding["result_id"])
            if (binding.get("result_fingerprint")
                    and fingerprint(result) != binding["result_fingerprint"]):
                raise StoreError("consumed Result changed after reservation")
            if _source_hashes(store, binding["run_id"], result) != binding["artifact_hashes"]:
                raise StoreError("consumed Result artifact provenance changed after reservation")
            if binding.get("status") == "unavailable":
                # A failed/missing member is a provenance fact, never a numeric
                # input. Neither observations nor raw files supply a fallback.
                missing_reason = binding["unavailable_reason"]
                unavailable_source = dict(binding)
            else:
                evidence = analysis.bind_energy(store, binding["run_id"], binding["result_id"],
                                                expected_attempt_id=binding["attempt_id"])
        members.append(analysis.AnalysisMember(
            id=member_id, required=member_id in required, evidence=evidence,
            missing_reason=missing_reason if not evidence else None,
            unavailable_source=unavailable_source))
    return members


def _current_comparison_members(store, request, members, goal=None):
    """A qualified historical energy must also match its requested operand."""
    systems = {system.id: system for system in request.systems}
    for member in members:
        if member.evidence is None:
            continue
        evidence = member.evidence
        system = systems.get(member.id)
        from orca_agent.applicability import current_conditions, geometry_lineage
        declared = system.conditions if system else {}
        assessment = current_conditions(request, goal, evidence.conditions.model_dump(),
                                        system.id if system else None)
        expected = assessment["conditions"]
        mismatch = assessment["reasons"]
        if "system" in declared and declared["system"] != "H2O":
            mismatch.append("system")
        if system and system.atom_mapping and system.atom_mapping != list(range(len(evidence.elements))):
            mismatch.append("atom_mapping")
        if system and system.geometry_artifact_id:
            store.artifact_path(system.geometry_artifact_id)
            source = store.load_result(evidence.run_id, evidence.result_id)
            root, _ = geometry_lineage(store, source)
            if store.load_artifact(system.geometry_artifact_id).sha256 != root:
                mismatch.append("geometry")
        if goal and goal.conditions.get("geometry_relation"):
            source = store.load_result(evidence.run_id, evidence.result_id)
            _, producer = geometry_lineage(store, source)
            if goal.conditions["geometry_relation"] == "fixed_initial" and (
                    producer or evidence.tool != "orca.sp"):
                mismatch.append("fixed_initial")
            if goal.conditions["geometry_relation"] == "optimized" and (
                    evidence.tool != "orca.opt" and producer is None):
                mismatch.append("optimized_geometry_evidence_missing")
        if mismatch:
            member.unavailable_source = {key: value for key, value in evidence.model_dump(mode="json").items()
                                         if key not in {"energy_eh", "unit"}}
            member.unavailable_source.update(expected_conditions=expected, mismatched_fields=mismatch)
            member.missing_reason = "source_not_applicable_to_requested_operand:" + ",".join(mismatch)
            member.evidence = None
    return members


def compare(store, run, call):
    goal = _goal(store, run, call, "energy_difference")
    parameters = analysis.EnergyCompareParameters.model_validate(goal.conditions["comparison"])
    declared = goal.conditions.get("members", [])
    if not isinstance(declared, list) or len(declared) > 5:
        raise StoreError("comparison requires a bounded requested member list")
    declared = [_MemberRequirement.model_validate(item) for item in declared]
    if len({item.id for item in declared}) != len(declared):
        raise StoreError("requested comparison members must be unique")
    required = [parameters.member_a, parameters.member_b]
    if any(item.id in required and not item.required for item in declared):
        raise StoreError("comparison operands cannot be optional")
    required.extend(item.id for item in declared if item.required and item.id not in required)
    members = _members(store, call, required, [item.id for item in declared])
    members = _current_comparison_members(store, store.load_request(run), members, goal)
    return analysis.energy_compare(members, parameters)


def sample(store, run, call):
    goal = _goal(store, run, call, "sampling")
    parameters = analysis.SamplingParameters.model_validate(goal.conditions["sampling"])
    candidates = [analysis.SamplingCandidate.model_validate(v)
                  for v in goal.conditions["candidates"]]
    if len(candidates) != 5:
        raise StoreError("sampling requires five authorized candidates")
    geometry = {}
    for candidate in candidates:
        if candidate.artifact_id not in run.permission.artifact_ids:
            raise StoreError("sampling candidate is outside the permission snapshot")
        geometry[candidate.artifact_id] = store.artifact_path(candidate.artifact_id).read_bytes()
    required = [c.id for c in candidates if c.required_initial]
    members = _current_comparison_members(store, store.load_request(run), _members(
        store, call, required, [c.id for c in candidates]), goal)
    return analysis.finite_sampling(candidates, members, geometry, parameters)


def import_evidence(store, run, call):
    from orca_agent.tools.evidence import import_source
    sources = store._read_json(f"runs/{run.id}/sources.json")
    return import_source(store, call.parameters["source_id"], sources, run_id=run.id)


def execute_call(store, run, tool_name, parameters, *, step=None, results=None, fault=None,
                 decision_id=None):
    definition = get_tool(tool_name)
    consumption = bind_inputs(store, run, step, results or {}) if step else {}
    if "write_analysis" in definition.effects and step is not None:
        from orca_agent.applicability import purpose_snapshot
        request = store.load_request(run)
        goal = next((g for g in request.goals if g.id == parameters.get("goal_id")), None)
        if goal is None:
            raise StoreError("analysis has no current requested goal")
        consumption["_current_purpose"] = purpose_snapshot(request, goal)
    if decision_id is not None:
        consumption["_decision_id"] = TypeAdapter(Identifier).validate_python(decision_id)
    call = store.reserve_call(run, tool_name, parameters, step, consumption=consumption)
    if fault:
        fault("after_call_reserved")
    module, function = definition.implementation.rsplit(".", 1)
    implementation = getattr(importlib.import_module(module), function)
    control_error = None
    try:
        with store.control_lock(run.id):
            store.check_execution_control(run)
            if call.control_generation != run.control_generation:
                raise ControlChanged("reserved Tool basis differs from current control")
            if definition.effects == ["read_registered_artifact"]:
                data = implementation(store, **call.parameters)
            else:
                data = implementation(store, run, call)
        qualified = {k: QualifiedOutput.model_validate(v)
                     for k, v in data.get("qualified_outputs", {}).items()}
        checks = {k: list(v.checks) for k, v in qualified.items()}
        if isinstance(data.get("checks"), list) and definition.output_ports:
            checks[definition.output_ports[0]] = [Check.model_validate(v) for v in data["checks"]]
        if not definition.output_ports:
            # Qualification applies only to the bounded reading operation, not
            # to the observed physical quantity. A missing field is still an
            # honest successful operation but lacks the requested extracted value.
            found = data.get("status") not in {"missing", "missing_json"}
            checks.update({port: [Check(
                name="bounded_evidence_read", status="passed" if found else "unverified",
                rule_version=definition.check_version,
                detail="read operation only; observed values remain scientifically unverified",
                source={key: data[key] for key in ("artifact_id", "sha256", "view", "path", "coverage")
                        if key in data},
            )] for port in definition.observation_outputs})
        artifacts = list(data.get("artifact_ids", []))
        if call.parameters.get("artifact_id"):
            artifacts.append(call.parameters["artifact_id"])
        artifacts.extend(item["id"] for item in data.get("artifacts", []) if isinstance(item, dict)
                         and isinstance(item.get("id"), str))
        if "write_analysis" in definition.effects:
            path = store.path(f"runs/{run.id}/calls/{call.id}/analysis.json")
            atomic_write(path, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode(),
                         immutable=True)
            artifact = store.import_artifact(path, "analysis", run_id=run.id,
                                             source={"call_id": call.id, "consumption": consumption})
            artifacts.append(artifact.id)
            comparison = qualified.get("energy_difference")
            members = data.get("members", [])
            if ("member_table" in definition.output_ports and comparison is not None
                    and data.get("scientific_status") == "passed"
                    and data.get("reason") == "compatible_comparison"
                    and members and all(m.get("status") == "qualified"
                                        for m in members if m.get("required"))):
                # The table is the same checked analysis, with its own immutable
                # artifact binding. Missing optional rows remain explicit and
                # never receive inferred or unverified numerical values.
                qualified["member_table"] = QualifiedOutput(
                    artifact_id=artifact.id, checks=list(comparison.checks),
                    source={"members": members, "analysis_goal_id": call.parameters["goal_id"],
                            "artifact_id": artifact.id, "sha256": artifact.sha256})
                checks["member_table"] = list(comparison.checks)
        observation = {name: data for name in definition.observation_outputs}
        result = Result(run_id=run.id, step_id=call.step_id, call_id=call.id,
                        operation_status="completed", checks=checks, qualified_outputs=qualified,
                        observations=observation, artifact_ids=list(dict.fromkeys(artifacts)),
                        source={"tool": tool_name, "consumption": consumption,
                                "request_version": run.request_version, "plan_version": run.plan_version})
    except (KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
        control_error = exc if isinstance(exc, ControlChanged) else None
        diagnostic = {"category": type(exc).__name__,
                      "message": "Tool rejected inputs or unavailable evidence"}
        if (definition.effects == ["read_registered_artifact"]
                and "32 KiB evidence window" in str(exc)):
            diagnostic["reason"] = "evidence_response_byte_limit"
        result = Result(run_id=run.id, step_id=call.step_id, call_id=call.id,
                        operation_status="cancelled" if control_error else "failed",
                        diagnostics=[diagnostic],
                        source={"tool": tool_name, "consumption": consumption,
                                **({"not_started": True, "control_generation": call.control_generation}
                                   if control_error else {})})
    store.save_result(result)
    if fault:
        fault("after_result_saved")
    store.finish_call(run, call, result)
    if fault:
        fault("after_run_updated")
    if control_error:
        raise control_error
    return result
