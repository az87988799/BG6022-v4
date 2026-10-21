"""Append only settled a026 costs to its audited parent; no product execution."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
parent_path = root / "accounting-through-a025.json"
expected = "52e7715b688d46b9f77d3749038b1971479de2993345380840262685aca958e4"
sha = lambda raw: hashlib.sha256(raw).hexdigest()
assert sha(parent_path.read_bytes()) == expected
parent = json.loads(parent_path.read_bytes())
outcome_path = root / "attempts/a026/outcome.json"
outcome = json.loads(outcome_path.read_bytes())
assert not outcome["unknown_fields"] and not outcome["unsettled_execution"]
assert outcome["source_unchanged"] and outcome["usage"]["model_tokens_unknown"] == 0
keys = [key for key in parent["totals"] if key.startswith("budget_occupied_through_")]
assert len(keys) == 1
occupied = parent["totals"][keys[0]]
after = {key: value + outcome["known_usage"][key] for key, value in occupied.items()}
record = {"kind": "incremental_runtime_probe_budget_gate_not_scientific_audit",
          "campaign": parent["campaign"],
          "parent": {"path": str(parent_path), "sha256": expected},
          "increment": {"path": str(outcome_path), "sha256": sha(outcome_path.read_bytes()),
                        "known_usage": outcome["known_usage"], "new_unknown": []},
          "totals": {"budget_occupied_through_a026": after},
          "note": "Historical known and unknown reservations retained from audited parent; p002 rechecks every outcome before execution."}
target = root / "accounting-through-a026-probe-gate.json"
with target.open("x", encoding="utf-8", newline="\n") as stream:
    stream.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"path": str(target), "sha256": sha(target.read_bytes()), "occupied": after}))
