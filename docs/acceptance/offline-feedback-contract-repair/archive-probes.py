"""Archive bounded original bytes; excluded binaries remain in the local Run."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
SOURCE = ROOT / "data/offline-feedback-contract-repair-validation"
TEXT_SUFFIXES = {".json", ".out", ".err", ".inp", ".xyz", ".py", ".txt", ".log"}


def main():
    label = sys.argv[1]
    assert label in {"initial", "native-repair-followup"}
    destination = OUT / "evidence" / label
    assert not destination.exists()
    cases = sorted(path for path in SOURCE.iterdir()
                   if path.is_dir() and (path / "evidence.json").is_file())
    if label == "native-repair-followup":
        cases = [path for path in cases if path.name.startswith("test_native_nmr")]
    selected, excluded, summaries = [], [], []
    for case in cases:
        evidence = json.loads((case / "evidence.json").read_bytes())
        summaries.append({"case": case.name, "run_id": evidence["run"]["id"],
                          "outcome": evidence["outcome"]["state"],
                          "reason": evidence["outcome"].get("reason"),
                          "usage": evidence["run"]["usage"]})
        for path in sorted(case.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(SOURCE)
            # Artifact copies are the immutable original-byte archive. Avoid
            # duplicating the same task files from mutable execution directories.
            if "steps" in relative.parts or path.suffix in {".lock", ".lck"}:
                continue
            raw = path.read_bytes()
            info = {"path": relative.as_posix(), "bytes": len(raw),
                    "sha256": hashlib.sha256(raw).hexdigest()}
            keep = path.suffix in TEXT_SUFFIXES and len(raw) <= 1024 * 1024
            if keep:
                try:
                    raw.decode("utf-8")
                except UnicodeDecodeError:
                    keep = False
            (selected if keep else excluded).append(info)
    assert sum(item["bytes"] for item in selected) <= 32 * 1024 * 1024
    for item in selected:
        target = destination / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE / item["path"], target)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item["sha256"]
    index = {"kind": "bounded original-byte developer evidence, not an autonomous acceptance bundle",
             "label": label, "cases": summaries, "files": selected, "excluded": excluded,
             "exclusion": "Only bounded UTF-8 source/receipts/raw text; binary ORCA files and duplicate execution-directory copies stay local"}
    (destination / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(json.dumps({"label": label, "files": len(selected),
                      "bytes": sum(item["bytes"] for item in selected),
                      "cases": summaries}, indent=2))


if __name__ == "__main__":
    main()
