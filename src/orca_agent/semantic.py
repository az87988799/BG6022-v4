"""Bounded user semantic candidates, activated in the existing Agent/Run.

Quotes authenticate provenance ranges, not arbitrary natural-language truth.
These local schemas never grant permission or certify scientific success.
"""

import re
import unicodedata
from typing import Any, Literal, get_args

from pydantic import Field, ValidationError

from orca_agent.applicability import PROFILE, canonical_condition
from orca_agent.context import _schema
from orca_agent.minimum_evidence import LEGACY_NAMES, REQUIREMENTS
from orca_agent.minimum_evidence import RULE_VERSION as MINIMUM_EVIDENCE_VERSION
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, Identifier, Record, Request
from orca_agent.natural import _authorized_geometry, _missing_information
from orca_agent.proposals import ProposalError, _schema_error
from orca_agent.store import StoreError
from orca_agent.tools.registry import SCIENCE_COMPOSITIONS, catalog, get_tool, validate_parameters

VERSION = "request-semantics-1"
ConditionName = Literal["method", "basis", "charge", "multiplicity", "electronic_state", "environment",
                        "temperature_K", "standard_state"]
CONDITIONS = frozenset(get_args(ConditionName))
PHYSICAL = CONDITIONS - {"temperature_K", "standard_state"}
LEXICAL_ALIASES = {
    ("charge", 0): ("中性", "neutral"),
    ("multiplicity", 1): ("单重态", "单重", "singlet"),
    ("environment", "gas"): ("气相", "gas", "vacuum"),
    ("environment", "gas_phase"): ("气相", "gas phase", "vacuum"),
    ("environment", "water_solvent"): ("水溶剂", "水溶液", "water solvent", "aqueous"),
    ("electronic_state", "RHF"): ("rhf", "闭壳层"),
    ("method", "HF"): ("hf", "rhf", "hartree-fock"),
}
SYSTEM_ALIASES = {"water": ("水", "h2o"), "methane": ("甲烷", "ch4"),
                  "h2o": ("水", "water"), "ch4": ("甲烷", "methane")}
READ_TOOLS = {port: tool["name"] for tool in catalog()
              if tool["effects"] == ["read_registered_artifact"] for port in tool["observation_outputs"]}
RULES = {port: tool["check_version"] for tool in catalog()
         for port in tool["output_ports"] + (tool["observation_outputs"]
             if tool["effects"] == ["read_registered_artifact"] else [])}
RULES["unresolved"] = "unresolved-1"
CONTINUE = {"继续", "继续，沿用之前条件", "继续，沿用之前的全部条件", "继续，沿用之前的全部条件。",
            "continue", "continue with the previous conditions"}
STATUS = {"现在是什么状态", "现在什么状态", "状态", "status", "what is the status"}
PAUSE = {"不要继续", "暂停", "pause", "do not continue"}
CANCEL = {"取消", "cancel"}


class FieldEvidence(Record):
    value: Any = None
    source: Literal["explicit", "default", "inherited", "inferred", "unknown", "not_applicable"]
    text_basis: str = Field(default="", max_length=1000)
    request_version: int | None = None
    system_ref: Identifier | None = None
    default_rule: str | None = None


class SemanticGoal(Record):
    key: Identifier
    port: str
    text_basis: str = Field(min_length=1, max_length=1000)
    system_refs: list[Identifier] = Field(default_factory=list, max_length=5)
    required: bool = Field(default=True, strict=True)
    geometry_relation: Literal["fixed_initial", "optimized"] | None = None
    minimum_evidence: list[str] = Field(default_factory=list, max_length=8)
    conditions: dict[ConditionName, FieldEvidence] = Field(default_factory=dict, max_length=8)
    query: dict | None = None
    unresolved: list[str] = Field(default_factory=list, max_length=8)


