"""Freeze the successfully probed image while retaining original selection bytes."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent
sha = lambda raw: hashlib.sha256(raw).hexdigest()
read = lambda path: json.loads(path.read_bytes())
selection = root / "runtime/selection.json"
original = selection.read_bytes()
previous = json.loads(original)
assert previous["image_id"] == "sha256:5ae21d2b67503cb41e6f74dd62a51368afb5afbfbcebfec574ff460534c1b712"
build = read(root / "runtime/build-r2/result.json")
probe = root / "probes/p002"
outcome = read(probe / "outcome.json")
meta = read(probe / "probe.json")
assert outcome["runtime_probe_passed"] and outcome["source_unchanged"]
assert not outcome["error"] and not outcome["unknown_fields"] and not outcome["unsettled_execution"]
assert meta["runtime"] == {key: build[key] for key in ("image_id", "manifest_sha256")}
assert read(probe / "case-result.json")["input_hashes_unchanged"]
backup = root / "runtime/selection-r1-before-r2.json"
with backup.open("xb") as stream:
    stream.write(original)
record = {**meta["runtime"], "python_runtime_layout": "isolated-1", "verified_live": True,
          "probe": "p002", "probe_outcome_sha256": sha((probe / "outcome.json").read_bytes()),
          "source_manifest_sha256": meta["source_manifest_sha256"], "known_usage": outcome["known_usage"],
          "previous_selection": {"path": str(backup), "sha256": sha(original)},
          "verification_scope": "Python namespace separation and runtime collection only; native execution proof remains separately recorded in p001."}
assert selection.read_bytes() == original
selection.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(record, ensure_ascii=False))
