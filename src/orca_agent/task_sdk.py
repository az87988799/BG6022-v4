"""Copied into the isolated runtime as ``orca_task_sdk``; no host imports.

The SDK transports one explicit native ORCA request. Authorization, budgets,
paths, resources and execution are checked independently by the coordinator.
It contains no scientific workflow or output parser.
"""

import base64
import hashlib
import json
import os
import time
from pathlib import Path

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024


def run_orca(input_path, *, dependencies=(), request_key="calculation"):
    """Submit a native input file and explicitly named relative dependencies.

    Returns an execution receipt and paths to existing raw output copies.
    A completed process is not a scientific qualification.
    """
    request_root = Path(os.environ["ORCA_TASK_REQUESTS"])
    response_root = Path(os.environ["ORCA_TASK_RESPONSES"])
    if (request_root / "request.json").exists() or (response_root / "response.json").exists():
        raise RuntimeError("only one SDK ORCA request is enabled per task execution")
    with (request_root / "submitted").open("xb"):
        pass
    request = {
        "version": PROTOCOL_VERSION,
        "request_key": request_key,
        "input": Path(input_path).read_text(encoding="utf-8"),
        "dependencies": [
            {"target_path": str(path).replace("\\", "/"),
             "content_base64": base64.b64encode(Path(path).read_bytes()).decode("ascii")}
            for path in dependencies
        ],
    }
    encoded = json.dumps(request, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError("SDK request exceeds the bounded transport size")
    target = request_root / "request.json"
    # The coordinator enforces the same one-call limit even if this SDK is bypassed.
    with (request_root / "request.pending").open("xb") as stream:
        stream.write(encoded)
    os.replace(request_root / "request.pending", target)
    response_path = response_root / "response.json"
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
    if response.get("request_key") != request_key or response.get("input_fingerprint") != digest:
        raise ValueError("coordinator response does not match this input request")
    return response
