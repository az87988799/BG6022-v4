"""User-owned natural-language requests and authenticated multi-turn corrections.

Bundles specify purpose, evidence and permission, never an execution Plan. Missing
physical information stays unknown until explicitly supplied by the user.
"""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from orca_agent.models import (
    BudgetLimits,
    Goal,
    Identifier,
    PermissionSnapshot,
    Record,
    Request,
    SystemInput,
    require_electronic_integer,
)
from orca_agent.store import StoreError, _is_link, atomic_write, sha256_file
from orca_agent.tools.registry import get_tool

_PHYSICAL = ("method", "basis", "charge", "multiplicity")
_SCIENTIFIC_RULES = {"orca-hf-1", "orca-hf-2"}


def _validate_new_electronic_conditions(conditions):
    """Reject coercible new scalars while historical raw dictionaries remain readable."""
    for name in ("charge", "multiplicity"):
        value = conditions.get(name)
        if value is not None and value != "unknown":
            require_electronic_integer(value)


def agent_budget(**overrides):
    return BudgetLimits.model_validate({"orca_starts": 4, "extra_orca_starts": 3,
        "attempts_per_step": 3, "model_calls": 8, "plan_revisions": 2,
        "model_tokens": 48000, "input_tokens": 12000, "output_tokens": 2000,
        "decision_rounds": 12, "evidence_reads": 24, "analysis_executions": 8,
        "corrections_per_proposal": 1, "transport_retries": 1, **overrides})


def _missing_information(request):
    """Maintain only program-generated missing markers; preserve user uncertainty."""
    request = request.model_copy(deep=True)
    systems = {system.id: system for system in request.systems}
    for goal in request.goals:
        goal.unresolved = [item for item in goal.unresolved if not item.startswith(("missing:", "applicability:"))]
        if goal.minimum_check_version == "unresolved-1":
            goal.unresolved.append("missing:goal_definition")
        if goal.port in {"sampling", "sampling_check"} and not (
                goal.conditions.get("sampling") and goal.conditions.get("candidates")):
            goal.unresolved.append("missing:sampling_specification")
        if goal.minimum_check_version not in _SCIENTIFIC_RULES:
            continue
        selected = goal.system_ids or list(systems) or [None]
        unbound_identity = False
        if not goal.system_ids and goal.identity:
            names = set(goal.identity.get("canonical_names", []))
            if goal.identity.get("explicitly_unknown"):
                unbound_identity = True
            elif names and systems:
                # Use the same finite name binding as semantic grounding, not
                # registry uniqueness alone. Legacy identity={} keeps its old
                # input rules; multiple registered inputs need an explicit ID.
                from orca_agent.semantic import _registered_identity
                unbound_identity = not (len(systems) == len(names) == 1
                    and _registered_identity(next(iter(systems.values()))) in names)
            if unbound_identity:
                selected = [None]
        for system_id in selected:
            system = systems.get(system_id) if system_id is not None else None
            suffix = f":{system_id}" if system_id is not None else ""
            if system_id is not None and system is None:
                goal.unresolved.append(f"missing:system{suffix}")
                continue
            geometry = (None if unbound_identity else
                        system.geometry_artifact_id if system else request.geometry_artifact_id)
            if geometry is None and not (system and system.geometry_source == "prepare"):
                goal.unresolved.append(f"missing:geometry{suffix}")
            for name in _PHYSICAL:
                value = system.conditions.get(name, getattr(request, name)) if system else getattr(request, name)
                if value is None:
                    goal.unresolved.append(f"missing:{name}{suffix}")
        goal.unresolved = list(dict.fromkeys(goal.unresolved))
        from orca_agent.applicability import effective_conditions
        for system_id in selected:
            assessed = effective_conditions(request, goal, system_id)
            goal.unresolved.extend("applicability:" + reason for reason in assessed["reasons"]
                                   if reason.startswith(("unsupported_condition:", "conflicting_")))
        goal.unresolved = list(dict.fromkeys(goal.unresolved))
    request.normalization_status = ("clarification" if request.unresolved
                                    or any(goal.unresolved for goal in request.goals) else "normalized")
    return Request.model_validate(request.model_dump())