class SemanticCandidate(Record):
    schema_version: Literal["request-semantics-1"]
    message_ids: list[Identifier] = Field(min_length=1, max_length=24)
    kind: Literal["normalize", "amend", "replace_goals", "clarify", "continue", "status"]
    text_basis: str = Field(min_length=1, max_length=1000)
    conditions: dict[ConditionName, FieldEvidence] = Field(default_factory=dict, max_length=8)
    system_conditions: dict[Identifier, dict[ConditionName, FieldEvidence]] = Field(default_factory=dict, max_length=5)
    goals: list[SemanticGoal] | None = Field(default=None, min_length=1, max_length=8)
    replaces: list[Identifier] = Field(default_factory=list, max_length=8)
    goal_bindings: dict[Identifier, list[Identifier]] = Field(default_factory=dict, max_length=8)
    resolves: list[str] = Field(default_factory=list, max_length=8)
    unresolved: list[str] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(default_factory=list, max_length=5)


def pending_messages(store, run):
    return [m for m in store.read_control(run.id)["messages"] if m["id"] not in run.processed_messages]


def _goal_binding_contract(request, *, defines_goals=False):
    """One target set for model schema and activation; new Goals bind themselves."""
    identifiers = [] if request is None or defines_goals else [
        goal.id for goal in request.goals if not (
            goal.id == "raw_request" and goal.port == "unresolved"
            and goal.original_text == request.original_text)]
    schema = {"propertyNames": {"enum": identifiers}} if identifiers else {"maxProperties": 0}
    return identifiers, schema


def action_parameters(allowed_tools=(), *, request=None):
    schema = _schema(SemanticCandidate.model_json_schema())
    binding_ids, binding_schema = _goal_binding_contract(request)
    _, new_goal_binding_schema = _goal_binding_contract(request, defines_goals=True)
    bindings = schema["properties"]["goal_bindings"]
    if binding_ids:
        # Enumerated Goal IDs already constrain key syntax. Keep the Pydantic
        # value schema without repeating its now-redundant Identifier regex.
        bindings["additionalProperties"] = next(iter(bindings.pop("patternProperties").values()))
        bindings.update(binding_schema)
    else:
        schema["properties"]["goal_bindings"] = {"type": "object", **binding_schema}
    if binding_schema != new_goal_binding_schema:
        schema["anyOf"] = [{"properties": {"goals": {"type": "null"}}},
                           {"properties": {"goal_bindings": new_goal_binding_schema}}]
    port_rules = {port: rule for port, rule in RULES.items() if port != "unresolved"}
    return {"normalize_request": {
        "instruction": "Goal ports are minimum_evidence_rules.port_rules keys; query follows query_schemas[port]. "
        "Use environment for gas/solvent. condition_lexicon[field] rows=[value,explicit aliases], not inference. "
        "electronic_state means RHF/UHF reference, not ground/excited. "
        "system_refs -> System.geometry_artifact_id; never put geometry in conditions. "
        "explain_results is existing configuration; keep. "
        "minimum_evidence defaults to []; checks apply. "
        "Keep actual user unsupported/unknown requirements unresolved; no invented rules. "
        "Energy requires geometry_relation=fixed_initial (SP) or optimized (after Opt). "
        "Unknown/inferred:conditions/system_conditions. "
        "Energy needs temperature_K/standard_state only if requested. Absent display unit:unknown, not a question. "
        "science_scope: capability limits, not permission/defaults. "
        "No execution; copy AUTHORITY.pending_user_message_ids. "
        "Verbatim text_basis: no translation, paraphrase or added parentheses. "
        "normalize replaces raw_request/missing:goal_definition with requested Goals, "
        "after clarification too. Never resolves missing:goal_definition. "
        "Registration/no-execution is not a Goal. amend keeps goals; "
        "New goals:system_refs; existing Goal.id:goal_bindings, never both. "
        "replace_goals:explicit replacement + all old IDs. "
        "gaps:field:<field>/system:<goal_id>; resolves=answered gaps.",
        "science_scope": {"systems": list(SCIENCE_COMPOSITIONS), "conditions": dict(PROFILE),
                          "names": {formula: list(SYSTEM_ALIASES.get(formula.casefold(), ()))
                                    for formula in SCIENCE_COMPOSITIONS},
                          "ports": sorted({port for tool in catalog() if "execute_orca" in tool["effects"]
                                           for port in tool["output_ports"]})},
        "questions_policy": (
            "Named identity != registered System/geometry; no geometry != unknown identity. "
            "Explicit registration-only: known scope/geometry limits get declarative notices in questions; "
            "no reply, confirmation or resource request. Separately disclose science_scope support and "
            "geometry registration; neither implies the other. Preserve explicit choices. "
            "No execution permission alone is not registration-only intent. New gaps need visible questions text. "
            "Ask for critical unknowns in conditions/identity/quantity blocking current scope; "
            "execution intent may need resources/authorization."),
        "schema": schema,
        "condition_lexicon": {field: [[value, aliases] for (name, value), aliases in LEXICAL_ALIASES.items()
                                      if name == field] for field in dict.fromkeys(name for name, _ in LEXICAL_ALIASES)},
        "minimum_evidence_rules": {"version": MINIMUM_EVIDENCE_VERSION, "registered": REQUIREMENTS,
                                   "legacy_aliases": LEGACY_NAMES, "port_rules": port_rules},
        "query_schemas": {port: _schema(get_tool(name).parameter_schema) for port, name in READ_TOOLS.items()
                          if name in allowed_tools}}}


