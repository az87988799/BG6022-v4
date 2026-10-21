"""Append a bounded LF review diff of frozen candidate source; never edit originals."""
import argparse
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / "data/live-repair"
FILE_LIMIT = 262144


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def projection(raw):
    raw.decode("utf-8")
    assert b"\x00" not in raw, "Binary source requires separate review"
    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    assert raw.count(b"\r") == crlf, "Bare CR source requires separate review"
    kind = "mixed" if 0 < crlf < lf else "CRLF" if crlf else "LF" if lf else "none"
    projected = raw.replace(b"\r\n", b"\n")
    newline_map = {}
    if kind == "mixed":
        minority = "CRLF" if crlf <= lf - crlf else "LF"
        positions = [i for i, line in enumerate(raw.split(b"\n")[:-1], 1)
                     if line.endswith(b"\r") == (minority == "CRLF")]
        newline_map = {"minority_ending": minority, "line_numbers": positions}
        marked = set(positions)
        lines = projected.split(b"\n")
        restored = b"".join(line + (b"\r\n" if ((i in marked) == (minority == "CRLF")) else b"\n")
                             for i, line in enumerate(lines[:-1], 1)) + lines[-1]
    else:
        restored = projected.replace(b"\n", b"\r\n") if kind == "CRLF" else projected
    assert restored == raw
    return projected, {"sha256": sha(raw), "bytes": len(raw), "newline": kind,
                       "lf_count": lf, "crlf_count": crlf, **newline_map,
                       "review_projection_sha256": sha(projected)}


def allowed(name):
    assert isinstance(name, str) and not re.search(r"[\\:]", name)
    assert all(part not in {"", ".", ".."} for part in name.split("/"))
    assert name.startswith(("src/", "scripts/container_runtime/"))
    assert "__pycache__" not in name.split("/") and not name.endswith((".pyc", ".pyo"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("candidate", help="One explicit frozen candidate, for example a022")
    parser.add_argument("--write", action="store_true", help="Create two new files exclusively; default is read-only")
    args = parser.parse_args()
    assert re.fullmatch(r"a[0-9]{3}", args.candidate)
    folder = CONTROL / "attempts" / args.candidate
    manifest_path = folder / "attempt.json"
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    snapshot = Path(manifest["snapshot"]).resolve()
    assert snapshot.name == args.candidate
    base = manifest["base_commit"]
    assert re.fullmatch(r"[0-9a-f]{40}", base)
    assert subprocess.check_output(["git", "rev-parse", f"{base}^{{commit}}"], cwd=ROOT).decode().strip() == base
    source_ids = {name: record["sha256"] for name, record in manifest["source_files"].items()}
    assert sha(json.dumps(source_ids, sort_keys=True, separators=(",", ":")).encode()) == manifest["source_manifest_sha256"]
    frozen = {}
    for name, identity in manifest["source_files"].items():
        allowed(name)
        path = (snapshot / name).resolve()
        assert path.is_relative_to(snapshot)
        raw = path.read_bytes()
        assert sha(raw) == identity["sha256"] and len(raw) == identity["bytes"], "Frozen source changed"
        frozen[name] = raw
    baseline_names = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", base, "--",
        "src", "scripts/container_runtime"], cwd=ROOT).decode("utf-8").splitlines()
    baseline_names = {name for name in baseline_names if "__pycache__" not in name.split("/")
                      and not name.endswith((".pyc", ".pyo"))}
    files, chunks, changed = [], [], []
    for name in sorted(baseline_names | frozen.keys()):
        allowed(name)
        original = subprocess.check_output(["git", "show", f"{base}:{name}"], cwd=ROOT) if name in baseline_names else None
        captured = frozen.get(name)
        old, before = projection(original) if original is not None else (b"", None)
        new, after = projection(captured) if captured is not None else (b"", None)
        files.append({"path": name, "baseline": before, "frozen": after,
                      "review_text_changed": old != new,
                      "newline_only_change": original != captured and old == new})
        if old != new:
            changed.append(name)
            chunks.append(b"diff --git a/" + name.encode() + b" b/" + name.encode() + b"\n")
            chunks.extend(difflib.diff_bytes(difflib.unified_diff,
                old.splitlines(keepends=True), new.splitlines(keepends=True),
                fromfile=("a/" + name).encode() if original is not None else b"/dev/null",
                tofile=("b/" + name).encode() if captured is not None else b"/dev/null", n=3))
    patch = b"".join(chunks)
    record = {"schema": "frozen-source-review-diff-1", "candidate": args.candidate,
        "base_commit": base, "source_manifest_sha256": manifest["source_manifest_sha256"],
        "attempt_metadata_sha256": sha(manifest_raw), "snapshot_local_ref": snapshot.as_posix(),
        "patch_sha256": sha(patch), "bytes": len(patch), "source_files": files,
        "changed_review_files": changed, "frozen_source_files_checked": len(frozen),
        "contract": "Review-only unified diff of UTF-8 source after CRLF-to-LF projection. This is not a byte-exact git-apply patch. Original baseline and frozen byte hashes, byte lengths and newline styles are preserved per file; mixed newlines include one-based line positions for the less frequent ending. Originals are never normalized or written. Each projection is round-trip verified against exact original bytes.",
        "originals_unchanged": True, "audit_script_sha256": sha(Path(__file__).read_bytes())}
    metadata = (json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    assert len(patch) <= FILE_LIMIT and len(metadata) <= FILE_LIMIT, "Bounded source review exceeds file cap"

    def verify_originals():
        assert manifest_path.read_bytes() == manifest_raw
        assert all((snapshot / name).read_bytes() == raw for name, raw in frozen.items())

    verify_originals()
    targets = ((folder / "source.patch", patch), (folder / "source-patch.json", metadata))
    assert not any(path.exists() for path, _ in targets), "Append only: prior source evidence exists"
    if args.write:
        for path, raw in targets:
            with path.open("xb") as stream:
                stream.write(raw)
        verify_originals()
    print(json.dumps({"candidate": args.candidate, "written": args.write, "base_commit": base,
        "source_files_checked": len(frozen), "changed_review_files": changed,
        "newline_only_files": sum(row["newline_only_change"] for row in files),
        "files": [{"path": path.relative_to(ROOT).as_posix(), "sha256": sha(raw), "bytes": len(raw)}
                  for path, raw in targets], "originals_unchanged": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