class SourceFile(Record):
    path: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    role: Annotated[str, Field(strict=True, min_length=1, max_length=100)]
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


class SourceManifest(Record):
    files: Annotated[list[SourceFile], Field(min_length=1, max_length=16)]


def _source_registry(sources, permission, *, base=None):
    """Register user files and known hashes without creating derived artifacts."""
    sources = {} if sources is None else sources
    if not isinstance(sources, dict) or len(sources) > 16:
        raise StoreError("source registry is bounded to 16 user source IDs")
    if set(sources) != set(permission.source_ids):
        raise StoreError("source registry differs from the explicit source permission")
    registered = {}
    for source_id, value in sources.items():
        TypeAdapter(Identifier).validate_python(source_id)
        manifest = SourceManifest.model_validate(value)
        size = 0
        for member in manifest.files:
            original = Path(member.path)
            if base is not None:
                if original.is_absolute():
                    raise StoreError("bundle source must be inside its user input directory")
                original = base / original
                if not original.resolve().is_relative_to(base):
                    raise StoreError("bundle source must be inside its user input directory")
            if _is_link(original):
                raise StoreError("registered source cannot be a link")
            if original.exists():
                if not original.is_file() or original.stat().st_size > 16 * 1024 * 1024:
                    raise StoreError("registered source must be a file of at most 16 MiB")
                size += original.stat().st_size
                if size > 64 * 1024 * 1024:
                    raise StoreError("registered source exceeds 64 MiB in total")
                observed_hash = sha256_file(original)
                if member.sha256 is not None and member.sha256 != observed_hash:
                    raise StoreError("registered source hash changed")
                member.sha256 = observed_hash
            member.path = str(original.resolve())
        registered[source_id] = manifest.model_dump(mode="json")
    return registered


def _authorized_geometry(store, request, permission):
    geometry_ids = {request.geometry_artifact_id, *(s.geometry_artifact_id for s in request.systems)} - {None}
    if not geometry_ids.issubset(permission.artifact_ids):
        raise StoreError("Request geometry is outside the unchanged permission snapshot")
    for artifact_id in geometry_ids:
        path = store.artifact_path(artifact_id)
        if path.stat().st_size > 65536:
            raise StoreError("geometry exceeds 64 KiB")


def initialize_agent(store, config, request, permission, budget=None, *, sources=None,
                     batch_category=None, defer_environment=False, science_baseline_policy="legacy"):
    """Permission and purpose enter here from the user, never from model proposals."""
    pending = request.normalization_status == "pending"
    request = _missing_information(Request.model_validate(request.model_dump()))
    if pending:
        request.normalization_status = "pending"
    permission = PermissionSnapshot.model_validate(permission.model_dump())
    budget = BudgetLimits.model_validate((budget or agent_budget()).model_dump())
    if not permission.model_execution:
        raise StoreError("natural-language Agent requires explicit model permission")
    for name in permission.allowed_tools:
        get_tool(name)
    registered = _source_registry(sources, permission)
    _authorized_geometry(store, request, permission)
    if batch_category not in {None, "formal", "development"}:
        raise StoreError("invalid acceptance batch category")
    environment = None
    if permission.scientific_execution and not defer_environment:
        from orca_agent.doctor import diagnose
        from orca_agent.versions import is_supported_orca_version
        environment = diagnose(config)
        if not environment["orca"]["compatible"] or not is_supported_orca_version(
                environment["orca"].get("version")):
            raise StoreError("ORCA installation is missing, incompatible or unverified")
        if not config.mpi_path or not config.mpi_path.is_file():
            raise StoreError("parallel execution requires the explicit MPI executable")
        environment["orca"]["sha256"] = sha256_file(config.orca_path)
        environment["mpi"]["sha256"] = sha256_file(config.mpi_path)
    elif not permission.scientific_execution:
        budget = BudgetLimits.model_validate({**budget.model_dump(),
                                              "orca_starts": 0, "extra_orca_starts": 0})
    run = store.create_run(request, None, permission, budget,
                           science_baseline_policy=science_baseline_policy)
    if environment:
        import json
        atomic_write(store.path(f"runs/{run.id}/environment.json"),
                     (json.dumps(environment, ensure_ascii=False, indent=2) + "\n").encode(), immutable=True)
    store._write_json(f"runs/{run.id}/sources.json", registered, immutable=True)
    run.agent_enabled = True
    run.batch_category = batch_category
    store.save_run(run)
    return run


