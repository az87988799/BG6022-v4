"""Bounded user semantic candidates, activated in the existing Agent/Run.

Quotes authenticate provenance ranges, not arbitrary natural-language truth.
These local schemas never grant permission or certify scientific success.
"""

import re
import unicodedata
from copy import deepcopy
from typing import Any, Literal, get_args

from pydantic import Field, ValidationError

from orca_agent.applicability import PROFILE, canonical_condition
from orca_agent.minimum_evidence import LEGACY_NAMES, REQUIREMENTS
from orca_agent.minimum_evidence import RULE_VERSION as MINIMUM_EVIDENCE_VERSION
from orca_agent.model_usage import current_basis
from orca_agent.models import Goal, Identifier, Record, Request
from orca_agent.natural import _authorized_geometry, _missing_information
from orca_agent.proposals import ProposalError, _schema_error
from orca_agent.schema_projection import project_schema as _schema
from orca_agent.semantic_notices import NOTICE_CONTRACT_VERSION, notice_choices, validate_notices
from orca_agent.store import StoreError
from orca_agent.tools.registry import (
    SCIENCE_COMPOSITIONS,
    SCIENCE_IDENTITIES,
    catalog,
    get_tool,
    validate_parameters,
)

VERSION = "request-semantics-2"
LEGACY_VERSION = "request-semantics-1"
ConditionName = Literal["method", "basis", "charge", "multiplicity", "electronic_state", "environment",
                        "temperature_K", "standard_state"]
CONDITIONS = frozenset(get_args(ConditionName))
PHYSICAL = CONDITIONS - {"temperature_K", "standard_state"}
LEXICAL_ALIASES = {
    ("charge", 0): ("中性", "neutral", "电荷为零", "电荷零"),
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
    message_id: Identifier | None = None


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
    message_id: Identifier | None = None
    analysis_goal_ref: Identifier | None = None


class SemanticCandidate(Record):
    # Historical candidates remain parseable, but never activate under old rules.
    schema_version: Literal["request-semantics-1", "request-semantics-2"]
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
    notices: list[str] = Field(default_factory=list, max_length=5)
    question_gaps: dict[str, list[str]] = Field(default_factory=dict, max_length=5)


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


def action_parameters(allowed_tools=(), *, request=None, text_input=False):
    schema = _schema(SemanticCandidate.model_json_schema())
    schema["properties"]["schema_version"] = {"const": VERSION, "type": "string"}
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
    contract = {"normalize_request": {
        "instruction": "Ports/rules:minimum_evidence_rules; queries:query_schemas[port]; [] keeps basic checks. "
        "minimum_evidence=[rule ID strings], not registry objects. Empty arrays=[]; empty objects={}, not null. "
        "Omit unused optional fields instead of filling placeholders. replaces is an array of old Goal IDs. "
        "Lexicon lists canonical values; quote original user wording. environment=gas/solvent; electronic_state=RHF/UHF. "
        "Keep unknown/unsupported and explain_results; unknown/inferred belong in conditions/system_conditions. "
        "Energy needs energy Goal, not key/reason/geometry; fixed_initial=SP, optimized=after Opt. "
        "temperature/standard_state only if requested; absent unit=unknown, no question. "
        "Copy pending IDs; unique verbatim text_basis must match field/target. "
        "normalize replaces raw_request/missing:goal_definition; amend keeps goals; replace_goals needs explicit replacement+all old IDs. "
        "NewGoal system_refs; existingGoal goal_bindings keyed by Goal.id. No geometry conditions or registration/no-execution Goal. "
        "science_scope=capability, not permission/defaults. resolves=answered field:<field>/system:<goal_id>; question_gaps=questions to gaps.",
        "notice_contract_version": NOTICE_CONTRACT_VERSION,
        "notice_choices": notice_choices(),
        "science_scope": {"systems": list(SCIENCE_COMPOSITIONS), "conditions": dict(PROFILE),
                          "names": {formula: list(SYSTEM_ALIASES.get(formula.casefold(), ()))
                                    for formula in SCIENCE_COMPOSITIONS},
                          "ports": sorted({port for tool in catalog() if "execute_orca" in tool["effects"]
                                           for port in tool["output_ports"]})},
        "questions_policy": (
            "For explicit registration-only, disclose scope and geometry limits via notices, "
            "without asking for resources/confirmation. Copy each applicable notice_choices sentence once. "
            "Otherwise ask only critical gaps; new gaps need a question or notice. Preserve explicit choices. "
            "No execution permission alone is not registration-only intent."),
        "schema": schema,
        "condition_lexicon": {field: [value for name, value in LEXICAL_ALIASES if name == field]
                              for field in dict.fromkeys(name for name, _ in LEXICAL_ALIASES)},
        "minimum_evidence_rules": {"version": MINIMUM_EVIDENCE_VERSION, "registered": REQUIREMENTS,
                                   "legacy_aliases": LEGACY_NAMES, "port_rules": port_rules},
        "query_schemas": {port: _schema(get_tool(name).parameter_schema) for port, name in READ_TOOLS.items()
                          if name in allowed_tools}}}
    if request and request.semantic_defaults:
        contract["normalize_request"]["authorized_defaults"] = {
            "source": "default", "default_rule": "local-hf-1", "values": request.semantic_defaults}
    if request and (text_input or any(system.geometry_source == "prepare" for system in request.systems)):
        contract["normalize_request"]["input_acquisition"] = (
            "geometry_source=prepare: structure.resolve obtains SMILES; structure.prepare via OPI supplies XYZ. "
            "Null XYZ is NOT missing:geometry; omit missing_geometry; no user XYZ or invented artifacts. "
            "Keep system_refs. Permission still gates execution. Unknown/unsupported unresolved; "
            "ask SP vs Opt only when absent (ambiguous_geometry_relation).")
    if (set(allowed_tools) & {"analysis.finite_sampling", "analysis.sampling_check"}
            or request and any(g.port in {"sampling", "sampling_check"} for g in request.goals)):
        contract["normalize_request"]["sampling_intent"] = (
            "sampling_check checks existing criterion (false may complete); sampling requires satisfying it. "
            "Preserve each intent. analysis_goal_ref inherits frozen spec; absent=missing:sampling_specification. "
            "Never invent thresholds/candidates.")
    return contract


def _quote(text, messages):
    if not text.strip() or not any(text in m["text"] for m in messages):
        raise StoreError("semantic evidence must quote a supplied user message")


def _locate(text, messages, message_id=None):
    """Authenticate one exact occurrence; offsets are derived, never model authority."""
    matches = []
    seen = set()
    for message in messages:
        key = message.get("id", id(message))
        if key in seen or (message_id is not None and message.get("id") != message_id):
            continue
        seen.add(key)
        for match in re.finditer(re.escape(text), message["text"]) if text.strip() else []:
            matches.append((message, match.start(), match.end()))
    if not matches:
        raise StoreError("semantic evidence must quote a supplied user message and its exact message ID")
    if len(matches) != 1:
        raise StoreError("semantic evidence quote is repeated; supply a unique fuller quote or message ID")
    return matches[0]


IDENTITIES = {"water": ("water", "h2o", "水"), "methane": ("methane", "ch4", "甲烷"),
              "ethanol": ("ethanol", "乙醇"), "ammonia": ("ammonia", "氨"),
              "carbon_dioxide": ("carbon dioxide", "二氧化碳")}


def _mentions(text, *, targets=False):
    found = []
    for identity, aliases in IDENTITIES.items():
        for alias in aliases:
            pattern = (r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])"
                       if alias.isascii() else re.escape(alias))
            for match in re.finditer(pattern, text, re.I):
                before, after = text[:match.start()], text[match.end():]
                solvent = (re.match(r"\s*(?:溶剂|溶液|作为溶剂|为溶剂|as\s+(?:(?:the|a)\s+)?solvent)", after, re.I)
                           or re.search(r"(?:溶剂(?:为|是)|solvent\s*(?:is|=|:)?|\bin)\s*$", before, re.I))
                if not targets or not solvent:
                    found.append((match.start(), match.end(), identity, match.group()))
    return sorted(found)


