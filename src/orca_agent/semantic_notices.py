"""Finite, model-selected declarations for two known scientific input limits.

These are complete statement choices, not a classifier of arbitrary prose.
They neither grant execution nor certify the rest of a model explanation.
"""

from orca_agent.proposals import ProposalError
from orca_agent.tools.registry import SCIENCE_IDENTITIES

NOTICE_CONTRACT_VERSION = "registration-notices-1"
_PORTS = frozenset({"energy", "optimized_geometry"})


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
    """Check exact model selections without editing its notices or reason."""
    choices = notice_choices()
    affected = required_notice_kinds(request)
    selected = {kind for kind, text in choices.items() if text in notices}
    missing = sorted(set(affected) - selected)
    inapplicable = sorted(selected - set(affected))
    duplicate = sorted(kind for kind in selected if notices.count(choices[kind]) > 1)
    if missing or inapplicable or duplicate:
        raise ProposalError(
            "Select each applicable complete notice_choices statement exactly once in notices; "
            "free wording cannot replace these limited declarations.",
            path=["parameters", "notices"], missing_notice_choices=missing,
            inapplicable_notice_choices=inapplicable, duplicate_notice_choices=duplicate)
    return {"version": NOTICE_CONTRACT_VERSION, "required": affected, "selected": sorted(selected)}
