"""Explicit test-only schema derivation; never rewrites retained model receipts."""

import copy

from orca_agent.semantic import VERSION


def current_candidate(parameters):
    """Exercise a historical failure shape under the current activation contract.

    Only the protocol version changes here. Test-authored source, binding or
    notice corrections must remain explicit at the call site. This derived
    candidate is not evidence that the historical model passed the new schema.
    """
    derived = copy.deepcopy(parameters)
    derived["schema_version"] = VERSION
    return derived
