"""Bounded raw evidence observations; never executes or converts scientific files.

The raw_json view names literal JSON keys, not OPI's normalized property view.
Scientific parsing and qualification remain the adapter's responsibility.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from orca_agent.models import Identifier, Record

MAX_SOURCE_BYTES = 16 * 1024 * 1024
MAX_RETURN_BYTES = 32 * 1024
MAX_ELEMENTS = 200
MAX_DEPTH = 12
READ_SECONDS = 5
OBSERVATION = "unverified observation; not a scientific qualification"


class EvidenceKey(Record):
    kind: Literal["key"]
    key: Annotated[str, Field(strict=True, min_length=1, max_length=100)]


class EvidenceIndex(Record):
    kind: Literal["index"]
    index: Annotated[int, Field(strict=True, ge=0, le=1000000)]


class EvidenceSlice(Record):
    kind: Literal["slice"]
    start: Annotated[int, Field(strict=True, ge=0, le=1000000)]
    stop: Annotated[int, Field(strict=True, ge=1, le=1000000)]

    @model_validator(mode="after")
    def finite_slice(self):
        if not 0 < self.stop - self.start <= MAX_ELEMENTS:
            raise ValueError("slice must select 1 to 200 elements")
        return self


EvidencePath = Annotated[
    EvidenceKey | EvidenceIndex | EvidenceSlice, Field(discriminator="kind")
]


class EvidenceValueParameters(Record):
    artifact_id: Identifier
    path: Annotated[list[EvidencePath], Field(max_length=MAX_DEPTH)] = Field(default_factory=list)
    view: Literal["raw_json"] = "raw_json"

    @model_validator(mode="after")
    def terminal_slice(self):
        if any(isinstance(part, EvidenceSlice) for part in self.path[:-1]):
            raise ValueError("array slice must be the final path segment")
        return self


class EvidenceDiscoverParameters(EvidenceValueParameters):
    offset: Annotated[int, Field(strict=True, ge=0, le=10000)] = 0
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_ELEMENTS)] = 40


class EvidenceSearchParameters(Record):
    artifact_id: Identifier
    query: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    start_line: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1
    max_lines: Annotated[int, Field(strict=True, ge=1, le=MAX_ELEMENTS)] = 200
    max_hits: Annotated[int, Field(strict=True, ge=1, le=MAX_ELEMENTS)] = 40
    case_sensitive: Annotated[bool, Field(strict=True)] = False


def _clock(started):
    if time.monotonic() - started > READ_SECONDS:
        raise TimeoutError("evidence read exceeded 5 seconds")


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _finish(value):
    if len(_encoded(value)) > MAX_RETURN_BYTES:
        raise ValueError("response exceeds the 32 KiB evidence window")
    return value


@contextmanager
def _source(store, artifact_id, *, maximum=MAX_SOURCE_BYTES):
    started = time.monotonic()
    artifact = store.load_artifact(artifact_id)
    if artifact.size > maximum:
        raise ValueError(f"evidence source exceeds {maximum // (1024 * 1024)} MiB")
    path = store.artifact_path(artifact_id)
    _clock(started)
    try:
        yield artifact, path, started
    finally:
        # Recheck even on an absent field, malformed input, or a failed extraction.
        store.artifact_path(artifact_id)
        _clock(started)


def _identity(artifact, view, path=None):
    location = [] if path is None else path
    geometry_indices = [
        location[index + 1]["index"] for index, item in enumerate(location[:-1])
        if item == {"kind": "key", "key": "Geometries"}
        and location[index + 1].get("kind") == "index"
    ]
    return {
        "artifact_id": artifact.id, "sha256": artifact.sha256, "role": artifact.role,
        "file": artifact.path, "source": artifact.source,
        "view": {"type": view, "version": "1"}, "path": location,
        "geometry_indices": geometry_indices, "stage": "unknown", "units": None,
        "conditions": "unknown", "scientific_status": "unverified",
        "validation": OBSERVATION,
    }


def _coverage(response, requested, returned, *, complete=True, reason=None, next_start=None):
    """Coverage is of the explicit query window, not of the entire source file.

    Continuations are source-bound descriptions for the next explicit Tool call;
    they neither grant file access nor start automatic pagination.
    """
    cursor = None
    if not complete and next_start is not None:
        cursor = {key: response[key] for key in ("artifact_id", "sha256", "view", "path",
                                                "run_id", "inventory_sha256")
                  if key in response}
        cursor.update(requested=requested, next_start=next_start)
    return {"version": "evidence-coverage-1", "requested": requested, "returned": returned,
            "complete": complete, "truncated": not complete and reason not in {
                "missing", "missing_json"}, "reason": reason, "next_cursor": cursor}


def observation_complete(observation):
    """Older saved observations remain readable; explicit incompleteness wins."""
    if ("coverage" not in observation and "field" in observation
            and isinstance(observation.get("value"), list) and len(observation["value"]) >= 32):
        # Old field readers silently sliced at 32. A retained prefix cannot
        # prove that the original array ended at that boundary.
        return False
    return (observation.get("status") not in {"partial", "missing", "missing_json"}
            and observation.get("coverage", {}).get("complete") is not False)


def _value_coverage(response, parts, value, *, returned_count=None):
    if parts and parts[-1]["kind"] == "slice":
        requested = {"kind": "array", "start": parts[-1]["start"], "stop": parts[-1]["stop"]}
    elif isinstance(value, (list, dict)):
        requested = {"kind": "array" if isinstance(value, list) else "object",
                     "start": 0, "stop": len(value)}
    else:
        requested = {"kind": "value"}
    returned = dict(requested)
    if "start" in returned:
        count = len(value) if returned_count is None else returned_count
        returned["stop"] = returned["start"] + count
    complete = returned_count is None or returned_count == len(value)
    return _coverage(response, requested, returned, complete=complete,
                     reason=None if complete else "element_limit",
                     next_start=None if complete else returned["stop"])


def _json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON keys cannot be addressed unambiguously")
        result[key] = value
    return result


def _json_constant(value):
    raise ValueError("nonfinite JSON values cannot be observed")


def _json_data(path, started):
    # Plain data reading intentionally does not call OPI.parse or create JSON.
    data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_json_pairs,
                      parse_constant=_json_constant)
    _clock(started)
    return data


def _select(data, parts):
    parent = None
    for part in parts:
        parent = data
        if part["kind"] == "key":
            if not isinstance(data, dict) or part["key"] not in data:
                return False, None, parent
            data = data[part["key"]]
        elif part["kind"] == "index":
            if not isinstance(data, list) or part["index"] >= len(data):
                return False, None, parent
            data = data[part["index"]]
        else:
            if not isinstance(data, list):
                return False, None, parent
            data = data[part["start"]:part["stop"]]
    return True, data, parent


def _units(data, parent):
    for candidate in (data, parent):
        if isinstance(candidate, dict):
            for key in ("unit", "units", "Unit", "Units"):
                unit = candidate.get(key)
                if isinstance(unit, str) and len(unit) <= 100:
                    return {"value": unit, "basis": f"explicit sibling field {key}"}
    return None


def _bounded_value(data):
    """Count nested members too, so nested arrays cannot multiply the allowance."""
    count = 0
    pending = [(data, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise ValueError("returned value exceeds depth 12; select a deeper explicit path")
        if isinstance(item, (dict, list)):
            count += len(item)
            if count > MAX_ELEMENTS:
                raise ValueError("value exceeds 200 elements; select a bounded array slice or field")
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return data


def _kind(data):
    if data is None:
        return "null"
    if isinstance(data, bool):
        return "boolean"
    if isinstance(data, dict):
        return "object"
    if isinstance(data, list):
        return "array"
    if isinstance(data, str):
        return "string"
    return "number"


def _missing_json(response):
    return _finish({**response, "status": "missing_json", "available_view": "raw_text",
                    "coverage": _coverage(response, {"kind": "value"}, None,
                                          complete=False, reason="missing_json"),
                    "requirement": "supply an existing JSON artifact or explicitly plan postprocessing"})


def read_value(store, artifact_id, path=None, view="raw_json"):
    """Observe a typed key/index/terminal-slice path in one existing raw JSON file."""
    params = EvidenceValueParameters(artifact_id=artifact_id,
                                     path=[] if path is None else path, view=view)
    parts = [part.model_dump() for part in params.path]
    with _source(store, artifact_id) as (artifact, source, started):
        response = _identity(artifact, view, parts)
        if source.suffix.lower() != ".json":
            return _missing_json(response)
        found, value, parent = _select(_json_data(source, started), parts)
        if not found:
            return _finish({**response, "status": "missing", "coverage": _coverage(
                response, {"kind": "value"}, None, complete=False, reason="missing")})
        return _finish({**response, "status": "observed", "value": _bounded_value(value),
                        "coverage": _value_coverage(response, parts, value),
                        "units": _units(value, parent)})


def discover_content(store, artifact_id, path=None, offset=0, limit=40, view="raw_json"):
    """List actual immediate members and typed entry paths, without guessing properties."""
    params = EvidenceDiscoverParameters(artifact_id=artifact_id,
                                        path=[] if path is None else path, offset=offset,
                                        limit=limit, view=view)
    parts = [part.model_dump() for part in params.path]
    with _source(store, artifact_id) as (artifact, source, started):
        response = _identity(artifact, view, parts)
        if source.suffix.lower() != ".json":
            return _missing_json(response)
        found, value, parent = _select(_json_data(source, started), parts)
        if not found:
            return _finish({**response, "status": "missing", "coverage": _coverage(
                response, {"kind": "entries", "start": offset, "stop": offset + limit},
                None, complete=False, reason="missing")})
        total = len(value) if isinstance(value, (dict, list)) else 0
        requested = {"kind": "entries", "start": offset, "stop": offset + limit}
        available_stop = max(offset, min(total, offset + limit))
        response.update(status="observed", type=_kind(value), total=total, offset=offset,
                        entries=[], units=_units(value, parent), next_offset=None)
        if isinstance(value, dict):
            items = (({"kind": "key", "key": key}, child)
                     for key, child in value.items())
        elif isinstance(value, list):
            items = (({"kind": "index", "index": index}, child)
                     for index, child in enumerate(value))
        else:
            items = iter(())
        for position, (segment, child) in enumerate(items):
            _clock(started)
            if position < offset:
                continue
            if len(response["entries"]) >= limit:
                break
            entry_parts = parts
            if parts and parts[-1]["kind"] == "slice":
                entry_parts = parts[:-1]
                segment = {"kind": "index", "index": parts[-1]["start"] + segment["index"]}
            entry_path = [*entry_parts, segment]
            entry = {"path": entry_path, "type": _kind(child),
                     "addressable": len(entry_path) <= MAX_DEPTH
                     and (segment["kind"] != "key" or 0 < len(segment["key"]) <= 100)}
            if isinstance(child, (dict, list)):
                entry["length"] = len(child)
            candidate = {**response, "entries": [*response["entries"], entry]}
            # Reserve space for the continuation index when the response becomes full.
            candidate["next_offset"] = offset + len(candidate["entries"])
            candidate["coverage"] = _coverage(response, requested,
                {"kind": "entries", "start": offset, "stop": candidate["next_offset"]},
                complete=False, reason="byte_limit", next_start=candidate["next_offset"])
            if len(_encoded(candidate)) > MAX_RETURN_BYTES:
                if not response["entries"]:
                    raise ValueError("content descriptor exceeds the 32 KiB evidence window")
                break
            response["entries"].append(entry)
        next_offset = offset + len(response["entries"])
        response["next_offset"] = next_offset if next_offset < total else None
        complete = next_offset >= available_stop
        response["status"] = "observed" if complete else "partial"
        response["coverage"] = _coverage(response, requested,
            {"kind": "entries", "start": offset, "stop": next_offset}, complete=complete,
            reason=None if complete else "byte_limit", next_start=None if complete else next_offset)
        return _finish(response)


def search_text(store, artifact_id, query, start_line=1, max_lines=200, max_hits=40,
                case_sensitive=False):
    """Literal substring search in a bounded physical-line window; no regex or eval."""
    params = EvidenceSearchParameters(artifact_id=artifact_id, query=query, start_line=start_line,
                                      max_lines=max_lines, max_hits=max_hits,
                                      case_sensitive=case_sensitive)
    needle = params.query if case_sensitive else params.query.casefold()
    with _source(store, artifact_id) as (artifact, path, started):
        response = _identity(artifact, "raw_text")
        requested = {"kind": "lines", "start": start_line, "stop": start_line + max_lines,
                     "query": query, "case_sensitive": case_sensitive}
        response.update(status="observed", matches=[], query=query, start_line=start_line,
                        scanned_lines=0, next_line=None)
        reason = None
        with path.open("rb") as stream:
            for number in range(1, start_line + max_lines):
                _clock(started)
                raw = stream.readline(MAX_RETURN_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_RETURN_BYTES:
                    raise ValueError("line exceeds the 32 KiB evidence window")
                if number < start_line:
                    continue
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if needle in (text if case_sensitive else text.casefold()):
                    match = {"line": number, "text": text}
                    candidate = {**response, "matches": [*response["matches"], match],
                                 "next_line": number + 1}
                    candidate["coverage"] = _coverage(response, requested,
                        {"kind": "lines", "start": start_line, "stop": number + 1},
                        complete=False, reason="byte_limit", next_start=number + 1)
                    if len(_encoded(candidate)) > MAX_RETURN_BYTES:
                        if not response["matches"]:
                            raise ValueError("search match exceeds the 32 KiB evidence window")
                        response["next_line"] = number
                        reason = "byte_limit"
                        break
                    response["matches"].append(match)
                response["scanned_lines"] += 1
                if len(response["matches"]) >= max_hits or response["scanned_lines"] >= max_lines:
                    response["next_line"] = number + 1
                    if response["scanned_lines"] < max_lines and stream.read(1):
                        reason = "hit_limit"
                    break
        stop = start_line + response["scanned_lines"]
        response["status"] = "partial" if reason else "observed"
        response["coverage"] = _coverage(response, requested,
            {"kind": "lines", "start": start_line, "stop": stop}, complete=reason is None,
            reason=reason, next_start=stop if reason else None)
        return _finish(response)


def inspect_artifact(store, artifact_id, start_line=1, lines=40):
    from orca_agent.tools.registry import EvidenceTextParameters

    parameters = EvidenceTextParameters(artifact_id=artifact_id, start_line=start_line, lines=lines)
    start_line, lines = parameters.start_line, parameters.lines
    with _source(store, artifact_id) as (artifact, path, started):
        output, used = [], 0
        response = _identity(artifact, "raw_text")
        requested = {"kind": "lines", "start": start_line, "stop": start_line + lines}
        partial = False
        with path.open("rb") as stream:
            for number in range(1, start_line + lines):
                _clock(started)
                raw = stream.readline(MAX_RETURN_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_RETURN_BYTES:
                    raise ValueError("line exceeds the 32 KiB evidence window")
                if number >= start_line:
                    item = {"line": number,
                            "text": raw.decode("utf-8", errors="replace").rstrip("\r\n")}
                    candidate = {**response, "lines": [*output, item],
                                 "returned_bytes": used + len(raw), "status": "partial",
                                 "coverage": _coverage(response, requested,
                                     {"kind": "lines", "start": start_line, "stop": number + 1},
                                     complete=False, reason="byte_limit", next_start=number + 1)}
                    if len(_encoded(candidate)) > MAX_RETURN_BYTES:
                        if not output:
                            raise ValueError("text line including metadata exceeds the 32 KiB evidence window")
                        partial = True
                        break
                    used += len(raw)
                    output.append(item)
        stop = start_line + len(output)
        return _finish({**response, "lines": output, "returned_bytes": used,
                        "status": "partial" if partial else "observed",
                        "coverage": _coverage(response, requested,
                            {"kind": "lines", "start": start_line, "stop": stop},
                            complete=not partial, reason="byte_limit" if partial else None,
                            next_start=stop if partial else None)})


def read_field(store, artifact_id, field):
    """Compatibility reader; typed read_value supports larger JSON and explicit arrays."""
    from orca_agent.tools.registry import EvidenceFieldParameters

    parameters = EvidenceFieldParameters(artifact_id=artifact_id, field=field)
    field = parameters.field
    with _source(store, artifact_id, maximum=1024 * 1024) as (artifact, path, started):
        parts = [{"kind": "key", "key": key} for key in field.split(".")]
        response = {**_identity(artifact, "raw_json", parts), "field": field}
        found, data, parent = _select(_json_data(path, started), parts)
        if not found:
            return _finish({**response, "status": "missing", "coverage": _coverage(
                response, {"kind": "value"}, None, complete=False, reason="missing")})
        coverage = _value_coverage(response, parts, data,
                                   returned_count=min(len(data), 32) if isinstance(data, list) else None)
        if isinstance(data, list):
            data = data[:32]
        return _finish({**response, "status": "observed" if coverage["complete"] else "partial",
                        "value": _bounded_value(data), "coverage": coverage,
                        "units": _units(data, parent),
                        "validation": OBSERVATION + "; arrays limited to first 32 members"})


def list_artifacts(store, run_id, offset=0, limit=40):
    from orca_agent.tools.registry import EvidenceListParameters

    parameters = EvidenceListParameters(run_id=run_id, offset=offset, limit=limit)
    offset, limit = parameters.offset, parameters.limit
    started = time.monotonic()
    run = store.load_run(run_id)
    initial = store.load_request(run).geometry_artifact_id
    ids = [initial] if initial else []
    ids.extend(run.permission.artifact_ids)
    for result_id in run.result_ids:
        _clock(started)
        ids.extend(store.load_result(run.id, result_id).artifact_ids)
    ids = list(dict.fromkeys(ids))
    requested = {"kind": "artifacts", "start": offset, "stop": offset + limit}
    available_stop = max(offset, min(len(ids), offset + limit))
    response = {"run_id": run_id, "inventory_sha256": hashlib.sha256(_encoded(ids)).hexdigest(),
                "offset": offset, "total": len(ids),
                "artifacts": [], "next_offset": None, "validation": OBSERVATION}
    verified_bytes = 0
    for artifact_id in ids[offset:offset + limit]:
        artifact = store.load_artifact(artifact_id)
        item = artifact.model_dump(mode="json")
        if verified_bytes + artifact.size > MAX_SOURCE_BYTES:
            item["integrity"] = {"status": "not_verified", "reason": "content_verification_byte_limit"}
        else:
            verified_bytes += artifact.size
            try:
                store.artifact_path(artifact_id)
                item["integrity"] = {"status": "verified"}
            except (OSError, RuntimeError, ValueError):
                item["integrity"] = {"status": "not_verified", "reason": "content_unavailable_or_changed"}
        _clock(started)
        candidate = {**response, "artifacts": [*response["artifacts"], item],
                     "next_offset": offset + len(response["artifacts"]) + 1,
                     "status": "partial"}
        candidate["coverage"] = _coverage(response, requested,
            {"kind": "artifacts", "start": offset, "stop": candidate["next_offset"]},
            complete=False, reason="byte_limit", next_start=candidate["next_offset"])
        if len(_encoded(candidate)) > MAX_RETURN_BYTES:
            if not response["artifacts"]:
                raise ValueError("artifact metadata exceeds the 32 KiB evidence window")
            break
        response["artifacts"].append(item)
    next_offset = offset + len(response["artifacts"])
    response["next_offset"] = next_offset if next_offset < len(ids) else None
    complete = next_offset >= available_stop
    response["status"] = "observed" if complete else "partial"
    response["coverage"] = _coverage(response, requested,
        {"kind": "artifacts", "start": offset, "stop": next_offset}, complete=complete,
        reason=None if complete else "byte_limit", next_start=None if complete else next_offset)
    return _finish(response)


def import_source(store, source_id, sources: Mapping[str, dict[str, Any]], run_id=None):
    """Copy a caller-authorized source manifest, never a model-selected disk path.

    The Tool invocation supplies the trusted registry mapping and checks the import
    effect, permission and Step before entering here. This operation writes artifacts.
    Missing members remain explicit facts; no original calculation is reconstructed.
    """
    TypeAdapter(Identifier).validate_python(source_id)
    if source_id not in sources:
        raise ValueError("source ID has not been registered by the user")
    from orca_agent.store import _is_link, sha256_file

    manifest = sources[source_id]
    files = manifest.get("files", [])
    if not isinstance(files, list) or not 1 <= len(files) <= 16:
        raise ValueError("source manifest must contain 1 to 16 files")
    total = 0
    prepared, missing = [], []
    for entry in files:
        path = Path(entry["path"])
        if not path.exists():
            missing.append({"file": path.name, "status": "missing"})
            continue
        if not path.is_file() or _is_link(path):
            raise ValueError("source member must be a regular non-link file")
        size = path.stat().st_size
        if size > MAX_SOURCE_BYTES:
            raise ValueError("import source exceeds 16 MiB per file")
        total += size
        if total > 64 * 1024 * 1024:
            raise ValueError("import source exceeds 64 MiB in total")
        actual_hash = sha256_file(path)
        if entry.get("sha256") and entry["sha256"] != actual_hash:
            raise ValueError("registered source hash changed")
        prepared.append((path, entry["role"], actual_hash))
    imported = []
    for path, role, source_hash in prepared:
        if sha256_file(path) != source_hash:
            raise ValueError("registered source hash changed before import")
        artifact = store.import_artifact(path, role, run_id=run_id, expected_sha256=source_hash, source={
            "source_id": source_id, "kind": "user_registered_import",
            "original_file": path.name, "original_sha256": source_hash,
            "original_attempt": "unknown", "original_input": "unknown",
            "original_calculation_time": "unknown", "original_cost": "unknown",
        })
        if artifact.sha256 != source_hash:
            raise ValueError("registered source changed during import; snapshot not bound to source")
        imported.append(artifact.id)
    return _finish({"source_id": source_id, "status": "partial" if missing else "imported",
                    "artifact_ids": imported, "missing": missing,
                    "conditions": "unknown",
                    "scientific_status": "unverified", "validation": OBSERVATION,
                    "effects": ["create_artifact"], "imported_bytes": total})
