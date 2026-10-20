"""Conservative per-decision output reservations within a frozen token ceiling.

This module has no model, Store or registry access. The caller supplies the
current actions and identifies short actions from trusted execution/schema
facts. Smaller reservations are not estimates of actual model consumption.
"""

from collections.abc import Iterable, Mapping

_FULL_OUTPUT_ACTIONS = frozenset({"initial_plan", "revise_plan", "normalize_request"})
_TERMINAL_ACTIONS = frozenset({"stop", "clarify"})
_CONTROL_OUTPUT_TOKENS = 2048
_READ_OUTPUT_TOKENS = 4096
_TERMINAL_REASON_TOKENS = 4096


def _action_set(values: Iterable[str], name: str) -> set[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an iterable of action names")
    selected = set()
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must contain nonempty action names")
        selected.add(value)
    return selected


def _was_truncated(feedback: Mapping[str, object] | None) -> bool:
    """Recognize structured transport feedback, never prose or task output."""
    if not isinstance(feedback, Mapping):
        return False
    records = [feedback]
    validation = feedback.get("validation_error")
    if isinstance(validation, Mapping):
        records.append(validation)
    return any(record.get("category") == "truncated"
               or record.get("error_category") == "truncated" for record in records)


def select_output_tokens(
    *,
    allowed_actions: Iterable[str],
    configured_limit: int,
    short_actions: Iterable[str] = (),
    terminal_minimum_bytes: int | None = None,
    feedback: Mapping[str, object] | None = None,
) -> int:
    """Choose a reservation without changing the frozen limit or any usage.

    ``configured_limit`` is the already validated minimum of the Run and model
    ceilings. ``short_actions`` must come from trusted facts: ``call_tool`` is
    short only when it selects a frozen Step; named immediate tools must have
    bounded read/control arguments, never code authoring. Unknown actions and
    any action capable of producing a full Plan retain the entire ceiling.

    For a terminal-only decision, ``terminal_minimum_bytes`` is the UTF-8 size
    of its complete required JSON response, including every Goal/reference and
    the response envelope. Bytes provide a conservative BPE allowance, not a
    tokenizer measurement; additional room permits reason/clarification text.
    Missing terminal sizing or a preceding truncated response keeps the full
    ceiling. A required response exceeding that ceiling is never truncated by
    this function: the original ceiling remains and existing validation applies.
    """
    if type(configured_limit) is not int or configured_limit < 1:
        raise ValueError("configured_limit must be a positive integer")
    if terminal_minimum_bytes is not None and (
        type(terminal_minimum_bytes) is not int or terminal_minimum_bytes < 1
    ):
        raise ValueError("terminal_minimum_bytes must be a positive integer or None")
    actions = _action_set(allowed_actions, "allowed_actions")
    short = _action_set(short_actions, "short_actions")
    if not actions:
        raise ValueError("allowed_actions cannot be empty")
    if _was_truncated(feedback) or actions & _FULL_OUTPUT_ACTIONS:
        return configured_limit
    if actions <= _TERMINAL_ACTIONS:
        if terminal_minimum_bytes is None:
            return configured_limit
        return min(configured_limit, terminal_minimum_bytes + _TERMINAL_REASON_TOKENS)
    if actions <= short:
        ceiling = _CONTROL_OUTPUT_TOKENS if actions == {"call_tool"} else _READ_OUTPUT_TOKENS
        return min(configured_limit, ceiling)
    return configured_limit