def _registered_identity(system):
    names = {entry[2] for entry in _mentions(system.id + " " + system.label, targets=True)}
    return next(iter(names)) if len(names) == 1 else None


def _propositions(text):
    """Small bounded clause splitter retaining question punctuation and offsets."""
    return [(match.start(), match.end(), match.group()) for match in re.finditer(
        r"[^，,。.;；\n!?？]+[!?？]?", text)]


def _goal_grounding(request, text, system_ids, messages, *, message_id=None, original_identity=None,
                    text_input=False):
    message, start, end = _locate(text, messages, message_id)
    # Expand short quotes to their proposition so clipping off the named target
    # cannot bind the remaining word "energy" to an unrelated registered system.
    clauses = [part for left, right, part in _propositions(message["text"])
               if left < end and right > start]
    proposition = "，".join(clauses)
    contextual = _mentions(proposition, targets=True)
    quoted = _mentions(text, targets=True)
    # A registered geometry is available evidence, not confirmation of an
    # explicitly unknown target. Include the pronoun's preceding statement so
    # clipping the uncertainty out of text_basis cannot restore an old identity.
    identity_context = proposition if quoted else message["text"][:end]
    explicitly_unknown = bool(re.search(
        r"(?:不知道|不确定|未确定)[^，,。.;；\n]{0,16}(?:哪个|什么)[^，,。.;；\n]{0,8}(?:分子|体系|对象)|"
        r"(?:分子|体系|对象)(?:的)?(?:身份|名称)?(?:目前|仍|尚)?(?:为|是)?(?:未知|不确定|未确认)|"
        r"\b(?:molecule(?:\s+identity)?|molecular\s+identity|system\s+identity|target\s+identity)\s+"
        r"(?:(?:is|remains|still|now|currently)\s+)*(?:unknown|uncertain|unconfirmed|undetermined)\b",
        identity_context, re.I))
    if text_input:
        from orca_agent.natural import _affirmative_text_identity
        explicitly_unknown = explicitly_unknown or not _affirmative_text_identity(identity_context)
    if explicitly_unknown and system_ids:
        raise StoreError("explicitly unknown goal identity cannot inherit a registered system")
    mentions = (quoted if quoted and {entry[2] for entry in quoted} <= {
        entry[2] for entry in contextual} else contextual)
    if explicitly_unknown:
        mentions = []
    elif not mentions:
        preceding = _mentions(message["text"][:start], targets=True)
        preceding_ids = {entry[2] for entry in preceding}
        if len(preceding_ids) > 1 and system_ids:
            raise StoreError("ambiguous goal pronoun has multiple named antecedents")
        mentions = preceding
    identities = {entry[2] for entry in mentions}
    prior = set((original_identity or {}).get("canonical_names", []))
    if prior and identities and identities != prior:
        raise StoreError("goal binding contradicts the original goal identity")
    identities = set() if explicitly_unknown else prior or identities
    registered = {system.id: system for system in request.systems}
    if len(system_ids) != len(set(system_ids)) or set(system_ids) - set(registered):
        raise StoreError("semantic goal references unknown/duplicate registered systems")
    bound = {_registered_identity(registered[system_id]) for system_id in system_ids}
    if system_ids and identities and (None in bound or bound != identities):
        raise StoreError("goal system binding contradicts the named target in its original proposition")
    if system_ids and not identities:
        named = _grounded_systems(request, [{"text": proposition}])
        if named and named != set(system_ids):
            raise StoreError("goal system binding contradicts its quoted proposition")
        if not named and len(request.systems) > 1:
            raise StoreError("ambiguous goal pronoun needs one confirmed system context")
    evidence = {"message_id": message.get("id"), "text_basis": text, "start": start,
                "end": end, "schema": VERSION}
    identity = {"canonical_names": sorted(identities),
                "requested_names": list(dict.fromkeys(entry[3] for entry in mentions)),
                "support_status": ("supported" if identities and identities <= set(SCIENCE_IDENTITIES)
                                   else "unsupported" if identities else "unknown")}
    if explicitly_unknown:
        identity["explicitly_unknown"] = True
    return identity, evidence


