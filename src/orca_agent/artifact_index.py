"""Bounded file identities already recorded in a Result; no file access.

This display does not authorize a read or execution. Consumers still verify
the registered Artifact, producer receipt, permission and hash at use time.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

from orca_agent.models import Result

MAX_INDEX_ROWS = 12
MAX_INDEX_BYTES = 4096
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
_DIGEST = re.compile(r"[a-f0-9]{64}")


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                          sort_keys=True, allow_nan=False).encode("utf-8"))


def _file_key(value):
    if not isinstance(value, str) or not 0 < len(value) <= 512:
        return False
    parts = value.split("/")
    return (not PurePosixPath(value).is_absolute()
            and not any(char in value for char in '\\:<>\"|?*')
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            and all(part not in {"", ".", ".."} and not part.endswith((" ", "."))
                    and not PureWindowsPath(part).is_reserved() for part in parts))


def _row(key, value, registered):
    if not _file_key(key) or not isinstance(value, Mapping):
        return None
    identifier, digest = value.get("artifact_id"), value.get("sha256")
    if (not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier)
            or identifier not in registered or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)):
        return None
    row = {"file_key": key, "artifact_id": identifier, "sha256": digest}
    # Never guess a role from an extension, or a size from the displayed value.
    role = value.get("role")
    if (isinstance(role, str) and 0 < len(role) <= 96
            and not any(ord(char) < 32 or ord(char) == 127 for char in role)
            and not any(char in role for char in "/\\:")):
        row["role"] = role
    if type(value.get("size")) is int and value["size"] >= 0:
        row["size"] = value["size"]
    return row


def result_file_index(result: Result, *, max_bytes=MAX_INDEX_BYTES, max_rows=MAX_INDEX_ROWS):
    """Project exact settled Result file identities without opening the Store.

    A Result's terminal operation label is display data, not proof that its
    producer has settled. The ordinary consumption gates independently check
    that binding. Unknown operations are deliberately not advertised here.
    """
    if (type(max_bytes) is not int or not 512 <= max_bytes <= MAX_INDEX_BYTES
            or type(max_rows) is not int or not 1 <= max_rows <= MAX_INDEX_ROWS):
        raise ValueError("file index limits exceed the bounded display contract")
    if result.operation_status == "unknown" or not (result.attempt_id or result.call_id):
        return None
    source = result.source.get("files")
    origin = "Result.source.files"
    if not isinstance(source, dict):
        written = result.observations.get("written_artifact")
        if (result.operation_status != "completed" or not result.call_id
                or result.source.get("tool") != "workspace.write"
                or not isinstance(written, dict)):
            return None
        name = written.get("name")
        # Receipt observations are untrusted JSON; lists/maps cannot be keys.
        source = {name if isinstance(name, str) else "": {
            **{key: written[key] for key in ("artifact_id", "sha256", "role") if key in written},
            **({"size": written["bytes"]} if "bytes" in written else {}),
        }}
        origin = "Result.observations.written_artifact"
    if not source:
        return None
    registered = set(result.artifact_ids)
    rows = [_row(key, value, registered) for key, value in source.items()]
    digest_sets = {}
    for row in rows:
        if row:
            digest_sets.setdefault(row["artifact_id"], set()).add(row["sha256"])
    usable = [row for row in rows if row and len(digest_sets[row["artifact_id"]]) == 1]
    # Present task outputs before sealed inputs and coordinator files. This is
    # display order only: no file is selected, bound, read or executed for users.
    usable.sort(key=lambda row: (not row["file_key"].startswith("task/"), row["file_key"]))
    invalid = len(source) - len(usable)
    index = {"source": origin, "result_id": result.id,
             **({"attempt_id": result.attempt_id} if result.attempt_id else {"call_id": result.call_id}),
             "files": [], "total": len(source), "omitted": len(source),
             "omission_reasons": ["unaddressable_metadata"] if invalid else [],
             "validation": "Metadata only; consumption still checks producer, permission and hash."}
    for row in usable:
        if len(index["files"]) >= max_rows:
            index["omission_reasons"].append("row_limit")
            break
        candidate = {**index, "files": [*index["files"], row],
                     "omitted": len(source) - len(index["files"]) - 1}
        # Reserve space for a later byte/row boundary marker.
        bounded = {**candidate, "omission_reasons": [*candidate["omission_reasons"], "byte_limit"]}
        if _bytes(bounded) > max_bytes:
            index["omission_reasons"].append("byte_limit")
            break
        index = candidate
    if _bytes(index) > max_bytes:
        # Long producer identities can consume the smallest display window.
        # Keep a bounded explicit omission; the original Result is unchanged.
        return {"source": origin, "result_id": result.id,
                **({"attempt_id": result.attempt_id} if result.attempt_id else {"call_id": result.call_id}),
                "files": [], "total": len(source),
                "omitted": len(source), "omission_reasons": ["byte_limit"],
                "validation": "No file identities fit; no content was read."}
    return index