def _quote(text, messages):
    if not text.strip() or not any(text in m["text"] for m in messages):
        raise StoreError("semantic evidence must quote a supplied user message")


def _grounded_systems(request, messages):
    """Only explicit registered names/known molecular aliases disambiguate a binding."""
    text = "\n".join(message["text"] for message in messages).casefold()
    found = set()
    for system in request.systems:
        names = [system.id.casefold(), *SYSTEM_ALIASES.get(system.id.casefold(), ())]
        for name in names:
            if (re.search(r"(?<![a-z0-9_])" + re.escape(name) + r"(?![a-z0-9_])", text)
                    if name.isascii() else name in text):
                found.add(system.id)
    return found


def _resolution_matches(gap, candidate):
    """Unrelated answers cannot erase a previous blocking scientific question."""
    confirmed = {name for name, value in candidate.conditions.items()
                 if value.source not in {"unknown", "inferred", "not_applicable"}}
    confirmed |= {f"{scope}:{name}" for scope, fields in candidate.system_conditions.items()
                  for name, value in fields.items()
                  if value.source not in {"unknown", "inferred", "not_applicable"}}
    key = re.sub(r"^(?:missing|unknown|unconfirmed|field|ambiguous):", "", gap)
    if key in confirmed:
        return True
    # _missing_information identifies a missing scoped condition as
    # missing:<field>:<system>; explicit model questions use field:<field>.
    # A global answer can supply the inherited value, while scoped answers
    # must name this exact system. Effective conditions still check overrides.
    parts = gap.split(":")
    if len(parts) == 3 and parts[0] == "missing" and parts[1] in CONDITIONS:
        return parts[1] in confirmed or f"{parts[2]}:{parts[1]}" in confirmed
    if gap.startswith("system:"):
        return gap.removeprefix("system:") in candidate.goal_bindings
    return gap in {"ambiguous_system", "ambiguous_pronoun"} and len(candidate.goal_bindings) == 1