def _field_propositions(request, message, start, end, *, system):
    """Resolve scope from the containing proposition, not every name in a message."""
    carry = set()
    selected = []
    parts = []
    for left, right, clause in _propositions(message["text"]):
        boundaries = [match for match in re.finditer(r"\b(?:and|but)\b|但是|但|而", clause, re.I)
                      if re.search(r"R?HF|UHF|用|方法|电荷|多重度|\b(?:use[sd]?|charge|multiplicity)\b",
                                   clause[:match.start()], re.I)]
        position = 0
        for boundary in boundaries:
            parts.append((left + position, left + boundary.start(), clause[position:boundary.start()]))
            position = boundary.end()
        parts.append((left + position, right, clause[position:]))
    for left, right, clause in parts:
        names = _grounded_systems(request, [{"text": clause}])
        if names:
            carry = names
        scope = names or carry
        if left < end and right > start:
            selected.append((clause, scope))
    target = {system.id} if system else None
    if target:
        matching = [(clause, scope) for clause, scope in selected if not scope or scope == target]
        return matching
    return selected


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
    evidence = item.model_dump(mode="json")
    labels = {"method": r"方法|\bmethod\b", "basis": r"基组|\bbasis\b",
              "charge": r"电荷|\bcharge\b", "multiplicity": r"多重度|\bmultiplicity\b",
              "electronic_state": r"电子态|\breference\b", "environment": r"环境|\benvironment\b"}
    if item.source in {"explicit", "default", "inherited"}:
        source_message = _locate(item.text_basis, messages, item.message_id)[0] if item.source == "explicit" else None
        after_source = source_message is None
        for message in messages:
            if message is source_message:
                after_source = True
                continue
            if not after_source or name not in labels:
                continue
            for clause, _ in _field_propositions(request, message, 0, len(message["text"]), system=system):
                if re.search(labels[name], clause, re.I) and re.search(
                        r"未知|不确定|不知道|尚未确定|\b(?:unknown|uncertain|undecided)\b", clause, re.I):
                    raise StoreError("later explicit unknown condition cannot be overwritten by an older value")
    if item.source == "explicit":
        message, start, end = _locate(item.text_basis, messages, item.message_id)
        if item.system_ref is not None and (system is None or item.system_ref != system.id):
            raise StoreError("explicit condition system_ref must match its target scope")
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
        clauses = _field_propositions(request, message, start, end, system=system)
        pertinent = []
        for clause, scope in clauses:
            # Contrasting independent execution instructions cannot negate an
            # earlier method assertion ("Use RHF but do not execute").
            for part in re.split(r"\bbut\b|但是|但", clause, flags=re.I):
                if not any(present(token, part) for token in tokens):
                    continue
                if system is None and len(request.systems) > 1 and scope and scope != {
                        entry.id for entry in request.systems}:
                    raise StoreError("system-scoped condition cannot become a global condition")
                pertinent.append(part)
                # These phrases negate geometric constraints, not the method,
                # charge or reference state. Keep any surrounding negation.
                condition_clause = re.sub(r"无约束|\bwithout\s+(?:geometric\s+)?constraints?\b",
                                          " ", part, flags=re.I)
                if re.search(
                        r"不|非|未|无|禁止|拒绝|避免|排除|可能|也许|推断|推测|假设|待确认|尚待|或者|[?？]|"
                        r"\b(?:not|no|never|without|unknown|maybe|perhaps|uncertain|if|either|or)\b|n't\b",
                        condition_clause, re.I):
                    raise StoreError("explicit condition has a negated or uncertain value basis")
        if not pertinent:
            raise StoreError("explicit condition lacks a value in its target system proposition")
        # Other affirmative values of the same field in this same scope are a
        # conflict, even if a clipped positive quote selected only one of them.
        labels = {"charge": r"(?:净?电荷(?:数)?|\bcharge\b)",
                  "multiplicity": r"(?:自旋多重度|多重度|\bmultiplicity\b)"}
        if name in labels:
            for clause, scope in _field_propositions(request, message, 0, len(message["text"]), system=system):
                numbers = re.findall(labels[name] + r"\s*(?:(?:为|是|等于|设为|取|is|=|:|：)\s*)?([+-]?\d+)",
                                     clause, re.I)
                if any(int(number) != value for number in numbers):
                    raise StoreError("explicit condition has conflicting values in the same proposition")
        if name in {"method", "electronic_state"}:
            for clause, scope in _field_propositions(request, message, 0, len(message["text"]), system=system):
                methods = re.findall(r"(?<![a-z0-9_])(?:RHF|UHF|HF|B3LYP|PBE0?|MP2|DFT)(?![a-z0-9_])",
                                     clause, re.I)
                if name == "electronic_state":
                    methods = [method for method in methods if method.upper() in {"RHF", "UHF"}]
                expected = str(canonical_condition(name, value)).upper()
                for method in methods:
                    normalized = "HF" if name == "method" and method.upper() == "RHF" else method.upper()
                    if normalized != expected and not re.search(
                            r"不|非|未|无|\b(?:not|no|never|without)\b|n't\b", clause, re.I):
                        raise StoreError("explicit condition has conflicting values in the same system scope")
        evidence.update(message_id=message.get("id"), start=start, end=end,
                        system_ref=system.id if system else None)
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
        # A fresh text Request has no recorded provenance yet. Defaults still
        # cannot erase a field mentioned in the trusted input, including a
        # rejected/unknown value. The candidate must preserve that declaration.
        mentions = {
            "method": r"方法|\b(?:method|R?HF|UHF|B3LYP|PBE0?|MP2|DFT|Hartree[- ]Fock)\b",
            "basis": r"基组|\bbasis\b|STO[- ]?3G|\b(?:cc-p|def2|6-31)",
            "charge": r"电荷|中性|\b(?:charge|neutral|charged|cation|anion)\b",
            "multiplicity": r"多重度|单重|双重|三重|\b(?:multiplicity|singlet|doublet|triplet)\b",
            "electronic_state": r"电子态|闭壳层|开壳层|\b(?:reference|RHF|UHF)\b",
            "environment": r"环境|溶剂|气相|水溶液|\b(?:environment|solvent|aqueous|vacuum|gas)\b|"
                           r"\bin\s+(?:water|methanol|ethanol|solution)\b",
            "temperature_K": r"温度|\btemperature\b",
            "standard_state": r"标准态|\bstandard\s+state\b",
        }
        if any(re.search(mentions[name], clause, re.I) for message in messages
               for clause, _ in _field_propositions(request, message, 0, len(message["text"]), system=system)):
            raise StoreError("a condition declared in user text cannot be labeled or overwritten as a default")
    elif item.source == "inferred":
        message, start, end = _locate(item.text_basis, messages, item.message_id)
        evidence.update(message_id=message.get("id"), start=start, end=end,
                        system_ref=system.id if system else None)
    elif item.source == "unknown":
        if value is not None:
            raise StoreError("unknown condition cannot supply a value")
    elif name in PHYSICAL:
        raise StoreError("physical scientific conditions cannot be waived as not_applicable")
    return value, item.source, evidence


