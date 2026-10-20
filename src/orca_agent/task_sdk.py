"""Thin, bounded task IPC; the current Run coordinator owns every execution."""

import base64
import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

PROTOCOL_VERSION = 2
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_EXCHANGES = 1024
_INFLIGHT = threading.Lock()


def _exchange(request):
    if not _INFLIGHT.acquire(blocking=False):
        raise RuntimeError("concurrent SDK requests are not supported; submit sequentially")
    try:
        encoded = json.dumps(request, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("SDK request exceeds the bounded transport size")
        request_root = Path(os.environ["ORCA_TASK_REQUESTS"])
        response_root = Path(os.environ["ORCA_TASK_RESPONSES"])
        for sequence in range(1, MAX_EXCHANGES + 1):
            try:
                with (request_root / f"submitted-{sequence:06d}").open("xb"):
                    pass
                break
            except FileExistsError:
                continue
        else:
            raise RuntimeError("SDK exchange count exhausted")
        name = f"request-{sequence:06d}.json"
        with (request_root / (name + ".pending")).open("xb") as stream:
            stream.write(encoded)
        os.replace(request_root / (name + ".pending"), request_root / name)
        response_path = response_root / f"response-{sequence:06d}.json"
        deadline = time.monotonic() + float(os.environ["ORCA_TASK_TIMEOUT_SECONDS"])
        while not response_path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("coordinator response deadline exhausted")
            time.sleep(0.05)
        with response_path.open("rb") as stream:
            data = stream.read(MAX_RESPONSE_BYTES + 1)
        if len(data) > MAX_RESPONSE_BYTES:
            raise ValueError("coordinator response exceeds the bounded transport size")
        response = json.loads(data)
        if not isinstance(response, dict):
            raise ValueError("coordinator response must be an object")
        if response.get("error"):
            raise RuntimeError(response["error"])
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":"),
                                           allow_nan=False).encode("utf-8")).hexdigest()
        if (response.get("request_key") != request["request_key"]
                or response.get("input_fingerprint") != digest):
            raise ValueError("coordinator response does not match this input request")
        return response
    finally:
        _INFLIGHT.release()


def run_orca(input_path, *, dependencies=(), request_key="calculation",
             purpose="new_candidate", retry_of=None):
    """Submit one explicit input. Reuse the stable key only for identical inputs.

    Finished receipts may be reused across attempts. A failed calculation needs
    a new key, purpose='failure_retry', and retry_of pointing to the failed key.
    Process completion does not confer scientific qualification.
    """
    return _exchange({
        "version": PROTOCOL_VERSION, "kind": "orca", "request_key": request_key,
        "purpose": purpose, "retry_of": retry_of,
        "input": Path(input_path).read_text(encoding="utf-8"),
        "dependencies": [
            {"target_path": str(path).replace("\\", "/"),
             "content_base64": base64.b64encode(Path(path).read_bytes()).decode("ascii")}
            for path in dependencies],
    })


def receipt_source(receipt, filename, *, location=None):
    """Declare one explicitly selected original receipt file as an observation source.

    This helper does not read files or certify a receipt, its conditions or its
    scientific output. Collection independently resolves the declaration against
    archived current-Run evidence. Failed settled receipts may describe failures.
    """
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a run_orca response object")
    key = receipt.get("request_key")
    if not isinstance(key, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", key) is None:
        raise ValueError("receipt request_key is invalid")
    status = receipt.get("operation_status")
    if not isinstance(status, str) or status not in {"completed", "failed", "timed_out", "cancelled"}:
        raise ValueError("observation source requires a settled receipt")
    if (not isinstance(filename, str) or not 1 <= len(filename) <= 240
            or "\\" in filename or ":" in filename
            or any(part in {"", ".", ".."} for part in filename.split("/"))):
        raise ValueError("filename must be an original task-relative files key")
    files = receipt.get("files")
    if (not isinstance(files, dict) or filename not in files
            or not isinstance(files[filename], str) or not files[filename]):
        raise ValueError("filename is not an original receipt files key")
    if location is not None and (not isinstance(location, str) or len(location) > 1000):
        raise ValueError("location must be a string of at most 1000 characters or None")
    source = {"internal_request_key": key, "filename": filename}
    if location is not None:
        source["location"] = location
    return source


def checkpoint(state, *, request_key="checkpoint", requires_decision=True, reason=None):
    """Persist JSON state and completed receipt keys; yield for a new decision.

    This is data, not a Python stack snapshot. Continue in a new explicit attempt
    using the saved state and stable completed request keys.
    """
    response = _exchange({"version": PROTOCOL_VERSION, "kind": "checkpoint",
                          "request_key": request_key, "state": state,
                          "requires_decision": requires_decision, "reason": reason})
    if requires_decision:
        raise SystemExit(0)
    return response
