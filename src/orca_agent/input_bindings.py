"""Validate Run-local acquired inputs against frozen purpose and exact provenance."""

from orca_agent.models import fingerprint


def input_check_version(tool, request, system_id):
    system = next((system for system in request.systems if system.id == system_id), None)
    if system and system.identity.get("contract") == "general-identity-2":
        return tool.check_contract.get("general_check_version", tool.check_version)
    if system and system.identity.get("contract") == "general-identity-3":
        return tool.check_contract.get("name_query_check_version", tool.check_version)
    return tool.check_version


def required_input_rule(tool, request, system_id, port):
    system = next((system for system in request.systems if system.id == system_id), None)
    if system and system.identity.get("contract") == "general-identity-2":
        return tool.check_contract.get("general_input_checks", {}).get(port, tool.required_input_checks.get(port))
    if system and system.identity.get("contract") == "general-identity-3":
        return tool.check_contract.get("name_query_input_checks", {}).get(port, tool.required_input_checks.get(port))
    return tool.required_input_checks.get(port)


def input_purpose(request, system_id):
    from orca_agent.applicability import effective_conditions

    system = next((s for s in request.systems if s.id == system_id), None)
    if system is None or system.geometry_source != "prepare":
        raise ValueError("system_has_no_authorized_input_acquisition_intent")
    return {"version": "structure-input-purpose-1", "request_id": request.id,
            "request_version": request.version, "system_id": system_id,
            "identity": system.identity, "geometry_source": system.geometry_source,
            "conditions": effective_conditions(request, system_id=system_id)["conditions"]}


def _identity_origin_request(store, run, request, call, *, port="resolved_identity"):
    """Reuse inputs only across authenticated choices, never user edits."""
    from orca_agent.applicability import effective_conditions
    from orca_agent.condition_selection import (
        GENERAL_SELECTION_VERSION,
        SIMPLIFIED_SELECTION_VERSION,
        general_admission_enabled,
        simplified_workflow,
    )

    simplified = simplified_workflow(run)
    if ((not general_admission_enabled(store, run) and not simplified)
            or call.request_version >= request.version):
        raise ValueError("input_result_call_scope_or_version_mismatch")
    original = store.load_request_revision(run, call.request_version)
    for version in range(call.request_version + 1, request.version + 1):
        revision = store.load_request_revision(run, version)  # validates activated decision hashes
        summaries = [item for item in run.decisions if item.get("request_version") == version
                     and item.get("basis", {}).get("request_version") == version - 1]
        if not summaries:
            raise ValueError("identity_reuse_requires_authenticated_condition_selection")
        for summary in summaries:
            record = store._read_json(f"runs/{run.id}/decisions/{summary['id']}.json")
            expected = SIMPLIFIED_SELECTION_VERSION if simplified else GENERAL_SELECTION_VERSION
            if record.get("condition_selection", {}).get("schema") != expected:
                raise ValueError("identity_reuse_requires_authenticated_condition_selection")
        if (revision.systems != original.systems or revision.messages != original.messages
                or revision.goals != original.goals or revision.original_text != original.original_text):
            raise ValueError("identity_reuse_cannot_change_identity_geometry_or_user_purpose")
        if port == "prepared_geometry":
            before = effective_conditions(original, system_id=call.parameters["system_id"])["conditions"]
            after = effective_conditions(revision, system_id=call.parameters["system_id"])["conditions"]
            if any(before.get(name) != after.get(name) for name in ("charge", "multiplicity")):
                raise ValueError("prepared_geometry_reuse_cannot_change_electronic_preparation_state")
    if revision != request:
        raise ValueError("identity_reuse_requires_activated_request")
    return original