def _text_geometry_relation(request, messages, system_ids):
    """Finite grounding for new text intake; silence never selects SP or Opt."""
    scope = next((s for s in request.systems if system_ids == [s.id]), None)
    selected = set()
    for message in messages:
        current = set()
        denied = set()
        uncertain = False
        clauses = [part for clause, _ in _field_propositions(
            request, message, 0, len(message["text"]), system=scope)
            for part in re.split(r"\bbut\b|但是|但", clause, flags=re.I)]
        for clause in clauses:
            # A request to acquire starting coordinates is an input dependency,
            # not a request for their electronic energy. Preserve an initial-
            # geometry hint if energy directly qualifies that same noun phrase.
            relation_text = re.sub(
                r"(?:取得|获取|获得|准备|生成|提供|\b(?:obtain|acquire|prepare|generate|provide|get)\b)"
                r"(?:(?!电子能|能量|\benerg(?:y|ies)\b).){0,40}(?:初始几何|\binitial\s+geometry\b)"
                r"(?!\s*(?:的|上(?:的)?)?\s*(?:电子能|能量|\b(?:electronic\s+)?energ(?:y|ies)\b))",
                " ", clause, flags=re.I)
            # A forbidden label supplies no positive optimized relation. It
            # must not erase an independent SP request or permit an earlier
            # optimized relation to survive an explicit contradictory update.
            forbidden_label = (r"(?:不得|不要|不能|不许)\s*(?:称为|称作|声称|视为).{0,12}优化|"
                               r"\b(?:do\s+not|don't|never|must\s+not)\s+(?:call|describe|claim|label)"
                               r".{0,32}\boptimi[sz]ed\b")
            if re.search(forbidden_label, clause, re.I):
                denied.add("optimized")
                clause = re.sub(forbidden_label, " ", clause, flags=re.I)
                relation_text = re.sub(forbidden_label, " ", relation_text, flags=re.I)
            if re.search(r"不要|不得|不确定|未知|\b(?:not|unknown|uncertain)\b", clause, re.I):
                if re.search(r"几何|单点|优化|\b(?:geometry|sp|opt|optimization|energy\s+relation)\b", clause, re.I):
                    uncertain = True
                continue
            if re.search(r"优化|\b(?:opt|optimi[sz](?:e|ed|ation))\b", clause, re.I):
                current.add("optimized")
            if re.search(r"单点|初始几何|\b(?:sp|single[- ]point|initial\s+geometry)\b", relation_text, re.I):
                current.add("fixed_initial")
        if uncertain or current & denied:
            selected = set()
        elif current:
            selected = current
        elif denied:
            selected -= denied
    return next(iter(selected)) if len(selected) == 1 else None


_ELECTRONIC_ENERGY = re.compile(
    r"电子能(?:量)?|单点能|\b(?:electronic|single[- ]point|sp)\s+energ(?:y|ies)\b", re.I)
_GOAL_REPLACEMENT = (
    r"(?:改|换)(?:成|为)|替换|撤回.{0,24}目标|取消.{0,24}目标|"
    r"(?:replace\s+.+\s+with|change\s+.+\s+to|withdraw\s+.+goal)")
_NEGATED_GOAL_CHANGE = (
    r"(?:不(?=取消|撤回)|不要|不得|不许|禁止|别|不再|不想|do\s+not|don't|never)"
    r"[^，。！？,.;\n]{0,20}(?:改|换|替|撤|取消|change|replace|withdraw|cancel)")


def _explicit_goal_replacement(text):
    """A changed condition is not authorization to replace physical goals."""
    for _, _, clause in _propositions(text):
        if not re.search(_GOAL_REPLACEMENT, clause, re.I) or re.search(_NEGATED_GOAL_CHANGE, clause, re.I):
            continue
        if re.search(r"(?:方法|基组|电荷|多重度|电子态|环境|溶剂|温度|标准态|"
                     r"\b(?:method|basis|charge|multiplicity|state|environment|solvent|temperature|conditions?)\b)"
                     r"\s*(?:明确|仅|只)?\s*(?:改|换|替换|change|replace)", clause, re.I):
            continue
        if re.search(r"\b(?:change|replace)\s+.{0,40}\b(?:method|basis|charge|multiplicity|state|"
                     r"environment|solvent|temperature|conditions?)\s+(?:to|with)\b", clause, re.I):
            continue
        if re.search(r"目标|物理量|电子能|能量|几何|结构|\b(?:goals?|quantity|energy|geometry|structure)\b",
                     clause, re.I) or _mentions(clause, targets=True):
            return True
    return False


