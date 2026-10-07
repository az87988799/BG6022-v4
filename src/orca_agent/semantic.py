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


def action_parameters(allowed_tools=(), *, request=None):
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
    return {"normalize_request": {
        "instruction": "Ports:minimum_evidence_rules.port_rules; query:query_schemas[port]. "
        "environment=gas/solvent; condition_lexicon rows=[value,explicit aliases], not inference. "
        "electronic_state=RHF/UHF, not ground/excited. "
        "system_refs select geometry; no geometry conditions. Keep explain_results. "
        "minimum_evidence=[] still requires checks. Preserve unsupported/unknown requirements. "
        "Energy:geometry_relation=fixed_initial (SP) or optimized (after Opt). "
        "Unknown/inferred:conditions/system_conditions. "
        "Energy:temperature_K/standard_state only if requested; absent display unit=unknown, no question. "
        "science_scope: capability limits, not permission/defaults. "
        "Copy AUTHORITY.pending_user_message_ids; no execution. "
        "Verbatim unique text_basis; optional message_id; match field/target scope. "
        "normalize retires raw_request/missing:goal_definition, even after clarification; never resolves it. "
        "Registration/no-execution is not a Goal. amend keeps goals. "
        "New goals:system_refs; existing Goal.id:goal_bindings, never both. "
        "replace_goals:explicit replacement + all old IDs. "
        "gaps:field:<field>/system:<goal_id>; resolves=answered gaps. "
        "notices=declarations; question_gaps maps questions to current gap IDs.",
        "science_scope": {"systems": list(SCIENCE_COMPOSITIONS), "conditions": dict(PROFILE),
                          "names": {formula: list(SYSTEM_ALIASES.get(formula.casefold(), ()))
                                    for formula in SCIENCE_COMPOSITIONS},
                          "ports": sorted({port for tool in catalog() if "execute_orca" in tool["effects"]
                                           for port in tool["output_ports"]})},
        "questions_policy": (
            "Named identity != registered System/geometry; no geometry != unknown identity. "
            "Explicit registration-only: known scope/geometry limits get declarative notices in notices; "
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


def _goal_grounding(request, text, system_ids, messages, *, message_id=None, original_identity=None):
    message, start, end = _locate(text, messages, message_id)
    # Expand short quotes to their proposition so clipping off the named target
    # cannot bind the remaining word "energy" to an unrelated registered system.
    clauses = [part for left, right, part in _propositions(message["text"])
               if left < end and right > start]
    proposition = "，".join(clauses)
    contextual = _mentions(proposition, targets=True)
    quoted = _mentions(text, targets=True)
    mentions = (quoted if quoted and {entry[2] for entry in quoted} <= {
        entry[2] for entry in contextual} else contextual)
    if not mentions:
        preceding = _mentions(message["text"][:start], targets=True)
        preceding_ids = {entry[2] for entry in preceding}
        if len(preceding_ids) > 1 and system_ids:
            raise StoreError("ambiguous goal pronoun has multiple named antecedents")
        mentions = preceding
    identities = {entry[2] for entry in mentions}
    prior = set((original_identity or {}).get("canonical_names", []))
    if prior and identities and identities != prior:
        raise StoreError("goal binding contradicts the original goal identity")
    identities = prior or identities
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


def _goals(candidate, request, messages):
    systems = {s.id for s in request.systems}
    goals = []
    for goal_index, item in enumerate(candidate.goals or []):
        identity, text_evidence = _goal_grounding(request, item.text_basis, item.system_refs,
                                                 messages, message_id=item.message_id)
        if len(item.system_refs) != len(set(item.system_refs)) or set(item.system_refs) - systems:
            raise StoreError("semantic goal references unknown/duplicate registered systems")
        conditions = {}
        unresolved = list(item.unresolved)
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
                              unresolved=unresolved,
                              identity=({**identity, "canonical_names": [_registered_identity(next(
                                  system for system in request.systems if system.id == member))]}
                                        if member else identity), text_evidence=text_evidence))
    if not goals or len(goals) > 8:
        raise StoreError("normalization requires one to eight bounded goals")
    return goals


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
    blocking = {gap for gap in gaps if scope != "registration_only" or gap.startswith(
        ("unconfirmed:", "unknown:", "field:", "ambiguous", "system:", "missing:goal_definition",
         "missing:charge", "missing:multiplicity", "missing:method", "missing:basis"))}
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
            pending, original_identity=original_identity)
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
    semantics = {"schema": VERSION, "kind": candidate.kind,
                 "candidate": candidate.model_dump(mode="json"),
                 "replaced_goal_ids": [g.id for g in request.goals] if candidate.goals else [],
                 **communication}
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