def _field(name, item, request, messages, *, system=None):
    if name not in CONDITIONS:
        path = name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", str(name)) else "[field]"
        raise StoreError(f"semantic condition {path} is outside the field whitelist; allowed keys: "
                         + ", ".join(get_args(ConditionName)))
    value = item.value
    if name in {"charge", "multiplicity"} and value is not None:
        if type(value) is not int or (name == "multiplicity" and value < 1):
            raise StoreError("charge/multiplicity need exact integer values")
    elif name == "temperature_K" and value is not None:
        if type(value) not in (int, float) or value <= 0:
            raise StoreError("temperature requires a positive numeric value")
    elif value is not None and (not isinstance(value, str) or not 0 < len(value) <= 100):
        raise StoreError("condition needs a bounded string value")
    if item.source == "explicit":
        _quote(item.text_basis, messages)
        # Check literal or a small documented lexical normalization, never use
        # a trustworthy message ID as proof of every proposed field value.
        tokens = {str(value).casefold()}
        if name == "charge" and type(value) is int and value > 0:
            tokens.add(f"+{value}")
        tokens.update(LEXICAL_ALIASES.get((name, value), ()))
        def present(token, text=item.text_basis):
            if re.fullmatch(r"[a-z0-9_.+\-/]+", token):
                return re.search(r"(?<![a-z0-9_.+\-])" + re.escape(token)
                                 + r"(?![a-z0-9_.+\-])", text.casefold()) is not None
            return token in text.casefold()
        if value is None or not any(present(token) for token in tokens):
            raise StoreError("explicit condition lacks a supported lexical value basis")
        # A clipped quote must not discard the negation/uncertainty around the
        # value in its authenticated message. Ambiguous clauses stay unresolved.
        for message in messages:
            if item.text_basis not in message["text"]:
                continue
            for clause in re.split(r"[，,。.;；\n]", message["text"]):
                # These phrases negate geometric constraints, not the method,
                # charge or reference state. Keep any surrounding negation.
                condition_clause = re.sub(r"无约束|\bwithout\s+(?:geometric\s+)?constraints?\b",
                                          " ", clause, flags=re.I)
                if any(present(token, clause) for token in tokens) and re.search(
                        r"不|非|未|无|禁止|拒绝|避免|排除|可能|也许|推断|推测|假设|待确认|尚待|或者|[?？]|"
                        r"\b(?:not|no|never|without|unknown|maybe|perhaps|uncertain|if|either|or)\b|n't\b",
                        condition_clause, re.I):
                    raise StoreError("explicit condition has a negated or uncertain value basis")
        if name in {"charge", "multiplicity"}:
            label = (r"(?:净?电荷(?:数)?|(?<![a-z])charge(?![a-z]))" if name == "charge" else
                     r"(?:自旋多重度|多重度|(?<![a-z])multiplicity(?![a-z]))")
            literal = (r"\+?" + str(value)) if value >= 0 else re.escape(str(value))
            association = label + r"\s*(?:(?:为|是|等于|设为|取|is|=|:|：)\s*)?" + literal + r"(?![\d.])"
            named_alias = any(present(token) for token in LEXICAL_ALIASES.get((name, value), ()))
            if not named_alias and not re.search(association, item.text_basis, re.I):
                raise StoreError("explicit numeric condition lacks its own field/value association")
    elif item.source == "inherited":
        if item.request_version != request.version:
            raise StoreError("inherited conditions require the exact current Request version")
        def recorded(scope):
            if scope is not None and name in scope.conditions:
                return canonical_condition(name, scope.conditions[name]), scope.conditions_source.get(name)
            return (canonical_condition(name, request.conditions.get(name, getattr(request, name, None))),
                    request.conditions_source.get(name))

        expected = canonical_condition(name, value)
        prior, provenance = recorded(system)
        if expected is None or prior is None or prior != expected or provenance not in {
                "explicit", "default", "inherited"}:
            raise StoreError("inherited target scope must retain the same confirmed value "
                             "from its unique recorded source")
        if item.system_ref:
            source = next((s for s in request.systems if s.id == item.system_ref), None)
            if source is None:
                raise StoreError("inherited system reference is not registered")
            prior, provenance = recorded(source)
            if prior is None or prior != expected or provenance not in {"explicit", "default", "inherited"}:
                raise StoreError("inherited value differs from its unique recorded source")
    elif item.source == "default":
        expected = canonical_condition(name, value)
        authorized = canonical_condition(name, request.semantic_defaults.get(name))
        if (item.default_rule != "local-hf-1" or name not in request.semantic_defaults
                or expected is None or authorized is None or authorized != expected):
            raise StoreError("default rule/value was not authorized in user configuration")
        prior = (system.conditions.get(name) if system and name in system.conditions else
                 request.conditions.get(name, getattr(request, name, None)))
        provenance = (system.conditions_source.get(name) if system and name in system.conditions else
                      request.conditions_source.get(name))
        if provenance in {"unknown", "inferred", "explicit", "inherited", "not_applicable"} or (
                prior is not None and canonical_condition(name, prior) != expected):
            raise StoreError("declared or unconfirmed conditions cannot be replaced by a default")
    elif item.source == "inferred":
        _quote(item.text_basis, messages)
    elif item.source == "unknown":
        if value is not None:
            raise StoreError("unknown condition cannot supply a value")
    elif name in PHYSICAL:
        raise StoreError("physical scientific conditions cannot be waived as not_applicable")
    return value, item.source, item.model_dump(mode="json")