def _explicit_energy_requests(request, messages):
    """Bounded affirmative electronic-energy obligations, not a general parser.

    Use full user propositions and the same identity/solvent and relation
    lexicons as grounding. A model's Goal key or clipped quote is not input.
    """
    obligations = []
    carry = set()
    relations = {}
    for message in messages:
        for start, end, clause in _propositions(message["text"]):
            if re.search(r"[?？]|是否|能否|\bwhether\b", clause, re.I):
                continue
            # Separate independently requested quantities/targets, retaining
            # conjunctions inside a shared target list (water and methane).
            boundaries = [match for match in re.finditer(r"\b(?:and|but)\b|但是|但|而|并且", clause, re.I)
                          if re.search(r"给出|报告|计算|优化|登记|执行|启动|运行|方法|基组|电荷|多重度|"
                                       r"几何|结构|\b(?:report|calculate|optimi[sz]e|register|execute|run|start|method|basis|charge|multiplicity|geometry|structure)\b",
                                       clause[match.end():], re.I)]
            positions = [0, *(match.end() for match in boundaries)]
            ends = [*(match.start() for match in boundaries), len(clause)]
            for left, right in zip(positions, ends, strict=True):
                part = clause[left:right]
                names = {item[2] for item in _mentions(part, targets=True)}
                if names:
                    carry = names
                targets = names or carry
                withdrawn = (re.search(r"(?:取消|撤回).{0,24}目标|\b(?:cancel|withdraw).{0,40}goal\b", part, re.I)
                             and not re.search(_NEGATED_GOAL_CHANGE, part, re.I))
                if withdrawn and _ELECTRONIC_ENERGY.search(part):
                    # Explicit cancellation retires earlier matching quantity
                    # requests even during first normalization of several messages.
                    prior_targets = {item["target"] for item in obligations}
                    all_targets = bool(re.search(r"所有|全部|\ball\b", part, re.I))
                    retired = names or (prior_targets if all_targets or len(prior_targets) == 1 else set())
                    obligations = [item for item in obligations if item["target"] not in retired]
                    continue
                if _explicit_goal_replacement(part):
                    obligations = []
                # Questions, alternatives and negated requests are not positive
                # obligations. Independent no-execution clauses do not erase a
                # preceding requested physical quantity.
                quantity_text = re.sub(
                    r"不(?:要|再|得)?(?:执行|运行|启动)(?:任何)?(?:计算|程序|任务)|"
                    r"\bwithout\s+(?:executing|running|starting)\s+(?:any\s+)?(?:calculations?|computations?|orca|jobs?)\b",
                    " ", part, flags=re.I)
                if re.search(r"不要|不得|不(?:再|必|需|想)?(?:给出|报告|计算|提供|登记|记录|要求|需要)|"
                             r"无[须需]|或者|也许|可能需要|\b(?:not|no|never|without|if|either|or|maybe|perhaps)\b|n't\b",
                             quantity_text, re.I):
                    continue
                relation = _text_geometry_relation(request, [{"text": part}], [])
                if relation:
                    for target in targets or {None}:
                        relations[target] = relation
                if not _ELECTRONIC_ENERGY.search(part):
                    continue
                # Only explicit imperatives or compact requested-quantity names
                # are covered; mentions in explanations are outside this rule.
                if not re.search(r"给出|报告|计算|算|登记|记录|需要|要求|求|\b(?:report|give|calculate|compute|register|need|want)\b",
                                 part, re.I) and not re.fullmatch(
                                     r"\s*(?:[\w -]+的)?(?:单点|优化后的)?电子能(?:量)?\s*", part):
                    continue
                for target in targets or {None}:
                    obligations.append({"target": target, "geometry_relation": relation or relations.get(target),
                                        "message_id": message.get("id"),
                                        "start": start + left, "end": start + right})
    return obligations


def _require_energy_coverage(candidate, request, messages, goals):
    missing = []
    def goal_targets(goal):
        known = goal.identity.get("canonical_names", [])
        # Coverage has independently grounded the target in user text. An
        # already validated system binding can match that target across messages;
        # registry uniqueness alone never creates a new target obligation.
        return set(known) if known else {_registered_identity(system) for system in request.systems
                                         if system.id in goal.system_ids}
    for obligation in _explicit_energy_requests(request, messages):
        if any(goal.port == "energy" and goal.required
               and (obligation["target"] is None
                    or goal_targets(goal) == {obligation["target"]})
               and (obligation["geometry_relation"] is None
                    or goal.conditions.get("geometry_relation") == obligation["geometry_relation"])
               for goal in goals):
            continue
        missing.append(obligation)
    if missing:
        raise ProposalError("Preserve each explicit electronic-energy request as a required energy Goal "
                            "with its named target and stated geometry relation; a key, reason or "
                            "optimized_geometry Goal cannot substitute.",
                            path=["parameters", "goals"], missing_energy_requests=missing)


def _sampling_intents(text):
    """Finite affirmative check/acquire vocabulary, preserving negation scope."""
    intents = set()
    for _, _, clause in _propositions(text):
        for part in re.split(r"\bbut\b|但是|但|并且|\band\s+(?=check|assess|obtain|acquire|achieve)", clause, flags=re.I):
            if not re.search(r"采样|\bsampling\b", part, re.I):
                continue
            part = re.sub(r"不(?:要|得)?(?:执行|运行|启动)(?:新增|追加|任何)?计算|"
                          r"\bwithout\s+(?:(?:running|executing|starting)\s+)?(?:any\s+)?"
                          r"(?:(?:new|additional)\s+)?calculations?\b", " ", part, flags=re.I)
            if re.search(r"不要|不得|不(?:用|需|必|要)?(?:检查|核查|判断|取得|获得|补充)|"
                         r"\b(?:not|never|without)\b|n't\b|是否(?:需要|应该|检查|取得)|能否|要不要|"
                         r"\bshould\s+we\b|\bwhether\s+to\b|(?:可能|也许).{0,12}(?:检查|取得)|"
                         r"\b(?:maybe|might|possibly)\s+(?:check|obtain|acquire)\b", part, re.I):
                continue
            if re.search(r"检查|核查|判断|\b(?:check|assess|evaluate|determine)\b", part, re.I):
                intents.add("sampling_check")
            if re.search(r"取得|获得|补充.{0,16}(?:直到|满足|达标)|\b(?:obtain|acquire|achieve)\b", part, re.I):
                intents.add("sampling")
    return intents