class GeometryInput(Record):
    id: Identifier
    file: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    conditions: dict = Field(default_factory=dict)


class RequestBundle(Record):
    text: str = Field(strict=True, min_length=1, max_length=8192)
    geometries: list[GeometryInput] = Field(default_factory=list, max_length=5)
    artifact_ids: list[Identifier] = Field(default_factory=list, max_length=16)
    result_ids: list[Identifier] = Field(default_factory=list, max_length=16)
    goals: list[Goal] | None = Field(default=None, min_length=1, max_length=8)
    semantic_defaults: dict = Field(default_factory=dict)
    conditions: dict = Field(default_factory=dict)
    conditions_source: dict[str, Literal["explicit", "default", "inherited", "inferred"]] = Field(default_factory=dict)
    unresolved: list[str] = Field(default_factory=list, max_length=16)
    allowed_tools: list[str] = Field(default_factory=list, max_length=64)
    scientific_execution: bool = Field(default=False, strict=True)
    artifact_writes: bool = Field(default=False, strict=True)
    allowed_repairs: dict[str, list[int]] = Field(default_factory=dict)
    allow_additional_science: bool = Field(default=False, strict=True)
    sources: dict = Field(default_factory=dict)
    budget: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len({item.id for item in self.geometries}) != len(self.geometries):
            raise ValueError("geometry identities must be unique")
        if not self.text.strip():
            raise ValueError("natural request text cannot be blank")
        for conditions in (self.conditions, self.semantic_defaults,
                           *(item.conditions for item in self.geometries),
                           *(goal.conditions for goal in self.goals or [])):
            _validate_new_electronic_conditions(conditions)
        return self


def initialize_bundle(store, config, path: Path):
    """Load a user bundle with natural text, purpose and permissions, never Steps."""
    path = Path(path).resolve(strict=True)
    if path.stat().st_size > 65536:
        raise StoreError("request bundle exceeds 64 KiB")
    bundle = RequestBundle.model_validate_json(path.read_text(encoding="utf-8"))
    for name in bundle.allowed_tools:
        get_tool(name)
    permission = PermissionSnapshot(model_execution=True, allowed_tools=bundle.allowed_tools,
        scientific_execution=bundle.scientific_execution, artifact_writes=bundle.artifact_writes,
        artifact_ids=bundle.artifact_ids, result_ids=bundle.result_ids, source_ids=list(bundle.sources),
        allowed_repairs=bundle.allowed_repairs, allow_additional_science=bundle.allow_additional_science)
    sources = _source_registry(bundle.sources, permission, base=path.parent)
    geometries = []
    for item in bundle.geometries:
        original = path.parent / item.file
        source = original.resolve(strict=True)
        if Path(item.file).is_absolute() or not source.is_relative_to(path.parent) or _is_link(original):
            raise StoreError("bundle geometry must be a non-link file inside its user input directory")
        if not source.is_file() or source.stat().st_size > 65536:
            raise StoreError("geometry must be a file of at most 64 KiB")
        geometries.append((item, source))
    systems = []
    for item, source in geometries:
        artifact = store.import_artifact(source, "initial_geometry")
        systems.append(SystemInput(id=item.id, geometry_artifact_id=artifact.id,
                                   conditions=item.conditions,
                                   conditions_source={name: "explicit" for name in item.conditions}))
    conditions = {name: bundle.conditions.get(name) for name in _PHYSICAL}
    provenance = {name: bundle.conditions_source.get(name, "explicit")
                  for name, value in bundle.conditions.items() if value is not None}
    if len(systems) == 1:
        for name in _PHYSICAL:
            if name not in bundle.conditions and name in systems[0].conditions:
                conditions[name] = systems[0].conditions[name]
                provenance[name] = "inherited"
    if systems:
        provenance["geometry"] = "explicit"
    raw = bundle.goals is None
    request = Request(original_text=bundle.text, systems=systems,
                      geometry_artifact_id=systems[0].geometry_artifact_id if len(systems) == 1 else None,
                      **conditions, conditions_source=provenance,
                      goals=bundle.goals or [Goal(id="raw_request", port="unresolved",
                          minimum_check_version="unresolved-1", original_text=bundle.text,
                          unresolved=["missing:goal_definition"])],
                      conditions=bundle.conditions, unresolved=bundle.unresolved,
                      semantic_defaults=bundle.semantic_defaults,
                      normalization_status="pending" if raw else "structured")
    permission.artifact_ids = list(dict.fromkeys(permission.artifact_ids
                                  + [system.geometry_artifact_id for system in systems]))
    run = initialize_agent(store, config, request, permission, agent_budget(**bundle.budget),
                           sources=sources, defer_environment=raw)
    if raw:
        store.enqueue_message(run.id, bundle.text)
    return run


