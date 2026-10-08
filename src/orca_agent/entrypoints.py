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
    if not config.text.enabled or not config.text.permission.model_execution:
        raise StoreError("new text requests require the explicitly enabled local text profile")
    return _create_once(store, text, submission_id,
                        lambda: natural.initialize_text(store, config, text))


def _create_once(store, text, submission_id, initialize):
    if submission_id is None:
        return initialize(), True
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
        entry = {"text_sha256": digest, "run_id": None,
                 "created_at": utc_now().isoformat(), "message_source": "run_control"}
        store._write_json(relative, entry)
        run = initialize()
        store._write_json(relative, {**entry, "run_id": run.id})
        return run, True


WATER_SP_TEXT = "直接计算水分子初始几何的单点电子能：气相 RHF/STO-3G，中性单重态；OPI 准备结构。"


def create_water_sp(store, config, *, submission_id):
    """An explicit structured preset, using the existing deterministic loop strategy."""
    from orca_agent.models import (
        BudgetLimits,
        EvidenceRef,
        Goal,
        InputRef,
        OutputBinding,
        Plan,
        Request,
        Step,
        SystemInput,
        new_id,
    )

    def initialize():
        permission = config.text.permission.model_copy(deep=True)
        required = {"structure.resolve", "structure.prepare", "orca.sp"}
        if (not config.text.enabled or not required.issubset(permission.allowed_tools)
                or not all((permission.scientific_execution, permission.artifact_writes,
                            permission.external_identity_queries, permission.geometry_preparation))):
            raise StoreError("water preset requires the enabled local scientific input profile")
        permission.model_execution = False
        permission.allowed_tools = sorted(required)
        permission.allow_additional_science = False
        message_id = new_id("message")
        evidence = {"message_id": message_id, "text_basis": WATER_SP_TEXT}
        identity = {"canonical_names": ["water"]}
        conditions = {"method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1,
                      "electronic_state": "RHF", "environment": "gas_phase"}
        request = Request(original_text=WATER_SP_TEXT,
            conditions={**conditions, "explain_results": False},
            conditions_source={key: "explicit" for key in conditions},
            messages=[{"id": message_id, "text": WATER_SP_TEXT, "source": "user",
                       "created_at": utc_now().isoformat(), "request_version": 1}],
            systems=[SystemInput(id="water", label="water", geometry_source="prepare",
                                 identity=identity)],
            goals=[Goal(id="goal_energy", port="energy", minimum_check_version="orca-hf-2",
                        original_text=WATER_SP_TEXT, identity=identity, text_evidence=evidence,
                        system_ids=["water"], conditions={"geometry_relation": "fixed_initial"})])
        plan = Plan(request_id=request.id, steps=[
            Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="water",
                 parameters={"system_id": "water"}),
            Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="water",
                 parameters={"system_id": "water", "charge": 0, "multiplicity": 1},
                 depends_on=["resolve"], inputs={"identity": EvidenceRef(
                     producer_step_id="resolve", port="resolved_identity")}),
            Step(id="science", logical_id="science", tool="orca.sp", system_id="water",
                 depends_on=["prepare"], geometry=InputRef(
                     producer_step_id="prepare", port="prepared_geometry"))],
            goal_map={"goal_energy": OutputBinding(step_id="science", port="energy")})
        configured = config.text.budget
        budget = BudgetLimits(attempts_per_step=1, orca_starts=min(1, configured.orca_starts),
            extra_orca_starts=0, run_seconds=min(900, configured.run_seconds),
            identity_queries=min(1, configured.identity_queries),
            structure_preparations=min(1, configured.structure_preparations))
        return store.create_run(request, plan, permission, budget,
                                science_baseline_policy="first_science_plan")

    return _create_once(store, WATER_SP_TEXT, submission_id, initialize)


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


def create_question(store, config, source_run_id, text, *, submission_id):
    """An explicitly read-only follow-up, linked to unchanged scientific history."""
    from orca_agent.models import Goal, Request
    if not text.strip() or len(text.encode("utf-8")) > 8192:
        raise StoreError("question must contain 1 to 8192 bytes")
    def initialize():
        source = store.load_run(source_run_id)
        from orca_agent.tools.knowledge import question_source_facts
        facts = question_source_facts(store, source.id)
        import re
        request = Request(original_text=text, goals=[Goal(id="question", port="knowledge_answer",
            minimum_check_version="knowledge-answer-1", original_text=text,
            conditions={"requires_sources": bool(re.search(r"来源链接|引用文献|文献|手册|官方|最新|版本|references?|manual|version", text, re.I))})],
            conditions={"explain_results": True, "read_only_source_run": source.id,
                        "source_fact_view": "query-facts-1",
                        "available_evidence": facts,
                        "query_scope": "Only explain these saved facts; no calculation or scientific permission."})
        permission = config.text.permission.model_copy(deep=True)
        permission.scientific_execution = False
        permission.external_identity_queries = False
        permission.geometry_preparation = False
        permission.allowed_tools = [name for name in ("knowledge.answer", "knowledge.search", "evidence.list",
                                    "evidence.discover", "evidence.value", "evidence.text", "evidence.search")
                                    if name in permission.allowed_tools]
        if any(name.startswith("evidence.") for name in permission.allowed_tools):
            permission.artifact_ids = sorted(artifact_ids(store, source.id))
            request.conditions["authorized_artifacts"] = [{"artifact_id": aid,
                "role": store.load_artifact(aid).role,
                "attempt_id": store.load_artifact(aid).attempt_id,
                "filename": store.artifact_path(aid).name} for aid in permission.artifact_ids]
        budget = config.text.budget.model_copy(update={"orca_starts": 0, "extra_orca_starts": 0,
            "identity_queries": 0, "structure_preparations": 0, "model_calls": min(4, config.text.budget.model_calls)})
        return natural.initialize_agent(store, config, request, permission, budget, defer_environment=True)
    return _create_once(store, source_run_id + "\n" + text, submission_id, initialize)


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
