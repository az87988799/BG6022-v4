"""Bounded acceptance bookkeeping only; never generates or executes task solutions.

One frozen environment root anchors the batch identity. A caller deliberately
choosing a different environment root is outside this local helper's guarantee.
Reservations are never refunded; immutable receipts detect missing history or
interrupted publication. Unknown usage stays null and occupies its full envelope.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from orca_agent.config import Config
from orca_agent.models import BudgetLimits, Goal, Request, Run, SystemInput
from orca_agent.store import Store, _is_link, atomic_write, controlled_path

METRICS = ("model_calls", "model_tokens", "orca_starts", "internal_work_units", "wall_seconds")
CATEGORIES = {"formal", "reference", "control"}
MAX_ROWS = 512
MAX_BYTES = 4 * 1024 * 1024


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("utf-8")


def _same(left, right):
    return _bytes(left) == _bytes(right)


def canonical_sha256(value):
    """Hash the exact canonical JSON representation used by this helper."""
    return hashlib.sha256(_bytes(value)).hexdigest()


def text_sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("invalid batch/slot identity")
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise ValueError("invalid frozen digest")
    return value


def _root(value):
    path = Path(value).absolute()
    if any(_is_link(item) for item in (path, *path.parents)):
        raise ValueError("batch paths cannot contain symlinks or junctions")
    return path.resolve()


def _read(path):
    if _is_link(path) or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError("missing, linked or oversized batch evidence")
    from orca_agent.llm import _strict_json
    return _strict_json(path.read_bytes())


def _write(path, value, *, immutable=False):
    data = _bytes(value)
    if len(data) > MAX_BYTES:
        raise ValueError("batch evidence exceeds its byte bound")
    atomic_write(path, data, immutable=immutable)


def _amount(value, name):
    if (type(value) not in ({int, float} if name == "wall_seconds" else {int})
            or not math.isfinite(value) or not 0 <= value <= 2**53):
        raise ValueError("invalid usage or reservation: " + name)
    return value


def _envelope(values):
    if not isinstance(values, dict) or set(values) != set(METRICS):
        raise ValueError("reservation must include all five cumulative metrics")
    return {name: _amount(values[name], name) for name in METRICS}


def _budget(value, reservation, *, allow_legacy=True):
    """Freeze every Run limit, including retries and non-accounting allowances."""
    fields = set(BudgetLimits.model_fields)
    accepted = [fields, fields - {"file_writes"}] if allow_legacy else [fields]
    if not isinstance(value, dict) or set(value) not in accepted:
        raise ValueError("each slot requires complete frozen BudgetLimits")
    validated = BudgetLimits.model_validate(value).model_dump(mode="json")
    if not _same(value, validated):
        raise ValueError("frozen Run budget must use its exact validated representation")
    amounts = {"model_calls": validated["model_calls"], "model_tokens": validated["model_tokens"],
               "orca_starts": validated["orca_starts"], "internal_work_units": validated["internal_work_units"],
               "wall_seconds": validated["run_seconds"]}
    _envelope(amounts)
    if any(amounts[name] > reservation[name] for name in METRICS):
        raise ValueError("frozen Run budget exceeds the slot reservation")
    if validated["extra_orca_starts"] > validated["orca_starts"]:
        raise ValueError("extra ORCA starts must remain inside the total ORCA envelope")
    return validated


def slot_id(task_id, repeat, category):
    _identifier(task_id)
    if category not in CATEGORIES or type(repeat) is not int or not 1 <= repeat <= MAX_ROWS:
        raise ValueError("invalid frozen task repetition/category")
    return f"{category}-{task_id}-{repeat:03d}"


def _policy(value):
    expected = {"version", "batch_id", "configuration_sha256", "limits", "slots"}
    if not isinstance(value, dict) or set(value) != expected or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("invalid batch policy schema")
    _identifier(value["batch_id"])
    _digest(value["configuration_sha256"])
    limits = value["limits"]
    if (not isinstance(limits, dict) or set(limits) != {"rows", *METRICS}
            or type(limits["rows"]) is not int or not 1 <= limits["rows"] <= MAX_ROWS):
        raise ValueError("invalid bounded batch limits")
    _envelope({name: limits[name] for name in METRICS})
    if not isinstance(value["slots"], list) or not 1 <= len(value["slots"]) <= limits["rows"]:
        raise ValueError("invalid bounded batch slot count")
    seen = set()
    for row in value["slots"]:
        if not isinstance(row, dict) or set(row) != {
                "task_id", "repeat", "category", "original_text_sha256", "reservation", "run_budget"}:
            raise ValueError("invalid frozen slot")
        key = slot_id(row["task_id"], row["repeat"], row["category"])
        if key.casefold() in seen:
            raise ValueError("duplicate frozen slot identity")
        seen.add(key.casefold())
        _digest(row["original_text_sha256"])
        envelope = _envelope(row["reservation"])
        if any(envelope[name] > limits[name] for name in METRICS):
            raise ValueError("slot reservation exceeds whole-batch limit")
        _budget(row["run_budget"], envelope)
    return deepcopy(value)


def create_batch(root, *, freeze_sha256, policy, environment_root):
    """Create once or reopen the exact batch. An existing anchor forbids reset."""
    root, environment = _root(root), _root(environment_root)
    policy = _policy(policy)
    identity = {"version": 1, "batch_id": policy["batch_id"], "root": str(root),
        "environment_root": str(environment), "freeze_sha256": _digest(freeze_sha256),
        "policy_sha256": canonical_sha256(policy)}
    anchor_dir = controlled_path(environment, "code-tools-batches")
    anchor_dir.mkdir(parents=True, exist_ok=True)
    anchor = controlled_path(anchor_dir, policy["batch_id"] + ".json")
    with FileLock(str(anchor.with_suffix(".lock")), timeout=10):
        if anchor.exists():
            if not _same(_read(anchor), identity):
                raise ValueError("frozen batch anchor differs; paths/identity cannot reset the batch")
            return snapshot(root)
        for row in policy["slots"]:
            _budget(row["run_budget"], row["reservation"], allow_legacy=False)
        if root.exists() and any(root.iterdir()):
            raise ValueError("new batch root must be empty")
        root.mkdir(parents=True, exist_ok=True)
        # Publish the anchor first. A crash or missing ledger then fails closed.
        _write(anchor, identity, immutable=True)
        _write(root / "identity.json", identity, immutable=True)
        _write(root / "policy.json", policy, immutable=True)
        _write(root / "ledger.json", {"version": 1, "rows": {}}, immutable=True)
    return snapshot(root)


def _load(root):
    root = _root(root)
    identity = _read(controlled_path(root, "identity.json"))
    if (set(identity) != {"version", "batch_id", "root", "environment_root", "freeze_sha256", "policy_sha256"}
            or identity["root"] != str(root)):
        raise ValueError("batch identity or root differs")
    environment = _root(identity["environment_root"])
    anchor = controlled_path(environment, "code-tools-batches/" + _identifier(identity["batch_id"]) + ".json")
    if not _same(_read(anchor), identity):
        raise ValueError("frozen environment anchor differs")
    policy = _policy(_read(controlled_path(root, "policy.json")))
    if canonical_sha256(policy) != identity["policy_sha256"] or policy["batch_id"] != identity["batch_id"]:
        raise ValueError("frozen policy changed")
    _digest(identity["freeze_sha256"])
    ledger = _read(controlled_path(root, "ledger.json"))
    if (not isinstance(ledger, dict) or set(ledger) != {"version", "rows"}
            or type(ledger["version"]) is not int or ledger["version"] != 1
            or not isinstance(ledger["rows"], dict) or len(ledger["rows"]) > policy["limits"]["rows"]):
        raise ValueError("invalid bounded ledger")
    slots = {slot_id(row["task_id"], row["repeat"], row["category"]): row for row in policy["slots"]}
    receipts = controlled_path(root, "receipts")
    actual = {entry.name for entry in receipts.iterdir()} if receipts.exists() else set()
    if actual != set(ledger["rows"]):
        raise ValueError("missing or orphan reservation; reconcile without restarting")
    run_ids = []
    for key, row in ledger["rows"].items():
        if (key not in slots or not isinstance(row, dict)
                or set(row) != {"slot", "run_id", "settlement", "workspace"}
                or not _same(row["slot"], slots[key])):
            raise ValueError("slot classification or reservation changed")
        directory = controlled_path(receipts, key)
        if not _same(_read(controlled_path(directory, "reservation.json")), row["slot"]):
            raise ValueError("immutable reservation differs")
        expected = {"reservation.json"}
        for field, filename in (("run_id", "run.json"), ("settlement", "settlement.json"), ("workspace", "workspace.json")):
            if row[field] is not None:
                expected.add(filename)
                if not _same(_read(controlled_path(directory, filename)), row[field]):
                    raise ValueError("immutable " + field + " receipt differs")
        if {entry.name for entry in directory.iterdir()} != expected:
            raise ValueError("orphan settlement/binding; reconcile without restarting")
        if row["run_id"] is not None:
            _identifier(row["run_id"])
            run_ids.append(row["run_id"])
        if row["settlement"] is not None:
            _settlement(row["settlement"])
        if row["workspace"] is not None:
            _workspace(root, key, row["workspace"], identity)
    if len(run_ids) != len({value.casefold() for value in run_ids}):
        raise ValueError("independent slots cannot share a Run identity")
    return root, identity, policy, ledger


def _totals(ledger):
    reserved = dict.fromkeys(METRICS, 0)
    occupied = dict.fromkeys(METRICS, 0)
    known = dict.fromkeys(METRICS, 0)
    unknown = dict.fromkeys(METRICS, 0)
    for row in ledger["rows"].values():
        for name, amount in row["slot"]["reservation"].items():
            value = row["settlement"]["usage"][name] if row["settlement"] else None
            reserved[name] += amount
            occupied[name] += max(amount, value if value is not None else 0)
            known[name] += value if value is not None else 0
            unknown[name] += value is None
    return {"reserved": reserved, "occupied": occupied, "known_usage": known, "unknown_slots": unknown}


def snapshot(root):
    root = _root(root)
    with FileLock(str(controlled_path(root, "batch.lock")), timeout=10):
        _, identity, policy, ledger = _load(root)
        totals = _totals(ledger)
        return {"identity": identity, "policy": policy, **ledger, "totals": totals,
                "overrun_metrics": [name for name in METRICS
                                    if totals["occupied"][name] > policy["limits"][name]]}


def reserve_slot(root, task_id, repeat, category):
    """Idempotently reserve a fixed slot; changing a Run ID cannot create more slots."""
    root, key = _root(root), slot_id(task_id, repeat, category)
    with FileLock(str(controlled_path(root, "batch.lock")), timeout=10):
        _, _, policy, ledger = _load(root)
        if key in ledger["rows"]:
            return {"slot_id": key, **deepcopy(ledger["rows"][key])}
        candidates = [row for row in policy["slots"] if slot_id(row["task_id"], row["repeat"], row["category"]) == key]
        if len(candidates) != 1:
            raise ValueError("task/repetition/category is not a frozen slot")
        slot = candidates[0]
        totals = _totals(ledger)["occupied"]
        if any(totals[name] + slot["reservation"][name] > policy["limits"][name] for name in METRICS):
            raise ValueError("whole-batch reservation exhausted")
        directory = controlled_path(root, "receipts/" + key)
        directory.mkdir(parents=True, exist_ok=False)
        _write(directory / "reservation.json", slot, immutable=True)
        ledger["rows"][key] = {"slot": slot, "run_id": None, "settlement": None, "workspace": None}
        _write(root / "ledger.json", ledger)
        return {"slot_id": key, **deepcopy(ledger["rows"][key])}


def _bind(root, key, field, value, filename):
    root = _root(root)
    with FileLock(str(controlled_path(root, "batch.lock")), timeout=10):
        _, _, _, ledger = _load(root)
        if key not in ledger["rows"]:
            raise ValueError("slot must be reserved first")
        row = ledger["rows"][key]
        if field == "run_id" and any(other_key != key and other["run_id"] is not None
                                    and other["run_id"].casefold() == value.casefold()
                                    for other_key, other in ledger["rows"].items()):
            raise ValueError("independent slots cannot share a Run identity")
        if row[field] is not None:
            if not _same(row[field], value):
                raise ValueError("immutable slot " + field + " cannot be replaced")
            return deepcopy(row)
        if field == "run_id":
            if row["workspace"] is None or row["settlement"] is not None:
                raise ValueError("Run binding requires its prepared, unsettled workspace")
        # The production Store allocates Run IDs at creation. Bind only that
        # untouched shell, while its coordinator and control locks are held.
        with _fresh_run(row, value) if field == "run_id" else nullcontext():
            _write(controlled_path(root, "receipts/" + key + "/" + filename), value, immutable=True)
            row[field] = value
            _write(root / "ledger.json", ledger)
        return deepcopy(row)


def bind_run(root, key, run_id):
    """Bind the sole untouched Run shell before any model/Tool action; reuse on resume."""
    return _bind(root, key, "run_id", _identifier(run_id), "run.json")


def _initial_request(request, config, original):
    """Accept raw intake only, never a structured goal or solution injected in JSON."""
    from orca_agent.natural import _affirmative_text_identity
    from orca_agent.semantic import _mentions
    from orca_agent.tools.registry import SCIENCE_IDENTITIES

    raw = Request(id=request.id, original_text=original, charge=None, multiplicity=None,
        method=None, basis=None, normalization_status="pending", conditions_source={},
        goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1",
                    original_text=original, unresolved=["missing:goal_definition"])])
    # The direct Store shell has no semantic intake yet. initialize_text adds
    # only the frozen defaults, explanation flag and existing lexical identity.
    if request == raw:
        return
    names = sorted({entry[2] for entry in _mentions(original, targets=True)})
    systems = []
    if len(names) == 1 and names[0] in SCIENCE_IDENTITIES and _affirmative_text_identity(original):
        systems = [SystemInput(id=names[0], label=names[0], geometry_source="prepare",
                               identity={"canonical_names": names})]
    raw.systems = systems
    raw.semantic_defaults = dict(config.text.defaults)
    raw.conditions = {"explain_results": True}
    if request != raw:
        raise ValueError("fresh Run Request includes prewritten goals, inputs or solution context")


@contextmanager
def _fresh_run(row, run_id):
    manifest = row["workspace"]
    data_root = _root(manifest["data_root"])
    runs = controlled_path(data_root, "runs")
    if ({item.name for item in data_root.iterdir()} != {"runs"} or not runs.is_dir()
            or {item.name for item in runs.iterdir()} != {run_id}):
        raise ValueError("fresh Run data root contains injected or previous task files, or no unique Run")
    directory = controlled_path(runs, run_id)
    if not directory.is_dir():
        raise ValueError("fresh Run directory is missing")
    config = Config.model_validate(_read(controlled_path(data_root.parent, "config.json")))
    store = Store(data_root, environment_root=manifest["environment_root"],
                  configuration=config.run_configuration())
    with store.run_lock(run_id), store.control_lock(run_id):
        required = {"run.json", "configuration.json", "permission.json", "budget.json",
                    "request-revisions", "coordinator.lock", "control.lock"}
        names = {item.name for item in directory.iterdir()}
        if not required <= names or names - required - {"sources.json", "control.json"}:
            raise ValueError("fresh Run contains missing or unknown files; no Plan/input/output is allowed")
        for name in names - {"request-revisions"}:
            path = controlled_path(directory, name)
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                raise ValueError("fresh Run metadata must be bounded regular files")
            if name.endswith(".lock") and path.stat().st_size:
                raise ValueError("fresh Run lock contains injected data")
        revisions = controlled_path(directory, "request-revisions")
        if not revisions.is_dir() or {item.name for item in revisions.iterdir()} != {"1.json"}:
            raise ValueError("fresh Run requires only its initial Request")
        run = Run.model_validate(_read(controlled_path(directory, "run.json")))
        request = Request.model_validate(_read(controlled_path(revisions, "1.json")))
        original = controlled_path(data_root.parent, "original.txt").read_bytes().decode("utf-8")
        if (run.id != run_id or run.request_id != request.id
                or run.request_version != 1 or request.version != 1
                or request.original_text != original
                or request.geometry_artifact_id is not None
                or any(system.geometry_artifact_id is not None for system in request.systems)
                or request.messages or request.condition_evidence):
            raise ValueError("fresh Run Request differs from its original text or imports prior inputs")
        _initial_request(request, config, original)
        expected = {
            "configuration.json": config.run_configuration().model_dump(mode="json"),
            "permission.json": config.text.permission.model_dump(mode="json"),
            "budget.json": row["slot"]["run_budget"],
        }
        for name, value in expected.items():
            if not _same(_read(controlled_path(directory, name)), value):
                raise ValueError("fresh Run frozen configuration, permission or complete budget differs")
        if (run.configuration != config.run_configuration() or run.permission != config.text.permission
                or not _same(run.budget.model_dump(mode="json"), row["slot"]["run_budget"])):
            raise ValueError("fresh Run configuration, permission or complete budget differs")
        allowed_category = "formal" if row["slot"]["category"] == "formal" else None
        if run.batch_category not in {None, allowed_category}:
            raise ValueError("fresh Run category differs from its frozen slot")
        # Comparing the complete default shell also rejects unknown usage, prior
        # decisions/results, Plan/Attempt IDs, processed messages and revisions.
        pristine = Run(id=run.id, request_id=request.id, request_version=1,
            configuration=config.run_configuration(), permission=config.text.permission,
            budget=config.text.budget, created_at=run.created_at, deadline=run.deadline,
            goal_status={goal.id: "insufficient_evidence" for goal in request.goals},
            agent_enabled=run.agent_enabled, batch_category=run.batch_category,
            science_baseline_policy=run.science_baseline_policy)
        pristine = Run.model_validate(pristine.model_dump())
        if not _same(run.model_dump(mode="json"), pristine.model_dump(mode="json")):
            raise ValueError("Run has actions, Plan, Attempt, usage or non-initial state before binding")
        now = datetime.now(timezone.utc)
        if (run.created_at.utcoffset() is None or run.deadline.utcoffset() is None
                or run.created_at > now):
            raise ValueError("fresh Run timestamps must be real timezone-aware creation facts")
        seconds = (run.deadline - run.created_at).total_seconds()
        if not 0 < seconds <= config.text.budget.run_seconds:
            raise ValueError("fresh Run deadline exceeds its frozen wall budget")
        if "sources.json" in names and _read(controlled_path(directory, "sources.json")) != {}:
            raise ValueError("fresh Run cannot import a source registry")
        if "control.json" in names:
            control = _read(controlled_path(directory, "control.json"))
            if not isinstance(control, dict):
                raise ValueError("fresh Run control metadata must be an object")
            messages = control.get("messages")
            if (set(control) != {"action", "generation", "messages"} or control["action"] is not None
                    or type(control["generation"]) is not int or control["generation"] != 1
                    or not isinstance(messages, list) or len(messages) != 1):
                raise ValueError("fresh Run permits only its one original pending user message")
            message = messages[0]
            if (not isinstance(message, dict)
                    or set(message) != {"id", "text", "source", "created_at", "request_version"}
                    or message["text"] != original or message["source"] != "user"
                    or type(message["request_version"]) is not int or message["request_version"] != 1):
                raise ValueError("fresh Run pending message differs or grants an injected update")
            _identifier(message["id"])
            try:
                created = datetime.fromisoformat(message["created_at"])
                if created.utcoffset() is None or not run.created_at <= created <= now:
                    raise ValueError("invalid initial message timestamp")
            except (TypeError, ValueError) as exc:
                raise ValueError("fresh Run initial message timestamp differs") from exc
        yield


def _settlement(value):
    if (not isinstance(value, dict) or set(value) != {"usage", "unknown_reasons"}
            or not isinstance(value["usage"], dict) or set(value["usage"]) != set(METRICS)
            or not isinstance(value["unknown_reasons"], dict)):
        raise ValueError("invalid bounded settlement")
    unknown = {name for name, amount in value["usage"].items() if amount is None}
    if set(value["unknown_reasons"]) != unknown:
        raise ValueError("each unknown metric requires its own retained explanation")
    for name, amount in value["usage"].items():
        if amount is not None:
            _amount(amount, name)
        elif (not isinstance(value["unknown_reasons"][name], str)
              or not 1 <= len(value["unknown_reasons"][name]) <= 2000):
            raise ValueError("unknown usage needs a bounded nonempty explanation")
    return value


def settle_slot(root, key, *, known_usage, unknown_fields):
    """Record known amounts and {metric:reason} unknowns; no refund or failure erasure."""
    if not isinstance(known_usage, dict) or not isinstance(unknown_fields, dict) or set(known_usage) & set(unknown_fields):
        raise ValueError("known and unknown usage must be disjoint")
    value = _settlement({"usage": {**known_usage, **dict.fromkeys(unknown_fields)},
                         "unknown_reasons": dict(unknown_fields)})
    return _bind(root, key, "settlement", value, "settlement.json")


def prepare_workspace(root, key, *, original_text, configuration, run_budget):
    """Create a fresh task workspace; only raw text, frozen config and limits enter it.

    Only config.data_root and config.text.budget are deterministically replaced.
    The later driver must use the returned shared environment_root explicitly.
    This helper starts no Run, model or program.
    """
    root = _root(root)
    config = Config.model_validate(configuration)
    budget = BudgetLimits.model_validate(run_budget)
    with FileLock(str(controlled_path(root, "batch.lock")), timeout=10):
        _, identity, policy, ledger = _load(root)
        row = ledger["rows"].get(key)
        if row is None or row["workspace"] is not None or row["run_id"] is not None or row["settlement"] is not None:
            raise ValueError("workspace requires a fresh reserved slot")
        if (not isinstance(original_text, str) or not original_text.strip()
                or len(original_text.encode("utf-8")) > 65536
                or text_sha256(original_text) != row["slot"]["original_text_sha256"]):
            raise ValueError("task text differs from the frozen slot")
        source_config = config.model_dump(mode="json")
        if canonical_sha256(source_config) != policy["configuration_sha256"]:
            raise ValueError("configuration differs from the frozen policy")
        actual_budget = _budget(run_budget, row["slot"]["reservation"])
        if not _same(actual_budget, row["slot"]["run_budget"]):
            raise ValueError("Run budget differs from the complete frozen slot budget")
        workspace = controlled_path(root, "workspaces/" + key)
        workspace.mkdir(parents=True, exist_ok=False)
        data_root = controlled_path(workspace, "data")
        data_root.mkdir()
        source_config["data_root"] = str(data_root)
        source_config["text"]["budget"] = budget.model_dump(mode="json")
        payloads = {"original.txt": original_text.encode("utf-8"),
                    "config.json": _bytes(source_config), "run-budget.json": _bytes(budget.model_dump(mode="json"))}
        manifest = {"slot_id": key, "freeze_sha256": identity["freeze_sha256"],
            "policy_sha256": identity["policy_sha256"], "data_root": str(data_root),
            "environment_root": identity["environment_root"],
            "source_configuration_sha256": policy["configuration_sha256"],
            "configuration_changes": ["data_root", "text.budget"],
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()}}
        for name, data in payloads.items():
            atomic_write(controlled_path(workspace, name), data, immutable=True)
        _write(workspace / "workspace.json", manifest, immutable=True)
        _write(controlled_path(root, "receipts/" + key + "/workspace.json"), manifest, immutable=True)
        row["workspace"] = manifest
        _write(root / "ledger.json", ledger)
        return deepcopy(manifest)


def _workspace(root, key, manifest, identity):
    """Check immutable driver inputs without traversing later product output."""
    directory = controlled_path(root, "workspaces/" + key)
    if (not isinstance(manifest, dict) or manifest.get("slot_id") != key
            or manifest.get("freeze_sha256") != identity["freeze_sha256"]
            or manifest.get("policy_sha256") != identity["policy_sha256"]
            or manifest.get("environment_root") != identity["environment_root"]
            or manifest.get("data_root") != str(controlled_path(directory, "data"))
            or manifest.get("configuration_changes") != ["data_root", "text.budget"]
            or not isinstance(manifest.get("files"), dict)
            or set(manifest["files"]) != {"original.txt", "config.json", "run-budget.json"}
            or not _same(_read(controlled_path(directory, "workspace.json")), manifest)):
        raise ValueError("workspace binding or frozen input manifest differs")
    if {entry.name for entry in directory.iterdir()} != {*manifest["files"], "workspace.json", "data"}:
        raise ValueError("workspace contains injected files outside its product data root")
    for name, digest in manifest["files"].items():
        path = controlled_path(directory, name)
        if not path.is_file() or path.stat().st_size > MAX_BYTES or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("frozen workspace input changed")
    if not controlled_path(directory, "data").is_dir():
        raise ValueError("workspace data root is missing")