def _affirmative_text_identity(text, *, answer=False):
    import re

    if answer and re.search(r"[?？]", text):
        return False
    return not re.search(
        r"(?:不知道|不确定|未确定)[^，,。.;；\n]{0,20}(?:身份|哪个|分子|体系|名称|是水|是甲烷)|"
        r"(?:身份|名称)(?:目前|仍|尚)?(?:为|是)?(?:未知|不确定|未确定|未确认)|"
        r"(?:分子|体系)(?:目前|仍|尚)?(?:为|是)?未知|"
        r"(?:可能|也许|是否|不是|并非)(?:是|为|叫|这个|那个|在说的)?(?:水|甲烷)|"
        r"\b(?:maybe|perhaps|possibly|probably|whether)\s+(?:(?:it|is|the|molecule)\s+){0,4}"
        r"(?:water|methane|h2o|ch4)\b|"
        r"\b(?:identity|molecule|target|system|name)\s+(?:(?:is|remains|still|now|currently)\s+)*"
        r"\b(?:unknown|uncertain|unconfirmed|undetermined)\b|"
        r"\b(?:not|never|isn['’]?t)\s+(?:(?:actually|the|a|choose|select|use)\s+){0,3}"
        r"(?:water|methane|h2o|ch4)\b|"
        r"\b(?:water|methane|h2o|ch4)\b[^,.;\n]{0,16}\b(?:not|isn['’]?t)\b"
        r"[^,.;\n]{0,16}\b(?:target|molecule|system)\b|"
        r"(?:不要|不选|不用)(?:选择|选用|选)?(?:水|甲烷)|"
        r"\b(?:is|could|might)\s+(?:it|this(?:\s+molecule)?|that(?:\s+molecule)?|"
        r"the\s+(?:molecule|target))\s+(?:be\s+)?(?:water|methane|h2o|ch4)\b|"
        r"\b(?:it|molecule|target|system)\s+(?:may|might|could)\s+be\s+(?:water|methane|h2o|ch4)\b|"
        r"(?:是|为)(?:水|甲烷)(?:分子)?吗", text, re.I)


def initialize_text(store, config, text):
    """Thin user-text intake; local profile owns permissions, never model output."""
    from orca_agent.semantic import _mentions
    from orca_agent.tools.registry import SCIENCE_IDENTITIES

    if not config.text.enabled:
        raise StoreError("pure-text input requires an explicitly enabled local text profile")
    if not isinstance(text, str) or not text.strip() or len(text) > 8192:
        raise StoreError("text input must contain 1 to 8192 characters")
    defaults = dict(config.text.defaults)
    _validate_new_electronic_conditions(defaults)
    names = sorted({entry[2] for entry in _mentions(text, targets=True)})
    systems = []
    if len(names) == 1 and names[0] in SCIENCE_IDENTITIES and _affirmative_text_identity(text):
        name = names[0]
        systems = [SystemInput(id=name, label=name, geometry_source="prepare",
                               identity={"canonical_names": [name]})]
    request = Request(original_text=text, charge=None, multiplicity=None, method=None, basis=None,
        systems=systems, conditions_source={}, semantic_defaults=defaults,
        conditions={"explain_results": True}, normalization_status="pending",
        goals=[Goal(id="raw_request", port="unresolved", minimum_check_version="unresolved-1",
                    original_text=text, unresolved=["missing:goal_definition"])])
    run = initialize_agent(store, config, request, config.text.permission,
                           config.text.budget, defer_environment=True,
                           science_baseline_policy="first_science_plan")
    store.enqueue_message(run.id, text)
    return run