def _sampling_specification(item, request, messages):
    message, start, end = _locate(item.text_basis, messages, item.message_id)
    proposition = ";".join(part for left, right, part in _propositions(message["text"])
                           if left < end and right > start)
    if item.port not in _sampling_intents(proposition):
        raise StoreError("sampling check/acquire port needs an affirmative matching user intent")
    if item.analysis_goal_ref is None:
        return {}, None
    sources = [goal for goal in request.goals if goal.port in {"sampling", "sampling_check"}
               and goal.conditions.get("sampling") and goal.conditions.get("candidates")]
    source = next((goal for goal in sources if goal.id == item.analysis_goal_ref), None)
    if source is None or (len(sources) > 1 and source.id not in proposition):
        raise StoreError("sampling specification needs an unambiguous registered Goal reference")
    if source.system_ids and item.system_refs != source.system_ids:
        raise StoreError("sampling specification reference has a different system scope")
    scope = None
    later_text = "\n".join(entry["text"] for entry in messages[messages.index(message):])
    for _, _, clause in _propositions(later_text):
        named = {goal.id for goal in sources
                 if re.search(r"(?<![\w-])" + re.escape(goal.id) + r"(?![\w-])", clause)}
        scope = named or scope
        if scope and source.id not in scope:
            continue
        for part in re.split(r"\bbut\b|但是|但", clause, flags=re.I):
            field = r"阈值|宽度|候选|\b(?:thresholds?|width|candidates?)\b"
            if not re.search(field, part, re.I):
                continue
            # Negating an unknown/change is not an instruction to erase a
            # confirmed specification. Other affirmative overrides stay gaps.
            part = re.sub(r"(?:不(?:是|再)|并非)\s*未知|\bnot\s+unknown\b", "", part, flags=re.I)
            if re.search(r"不(?:要|得|需)?(?:改|变|换)|\b(?:do\s+not|don't|never)\s+(?:change|modify|replace)\b",
                         part, re.I):
                continue
            override = r"未知|不确定|改|替换|\b(?:unknown|uncertain|change|modify|replace)\b"
            if re.search(rf"(?:{field}).{{0,24}}(?:{override})|(?:{override}).{{0,24}}(?:{field})", part, re.I):
                return {}, None
    return deepcopy(source.conditions), {"source": "inherited", "request_version": request.version,
                                         "goal_id": source.id}


def _require_sampling_coverage(request, messages, goals):
    required = set()
    sources = [goal.id for goal in request.goals if goal.port in {"sampling", "sampling_check"}
               and goal.conditions.get("sampling") and goal.conditions.get("candidates")]
    for message in messages:
        for _, _, part in _propositions(message["text"]):
            if _explicit_goal_replacement(part):
                required.clear()
            named = [identifier for identifier in sources
                     if re.search(r"(?<![\w-])" + re.escape(identifier) + r"(?![\w-])", part)]
            required.update((port, identifier) for port in _sampling_intents(part)
                            for identifier in named or [None])
    actual = {(goal.port, goal.text_evidence.get("sampling_specification", {}).get("goal_id"))
              for goal in goals if goal.required}
    actual.update((port, None) for port, _ in list(actual))
    missing = required - actual
    if missing:
        raise ProposalError("Keep explicit sampling checking and acquisition goals separate.",
                            path=["parameters", "goals"], missing_sampling_requests=sorted(missing, key=str))


def _goals(candidate, request, messages, *, text_input=False):
    systems = {s.id for s in request.systems}
    goals = []
    for goal_index, item in enumerate(candidate.goals or []):
        identity, text_evidence = _goal_grounding(request, item.text_basis, item.system_refs,
                                                 messages, message_id=item.message_id, text_input=text_input)
        if len(item.system_refs) != len(set(item.system_refs)) or set(item.system_refs) - systems:
            raise StoreError("semantic goal references unknown/duplicate registered systems")
        conditions = {}
        unresolved = list(item.unresolved)
        if identity.get("explicitly_unknown"):
            unresolved.append("ambiguous_system")
        if item.port in {"sampling", "sampling_check"}:
            conditions, inherited = _sampling_specification(item, request, messages)
            if inherited:
                text_evidence["sampling_specification"] = inherited
            else:
                unresolved.append("missing:sampling_specification")
            unresolved.extend("unsupported_system:" + name for name in identity["canonical_names"]
                              if name != "water")
        elif item.analysis_goal_ref is not None:
            raise StoreError("analysis_goal_ref is only valid for a sampling task")
        if item.port in {"energy", "optimized_geometry"}:
            unresolved.extend("unsupported_system:" + name for name in identity["canonical_names"]
                              if name not in SCIENCE_IDENTITIES)
            if not item.system_refs and request.systems and identity["canonical_names"]:
                registered_names = {_registered_identity(system) for system in request.systems}
                if registered_names != set(identity["canonical_names"]):
                    unresolved.extend("unbound_system:" + name for name in identity["canonical_names"])
            unresolved = list(dict.fromkeys(unresolved))
        for name, field in item.conditions.items():
            if field.source in {"unknown", "inferred"}:
                raise StoreError("unknown/inferred Goal conditions must be placed in "
                                 "Candidate.conditions/system_conditions; preserve goals.unresolved "
                                 "so the user can answer without replacing the goal")
            scope = next((system for system in request.systems
                          if item.system_refs == [system.id]), None)
            value, _, field_evidence = _field(name, field, request, messages, system=scope)
            conditions[name] = value
            text_evidence.setdefault("conditions", {})[name] = field_evidence
        acquisition = text_input or any(system.geometry_source == "prepare" for system in request.systems)
        grounded_relation = _text_geometry_relation(request, messages, item.system_refs) if acquisition else None
        if acquisition and item.port == "energy" and item.geometry_relation != grounded_relation:
            raise StoreError("text energy geometry relation needs explicit SP/optimization grounding; "
                             "otherwise preserve ambiguous_geometry_relation and ask")
        if item.geometry_relation:
            conditions["geometry_relation"] = item.geometry_relation
        elif item.port == "energy":
            if acquisition:
                unresolved.append("ambiguous_geometry_relation")
            else:
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
                              unresolved=unresolved,
                              identity=({**identity, "canonical_names": [_registered_identity(next(
                                  system for system in request.systems if system.id == member))]}
                                        if member else identity), text_evidence=text_evidence))
    if not goals or len(goals) > 8:
        raise StoreError("normalization requires one to eight bounded goals")
    _require_energy_coverage(candidate, request, messages, goals)
    _require_sampling_coverage(request, messages, goals)
    return goals