def validate_input_result(store, run, request, result, port, *, require_bound=True):
    from orca_agent.condition_selection import simplified_workflow
    from orca_agent.tools.registry import get_tool

    if result.run_id != run.id or (require_bound and result.id not in run.result_ids):
        raise ValueError("input_result_not_bound_to_current_run")
    calls = [c for c in run.calls if c.id == result.call_id]
    if len(calls) != 1 or result.operation_status != "completed":
        raise ValueError("input_result_requires_exact_completed_call")
    call = calls[0]
    tool = get_tool(call.tool)
    original_request = request
    if call.request_version != request.version:
        reusable = (port == "resolved_identity" and "query_external_identity" in tool.effects
                    or simplified_workflow(run) and port == "prepared_geometry"
                    and "prepare_geometry" in tool.effects)
        if not reusable:
            raise ValueError("input_result_call_scope_or_version_mismatch")
        original_request = _identity_origin_request(store, run, request, call, port=port)
    if ("write_input_artifact" not in tool.effects or port not in tool.output_ports
            or call.step_id != result.step_id or not call.frozen_step
            or call.frozen_step.tool != call.tool or call.frozen_step.system_id != call.parameters.get("system_id")
            or call.frozen_step.parameters.model_dump(mode="json") != call.parameters
            or result.source.get("request_version") != call.request_version):
        raise ValueError("input_result_call_scope_or_version_mismatch")
    if require_bound and (call.result_id != result.id or call.state != "completed"
                          or run.selected_results.get(call.step_id) != result.id):
        raise ValueError("input_result_selection_mismatch")
    if (call.tool not in run.permission.allowed_tools or not run.permission.artifact_writes
            or "query_external_identity" in tool.effects and not run.permission.external_identity_queries
            or "prepare_geometry" in tool.effects and not run.permission.geometry_preparation):
        raise ValueError("input_result_outside_frozen_permission")
    purpose = input_purpose(original_request, call.parameters["system_id"])
    if call.consumption.get("_input_purpose") != purpose:
        raise ValueError("input_result_purpose_changed")
    output = result.qualified_outputs.get(port)
    rule = input_check_version(tool, request, call.parameters["system_id"])
    required_names = set(tool.check_contract.get("required_checks", {}).get(port, []))
    if (not output or not output.artifact_id or output.checks != result.checks.get(port)
            or any(c.status != "passed" or c.rule_version != rule for c in output.checks)
            or not required_names.issubset({c.name for c in output.checks})):
        raise ValueError("input_result_required_checks_missing")
    artifact = store.load_artifact(output.artifact_id)
    store.artifact_path(artifact.id)
    if (artifact.id not in result.artifact_ids or artifact.run_id != run.id
            or artifact.source.get("call_id") != call.id):
        raise ValueError("input_artifact_provenance_mismatch")
    for identifier in result.artifact_ids:
        store.artifact_path(identifier)
    for binding in call.consumption.values():
        if isinstance(binding, dict) and binding.get("result_fingerprint"):
            prior = store.load_result(binding["run_id"], binding["result_id"])
            if fingerprint(prior) != binding["result_fingerprint"]:
                raise ValueError("input_source_result_changed")
            for identifier, digest in binding["artifact_hashes"].items():
                store.artifact_path(identifier)
                if store.load_artifact(identifier).sha256 != digest:
                    raise ValueError("input_source_artifact_changed")
            if port == "prepared_geometry":
                validate_input_result(store, run, request, prior, "resolved_identity")
    if port == "prepared_geometry" and not call.consumption.get("identity"):
        raise ValueError("prepared_geometry_requires_identity_consumption")
    return {"request_version": request.version, "system_id": call.parameters["system_id"],
            **({"source_request_version": call.request_version} if original_request != request else {}),
            "purpose_fingerprint": fingerprint(input_purpose(request, call.parameters["system_id"])), "call_id": call.id,
            "result_id": result.id, "artifact_id": artifact.id, "sha256": artifact.sha256,
            "rule_version": rule, "port": port}


def carry_selected_identity_inputs(store, run, request, bindings):
    """Revalidate selected inputs without relabeling any historical Call/Result."""
    from orca_agent.condition_selection import simplified_workflow

    updated = {}
    for system_id, ports in bindings.items():
        allowed_ports = ("resolved_identity", "prepared_geometry") if simplified_workflow(run) else ("resolved_identity",)
        for port in allowed_ports:
            binding = ports.get(port)
            if not binding:
                continue
            result = store.load_result(run.id, binding["result_id"])
            prior = store.load_request_revision(run, request.version - 1)
            if validate_input_result(store, run, prior, result, port) != binding:
                raise ValueError("identity_input_binding_changed")
            actual = validate_input_result(store, run, request, result, port)
            if actual["system_id"] != system_id:
                raise ValueError("identity_input_system_changed")
            updated.setdefault(system_id, {})[port] = actual
    return updated


def bind_input_result(store, run, result):
    from orca_agent.tools.registry import get_tool

    call = next((c for c in run.calls if c.id == result.call_id), None)
    if not call or "write_input_artifact" not in get_tool(call.tool).effects:
        return
    if result.operation_status != "completed":
        return
    request = store.load_request(run)
    # Old-version completion remains historical evidence; never relabel it.
    if request.version != call.request_version:
        return
    for port in get_tool(call.tool).output_ports:
        if port in result.qualified_outputs:
            binding = validate_input_result(store, run, request, result, port, require_bound=False)
            run.input_bindings.setdefault(binding["system_id"], {})[port] = binding


def prepared_geometry(store, run, request, system_id):
    binding = run.input_bindings.get(system_id, {}).get("prepared_geometry")
    if not binding:
        return None
    result = store.load_result(run.id, binding["result_id"])
    actual = validate_input_result(store, run, request, result, "prepared_geometry")
    if actual != binding:
        raise ValueError("prepared_input_binding_mismatch")
    return actual["artifact_id"]


def resolved_request(store, run, request=None):
    """Read-only consumption projection; never persist over the original Request."""
    request = (request or store.load_request(run)).model_copy(deep=True)
    for system in request.systems:
        if system.geometry_source == "prepare" and system.geometry_artifact_id is None:
            identifier = prepared_geometry(store, run, request, system.id)
            if identifier:
                system.geometry_artifact_id = identifier
    if len(request.systems) == 1 and request.geometry_artifact_id is None:
        request.geometry_artifact_id = request.systems[0].geometry_artifact_id
    return request