def _goals(candidate, request, messages):
    systems = {s.id for s in request.systems}
    goals = []
    for goal_index, item in enumerate(candidate.goals or []):
        _quote(item.text_basis, messages)
        if len(item.system_refs) != len(set(item.system_refs)) or set(item.system_refs) - systems:
            raise StoreError("semantic goal references unknown/duplicate registered systems")
        conditions = {}
        unresolved = list(item.unresolved)
        for name, field in item.conditions.items():
            if field.source in {"unknown", "inferred"}:
                raise StoreError("unknown/inferred Goal conditions must be placed in "
                                 "Candidate.conditions/system_conditions; preserve goals.unresolved "
                                 "so the user can answer without replacing the goal")
            value, _, _ = _field(name, field, request, messages)
            conditions[name] = value
        if item.geometry_relation:
            conditions["geometry_relation"] = item.geometry_relation
        elif item.port == "energy":
            raise StoreError("energy goal requires geometry_relation: fixed_initial or optimized")
        if item.query is not None:
            if item.port not in READ_TOOLS:
                raise StoreError("query can only constrain an evidence observation goal")
            validate_parameters(READ_TOOLS[item.port], item.query)
            conditions["query"] = item.query
        port = item.port if item.port in RULES else "unresolved"
        if port == "unresolved":
            unresolved.append("unsupported_quantity:" + item.port)
        for requirement_index, requirement in enumerate(item.minimum_evidence):
            canonical = LEGACY_NAMES.get(requirement, requirement)
            if canonical not in REQUIREMENTS and canonical not in {port, RULES[port]}:
                if not requirement.strip() or not any(requirement in message["text"] for message in messages):
                    raise ProposalError("Use [] for the port's basic checks, registered minimum_evidence_rules, "
                                        "or exact user quotes for unsupported requirements; never invent rule names.",
                                        path=["parameters", "goals", goal_index, "minimum_evidence", requirement_index],
                                        allowed_rules=[*REQUIREMENTS, *LEGACY_NAMES, port, RULES[port]])
                unresolved.append("unsupported_minimum_evidence:" + requirement)
        # A scalar goal is per system; splitting is explicit and retains text.
        members = item.system_refs if len(item.system_refs) > 1 and port in {
            "energy", "optimized_geometry"} else [None]
        for member in members:
            key = f"goal_{item.key}" + (f"_{member}" if member else "")
            goals.append(Goal(id=key, port=port, required=item.required,
                              minimum_check_version=RULES[port], original_text=item.text_basis,
                              system_ids=[member] if member else item.system_refs,
                              conditions=conditions, minimum_evidence=item.minimum_evidence,
                              unresolved=unresolved))
    if not goals or len(goals) > 8:
        raise StoreError("normalization requires one to eight bounded goals")
    return goals