def bind_text_identity(request, run, pending, candidate):
    """Register a newly answered text identity without granting another effect.

    This only fulfils the input intent of the new text entry. Ordinary bundles
    and old Runs cannot gain geometry acquisition by mentioning a molecule.
    """
    from orca_agent.semantic import _mentions
    from orca_agent.tools.registry import SCIENCE_IDENTITIES

    if run.science_baseline_policy != "first_science_plan" or request.systems or not pending:
        return request
    values = candidate.model_dump() if hasattr(candidate, "model_dump") else candidate
    referenced = {name for goal in values.get("goals") or [] for name in goal.get("system_refs", [])}
    referenced.update(name for names in values.get("goal_bindings", {}).values() for name in names)
    if len(referenced) != 1 or not referenced <= set(SCIENCE_IDENTITIES):
        return request
    message = pending[-1]
    text = message["text"]
    names = {entry[2] for entry in _mentions(text, targets=True)}
    if names != referenced or not message.get("id"):
        return request
    # A question, tentative choice or negated identity is not an affirmative
    # answer. Execution prohibitions alone do not negate the requested identity.
    if not _affirmative_text_identity(text, answer=True):
        return request
    prior_names = set()
    for goal in request.goals:
        if goal.identity.get("explicitly_unknown"):
            continue
        prior_names.update(goal.identity.get("canonical_names", []))
        if "canonical_names" not in goal.identity:
            prior_names.update(entry[2] for entry in _mentions(goal.original_text, targets=True))
    if prior_names and prior_names != referenced:
        return request
    name = next(iter(names))
    updated = request.model_copy(deep=True)
    updated.systems = [SystemInput(id=name, label=name, geometry_source="prepare", identity={
        "canonical_names": [name], "requested_names": list(dict.fromkeys(
            entry[3] for entry in _mentions(text, targets=True))),
        "text_evidence": {"message_id": message["id"], "text_basis": text},
    })]
    return updated