def _registration_blocking_gaps(request, gaps):
    """Known scope/geometry limits remain facts, not a demand for a reply."""
    blocking = set()
    for gap in gaps:
        parts = gap.split(":")
        if parts[0] in {"missing", "unconfirmed", "unknown", "field", "ambiguous"}:
            field = next((part for part in parts[1:] if part in PHYSICAL), None)
            if field:
                scopes = [system for system in request.systems if system.id in parts[1:]] or [None]
                for system in scopes:
                    value = (system.conditions.get(field, getattr(request, field, request.conditions.get(field)))
                             if system else getattr(request, field, request.conditions.get(field)))
                    source = (system.conditions_source.get(field, request.conditions_source.get(field))
                              if system else request.conditions_source.get(field))
                    if value is None or source in {"unknown", "inferred"}:
                        blocking.add(gap)
            elif gap in {"unknown:quantity", "field:quantity"}:
                if any(goal.port == "unresolved" for goal in request.goals):
                    blocking.add(gap)
            elif gap in {"unknown:query", "field:query"}:
                if any(goal.port == "unresolved" or (goal.port in READ_TOOLS and not goal.conditions.get("query"))
                       for goal in request.goals):
                    blocking.add(gap)
            elif gap == "missing:goal_definition" and any(
                    goal.port == "unresolved" and not any(
                        item.startswith("unsupported_quantity:") and item != "unsupported_quantity:unresolved"
                        for item in goal.unresolved) for goal in request.goals):
                blocking.add(gap)
        elif gap in {"ambiguous_system", "ambiguous_pronoun"} or gap.startswith("system:"):
            goals = [goal for goal in request.goals if not gap.startswith("system:")
                     or goal.id == gap.removeprefix("system:")]
            if any(not goal.identity.get("canonical_names") and not goal.system_ids for goal in goals):
                blocking.add(gap)
        elif gap == "ambiguous_geometry_relation" and any(
                goal.port == "energy" and not goal.conditions.get("geometry_relation") for goal in request.goals):
            blocking.add(gap)
    return blocking


