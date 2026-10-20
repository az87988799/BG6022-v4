"""Read-only text/provenance diagnosis of the settled T1; no Store or tool calls."""

from datetime import datetime, timezone
import difflib
import hashlib
import json
from pathlib import Path
import re
import sys

from orca_agent.context import _PATH
from orca_agent.models import Request, Run
from orca_agent.proposals import ProposalError
from orca_agent.semantic import (
    SemanticCandidate,
    _field_propositions,
    _general_geometry_context,
    _general_goal_geometry_relation,
    _general_scalar_geometry_relation,
    _validate_general_intake,
    bind_general_text_identity,
)

ROOT = Path(__file__).resolve().parents[4]
REVIEW = Path(__file__).resolve().parent
BATCH = ROOT / "data/code-tools-refreeze-1-batch"
WORK = BATCH / "workspaces/formal-T1-001"
START = json.loads((BATCH / "first-repetition-evidence/T1/start.json").read_bytes())
RUN = WORK / "data/runs" / START["run_id"]


def read(path):
    return json.loads(path.read_bytes())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def reference(path):
    raw = path.read_bytes()
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "bytes": len(raw),
        "sha256": sha(raw),
        "observed_mtime_utc": datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc).isoformat(),
    }


run_data = read(RUN / "run.json")
assert run_data["state"] == "failed" and not run_data["attempts"]
request_data = read(RUN / "request-revisions/1.json")
control = read(RUN / "control.json")
original = request_data["original_text"]
assert original == control["messages"][0]["text"]
assert original.encode() == (WORK / "original.txt").read_bytes()
marker = "[path redacted; use registered reference]"
obligations = [
    "保留平移/转动项",
    "解释是否能支持局部极小值结论",
    "保留原始输入、输出、脚本和失败尝试",
    "分别说明执行、科学检查和验证缺项",
    "不要把程序正常结束当成优化收敛或极小值确认",
    "若预算内不能完成，准确说明未完成部分",
]
records = []
for receipt in run_data["model_records"]:
    path = RUN / "model" / (receipt["id"] + ".request.json")
    document = read(path)
    context = json.loads(document["messages"][1]["content"])
    authority = context["AUTHORITY"]
    projection = authority["user_messages"][0]["message"]["text"]
    diffs = []
    for kind, a, z, b, y in difflib.SequenceMatcher(
            None, original, projection, autojunk=False).get_opcodes():
        if kind != "equal":
            diffs.append({
                "kind": kind, "original_character_range_half_open": [a, z],
                "original_utf8_byte_range_half_open": [
                    len(original[:a].encode()), len(original[:z].encode())],
                "original_text": original[a:z],
                "projected_character_range_half_open": [b, y],
                "projected_text": projection[b:y],
            })
    records.append({
        "model_id": receipt["id"],
        "model_receipt_created_at_utc": receipt["created_at"],
        "model_request_file": reference(path),
        "wire_request_hash_recorded_by_product": receipt["request_hash"],
        "projection_json_pointer": "/messages/1/content -> /AUTHORITY/user_messages/0/message/text",
        "projected_text": projection,
        "projected_text_utf8_sha256": sha(projection.encode()),
        "user_originals_projection_matches": authority["user_originals"][0]["text"] == projection,
        "user_originals_path_redacted": authority["user_originals"][0].get("path_redacted"),
        "frozen_path_regex_reproduces_projection": _PATH.sub(marker, original) == projection,
        "differences": diffs,
        "obligations": {
            text: {"in_original": text in original, "in_projected_user_text": text in projection}
            for text in obligations
        },
    })

# Pure in-memory text validation only. It never calls commit_candidate, Store,
# dispatch, model transport, Python task execution, ORCA, or network APIs.
second = run_data["decisions"][1]
candidate = SemanticCandidate.model_validate(read(
    RUN / "model" / (second["id"] + ".response.json"))["proposal"]["parameters"])
run = Run.model_validate(run_data)
request = bind_general_text_identity(
    Request.model_validate(request_data), run, control["messages"], candidate)