def commit_candidate(store, run, parameters, *, decision_id, basis, related_results=None, fault=None):
    try:
        candidate = SemanticCandidate.model_validate(parameters)
    except ValidationError as exc:
        if any(error["type"] == "literal_error" and error["loc"][-1] == "[key]"
               for error in exc.errors(include_input=False, include_context=False, include_url=False)):
            correction = _schema_error(exc, path=["parameters"])
            correction.detail.update(requirement="Use only the allowed scientific condition keys at these paths.",
                                     allowed_condition_keys=list(get_args(ConditionName)))
            raise correction from None
        raise _schema_error(exc, path=["parameters"]) from None
    request = store.load_request(run)
    binding_ids, _ = _goal_binding_contract(request, defines_goals=candidate.goals is not None)
    invalid_binding_ids = set(candidate.goal_bindings) - set(binding_ids)
    if invalid_binding_ids:
        raise ProposalError("Use existing Goal.id for goal_bindings; new goals bind only through system_refs.",
                            path=["parameters", "goal_bindings", sorted(invalid_binding_ids)[0]],
                            allowed_goal_ids=binding_ids)
    active_question = store.active_clarification(run)
    pending = pending_messages(store, run)
    if set(candidate.message_ids) != {m["id"] for m in pending} or len(candidate.message_ids) != len(pending):
        raise StoreError("semantic candidate must consume the exact pending message set")
    _quote(candidate.text_basis, pending)
    if any(m.get("request_version", request.version) != request.version for m in pending):
        raise StoreError("answer refers to an older Request; clarify against the current version")
    if candidate.kind in {"continue", "status"}:
        allowed = CONTINUE if candidate.kind == "continue" else STATUS
        if any(m["text"].strip().casefold() not in allowed for m in pending):
            raise StoreError("control-only semantics require an exact unambiguous user command")
        if (candidate.conditions or candidate.system_conditions or candidate.goals or candidate.unresolved
                or candidate.goal_bindings or candidate.resolves or candidate.replaces):
            raise StoreError("control-only message cannot change Request fields")
        return store.commit_revision(run, store.load_plan(run), decision_id=decision_id, basis=basis,
                                     user_message_ids=candidate.message_ids, fault=fault,
                                     semantic_record={"schema": VERSION, "kind": candidate.kind})
    initial = request.normalization_status == "pending" or (
        len(request.goals) == 1 and request.goals[0].id == "raw_request"
        and request.goals[0].port == "unresolved"
        and request.goals[0].original_text == request.original_text)
    if candidate.kind == "normalize" and not initial:
        raise StoreError("initial normalization cannot replace an established Request")
    if candidate.goals is not None and candidate.kind not in {"normalize", "replace_goals"}:
        raise StoreError("amend/clarify cannot replace goals")
    if candidate.kind == "replace_goals":
        if candidate.goals is None:
            raise StoreError("goal replacement must supply the new current goals")
        if set(candidate.replaces) != {g.id for g in request.goals}:
            raise StoreError("user replacement must identify every superseded goal")
        whole_messages = "\n".join(message["text"] for message in pending)
        negated = re.search(r"(?:不要|不得|不许|禁止|别|不再|不想|do\s+not|don't|never)"
                            r"[^，。！？,.;\n]{0,20}(?:改|换|替|撤|取消|change|replace|withdraw)",
                            whole_messages, re.I)
        explicit_replacement = re.search(
            r"(?:改|换)(?:成|为)|替换|撤回.{0,24}目标|取消.{0,24}目标|"
            r"(?:replace\s+.+\s+with|change\s+.+\s+to|withdraw\s+.+goal)",
            whole_messages, re.I)
        if negated or not explicit_replacement:
            raise StoreError("goal replacement requires an explicit user replacement phrase")
    values = request.model_dump(mode="json")
    grounding = request.messages + pending if initial else pending
    for scope, fields in [(None, candidate.conditions), *candidate.system_conditions.items()]:
        system = next((s for s in request.systems if s.id == scope), None) if scope else None
        if scope and system is None:
            raise StoreError("semantic conditions cannot invent a system")
        target = next(s for s in values["systems"] if s["id"] == scope) if scope else values
        for name, field in fields.items():
            value, source, evidence = _field(name, field, request, grounding, system=system)
            target["conditions"][name] = value
            target["conditions_source"][name] = source
            if scope is None and name in {"method", "basis", "charge", "multiplicity"}:
                target[name] = value
            values["condition_evidence"][f"{scope or 'request'}.{name}"] = {
                **evidence, "message_ids": candidate.message_ids, "schema": VERSION}
    question_gaps = active_question["unresolved"] if active_question else []
    known_gaps = set(request.unresolved) | set(question_gaps) | {gap for g in request.goals for gap in g.unresolved}
    if "missing:goal_definition" in candidate.resolves:
        raise ProposalError("normalize replaces the raw_request/missing:goal_definition placeholder automatically "
                            "with requested Goals. Omit that marker from resolves; resolves is only for "
                            "answered field/binding questions.", path=["parameters", "resolves"])
    if set(candidate.resolves) - known_gaps or any(
            not _resolution_matches(gap, candidate) for gap in candidate.resolves):
        raise StoreError("resolved ambiguity must identify an existing question and a grounded field/binding")
    values["unresolved"] = list(dict.fromkeys(
        ([] if initial else [gap for gap in request.unresolved if gap not in candidate.resolves])
        + candidate.unresolved))
    unresolved_questions = [gap for gap in question_gaps if not _resolution_matches(gap, candidate)]
    if candidate.kind == "replace_goals":
        unresolved_questions = []
    values["unresolved"] = list(dict.fromkeys(values["unresolved"] + unresolved_questions))
    for scope, fields in [(None, candidate.conditions), *candidate.system_conditions.items()]:
        for name, field in fields.items():
            marker = f"unconfirmed:{scope + ':' if scope else ''}{name}"
            values["unresolved"] = [gap for gap in values["unresolved"] if gap != marker]
            if name in PHYSICAL and field.source in {"unknown", "inferred"}:
                values["unresolved"].append(marker)
    if candidate.goals is not None:
        values["goals"] = [g.model_dump() for g in _goals(candidate, request, grounding)]
    systems = {system.id for system in request.systems}
    named_systems = _grounded_systems(request, pending)
    for goal_id, system_ids in candidate.goal_bindings.items():
        goal = next((g for g in values["goals"] if g["id"] == goal_id), None)
        if (goal is None or not system_ids or len(system_ids) != len(set(system_ids))
                or set(system_ids) - systems or set(system_ids) != named_systems
                or (goal["system_ids"] and goal["system_ids"] != system_ids)):
            raise StoreError("clarification must bind an unresolved goal to registered systems without replacing its scope")
        goal["system_ids"] = system_ids
    for goal in values["goals"]:
        goal["unresolved"] = [gap for gap in goal["unresolved"] if gap not in candidate.resolves]
    if candidate.kind == "normalize" and candidate.goals is None:
        raise StoreError("initial normalization must preserve the user's requested goals")
    if candidate.kind == "clarify" and not candidate.unresolved:
        raise StoreError("clarification must persist its blocking unresolved facts")
    retired_raw_placeholder = any(
        decision.get("semantics", {}).get("kind") == "normalize"
        and "raw_request" in decision["semantics"].get("replaced_goal_ids", [])
        for decision in run.decisions)
    has_current_goals = bool(values["goals"]) and all(goal["id"] != "raw_request" for goal in values["goals"])
    if has_current_goals and ((initial and candidate.kind == "normalize") or retired_raw_placeholder):
        # The raw-intake placeholder is program-owned, not a user ambiguity.
        # Successful goal construction retires it even if the candidate or an
        # earlier question repeats it, including on a later answer to that
        # question's other gaps. Only activated normalization proves retirement.
        # Keep the candidate/old revision intact;
        # _missing_information below still derives actual per-goal missing facts.
        placeholder = "missing:goal_definition"
        values["unresolved"] = [gap for gap in values["unresolved"] if gap != placeholder]
        unresolved_questions = [gap for gap in unresolved_questions if gap != placeholder]
        for goal in values["goals"]:
            goal["unresolved"] = [gap for gap in goal["unresolved"] if gap != placeholder]
    values.update(version=request.version + 1, messages=request.messages + pending)
    updated = _missing_information(Request.model_validate(values))
    prior_gaps = set(request.unresolved) | set(question_gaps) | {gap for goal in request.goals for gap in goal.unresolved}
    current_gaps = set(updated.unresolved) | {gap for goal in updated.goals for gap in goal.unresolved}
    new_gaps = current_gaps - prior_gaps
    visible_question = any(any(not char.isspace() and not unicodedata.category(char).startswith("C")
                              for char in question) for question in candidate.questions)
    if new_gaps and not visible_question:
        raise ProposalError("New unresolved conditions, including detected unsupported scope, require a visible question "
                            "or unsupported-scope notice in parameters.questions. Preserve explicit user choices; "
                            "do not ask to reconfirm them.",
                            path=["parameters", "questions"], new_unresolved=sorted(new_gaps))
    _authorized_geometry(store, updated, run.permission)
    for goal in updated.goals:
        query = goal.conditions.get("query", {})
        if query.get("artifact_id"):
            own = {a for rid in run.result_ids for a in store.load_result(run.id, rid).artifact_ids}
            if query["artifact_id"] not in set(run.permission.artifact_ids) | own:
                raise StoreError("semantic query must reference a registered authorized Artifact")
            store.artifact_path(query["artifact_id"])
        if query.get("run_id") and query["run_id"] != run.id:
            raise StoreError("semantic query cannot invent another Run reference")
    semantics = {"schema": VERSION, "kind": candidate.kind,
                 "candidate": candidate.model_dump(mode="json"),
                 "replaced_goal_ids": [g.id for g in request.goals] if candidate.goals else [],
                 "questions": candidate.questions}
    if active_question and not unresolved_questions:
        semantics["resolved_clarification_id"] = active_question["id"]
    return store.commit_revision(run, None, request=updated, decision_id=decision_id,
                                 basis=basis, user_message_ids=candidate.message_ids,
                                 related_results=related_results, semantic_record=semantics, fault=fault)


