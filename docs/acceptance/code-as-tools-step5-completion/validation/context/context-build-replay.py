"""Replay captured local build_context arguments; no Store writes or transport.

This evaluates the checked-out source, not an archived historical source tree.
The original v68 outcome is recorded in context-build-verification.json.
"""

import hashlib
import json
import pickle
from pathlib import Path

from orca_agent import context


def main():
    inputs = Path(__file__).with_name("context-replay-inputs.pkl")
    raw = inputs.read_bytes()
    expected = json.loads(inputs.with_name("context-provenance.json").read_bytes())
    expected_hash = next(row["sha256"] for row in expected["files"]
        if Path(row["copy_path"]).name == inputs.name)
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("captured local input hash differs from provenance")
    args, kwargs = pickle.loads(raw)  # Verified developer-owned local capture only.
    objects = [args[1].model_dump(mode="json"),
        *[result.model_dump(mode="json") for result in kwargs["results"]]]
    before = context._hash(objects)
    prepared = context.build_context(*args, **kwargs)
    after = context._hash([args[1].model_dump(mode="json"),
        *[result.model_dump(mode="json") for result in kwargs["results"]]])
    if before != after:
        raise ValueError("context construction changed captured Run/Results")
    print(json.dumps({"prompt_version": prepared.prompt_version,
        "input_token_bound": prepared.input_token_bound,
        "input_token_limit": args[1].budget.input_tokens,
        "reserved_tokens": prepared.reserved_tokens,
        "wire_bytes": len(prepared.body()["messages"][-1]["content"].encode()),
        "run_results_sha256_before": before, "run_results_sha256_after": after,
        "api_calls": 0, "orca_starts": 0}, indent=2))


if __name__ == "__main__":
    main()