def _communication(candidate, request, messages, gaps):
    """Separate a recorded requirement from a scientific result or required reply."""
    text = "\n".join(message["text"] for message in messages)
    registration = r"(?:只|仅)(?:做)?登记|(?:只|仅)记录(?:需求|要求)|\b(?:register|registration)\s+only\b"
    no_execution = r"不(?:要|再|得)?(?:执行|运行|启动|计算)|\b(?:do\s+not|don't|no)\s+(?:execute|run|compute|calculation)"
    scope = "science"
    # Only another explicit delivery instruction replaces a prior restriction;
    # method amendments, status and resume never create execution intent.
    for text in [request.original_text, *(entry["text"] for entry in request.messages), text]:
        if re.search(registration, text, re.I):
            scope = "registration_only"
        elif re.search(no_execution, text, re.I) or re.search(r"看看结果|只(?:看|查询|读取)", text):
            scope = "read_only"
        elif re.search(r"(?:现在|开始|请)(?:执行|运行|计算)|\b(?:execute|run|compute)\s+now\b", text, re.I):
            scope = "science"
    blocking = _registration_blocking_gaps(request, gaps) if scope == "registration_only" else gaps
    if set(candidate.question_gaps) - set(candidate.questions):
        raise ProposalError("question_gaps keys must be the exact supplied questions.",
                            path=["parameters", "question_gaps"])
    associations = {}
    for question in candidate.questions:
        declared = candidate.question_gaps.get(question)
        if declared is not None and (not declared or set(declared) - gaps):
            raise ProposalError("Each question must reference existing unresolved facts.",
                                path=["parameters", "question_gaps"])
        if not re.search(r"[?？]|请|是否|哪个|多少|什么|需要|\b(?:what|which|whether|please|confirm|provide)\b",
                         question, re.I):
            raise ProposalError("Declarative notices belong in notices, not questions.",
                                path=["parameters", "questions"])
        if declared is None:
            labels = {"charge": r"电荷|charge|电子态", "multiplicity": r"多重度|multiplicity|电子态",
                      "method": r"方法|method|条件", "basis": r"基组|basis|条件",
                      "environment": r"环境|溶剂|气相|environment|条件",
                      "geometry": r"几何|结构|geometry", "quantity": r"物理量|性质|quantity|property",
                      "system": r"体系|分子|对象|system|molecule", "query": r"字段|field|读取"}
            named = {name for name, pattern in labels.items() if re.search(pattern, question, re.I)}
            associated = {gap for gap in blocking if any(name in gap for name in named)}
            if not associated and len(blocking) == 1:
                associated = set(blocking)
            if not associated and blocking:
                raise ProposalError("Ambiguous question requires question_gaps identifying its concrete missing facts.",
                                    path=["parameters", "question_gaps"])
        else:
            associated = set(declared)
        if not associated or not associated.intersection(blocking):
            raise ProposalError("Question does not concern a missing fact required by the current delivery scope.",
                                path=["parameters", "questions"])
        associations[question] = sorted(associated)
    for notice in candidate.notices:
        if re.search(r"[?？]|请(?:提供|确认)|\bplease\s+(?:provide|confirm)\b", notice, re.I):
            raise ProposalError("Notices cannot ask for a required reply; use a question with its gap.",
                                path=["parameters", "notices"])
    if scope == "registration_only" and blocking and not associations:
        raise ProposalError("Critical unknowns needed to register the request require an actual question "
                            "associated with an existing blocking gap; notices alone cannot await a reply.",
                            path=["parameters", "questions"], blocking_gaps=sorted(blocking))
    return {"questions": candidate.questions, "notices": candidate.notices,
            "question_gaps": associations, "delivery_scope": scope,
            "awaiting_reply": bool(blocking)}


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
    if candidate.schema_version != VERSION:
        raise ProposalError("Legacy semantic candidates are read-only; submit request-semantics-2 with current evidence.",
                            path=["parameters", "schema_version"])
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
        negated = any(re.search(_NEGATED_GOAL_CHANGE, part, re.I) and not (
            re.search(r"阈值|宽度|候选|\b(?:thresholds?|width|candidates?)\b", part, re.I)
            and not re.search(r"目标|物理量|电子能|能量|几何|结构|\b(?:goals?|quantity|energy|geometry|structure)\b",
                              part, re.I)) for _, _, part in _propositions(whole_messages))
        explicit_replacement = _explicit_goal_replacement(whole_messages)
        if negated or not explicit_replacement:
            raise StoreError("goal replacement requires an explicit user replacement phrase")
    from orca_agent.natural import bind_text_identity
    request = bind_text_identity(request, run, pending, candidate)
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
    resolved_relations = set()
    if run.science_baseline_policy == "first_science_plan":
        for goal in values["goals"]:
            if goal["port"] != "energy" or "ambiguous_geometry_relation" not in goal["unresolved"]:
                continue
            relation = _text_geometry_relation(request, pending, goal["system_ids"])
            if relation is not None:
                goal["conditions"]["geometry_relation"] = relation
                goal["unresolved"].remove("ambiguous_geometry_relation")
                goal["text_evidence"]["geometry_relation"] = {
                    "value": relation, "source": "explicit", "message_ids": candidate.message_ids,
                    "rule": "text-geometry-relation-1"}
        if not any("ambiguous_geometry_relation" in goal["unresolved"] for goal in values["goals"]):
            resolved_relations.add("ambiguous_geometry_relation")
    def resolution_matches(gap):
        return gap in resolved_relations or _resolution_matches(gap, candidate)
    if "missing:goal_definition" in candidate.resolves:
        raise ProposalError("normalize replaces the raw_request/missing:goal_definition placeholder automatically "
                            "with requested Goals. Omit that marker from resolves; resolves is only for "
                            "answered field/binding questions.", path=["parameters", "resolves"])
    if set(candidate.resolves) - known_gaps or any(
            not resolution_matches(gap) for gap in candidate.resolves):
        raise StoreError("resolved ambiguity must identify an existing question and a grounded field/binding")
    values["unresolved"] = list(dict.fromkeys(
        ([] if initial else [gap for gap in request.unresolved
                            if gap not in set(candidate.resolves) | resolved_relations])
        + candidate.unresolved))
    unresolved_questions = [gap for gap in question_gaps if not resolution_matches(gap)]
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
        values["goals"] = [g.model_dump() for g in _goals(candidate, request, grounding,
            text_input=run.science_baseline_policy == "first_science_plan")]
    for goal_id, system_ids in candidate.goal_bindings.items():
        goal = next((g for g in values["goals"] if g["id"] == goal_id), None)
        if (goal is None or not system_ids or len(system_ids) != len(set(system_ids))
                or (goal["system_ids"] and goal["system_ids"] != system_ids)):
            raise StoreError("clarification must bind an unresolved goal to registered systems without replacing its scope")
        # The answer must respect both the current named answer and the original
        # proposition. A later mention cannot silently change a named molecule.
        original_identity = goal.get("identity") or {
            "canonical_names": sorted({entry[2] for entry in _mentions(goal["original_text"], targets=True)})}
        identity, evidence = _goal_grounding(request, candidate.text_basis, system_ids,
            pending, original_identity=original_identity,
            text_input=run.science_baseline_policy == "first_science_plan")
        if not identity["canonical_names"] and not _grounded_systems(request, pending):
            raise StoreError("clarification must name the registered system for an unresolved goal")
        goal["system_ids"] = system_ids
        goal["identity"] = identity
        goal["unresolved"] = [gap for gap in goal["unresolved"] if not gap.startswith("unbound_system:")]
        goal.setdefault("text_evidence", {}).update(binding=evidence)
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
                              for char in question) for question in candidate.questions + candidate.notices)
    if new_gaps and not visible_question:
        raise ProposalError("New unresolved conditions, including detected unsupported scope, require a visible question "
                            "or unsupported-scope notice in parameters.notices. Preserve explicit user choices; "
                            "do not ask to reconfirm them.",
                            path=["parameters", "questions"], new_unresolved=sorted(new_gaps))
    _authorized_geometry(store, updated, run.permission)
    for goal in updated.goals:
        if goal.port in {"energy", "optimized_geometry"} and len(goal.system_ids) == 1:
            system = next(system for system in updated.systems if system.id == goal.system_ids[0])
            if goal.identity.get("canonical_names") and system.geometry_artifact_id:
                from orca_agent.applicability import validate_goal_identity
                validate_goal_identity(store, updated, goal, system.geometry_artifact_id)
        query = goal.conditions.get("query", {})
        if query.get("artifact_id"):
            own = {a for rid in run.result_ids for a in store.load_result(run.id, rid).artifact_ids}
            if query["artifact_id"] not in set(run.permission.artifact_ids) | own:
                raise StoreError("semantic query must reference a registered authorized Artifact")
            store.artifact_path(query["artifact_id"])
        if query.get("run_id") and query["run_id"] != run.id:
            raise StoreError("semantic query cannot invent another Run reference")
    communication = _communication(candidate, updated, pending, current_gaps)
    notice_contract = validate_notices(updated, candidate.notices)
    semantics = {"schema": VERSION, "kind": candidate.kind,
                 "candidate": candidate.model_dump(mode="json"),
                 "replaced_goal_ids": [g.id for g in request.goals] if candidate.goals else [],
                 **communication, "notice_contract": notice_contract}
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
