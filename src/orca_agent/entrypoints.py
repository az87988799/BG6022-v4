"""Shared user entry functions; no executor, scientific policy, or model loop here."""

import hashlib

from filelock import FileLock

from orca_agent import natural, runner
from orca_agent.models import utc_now
from orca_agent.report import _safe, build_report
from orca_agent.store import StoreError, _id
from orca_agent.tools.registry import dispatch_evidence


def create_text(store, config, text, *, submission_id=None):
    """Persist an intake key before work; an uncertain intake never silently repeats."""
    if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 8192:
        raise StoreError("text must contain 1 to 8192 UTF-8 bytes")
    if submission_id is None:
        return natural.initialize_text(store, config, text), True
    _id(submission_id)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    relative = f"sessions/{submission_id}.json"
    with FileLock(str(store.path(f"sessions/{submission_id}.lock")), timeout=5):
        if store.path(relative).exists():
            entry = store._read_json(relative)
            if entry["text_sha256"] != digest:
                raise StoreError("submission ID already names different content")
            if not entry.get("run_id"):
                raise StoreError("interrupted intake requires review; it cannot be submitted twice")
            return store.load_run(entry["run_id"]), False
        if not config.text.enabled or not config.text.permission.model_execution:
            raise StoreError("new text requests require the explicitly enabled local text profile")
        entry = {"text_sha256": digest, "run_id": None,
                 "created_at": utc_now().isoformat(), "message_source": "run_control"}
        store._write_json(relative, entry)
        run = natural.initialize_text(store, config, text)
        store._write_json(relative, {**entry, "run_id": run.id})
        return run, True


def message(store, run_id, text, *, update=None, message_id=None):
    from orca_agent.semantic import CANCEL, PAUSE, STATUS
    command = text.strip().casefold()
    if update is None and command in STATUS:
        return {"run_id": run_id, "status": "status", "state": store.load_run(run_id).state}
    if update is None and command in PAUSE | CANCEL:
        return control(store, run_id, "cancel" if command in CANCEL else "pause")
    identity = store.enqueue_message(run_id, text, update=update, message_id=message_id)
    return {"run_id": run_id, "message_id": identity, "status": "queued",
            "execution": "active coordinator observes the message; otherwise explicitly resume"}


def control(store, run_id, action):
    if action not in {"pause", "cancel"}:
        raise StoreError("only pause and cancel are control signals")
    store.signal(run_id, action)
    return {"run_id": run_id, "requested": action,
            "confirmation": "requested; inspect Run state for acknowledgement"}


def execute(store, config, run_id, *, resume=False, **kwargs):
    return runner.execute(store, config, run_id, resume=resume, **kwargs)


def status(store, run_id):
    run = store.load_run(run_id)
    plan = store.load_plan(run)
    return {"run_id": run.id, "state": run.state, "goal_status": run.goal_status,
            "delivery_status": run.delivery_status,
            "control_requested": store.read_signal(run.id),
            "clarification": _safe(store.active_clarification(run)),
            "steps": [s.model_dump(mode="json") for s in plan.steps] if plan else [],
            "attempts": [{"step_id": a.step_id, "attempt_id": a.id, "state": a.state,
                          "number": a.number} for a in run.attempts],
            "budget": run.budget.model_dump(mode="json"),
            "usage": run.usage.model_dump(mode="json"), "deadline": run.deadline.isoformat()}


def report(store, run_id):
    return build_report(store, run_id)


def messages(store, run_id, *, offset=0, limit=40):
    if not 0 <= offset <= 512 or not 1 <= limit <= 100:
        raise StoreError("message page outside bounds")
    run = store.load_run(run_id)
    history = store.read_control(run_id)["messages"]
    page = history[offset:offset + limit]
    return {"items": [_safe({**m, "processed": m["id"] in run.processed_messages}) for m in page],
            "total": len(history), "offset": offset,
            "next_offset": offset + len(page) if offset + len(page) < len(history) else None}


def artifact_ids(store, run_id):
    run = store.load_run(run_id)
    ids = set(run.permission.artifact_ids)
    request = store.load_request(run)
    ids.update(s.geometry_artifact_id for s in request.systems if s.geometry_artifact_id)
    if request.geometry_artifact_id:
        ids.add(request.geometry_artifact_id)
    for result_id in run.result_ids:
        ids.update(store.load_result(run.id, result_id).artifact_ids)
    return ids


def evidence(store, run_id, tool, parameters):
    if tool not in {"evidence.list", "evidence.discover", "evidence.value", "evidence.field",
                    "evidence.text", "evidence.search"}:
        raise StoreError("only registered read operations are available")
    if tool == "evidence.list":
        if parameters.get("run_id", run_id) != run_id:
            raise StoreError("evidence listing must refer to the selected Run")
        parameters = {**parameters, "run_id": run_id}
    elif parameters.get("artifact_id") not in artifact_ids(store, run_id):
        raise StoreError("Artifact is outside the selected Run")
    return dispatch_evidence(store, tool, parameters)


def download(store, run_id, artifact_id):
    if artifact_id not in artifact_ids(store, run_id):
        raise StoreError("Artifact is outside the selected Run")
    artifact = store.load_artifact(artifact_id)
    if artifact.size > 16 * 1024 * 1024:
        raise StoreError("download exceeds the 16 MiB window; use local evidence path")
    path = store.artifact_path(artifact_id)
    with path.open("rb") as stream:
        content = stream.read(16 * 1024 * 1024 + 1)
    if len(content) != artifact.size or hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise StoreError("Artifact changed during download")
    store.artifact_path(artifact_id)
    return content, path.name, artifact.sha256