def deterministic_message(store, run, *, fault=None):
    """Return (Run, outcome) without model/Tool/permission expansion."""
    pending = pending_messages(store, run)
    if not pending:
        return run, None
    if len(pending) == 1 and "update" in pending[0]:
        from orca_agent.natural import apply_user_update
        message = pending[0]
        if message.get("request_version", run.request_version) != run.request_version:
            return run, None
        return apply_user_update(store, run.id, message["id"], message["update"]), "changed"
    texts = [m["text"].strip().casefold() for m in pending]
    if all(text in CONTINUE for text in texts):
        kind = "continue"
    elif all(text in STATUS for text in texts):
        kind = "status"
    elif len(texts) == 1 and texts[0] in PAUSE | CANCEL:
        action = "cancel" if texts[0] in CANCEL else "pause"
        # State and message consumption activate atomically; a pre-commit signal
        # would change the generation and invalidate this immutable replay.
        updated = store.commit_revision(run, store.load_plan(run),
            decision_id="message_" + pending[0]["id"], basis=current_basis(store, run),
            user_message_ids=[pending[0]["id"]], fault=fault,
            semantic_record={"schema": VERSION, "kind": action})
        return updated, "signal"
    else:
        return run, None
    parameters = {"schema_version": VERSION, "message_ids": [m["id"] for m in pending],
                  "kind": kind, "text_basis": pending[0]["text"]}
    updated = commit_candidate(store, run, parameters,
                               decision_id="message_" + pending[-1]["id"],
                               basis=current_basis(store, run), fault=fault)
    return updated, kind
