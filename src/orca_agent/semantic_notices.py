"""Finite program disclosures for independently determined scientific limits.

These are complete statement choices, not a classifier of arbitrary prose.
They neither grant execution nor certify the rest of a model explanation.
"""

from orca_agent.proposals import ProposalError
from orca_agent.tools.registry import SCIENCE_IDENTITIES

NOTICE_CONTRACT_VERSION = "registration-notices-2"
_PORTS = frozenset({"energy", "optimized_geometry", "dipole_moment"})


def notice_choices():
    """Expose the same complete statements that new semantic commits validate."""
    supported = ", ".join(f"{name}({formula})" for name, formula in SCIENCE_IDENTITIES.items())
    return {
        "unsupported_system": (
            f"Energy/optimization supports only {supported}; "
            "other requested targets stay unsupported after geometry registration."),
        "missing_geometry": (
            "At least one scientific target lacks its own bound registered geometry. "
            "This input gap is separate from capability; registration executes nothing."),
    }


def required_notice_kinds(request):
    """Read grounded identities and program-refreshed geometry gaps, never labels.

    The caller first runs semantic grounding and _missing_information. Unknown
    identity is not an unsupported identity; a missing geometry on one Goal is
    not attributed to every Goal. Other ports/limits keep their own contracts.
    """
    affected = {}
    for goal in request.goals:
        if goal.evidence_contract_version == "generic-observation-1":
            continue
        if goal.port not in _PORTS:
            continue
        names = set(goal.identity.get("canonical_names", []))
        if (not goal.identity.get("explicitly_unknown")
                and names - SCIENCE_IDENTITIES.keys()):
            affected.setdefault("unsupported_system", []).append(goal.id)
        if any(gap == "missing:geometry" or gap.startswith("missing:geometry:")
               for gap in goal.unresolved):
            affected.setdefault("missing_geometry", []).append(goal.id)
    return {kind: sorted(set(goals)) for kind, goals in sorted(affected.items())}


def validate_notices(request, notices):
    """Generate missing known declarations; model wording never grants coverage."""
    choices = notice_choices()
    affected = required_notice_kinds(request)
    legacy_prefix = choices["unsupported_system"].split(";", 1)[0].casefold()
    if "unsupported_system" not in affected and any(text.casefold().startswith(legacy_prefix) for text in notices):
        raise ProposalError("Legacy specialized checker coverage cannot declare a general molecular task unsupported.",
                            path=["parameters", "notices"], inapplicable_notice_choices=["unsupported_system"])
    selected = {kind for kind, text in choices.items() if text in notices}
    missing = sorted(set(affected) - selected)
    inapplicable = sorted(selected - set(affected))
    duplicate = sorted(kind for kind in selected if notices.count(choices[kind]) > 1)
    if inapplicable or duplicate:
        raise ProposalError(
            "Select each applicable complete notice_choices statement exactly once in notices; "
            "free wording cannot replace these limited declarations.",
            path=["parameters", "notices"], missing_notice_choices=missing,
            inapplicable_notice_choices=inapplicable, duplicate_notice_choices=duplicate)
    generated = [{"kind": kind, "text": choices[kind], "goal_ids": affected[kind],
                  "source": "program"} for kind in missing]
    for gap, goals in coverage_gaps(request).items():
        if gap.startswith("unsupported_system:") and "unsupported_system" in affected:
            continue
        generated.append({"kind": "scientific_coverage", "gap": gap, "goal_ids": goals,
                          "source": "program", "text":
            f"Scientific coverage remains unverified ({gap}); the original requirement is retained. "
            "Execution or an observation does not establish that this requirement passed."})
    return {"version": NOTICE_CONTRACT_VERSION, "required": affected, "selected": sorted(selected),
            "generated": generated}


def coverage_gaps(request):
    """Only independently grounded coverage facts are non-answerable limitations."""
    from orca_agent.applicability import PROFILE, canonical_condition, effective_conditions
    from orca_agent.input_bindings import input_check_version
    from orca_agent.minimum_evidence import LEGACY_NAMES, REQUIREMENTS
    from orca_agent.tools.registry import catalog, get_tool

    affected = {}
    inputs = {port: tool["name"] for tool in catalog() if "write_input_artifact" in tool["effects"]
              for port in tool["output_ports"]}
    for goal in request.goals:
        known = set()
        if goal.evidence_contract_version != "generic-observation-1":
            own_rules = {goal.port, goal.minimum_check_version}
            if goal.port in inputs and len(goal.system_ids) == 1:
                own_rules.add(input_check_version(get_tool(inputs[goal.port]), request, goal.system_ids[0]))
            known.update("unsupported_minimum_evidence:" + item for item in goal.minimum_evidence
                         if LEGACY_NAMES.get(item, item) not in REQUIREMENTS
                         and item not in own_rules)
            known.update("unsupported_system:" + name
                         for name in goal.identity.get("canonical_names", [])
                         if name not in SCIENCE_IDENTITIES and not goal.identity.get("explicitly_unknown"))
            for system_id in goal.system_ids or [None]:
                assessed = effective_conditions(request, goal, system_id)
                known.update("applicability:unsupported_condition:" + name
                             for name, expected in PROFILE.items()
                             if assessed["conditions"].get(name) is not None
                             and canonical_condition(name, assessed["conditions"][name]) != expected)
        for gap in set(goal.unresolved) & known:
            affected.setdefault(gap, []).append(goal.id)
    # A same-spelled unverified marker elsewhere cannot borrow this Goal's
    # independently established provenance.
    return {gap: sorted(set(goals)) for gap, goals in sorted(affected.items())
            if gap not in request.unresolved
            and all(gap not in goal.unresolved or goal.id in goals for goal in request.goals)}
