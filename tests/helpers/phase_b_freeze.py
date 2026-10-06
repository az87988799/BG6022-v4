"""Read-only verification of the explicit formal acceptance freeze."""

import hashlib
import json
from pathlib import Path

from orca_agent.store import sha256_file

PROJECT = Path(__file__).resolve().parents[2]
FREEZE = PROJECT / "docs/acceptance/phase-b/formal-freeze.json"


def freeze_hash(path, *, source_text=False):
    """Git may change source line endings; raw scientific evidence stays byte-exact."""
    data = Path(path).read_bytes()
    if source_text:
        data = data.replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def validate_freeze(label):
    if not FREEZE.is_file():
        raise ValueError("formal evaluation requires a saved code/profile/reference freeze")
    record = json.loads(FREEZE.read_text(encoding="utf-8"))
    if record["freeze_label"] != label:
        raise ValueError("formal evaluation label differs from the active freeze")
    if not record.get("code_commit") or not record.get("files"):
        raise ValueError("formal freeze lacks its code or file manifest")
    for name, digest in record["files"].items():
        path = (PROJECT / name).resolve()
        source_text = name in record.get("source_lf_normalization", [])
        if (not path.is_relative_to(PROJECT) or not path.is_file()
                or freeze_hash(path, source_text=source_text) != digest):
            raise ValueError(f"frozen evaluation file differs: {name}")
    return {"freeze_label": label, "code_commit": record["code_commit"],
            "freeze_sha256": sha256_file(FREEZE)}
