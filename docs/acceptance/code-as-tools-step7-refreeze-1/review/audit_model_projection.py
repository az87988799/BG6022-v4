"""Audit saved model payloads against a preregistered user text, read-only."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib


def strings(value, path=""):
    if isinstance(value, str):
        yield path, value
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return
        if isinstance(parsed, (dict, list)):
            yield from strings(parsed, path)
    elif isinstance(value, dict):
        for key, child in value.items():
            yield from strings(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from strings(child, f"{path}[{index}]")


def decode_keys(value, keys):
    if isinstance(value, list):
        return [decode_keys(child, keys) for child in value]
    if isinstance(value, dict):
        return {
            keys.get(key, key): child if key in {"KEYS", "SHARED_STRINGS"} else decode_keys(child, keys)
            for key, child in value.items()
        }
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task")
    parser.add_argument("--suffix", required=True)
    args = parser.parse_args()
    review = pathlib.Path(__file__).resolve().parent
    workspace = review.parents[3]
    registration = json.loads((review / "multi-task-preregistration.json").read_text(encoding="utf-8"))
    task = next(item for item in registration["tasks"] if item["task_id"] == args.task)
    start_path = workspace / "data/code-tools-refreeze-1-batch/first-repetition-evidence" / args.task / "start.json"
    start = json.loads(start_path.read_text(encoding="utf-8"))
    run_root = pathlib.Path(start["data_root"]) / "runs" / start["run_id"]
    rows = []
    for path in sorted((run_root / "model").glob("*.request.json"), key=lambda p: p.stat().st_mtime_ns):
        data = path.read_bytes()
        saved = json.loads(data.decode("utf-8"))
        key_encoding_decoded = False
        for message in saved.get("messages", []):
            content = message.get("content")
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and isinstance(parsed.get("KEYS"), dict):
                    message["content"] = decode_keys(parsed, parsed["KEYS"])
                    key_encoding_decoded = True
        leaves = list(strings(saved))
        # Container serialized strings do not count as a separately identified
        # alternative source; report the explicit nested leaf path instead.
        complete = [
            location for location, text in leaves
            if task["original_text"] in text
            and not location.endswith(".content")
            and not text.startswith("{")
        ]
        authority = [
            {"path": location, "text": text}
            for location, text in leaves
            if (".AUTHORITY.user_messages[" in location and location.endswith(".message.text"))
            or (".AUTHORITY.user_originals[" in location and location.endswith(".text"))
        ]
        leaf_map = dict(leaves)
        for entry in authority:
            text = entry["text"]
            entry["resolved_text"] = text
            if text.startswith("@") and text[1:].isdigit():
                prefix = entry["path"].split(".AUTHORITY.", 1)[0]
                reference = f"{prefix}.SHARED_STRINGS[{int(text[1:])}]"
                if reference in leaf_map:
                    entry["resolved_text"] = leaf_map[reference]
                    entry["shared_string_source"] = reference
        rows.append({
            "source": str(path),
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "declared_key_encoding_decoded": key_encoding_decoded,
            "authority_texts": authority,
            "complete_original_source_paths": complete,
            "has_complete_original_source": bool(complete),
            "authority_preserves_full_original": None if not authority else all(
                task["original_text"] in entry["resolved_text"] for entry in authority
            ),
        })
    output = review / f"{args.task.lower()}-model-projection-audit-{args.suffix}.json"
    payload = {
        "observed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "task": args.task,
        "scope": "All model request records saved at observation time; exact full original task text is checked at explicit leaf paths, including alternative sources. No inference that absent exact full text implies every individual obligation was absent.",
        "original_text": task["original_text"],
        "original_text_sha256": task["original_text_sha256"],
        "run_id": start["run_id"],
        "request_count": len(rows),
        "all_requests_have_complete_original_source": bool(rows) and all(row["has_complete_original_source"] for row in rows),
        "rows": rows,
    }
    with output.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({
        "path": str(output),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "request_count": len(rows),
        "all_requests_have_complete_original_source": payload["all_requests_have_complete_original_source"],
        "authority_loss_requests": [row["source"] for row in rows if row["authority_preserves_full_original"] is False],
        "authority_path_unidentified_requests": [row["source"] for row in rows if row["authority_preserves_full_original"] is None],
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