def apply_user_update(store, run_id, message_id, changes):
    """Apply explicit user fields, authenticate provenance, and invalidate the Plan.

    The accompanying CLI update file supplies normative fields. Message text or
    model output alone never selects changed conditions or increases permission.
    """
    from orca_agent.model_usage import current_basis

    with store.run_lock(run_id):
        run = store.load_run(run_id)
        request = store.load_request(run)
        message = next((m for m in store.read_control(run.id)["messages"] if m["id"] == message_id), None)
        if not message or message_id in run.processed_messages:
            raise StoreError("new trusted user message is required")
        allowed = {*_PHYSICAL, "geometry_artifact_id", "systems", "unresolved", "conditions",
                   "conditions_source", "goals"}
        if not isinstance(changes, dict) or set(changes) - allowed:
            raise StoreError("user update contains undeclared fields")
        values = request.model_dump(mode="json")
        changed_conditions = changes.get("conditions", {})
        if not isinstance(changed_conditions, dict):
            raise StoreError("updated conditions must be an object")
        # Validate both copies before merging: False == 0 and True == 1 must not
        # let a valid top-level integer hide an invalid nested user scalar.
        _validate_new_electronic_conditions(changes)
        _validate_new_electronic_conditions(changed_conditions)
        physical = {name: changed_conditions[name] for name in _PHYSICAL if name in changed_conditions}
        for name in _PHYSICAL:
            if name in changes:
                if name in physical and physical[name] != changes[name]:
                    raise StoreError("top-level and nested physical conditions conflict")
                physical[name] = changes[name]
        values.update({key: value for key, value in changes.items()
                       if key not in {"conditions", "conditions_source"}})
        values.update(physical)
        if "systems" in changes:
            if not isinstance(changes["systems"], list):
                raise StoreError("updated systems must be an array")
            systems = [SystemInput.model_validate(item) for item in changes["systems"]]
            prior_systems = {item.id: item for item in request.systems}
            for system in systems:
                _validate_new_electronic_conditions(system.conditions)
                old = prior_systems.get(system.id)
                for name, value in system.conditions.items():
                    if old is None or old.conditions.get(name) != value:
                        system.conditions_source[name] = "explicit"
            values["systems"] = [item.model_dump() for item in systems]
            if "geometry_artifact_id" not in changes:
                values["geometry_artifact_id"] = (systems[0].geometry_artifact_id
                                                  if len(systems) == 1 else None)
        elif "geometry_artifact_id" in changes and request.systems:
            if len(request.systems) != 1:
                raise StoreError("multiple systems require an explicit system-specific geometry update")
            values["systems"][0]["geometry_artifact_id"] = changes["geometry_artifact_id"]
        values["conditions"] = {**request.conditions, **changed_conditions,
                                **{name: value for name, value in physical.items()
                                   if name in request.conditions or name in changed_conditions}}
        supplied_provenance = changes.get("conditions_source", {})
        if not isinstance(supplied_provenance, dict):
            raise StoreError("condition provenance must be an object")
        provenance = {**request.conditions_source, **supplied_provenance}
        geometry_changed = values["geometry_artifact_id"] != request.geometry_artifact_id
        for name in {*physical, *changed_conditions, *(["geometry"] if geometry_changed else [])}:
            if name in changes.get("conditions_source", {}) and provenance[name] != "explicit":
                raise StoreError("explicit user changes cannot be relabeled as inferred or default")
            provenance[name] = "explicit"
        values.update(version=request.version + 1, messages=request.messages + [message],
                      conditions_source=provenance)
        updated = _missing_information(Request.model_validate(values))
        if "goals" in changes:
            for goal in updated.goals:
                _validate_new_electronic_conditions(goal.conditions)
        _authorized_geometry(store, updated, run.permission)
        # Neither a same-version Plan nor a copied Plan may execute under new
        # user conditions. Historical Steps remain recoverable from launch snapshots.
        active_question = store.active_clarification(run)
        clarification_resolved = bool(active_question and (
            "unresolved" in changes or "goals" in changes or all(
                gap.removeprefix("field:").removeprefix("missing:").removeprefix("unconfirmed:")
                in {*physical, *changed_conditions} for gap in active_question["unresolved"])))
        if active_question and not clarification_resolved:
            updated.unresolved = list(dict.fromkeys(updated.unresolved + active_question["unresolved"]))
            updated = _missing_information(updated)
        return store.commit_revision(run, None, request=updated, decision_id="user_" + message_id,
                                     basis=current_basis(store, run), user_message_ids=[message_id],
                                     semantic_record={"schema": "request-semantics-1",
                                         "kind": "user_update_file", "changed_fields": sorted(changes),
                                         **({"resolved_clarification_id": active_question["id"]}
                                            if clarification_resolved else {}),
                                         "replaced_goal_ids": [g.id for g in request.goals]
                                         if "goals" in changes else []})


def ensure_scientific_environment(store, config, run):
    """Raw semantic intake does not probe ORCA; first scientific use does."""
    path = store.path(f"runs/{run.id}/environment.json")
    if path.exists():
        return
    from orca_agent.doctor import diagnose
    from orca_agent.versions import is_supported_orca_version
    environment = diagnose(config)
    if not environment["orca"]["compatible"] or not is_supported_orca_version(
            environment["orca"].get("version")):
        raise StoreError("ORCA installation is missing, incompatible or unverified")
    if not config.mpi_path or not config.mpi_path.is_file():
        raise StoreError("parallel execution requires the explicit MPI executable")
    environment["orca"]["sha256"] = sha256_file(config.orca_path)
    environment["mpi"]["sha256"] = sha256_file(config.mpi_path)
    store._write_json(f"runs/{run.id}/environment.json", environment, immutable=True)
