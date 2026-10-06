"""Bounded artifact inspection. No executable or conversion dependency."""

import json
import time

from orca_agent.tools.registry import (
    EvidenceFieldParameters,
    EvidenceListParameters,
    EvidenceTextParameters,
)


def inspect_artifact(store, artifact_id, start_line=1, lines=40):
    parameters = EvidenceTextParameters(artifact_id=artifact_id, start_line=start_line, lines=lines)
    start_line, lines = parameters.start_line, parameters.lines
    artifact = store.load_artifact(artifact_id)
    if artifact.size > 32 * 1024 * 1024:
        raise ValueError("text inspection is limited to artifacts up to 32 MiB")
    started = time.monotonic()
    path = store.artifact_path(artifact_id)
    output, used = [], 0
    with path.open("rb") as stream:
        for number in range(1, start_line + lines):
            if time.monotonic() - started > 5:
                raise TimeoutError("evidence read exceeded 5 seconds")
            raw = stream.readline(32769)
            if not raw:
                break
            if len(raw) > 32768:
                raise ValueError("line exceeds the 32 KiB evidence window")
            if number >= start_line:
                if used + len(raw) > 32768:
                    break
                used += len(raw)
                output.append({"line": number, "text": raw.decode("utf-8", errors="replace").rstrip()})
    store.artifact_path(artifact_id)
    return {"artifact_id": artifact.id, "sha256": artifact.sha256, "role": artifact.role,
            "validation": "raw evidence observation; not a scientific qualification",
            "lines": output, "returned_bytes": used, "source": artifact.source}


def read_field(store, artifact_id, field):
    parameters = EvidenceFieldParameters(artifact_id=artifact_id, field=field)
    field = parameters.field
    artifact = store.load_artifact(artifact_id)
    if artifact.size > 1024 * 1024:
        raise ValueError("known-field reading is limited to JSON artifacts up to 1 MiB")
    parts = field.split(".")
    path = store.artifact_path(artifact_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            store.artifact_path(artifact_id)
            return {"artifact_id": artifact_id, "field": field, "status": "missing"}
        data = data[part]
    if isinstance(data, list):
        data = data[:32]
    if len(json.dumps(data, ensure_ascii=False).encode()) > 32768:
        raise ValueError("field exceeds the 32 KiB observation window")
    store.artifact_path(artifact_id)
    return {"artifact_id": artifact_id, "sha256": artifact.sha256, "field": field,
            "status": "observed", "value": data,
            "validation": "unverified observation; arrays limited to first 32 members"}


def list_artifacts(store, run_id, offset=0, limit=40):
    parameters = EvidenceListParameters(run_id=run_id, offset=offset, limit=limit)
    offset, limit = parameters.offset, parameters.limit
    run = store.load_run(run_id)
    ids = [store.load_request(run).geometry_artifact_id]
    for result_id in run.result_ids:
        ids.extend(store.load_result(run.id, result_id).artifact_ids)
    return {"run_id": run_id, "offset": offset, "total": len(ids),
            "artifacts": [store.load_artifact(a).model_dump(mode="json")
                          for a in ids[offset:offset + limit]]}