message = control["messages"][0]
relations = []
for item in candidate.goals[1:3]:
    context = _general_geometry_context(request, message, item)
    relations.append({
        "goal_key": item.key,
        "requested_geometry_relation": item.geometry_relation,
        "source_message_is_complete_original": message["text"] == original,
        "property_context": context,
        "context_relation": _general_scalar_geometry_relation(request, message, item, context),
        "whole_original_fallback_relation": _general_scalar_geometry_relation(
            request, message, item, original),
        "final_relation": _general_goal_geometry_relation(request, message, item),
        "uncertainty_trigger_clauses": [
            clause for clause, _ in _field_propositions(
                request, message, 0, len(original), system=request.systems[0])
            if re.search(r"不要|不得|不确定|未知|\b(?:not|unknown|uncertain)\b", clause, re.I)
            and re.search(r"几何|单点|优化|\b(?:geometry|sp|opt|optimization|energy\s+relation)\b",
                          clause, re.I)
        ],
    })
try:
    _validate_general_intake(candidate, request, control["messages"], run)
except ProposalError as exc:
    offline_rejection = exc.detail
else:
    raise AssertionError("Expected actual second-proposal text rejection")

result = {
    "schema_version": 1,
    "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
    "scope": "Read-only archived request comparisons and pure in-memory text validation; no scientific calculation.",
    "script": reference(Path(__file__)),
    "run_id": run.id,
    "original_sources": [reference(WORK / "original.txt"),
                         reference(RUN / "request-revisions/1.json"),
                         reference(RUN / "control.json")],
    "original_text_utf8_sha256": sha(original.encode()),
    "original_message_created_at_utc": message["created_at"],
    "original_text": original,
    "projection_records": records,
    "path_regex": {
        "source": reference(ROOT / "src/orca_agent/context.py"),
        "pattern": _PATH.pattern,
        "matches": [{"character_range_half_open": list(m.span()), "text": m.group()}
                    for m in _PATH.finditer(original)],
        "call_chain": "context._PATH -> _safe -> _original and AUTHORITY.user_messages",
    },
    "geometry_diagnosis": {
        "source": reference(ROOT / "src/orca_agent/semantic.py"),
        "source_chain": "pending_messages reads complete control.json; bind_general_text_identity and _validate_general_intake operate on that text. _general_geometry_context excludes the preceding sentence containing 计算; fallback _text_geometry_relation marks the separate 不要...优化... caveat uncertain and clears selected.",
        "relations": relations,
        "offline_rejection": offline_rejection,
        "matches_actual_second_rejection": offline_rejection == second["parameters"]["requirement"],
        "distinction": "This validation failure is reproduced from complete original text and is not attributed to model-context path redaction.",
    },
    "third_rejection_source": {
        "source": reference(ROOT / "src/orca_agent/semantic.py"),
        "mapping_source": reference(ROOT / "src/orca_agent/minimum_evidence.py"),
        "reason": "_input_goal preserves the exact but unregistered requirement 交付初始结构 as unsupported_minimum_evidence; commit_candidate requires a visible notice/question for the new unresolved entry. The third proposal supplied neither, and the full actual rejection remains in Run.decisions.",
    },
    "limitations": [
        "Source diagnosis is post-terminal. It does not constitute scientific or method preregistration.",
        "No candidate was activated and no product history, policy, prompt or input was changed.",
        "This is new real-round evidence of failure in the frozen candidate; no claim of newly introduced regression or successful repair.",
    ],
}
assert all(item["frozen_path_regex_reproduces_projection"] for item in records)
assert result["geometry_diagnosis"]["matches_actual_second_rejection"]
destination = REVIEW / "t1-source-diagnosis.json"
with destination.open("x", encoding="utf-8", newline="\n") as stream:
    json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    stream.write("\n")
sys.stdout.reconfigure(encoding="utf-8")
print(json.dumps(reference(destination), ensure_ascii=False))
