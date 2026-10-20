"""Bounded PubChem identity evidence and the single OPI initial-geometry path.

These are input guarantees, never optimized geometry or completed science. Tool
permission, budget reservation and environment occupancy belong to the caller.
Each implementation additionally checks its frozen input and publishes one Call
receipt; an incomplete intent is unknown and cannot trigger another execution.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import httpx
from pydantic import Field

from orca_agent.models import ElectronicInteger, Identifier, Record, fingerprint, utc_now

IDENTITY_RULE = "structure-identity-1"
PREPARATION_RULE = "structure-prepare-1"
GENERAL_IDENTITY_RULE = "structure-identity-2"
GENERAL_PREPARATION_RULE = "structure-prepare-2"
NAME_QUERY_IDENTITY_RULE = "structure-identity-3"
NAME_QUERY_PREPARATION_RULE = "structure-prepare-3"
CURRENT_IDENTITY_CONTRACT = "general-identity-3"
GENERAL_IDENTITY_CONTRACTS = ("general-identity-2", CURRENT_IDENTITY_CONTRACT)
GENERAL_IDENTITY_RULES = (GENERAL_IDENTITY_RULE, NAME_QUERY_IDENTITY_RULE)
RECEIPT_SCHEMA = "structure-call-1"
OPI_VERSION = "2.0.0"
RDKIT_VERSION = "2025.9.6"
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SYNONYM_BYTES = 1024 * 1024
IDENTITY_TIMEOUT_SECONDS = 120
WIKIDATA_ROOT = "https://www.wikidata.org/w/api.php"
IDENTITY_USER_AGENT = "ORCA-Agent/0.1 (https://github.com/az87988799/BG6022-v4; bounded chemical identity lookup)"
MAX_GEOMETRY_BYTES = 65536
PREPARE_TIMEOUT_SECONDS = 30
PREPARE_CORES = 1
PREPARE_MEMORY_MB = 1024
PUBCHEM_ROOT = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
PROPERTIES = ("MolecularFormula", "SMILES", "Charge", "IsotopeAtomCount", "AtomStereoCount",
              "BondStereoCount", "CovalentUnitCount")
ALIASES = {"water": ("water", "h2o", "水"), "methane": ("methane", "ch4", "甲烷")}
IDENTITY_PROFILE = {"water": {"formula": "H2O", "heavy": "O", "hydrogens": 2},
                    "methane": {"formula": "CH4", "heavy": "C", "hydrogens": 4}}


class ResolveParameters(Record):
    system_id: Identifier


class PrepareParameters(Record):
    system_id: Identifier
    # Preflight requires explicit values to match confirmed Request conditions;
    # defaults never resolve an unknown user charge or spin.
    charge: ElectronicInteger = 0
    multiplicity: Annotated[ElectronicInteger, Field(ge=1)] = 1


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _publish(path, data):
    from orca_agent.store import atomic_write
    atomic_write(path, data, immutable=True)


def _read_json(path, limit=1024 * 1024):
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError("bounded structure record missing or oversized")
    return json.loads(path.read_text(encoding="utf-8"))


def _call_directory(store, run, call):
    return store.path(f"runs/{run.id}/calls/{call.id}")


def _call_basis(run, call):
    return {"run_id": run.id, "call_id": call.id, "tool": call.tool,
            "step_id": call.step_id, "request_version": call.request_version,
            "parameters": call.parameters, "consumption": call.consumption}


def _source(run, call, **extra):
    return {"run_id": run.id, "call_id": call.id, "step_id": call.step_id,
            "request_version": call.request_version, **extra}


def recover_receipt(store, run, call):
    """Read a complete immutable receipt; absence is not proof of no execution."""
    path = _call_directory(store, run, call) / "receipt.json"
    if not path.exists():
        return None
    receipt = _read_json(path)
    if (not isinstance(receipt, dict) or not isinstance(receipt.get("data"), dict)
            or receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("basis") != _call_basis(run, call)
            or receipt.get("data_sha256") != fingerprint(receipt.get("data"))):
        raise ValueError("structure receipt identity or content hash differs")
    data = receipt["data"]
    if len(data.get("artifact_ids", [])) > 8:
        raise ValueError("structure receipt artifact count exceeds bound")
    expected = receipt.get("artifact_hashes", {})
    if set(expected) != set(data.get("artifact_ids", [])):
        raise ValueError("structure receipt lacks its complete artifact manifest")
    for artifact_id, digest in expected.items():
        store.artifact_path(artifact_id)
        artifact = store.load_artifact(artifact_id)
        if (artifact.sha256 != digest or artifact.run_id != run.id
                or artifact.source.get("call_id") != call.id):
            raise ValueError("structure receipt Artifact hash or Call provenance differs")
    return data


def _begin(store, run, call):
    recovered = recover_receipt(store, run, call)
    if recovered is not None:
        return recovered
    path = _call_directory(store, run, call) / "intent.json"
    if path.exists():
        raise ValueError("structure Call execution is unknown; receipt required before reconciliation")
    _publish(path, _json_bytes({"schema": RECEIPT_SCHEMA, "basis": _call_basis(run, call),
                               "created_at": utc_now().isoformat()}))
    return None


def _finish(store, run, call, data):
    hashes = {identifier: store.load_artifact(identifier).sha256
              for identifier in data.get("artifact_ids", [])}
    receipt = {"schema": RECEIPT_SCHEMA, "basis": _call_basis(run, call), "data": data,
               "data_sha256": fingerprint(data), "artifact_hashes": hashes}
    _publish(_call_directory(store, run, call) / "receipt.json", _json_bytes(receipt))
    return data


def _artifact(store, run, call, name, role, content):
    path = _call_directory(store, run, call) / name
    _publish(path, content)
    return store.import_artifact(path, role, run_id=run.id, source=_source(run, call))


def _alias_in(text, canonical):
    return any(re.search(r"(?<![a-z0-9_])" + re.escape(alias) + r"(?![a-z0-9_])", text, re.I)
               if alias.isascii() else alias in text for alias in ALIASES[canonical])


def _complete_quote_positions(message, evidence):
    """Current program bindings must contain every exact range in this message."""
    text = evidence.get("text_basis")
    locations = evidence.get("occurrences")
    if (not message or not isinstance(text, str) or not text.strip()
            or not isinstance(locations, list) or not locations):
        return False
    expected = [{"message_id": message.get("id"), "start": match.start(), "end": match.end()}
                for match in re.finditer(re.escape(text), message["text"])]
    if (locations != expected or any(type(row.get(name)) is not int
            for row in locations if isinstance(row, dict) for name in ("start", "end"))):
        return False
    primary = {"message_id": evidence.get("message_id"),
               "start": evidence.get("start"), "end": evidence.get("end")}
    return (all(type(primary[name]) is int for name in ("start", "end"))
            and primary in expected)


def _requirement(store, run, system_id):
    request = store.load_request(run)
    system = next((item for item in request.systems if item.id == system_id), None)
    identity = getattr(system, "identity", {}) if system else {}
    names = identity.get("canonical_names", [])
    if identity.get("contract") in GENERAL_IDENTITY_CONTRACTS:
        evidence = identity.get("text_evidence", {})
        message = next((entry for entry in request.messages
                        if entry.get("id") == evidence.get("message_id")), None)
        text = evidence.get("text_basis", "")
        requested = identity.get("requested_names", [])
        query = identity.get("query_name")
        authenticated = bool(message and text and message["text"].count(text) == 1)
        if identity.get("contract") == "general-identity-3" and "occurrences" in evidence:
            authenticated = _complete_quote_positions(message, evidence)
        if (system.geometry_source != "prepare" or identity.get("explicitly_unknown")
                or not authenticated
                or len(requested) != 1 or requested[0] not in text
                or not isinstance(query, str) or not 0 < len(query) <= 100
                or any(ord(char) < 32 for char in query)):
            raise ValueError("general identity requires frozen exact user-name and lookup provenance")
        return request, system, query, evidence
    if (system is None or getattr(system, "geometry_source", None) != "prepare" or len(names) != 1
            or names[0] not in IDENTITY_PROFILE or identity.get("explicitly_unknown")):
        raise ValueError("structure Tool requires one frozen supported identity and prepare input intent")
    canonical = names[0]
    related = [goal for goal in request.goals if goal.system_ids == [system_id]
               and goal.identity.get("canonical_names") == names]
    evidence = identity.get("text_evidence") or next((goal.text_evidence for goal in related
                                                     if goal.text_evidence), {})
    message = next((entry for entry in request.messages if entry.get("id") == evidence.get("message_id")), None)
    quote = evidence.get("text_basis", "")
    if (message is None or not quote or message["text"].count(quote) != 1
            or not _alias_in(quote, canonical)):
        raise ValueError("identity query requires authenticated exact user name evidence")
    return request, system, canonical, evidence


def validate_call_inputs(store, run, parameters, tool, consumption=None):
    """Pre-reservation validation shared with the registered Tool implementation."""
    if tool not in {"structure.resolve", "structure.prepare"}:
        raise ValueError("unsupported structure Tool")
    schema = ResolveParameters if tool == "structure.resolve" else PrepareParameters
    params = schema.model_validate(parameters)
    if tool not in run.permission.allowed_tools or not run.permission.artifact_writes:
        raise ValueError("structure Tool and artifact writes must be explicitly allowed")
    permission = "external_identity_queries" if tool == "structure.resolve" else "geometry_preparation"
    if not getattr(run.permission, permission, False):
        raise ValueError("structure Tool execution effect was not authorized")
    request, system, canonical, evidence = _requirement(store, run, params.system_id)
    if tool == "structure.prepare":
        from orca_agent.applicability import preparation_conditions
        from orca_agent.condition_selection import general_admission_enabled
        if run.permission.max_cores < PREPARE_CORES or run.permission.max_memory_mb < PREPARE_MEMORY_MB:
            raise ValueError("preparation resources exceed the frozen permission snapshot")
        conditions = preparation_conditions(request, system.id)
        general = general_admission_enabled(store, run)
        if ((not general and (params.charge != 0 or params.multiplicity != 1))
                or conditions["conditions"].get("charge") != params.charge
                or conditions["conditions"].get("multiplicity") != params.multiplicity
                or any("charge" in reason or "multiplicity" in reason
                       for reason in conditions["reasons"])):
            raise ValueError("preparation requires confirmed charge and multiplicity matching its parameters and frozen admission profile")
        if system.identity.get("ambiguities"):
            raise ValueError("initial geometry cannot silently resolve the frozen identity ambiguities")
        document, _ = _bound_identity(store, run, consumption or {}, canonical)
        if general:
            _validate_preparation_state(document, params)
    from orca_agent.input_bindings import input_check_version
    from orca_agent.tools.registry import get_tool
    return {"canonical_name": canonical, "system_id": system.id, "text_evidence": evidence,
            "general_identity": system.identity.get("contract") in GENERAL_IDENTITY_CONTRACTS,
            "check_version": input_check_version(get_tool(tool), request, system.id),
            "requested_identity": system.identity}


def _bound_identity(store, run, consumption, canonical):
    from orca_agent.condition_selection import general_admission_enabled
    binding = consumption.get("identity", {})
    if binding.get("port") != "resolved_identity" or binding.get("status") != "qualified":
        raise ValueError("preparation requires the qualified resolved_identity input")
    source_run, result_id = binding.get("run_id"), binding.get("result_id")
    if source_run != run.id and result_id not in run.permission.result_ids:
        raise ValueError("identity Result is outside frozen permissions")
    result = store.load_result(source_run, result_id)
    from orca_agent.input_bindings import validate_input_result
    request = store.load_request(run)
    validated = validate_input_result(store, run, request, result, "resolved_identity")
    rule = validated["rule_version"]
    output = result.qualified_outputs.get("resolved_identity")
    if (result.operation_status != "completed" or output is None
            or output.checks != result.checks.get("resolved_identity")
            or any(check.status != "passed" or check.rule_version != rule for check in output.checks)
            or result.source.get("tool") != "structure.resolve"
            or binding.get("result_fingerprint") != fingerprint(result)):
        raise ValueError("identity Result does not carry the required immutable check guarantee")
    path = store.artifact_path(output.artifact_id)
    artifact = store.load_artifact(output.artifact_id)
    if binding.get("artifact_hashes", {}).get(artifact.id) != artifact.sha256:
        raise ValueError("identity input Artifact changed after reservation")
    document = _read_json(path, MAX_GEOMETRY_BYTES)
    general = general_admission_enabled(store, run)
    if (document.get("schema") != rule or document.get("canonical_name") != canonical
            or not general and document.get("charge") != 0):
        raise ValueError("identity snapshot conflicts with current requested identity")
    graph = _general_graph(document.get("smiles")) if rule in GENERAL_IDENTITY_RULES else None
    if graph:
        if graph["unspecified_stereochemistry"]:
            raise ValueError("initial geometry cannot silently choose unspecified stereochemistry")
        if not general and (graph["radical_electrons"] or graph["stereochemistry"]):
            raise ValueError("historical initial-geometry profile excludes radicals and stereochemical centers/bonds")
        if document.get("graph") != graph or document.get("charge") != graph["formal_charge"]:
            raise ValueError("identity snapshot differs from its complete graph or formal charge")
    if rule == GENERAL_IDENTITY_RULE and document.get("alias_verified") is not True:
        raise ValueError("lookup alias lacks independently retrieved PubChem name evidence")
    if rule == NAME_QUERY_IDENTITY_RULE:
        requested = next(system.identity for system in request.systems if system.id == validated["system_id"])
        original = requested.get("requested_names", [])
        if (len(original) != 1 or document.get("original_name") != original[0]
                or document.get("query_name") != canonical
                or document.get("query_name_source") != requested.get("query_name_source")
                or document.get("name_mapping_independently_verified") is not False):
            raise ValueError("name-query provenance differs from the frozen requested identity")
        _name_evidence_target(original[0], document)
    if document.get("elements") != (graph["elements"] if graph else _validate_smiles(document.get("smiles"), canonical)):
        raise ValueError("identity atom mapping differs from its complete graph")
    return document, artifact


def _validate_preparation_state(document, params):
    from rdkit import Chem
    if document.get("charge") != params.charge:
        raise ValueError("preparation charge differs from the resolved molecular identity")
    electrons = sum(Chem.GetPeriodicTable().GetAtomicNumber(element)
                    for element in document["elements"]) - params.charge
    if electrons < 0 or params.multiplicity > electrons + 1 or (electrons - params.multiplicity + 1) % 2:
        raise ValueError("confirmed multiplicity is inconsistent with the electron count; spin is not inferred")


def _versions():
    observed = {"orca-pi": importlib.metadata.version("orca-pi"),
                "rdkit": importlib.metadata.version("rdkit")}
    if observed != {"orca-pi": OPI_VERSION, "rdkit": RDKIT_VERSION}:
        raise ValueError("structure runtime differs from the locked OPI/RDKit profile")
    return {**observed, "python": sys.version.split()[0]}


def _validate_smiles(smiles, canonical):
    """Check the complete connected graph; formula/CID alone are insufficient."""
    from rdkit import Chem
    if not isinstance(smiles, str) or not 0 < len(smiles) <= 128:
        raise ValueError("bounded isomeric SMILES missing")
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None or len(Chem.GetMolFrags(molecule)) != 1:
        raise ValueError("identity needs one valid covalent component")
    for atom in molecule.GetAtoms():
        if (atom.GetIsotope() or atom.GetFormalCharge() or atom.GetNumRadicalElectrons()
                or atom.GetChiralTag() != Chem.ChiralType.CHI_UNSPECIFIED):
            raise ValueError("charged, isotope-labelled, stereochemical or radical input is unsupported")
    explicit = Chem.AddHs(molecule)
    profile = IDENTITY_PROFILE[canonical]
    heavy = [atom for atom in explicit.GetAtoms() if atom.GetSymbol() != "H"]
    if (len(heavy) != 1 or heavy[0].GetSymbol() != profile["heavy"]
            or explicit.GetNumAtoms() != profile["hydrogens"] + 1
            or heavy[0].GetDegree() != profile["hydrogens"]
            or explicit.GetNumBonds() != profile["hydrogens"]):
        raise ValueError("returned connectivity differs from the requested water/methane identity")
    for bond in explicit.GetBonds():
        if (bond.GetBondType() != Chem.BondType.SINGLE or bond.GetStereo() != Chem.BondStereo.STEREONONE
                or {bond.GetBeginAtom().GetSymbol(), bond.GetEndAtom().GetSymbol()} != {profile["heavy"], "H"}):
            raise ValueError("returned bond graph differs from the requested identity")
    return [atom.GetSymbol() for atom in explicit.GetAtoms()]


def _general_graph(smiles):
    """Bounded graph facts, without inferring multiplicity or a ground state."""
    from rdkit import Chem
    from rdkit.Chem import rdMolDescriptors
    if not isinstance(smiles, str) or not 0 < len(smiles) <= 2048:
        raise ValueError("bounded isomeric SMILES missing")
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None or len(Chem.GetMolFrags(molecule)) != 1 or molecule.GetNumAtoms() > 128:
        raise ValueError("identity needs one valid covalent graph of at most 128 non-explicit-H atoms")
    if not molecule.GetNumAtoms() or any(not 1 <= atom.GetAtomicNum() <= 118 for atom in molecule.GetAtoms()):
        raise ValueError("identity graph requires concrete chemical elements")
    explicit = Chem.AddHs(molecule)
    if explicit.GetNumAtoms() > 384:
        raise ValueError("explicit hydrogen graph exceeds the preparation bound")
    stereo = Chem.FindPotentialStereo(molecule)
    return {"elements": [atom.GetSymbol() for atom in explicit.GetAtoms()],
            "bonds": [[bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()] for bond in explicit.GetBonds()],
            "formula": rdMolDescriptors.CalcMolFormula(molecule),
            "formal_charge": sum(atom.GetFormalCharge() for atom in molecule.GetAtoms()),
            "radical_electrons": sum(atom.GetNumRadicalElectrons() for atom in molecule.GetAtoms()),
            "stereochemistry": [str(item.type) + ":" + str(item.centeredOn) for item in stereo],
            "unspecified_stereochemistry": [str(item.type) + ":" + str(item.centeredOn) for item in stereo
                if item.specified != Chem.StereoSpecified.Specified],
            "isomeric_smiles": Chem.MolToSmiles(molecule, isomericSmiles=True),
            "isotopes": [[atom.GetIdx(), atom.GetIsotope()] for atom in explicit.GetAtoms() if atom.GetIsotope()]}


def validate_pubchem_response(raw, canonical, *, general=False):
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("PubChem response exceeds bounded size")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or not isinstance(payload.get("PropertyTable"), dict):
        raise ValueError("PubChem property envelope is missing or malformed")
    candidates = payload["PropertyTable"].get("Properties")
    if not isinstance(candidates, list) or len(candidates) != 1:
        raise ValueError("PubChem name lookup must return exactly one candidate")
    record = candidates[0]
    if not isinstance(record, dict):
        raise ValueError("PubChem candidate property object is malformed")
    if type(record.get("CID")) is not int or record["CID"] <= 0:
        raise ValueError("PubChem CID is missing or invalid")
    if general:
        metadata = {name: record.get(name) for name in PROPERTIES if name not in {"SMILES", "MolecularFormula"}}
        if (any(type(value) is not int for value in metadata.values())
                or any(value < 0 for key, value in metadata.items() if key != "Charge")
                or metadata["CovalentUnitCount"] != 1):
            raise ValueError("PubChem graph/state/component metadata is incomplete")
        graph = _general_graph(record.get("SMILES"))
        if record.get("MolecularFormula") != graph["formula"] or record["Charge"] != graph["formal_charge"]:
            raise ValueError("PubChem formula or formal charge differs from the returned complete graph")
        return {"schema": GENERAL_IDENTITY_RULE, "canonical_name": canonical, "cid": record["CID"],
            "smiles": record["SMILES"], "formula": record["MolecularFormula"], "charge": record["Charge"],
            "elements": graph["elements"], "graph": graph, "metadata": metadata,
            "limitations": ["Name lookup does not establish a unique experimental specimen or electronic ground state.",
                             "Unspecified stereochemistry is retained and blocks initial-geometry preparation."]}
    expected = {"Charge": 0, "IsotopeAtomCount": 0, "AtomStereoCount": 0,
                "BondStereoCount": 0, "CovalentUnitCount": 1}
    if any(type(record.get(name)) is not int or record[name] != value for name, value in expected.items()):
        raise ValueError("PubChem isotope/state/component metadata is unsupported or incomplete")
    if record.get("MolecularFormula") != IDENTITY_PROFILE[canonical]["formula"]:
        raise ValueError("PubChem formula conflicts with the requested identity")
    elements = _validate_smiles(record.get("SMILES"), canonical)
    return {"schema": IDENTITY_RULE, "canonical_name": canonical, "cid": record["CID"],
            "smiles": record["SMILES"], "formula": record["MolecularFormula"], "charge": 0,
            "elements": elements, "metadata": {key: record[key] for key in expected}}


def _wikidata_search_url(name):
    return WIKIDATA_ROOT + "?" + urlencode({"action": "wbsearchentities", "search": name, "language": "zh",
                                           "type": "item", "limit": "5", "format": "json"})


def _wikidata_entities_url(identifiers):
    return WIKIDATA_ROOT + "?" + urlencode({"action": "wbgetentities", "ids": "|".join(identifiers),
        "props": "labels|aliases|claims", "languages": "zh|zh-hans|zh-hant|en", "format": "json"})


def _wikidata_endpoint(url):
    parsed = urlsplit(url)
    if parsed.scheme + "://" + parsed.netloc + parsed.path != WIKIDATA_ROOT or parsed.fragment:
        return False
    query = parse_qs(parsed.query, strict_parsing=True)
    if any(len(values) != 1 for values in query.values()):
        return False
    values = {key: values[0] for key, values in query.items()}
    if values.get("action") == "wbsearchentities":
        name = values.get("search", "")
        return 0 < len(name) <= 100 and not any(ord(char) < 32 for char in name) and url == _wikidata_search_url(name)
    if values.get("action") == "wbgetentities":
        ids = values.get("ids", "").split("|")
        return (1 <= len(ids) <= 5 and len(set(ids)) == len(ids)
                and all(re.fullmatch(r"Q[1-9][0-9]{0,11}", item) for item in ids)
                and url == _wikidata_entities_url(ids))
    return False


def _http_get(url, *, send_guard=None, deadline=None):
    """Fixed public identity endpoints; no redirects, proxies or retries."""
    suffix = "/property/" + ",".join(PROPERTIES) + "/JSON"
    parsed = urlsplit(url)
    encoded_name = url[len(PUBCHEM_ROOT):-len(suffix)] if url.startswith(PUBCHEM_ROOT) and url.endswith(suffix) else ""
    name = unquote(encoded_name)
    synonyms = re.fullmatch(r"https://pubchem\.ncbi\.nlm\.nih\.gov/rest/pug/compound/cid/[1-9][0-9]{0,11}/synonyms/JSON", url)
    wikidata = _wikidata_endpoint(url)
    if not synonyms and not wikidata and (parsed.scheme != "https" or parsed.netloc != "pubchem.ncbi.nlm.nih.gov"
            or parsed.query or parsed.fragment or not 0 < len(name) <= 100
            or any(ord(char) < 32 for char in name) or quote(name, safe="") != encoded_name):
        raise ValueError("unregistered identity query endpoint")
    response_limit = MAX_SYNONYM_BYTES if synonyms or wikidata else MAX_RESPONSE_BYTES
    started = time.monotonic()
    remaining = min(20, deadline - started) if deadline is not None else 20
    if remaining <= 0:
        raise ValueError("identity retrieval deadline exhausted before HTTP")
    data = bytearray()
    status = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(min(10, remaining), connect=min(5, remaining)), follow_redirects=False, trust_env=False) as client:
            manager = client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity",
                                                        "User-Agent": IDENTITY_USER_AGENT})
            # The control generation is held stable through request submission,
            # then released for messages/cancel while the bounded body arrives.
            with send_guard or nullcontext():
                response = manager.__enter__()
            try:
                status = response.status_code
                # Preserve actual response bytes, never an unbounded decompressor.
                for chunk in response.iter_raw():
                    available = response_limit - len(data)
                    data.extend(chunk[:available])
                    if len(chunk) > available or time.monotonic() - started > remaining:
                        return status, bytes(data), False
                return status, bytes(data), (response.headers.get("content-encoding", "identity") == "identity"
                                             and time.monotonic() - started <= remaining)
            finally:
                manager.__exit__(*sys.exc_info())
    except httpx.HTTPError:
        return status, bytes(data), False


@contextmanager
def _send_guard(store, run):
    with store.control_lock(run.id):
        store.check_execution_control(run)
        yield


def _paced_query(store, url, *, run=None, deadline=None):
    from orca_agent.store import atomic_write, controlled_path
    lock = store._lock(controlled_path(store.environment_root, "identity-query.lock"))
    timeout = min(30, deadline - time.monotonic()) if deadline is not None else 30
    if timeout <= 0:
        raise ValueError("identity retrieval deadline exhausted before pacing")
    with lock.acquire(timeout=timeout):
        pace = controlled_path(store.environment_root, "identity-query-time.json")
        previous = _read_json(pace).get("started_at", 0) if pace.exists() else 0
        delay = previous + 1 - time.time()
        if delay > 2:
            raise ValueError("identity query clock moved backwards; quota cannot be assumed free")
        if delay > 0:
            if deadline is not None and time.monotonic() + delay >= deadline:
                raise ValueError("identity retrieval deadline exhausted during pacing")
            time.sleep(delay)
        atomic_write(pace, _json_bytes({"started_at": time.time()}))
        options = {"deadline": deadline} if deadline is not None else {}
        if run is not None:
            options["send_guard"] = _send_guard(store, run)
        return _http_get(url, **options)


def validate_wikidata_alias(search_raw, entities_raw, original_name, cid):
    """One exact Chinese label/alias plus one unambiguous P662 cross-reference."""
    if len(search_raw) > MAX_SYNONYM_BYTES or len(entities_raw) > MAX_SYNONYM_BYTES:
        raise ValueError("Wikidata identity evidence exceeds its byte bound")
    search, payload = json.loads(search_raw), json.loads(entities_raw)
    candidates = search.get("search", []) if isinstance(search, dict) else []
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5:
        raise ValueError("Wikidata name search lacks bounded candidate entities")
    ids = [item.get("id") for item in candidates if isinstance(item, dict)]
    entities = payload.get("entities", {}) if isinstance(payload, dict) else {}
    if (len(ids) != len(candidates) or len(set(ids)) != len(ids)
            or any(not isinstance(item, str) or not re.fullmatch(r"Q[1-9][0-9]{0,11}", item) for item in ids)
            or not isinstance(entities, dict) or set(entities) != set(ids)):
        raise ValueError("Wikidata entity receipt differs from its exact search selection")
    matches = []
    for identifier in ids:
        entity = entities[identifier]
        if not isinstance(entity, dict) or entity.get("id") != identifier or entity.get("type") != "item":
            continue
        labels, aliases, claims = entity.get("labels", {}), entity.get("aliases", {}), entity.get("claims", {})
        if not all(isinstance(value, dict) for value in (labels, aliases, claims)):
            continue
        names = []
        for language in ("zh", "zh-hans", "zh-hant"):
            label = labels.get(language, {})
            names.extend([label.get("value")] if isinstance(label, dict) else [])
            entries = aliases.get(language, [])
            if isinstance(entries, list):
                names.extend(item.get("value") for item in entries if isinstance(item, dict))
        if original_name not in names:
            continue
        values = set()
        statements = claims.get("P662", [])
        for statement in statements if isinstance(statements, list) else []:
            if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
                continue
            snak = statement.get("mainsnak", {})
            if not isinstance(snak, dict) or snak.get("snaktype") != "value" or snak.get("property") != "P662":
                continue
            data = snak.get("datavalue", {})
            value = data.get("value") if isinstance(data, dict) else None
            if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,11}", value):
                values.add(value)
        if values:
            matches.append((identifier, values, entity.get("lastrevid")))
    if len(matches) != 1 or matches[0][1] != {str(cid)}:
        raise ValueError("original name lacks one unambiguous matching PubChem cross-reference")
    return {"entity_id": matches[0][0], "entity_revision": matches[0][2], "original_name": original_name,
            "property": "P662", "pubchem_cid": cid, "source_kind": "community_maintained_cross_reference",
            "limitation": "A sourced name mapping, not an experimental specimen or electronic-state guarantee."}


def _failure(run, call, reason, artifacts=(), *, status="failed", **facts):
    return {"operation_status": status, "artifact_ids": list(artifacts), "qualified_outputs": {},
            "diagnostics": [{"category": "structure_input_unavailable", "reason": reason}],
            "source": _source(run, call, **facts)}


def _name_evidence_target(original_name, document):
    """Separate an explicit trailing formula, without treating it as a name match."""
    from collections import Counter

    from rdkit import Chem

    formula = r"(?:[A-Z][a-z]?(?:[1-9][0-9]*)?)+"
    patterns = (rf"(.+?)\s*\(\s*({formula})\s*\)", rf"(.+?)\s*（\s*({formula})\s*）",
                rf"(.+?)\s+({formula})", rf"(.*[\u3400-\u9fff])({formula})")
    match = next((found for pattern in patterns
                  if (found := re.fullmatch(pattern, original_name))), None)
    if match is None:
        return original_name, None
    name, declared = match.group(1).strip(), match.group(2)
    elements = {Chem.GetPeriodicTable().GetElementSymbol(number) for number in range(1, 119)}
    counts = Counter()
    for element, count in re.findall(r"([A-Z][a-z]?)([0-9]*)", declared):
        if element not in elements:
            return original_name, None  # E.g. a grade abbreviation is not a formula.
        counts[element] += int(count or 1)
    if counts != Counter(document["elements"]):
        raise ValueError("explicit name formula conflicts with the returned complete identity graph")
    return name, {"rule": "explicit-name-formula-1", "original_name": original_name,
                  "name_for_evidence": name, "explicit_formula": declared,
                  "resolved_formula": document["formula"], "formula_matches_graph": True,
                  "limitation": "Formula agreement does not confirm the name, connectivity or electronic state."}


def resolve(store, run, call):
    requirement = validate_call_inputs(store, run, call.parameters, call.tool, call.consumption)
    if recovered := _begin(store, run, call):
        return recovered
    canonical = requirement["canonical_name"]
    url = PUBCHEM_ROOT + quote(canonical, safe="") + "/property/" + ",".join(PROPERTIES) + "/JSON"
    rule = requirement["check_version"]
    artifacts = []
    deadline = min(time.monotonic() + IDENTITY_TIMEOUT_SECONDS,
                   time.monotonic() + max(0, (run.deadline - utc_now()).total_seconds()))
    source = {**requirement, "url": url, "requested_at": utc_now().isoformat(),
              "normalization_rule": rule, "http_requests": 0, "timeout_seconds": IDENTITY_TIMEOUT_SECONDS}
    try:
        status, raw, complete = _paced_query(store, url, run=run, deadline=deadline)
        source["http_requests"] += 1
        raw_artifact = _artifact(store, run, call, "pubchem-response.json", "identity_raw_response", raw)
        artifacts.append(raw_artifact.id)
        source.update(http_status=status, response_complete=complete, raw_sha256=raw_artifact.sha256,
                      raw_artifact_id=raw_artifact.id, fetched_at=utc_now().isoformat() if status else None)
        if status != 200 or not complete:
            return _finish(store, run, call, _failure(run, call, "pubchem_http_or_response_bound", artifacts, **source))
        document = {**validate_pubchem_response(raw, canonical, general=requirement["general_identity"]),
                    "schema": rule, "query": source}
        if requirement["general_identity"]:
            original_name = requirement["requested_identity"]["requested_names"][0]
            requested_name, name_evidence = _name_evidence_target(original_name, document)
            if name_evidence is not None:
                document["name_evidence"] = source["name_evidence"] = name_evidence
            if rule == NAME_QUERY_IDENTITY_RULE:
                document.update(original_name=original_name, query_name=canonical,
                    query_name_source=requirement["requested_identity"]["query_name_source"],
                    name_mapping_independently_verified=False)
                document["limitations"].append(
                    "The queried PubChem candidate and its graph/formula/charge were checked; "
                    "the original name or inferred translation was not independently matched "
                    "against synonyms or a cross-reference.")
            else:
                document["alias_verified"] = canonical == requested_name
            if rule == GENERAL_IDENTITY_RULE and not document["alias_verified"]:
                alias_url = ("https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/"
                             + str(document["cid"]) + "/synonyms/JSON")
                alias_status, alias_raw, alias_complete = _paced_query(store, alias_url, run=run, deadline=deadline)
                source["http_requests"] += 1
                alias_artifact = _artifact(store, run, call, "pubchem-synonyms.json", "identity_raw_response", alias_raw)
                artifacts.append(alias_artifact.id)
                alias_evidence = {"url": alias_url, "http_status": alias_status, "complete": alias_complete,
                                  "artifact_id": alias_artifact.id, "sha256": alias_artifact.sha256}
                source["alias_evidence"] = alias_evidence
                if alias_status == 200 and alias_complete:
                    alias_payload = json.loads(alias_raw)
                    listing = alias_payload.get("InformationList", {}) if isinstance(alias_payload, dict) else {}
                    entries = listing.get("Information", []) if isinstance(listing, dict) else []
                    if (isinstance(entries, list) and len(entries) == 1 and isinstance(entries[0], dict)
                            and entries[0].get("CID") == document["cid"]):
                        synonyms = entries[0].get("Synonym", [])
                        if isinstance(synonyms, list) and len(synonyms) <= 32768:
                            document["alias_verified"] = any(isinstance(name, str)
                                and name.casefold() == requested_name.casefold() for name in synonyms)
                document["alias_evidence"] = alias_evidence
                if not document["alias_verified"]:
                    cross_evidence = []
                    search_url = _wikidata_search_url(requested_name)
                    search_status, search_raw, search_complete = _paced_query(store, search_url, run=run, deadline=deadline)
                    source["http_requests"] += 1
                    search_artifact = _artifact(store, run, call, "wikidata-search.json", "identity_raw_response", search_raw)
                    artifacts.append(search_artifact.id)
                    cross_evidence.append({"url": search_url, "status": search_status, "complete": search_complete,
                                           "artifact_id": search_artifact.id, "sha256": search_artifact.sha256})
                    source["cross_reference_evidence"] = cross_evidence
                    if search_status == 200 and search_complete:
                        search_payload = json.loads(search_raw)
                        candidates = search_payload.get("search", []) if isinstance(search_payload, dict) else []
                        ids = [item.get("id") for item in candidates if isinstance(item, dict)] if isinstance(candidates, list) else []
                        if (1 <= len(ids) <= 5 and len(set(ids)) == len(ids)
                                and all(isinstance(item, str) and re.fullmatch(r"Q[1-9][0-9]{0,11}", item) for item in ids)):
                            entities_url = _wikidata_entities_url(ids)
                            entity_status, entity_raw, entity_complete = _paced_query(store, entities_url, run=run, deadline=deadline)
                            source["http_requests"] += 1
                            entity_artifact = _artifact(store, run, call, "wikidata-entities.json", "identity_raw_response", entity_raw)
                            artifacts.append(entity_artifact.id)
                            cross_evidence.append({"url": entities_url, "status": entity_status, "complete": entity_complete,
                                "artifact_id": entity_artifact.id, "sha256": entity_artifact.sha256})
                            if entity_status == 200 and entity_complete:
                                document["name_cross_reference"] = validate_wikidata_alias(
                                    search_raw, entity_raw, requested_name, document["cid"])
                                document["alias_verified"] = True
                                document["limitations"].append(document["name_cross_reference"]["limitation"])
                    if not document["alias_verified"]:
                        document["limitations"].append("The inferred lookup alias was not confirmed against the original user name.")
        artifact = _artifact(store, run, call, "identity.json", "resolved_identity", _json_bytes(document))
        artifacts.append(artifact.id)
        if rule == GENERAL_IDENTITY_RULE and document["alias_verified"] is not True:
            failure = _failure(run, call, "original_name_evidence_unverified", artifacts, **source)
            failure["identity"] = document
            return _finish(store, run, call, failure)
        check = {"name": "supported_identity_graph", "status": "passed", "rule_version": rule,
                 "detail": ("One returned name candidate and internally consistent graph/formula/charge; ambiguity retained."
                            if requirement["general_identity"] else
                            "Name, complete graph, neutral state, one component and no isotope/stereo labels checked."),
                 "source": source}
        data = {"operation_status": "completed", "artifact_ids": artifacts,
                "qualified_outputs": {"resolved_identity": {"artifact_id": artifact.id,
                    "checks": [check], "source": {**source, "sha256": artifact.sha256}}},
                "identity": document, "source": _source(run, call, **source)}
        return _finish(store, run, call, data)
    except (ValueError, KeyError, TypeError, RuntimeError, httpx.HTTPError, OSError) as exc:
        from orca_agent.store import ControlChanged
        cancelled = isinstance(exc, ControlChanged)
        if cancelled:
            source.update(not_started=source["http_requests"] == 0)
        failure = _failure(run, call, type(exc).__name__, artifacts,
                           status="cancelled" if cancelled else "failed", **source)
        failure["diagnostics"][0]["detail"] = str(exc)[:1000]
        return _finish(store, run, call, failure)


def _validate_coordinate_stereochemistry(atoms, smiles):
    """Check specified tetrahedral/double-bond labels against emitted coordinates."""
    from rdkit import Chem

    molecule = Chem.AddHs(Chem.MolFromSmiles(smiles))
    Chem.AssignStereochemistry(molecule, cleanIt=True, force=True)
    expected_atoms = {atom.GetIdx(): atom.GetProp("_CIPCode") for atom in molecule.GetAtoms()
                      if atom.HasProp("_CIPCode")}
    expected_bonds = {bond.GetIdx(): bond.GetStereo() for bond in molecule.GetBonds()
                      if bond.GetStereo() in {Chem.BondStereo.STEREOE, Chem.BondStereo.STEREOZ}}
    if len(molecule.GetAtoms()) != len(atoms):
        raise ValueError("stereochemistry reference atom mapping differs")
    generated = Chem.Mol(molecule)
    Chem.RemoveStereochemistry(generated)
    conformer = Chem.Conformer(len(atoms))
    conformer.Set3D(True)
    for index, atom in enumerate(atoms):
        conformer.SetAtomPosition(index, atom[1:])
    generated.AddConformer(conformer)
    Chem.AssignStereochemistryFrom3D(generated)
    Chem.AssignStereochemistry(generated, cleanIt=True, force=True)
    if (any(not generated.GetAtomWithIdx(index).HasProp("_CIPCode")
            or generated.GetAtomWithIdx(index).GetProp("_CIPCode") != expected
            for index, expected in expected_atoms.items())
            or any(generated.GetBondWithIdx(index).GetStereo() != expected
                   for index, expected in expected_bonds.items())):
        raise ValueError("generated coordinates differ from the specified stereochemistry")
    # Exotic stereo types must not acquire a guarantee from the checks above.
    supported = {Chem.StereoType.Atom_Tetrahedral, Chem.StereoType.Bond_Double}
    if any(item.type not in supported for item in Chem.FindPotentialStereo(molecule)):
        raise ValueError("coordinate stereochemistry checker does not cover this RDKit stereo type")
    return {"tetrahedral_centers": len(expected_atoms), "double_bonds": len(expected_bonds)}


def validate_prepared_geometry(xyz, canonical, expected_elements, *, graph=None, smiles=None):
    from orca_agent.models import CalculationParameters
    from orca_agent.tools.registry import validate_geometry
    if len(xyz.encode("utf-8")) > MAX_GEOMETRY_BYTES:
        raise ValueError("prepared geometry exceeds size bound")
    if graph is not None:
        from rdkit import Chem
        lines = xyz.splitlines()
        # OPI's XYZ serializer adds a terminal blank line. Ignore only trailing
        # whitespace for validation; preserve the generated bytes and hash.
        while lines and not lines[-1].strip():
            lines.pop()
        if len(lines) != len(expected_elements) + 2 or lines[0].strip() != str(len(expected_elements)):
            raise ValueError("generated XYZ count or row shape differs")
        atoms = []
        for line in lines[2:]:
            fields = line.split()
            if len(fields) != 4:
                raise ValueError("generated XYZ atom needs three coordinates")
            atom = [fields[0], *map(float, fields[1:])]
            if any(not math.isfinite(value) for value in atom[1:]):
                raise ValueError("generated XYZ coordinates must be finite")
            atoms.append(atom)
        if [atom[0] for atom in atoms] != expected_elements:
            raise ValueError("generated atom order/composition differs from the confirmed explicit-H graph")
        table = Chem.GetPeriodicTable()
        for first, second in graph["bonds"]:
            radius = table.GetRcovalent(atoms[first][0]) + table.GetRcovalent(atoms[second][0])
            if not 0.45 * radius <= math.dist(atoms[first][1:], atoms[second][1:]) <= 1.8 * radius:
                raise ValueError("generated geometry has a graph bond outside bounded covalent distances")
        if any(math.dist(atom[1:], other[1:]) < 0.4 for index, atom in enumerate(atoms) for other in atoms[:index]):
            raise ValueError("generated geometry has an abnormal short distance")
        stereo = {}
        if smiles is not None and graph["stereochemistry"]:
            stereo = _validate_coordinate_stereochemistry(atoms, smiles)
        return {"elements": expected_elements, "atom_mapping": list(range(len(atoms))), "unit": "angstrom",
                **({"stereochemistry_checked": stereo, "isotopes": graph["isotopes"]} if smiles is not None else {}),
                "guarantee": "initial graph/atom mapping and finite geometry only; not optimized or ground-state certified"}
    atoms = validate_geometry(xyz, CalculationParameters())
    if [atom[0] for atom in atoms] != expected_elements:
        raise ValueError("generated atom order/composition differs from the confirmed explicit-H graph")
    profile = IDENTITY_PROFILE[canonical]
    heavy = next(atom for atom in atoms if atom[0] == profile["heavy"])
    hydrogens = [atom for atom in atoms if atom[0] == "H"]
    upper = 1.3 if canonical == "water" else 1.4
    if len(hydrogens) != profile["hydrogens"] or any(
            not 0.6 <= math.dist(heavy[1:], atom[1:]) <= upper for atom in hydrogens):
        raise ValueError("generated geometry does not preserve the requested connected molecule")
    if any(math.dist(atom[1:], other[1:]) < 0.5 for index, atom in enumerate(atoms) for other in atoms[:index]):
        raise ValueError("generated geometry has an abnormal short distance")
    return {"elements": expected_elements, "atom_mapping": list(range(len(atoms))), "unit": "angstrom"}


def _generate(store, run, call, document, params):
    """One bounded managed process; no model-provided code or executable path."""
    from orca_agent.backends.local import new_job_name, run_managed
    from orca_agent.condition_selection import general_admission_enabled
    directory = _call_directory(store, run, call)
    worker_input = directory / "worker-input.json"
    _publish(worker_input, _json_bytes({"smiles": document["smiles"], "canonical_name": document["canonical_name"],
                                      "charge": params.charge, "multiplicity": params.multiplicity,
                                      "identity_rule": document["schema"],
                                      "general_admission": general_admission_enabled(store, run)}))
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
    environment.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", MKL_NUM_THREADS="1")
    # Source execution and an installed wheel use the same module; the path is
    # generated from this package location, never a user/model search path.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    def started(handle):
        _publish(directory / "worker-handle.json", _json_bytes(handle))
        store.update_lease_handle(run.id, call.id, handle)
        store.check_execution_control(run)
    control = store.control_lock(run.id)
    held = False
    def release(point=None):
        nonlocal held
        if held and point in (None, "after_resumed"):
            control.release()
            held = False
    try:
        control.acquire()
        held = True
        from orca_agent.store import ControlChanged
        try:
            store.check_execution_control(run)
        except ControlChanged:
            outcome = {"state": "cancelled", "not_started": True,
                       "reason": "control_changed_before_preparation"}
            _publish(directory / "worker-outcome.json", _json_bytes(outcome))
            return outcome
        outcome = run_managed(Path(sys.executable).resolve(),
            ["-m", "orca_agent.tools.structure", "--worker", str(worker_input)], directory,
            cores=PREPARE_CORES, total_memory_mb=PREPARE_MEMORY_MB,
            timeout_s=min(PREPARE_TIMEOUT_SECONDS,
                          store.run_configuration(run).resources.script_timeout_seconds,
                          max(0.001, (run.deadline - utc_now()).total_seconds())),
            job_name=new_job_name(), cancel_requested=lambda: store.read_signal(run.id) == "cancel",
            environment=environment, on_started=started, fault=release)
    finally:
        release()
    _publish(directory / "worker-outcome.json", _json_bytes(outcome))
    return outcome


def prepare(store, run, call):
    from orca_agent.condition_selection import general_admission_enabled
    requirement = validate_call_inputs(store, run, call.parameters, call.tool, call.consumption)
    if recovered := _begin(store, run, call):
        return recovered
    params = PrepareParameters.model_validate(call.parameters)
    document, identity_artifact = _bound_identity(store, run, call.consumption, requirement["canonical_name"])
    source = {**requirement, "identity_artifact_id": identity_artifact.id,
              "identity_sha256": identity_artifact.sha256, "smiles": document["smiles"],
              "charge": params.charge, "multiplicity": params.multiplicity,
              "generator": "opi.Structure.from_smiles", "random_seed": None,
              "generation_reproducibility": "no seed exposed; reuse this immutable XYZ",
              "admission_profile": "managed-native-1" if general_admission_enabled(store, run) else "historical",
              "timeout_seconds": min(PREPARE_TIMEOUT_SECONDS,
                  store.run_configuration(run).resources.script_timeout_seconds)}
    artifacts = []
    generation_entered = False
    outcome = None
    try:
        source["versions"] = _versions()
        generation_entered = True
        outcome = _generate(store, run, call, document, params)
        source["execution"] = outcome
        if outcome.get("not_started"):
            source["not_started"] = True
        directory = _call_directory(store, run, call)
        for name in ("worker-outcome.json", "stdout.out", "stderr.txt"):
            path = directory / name
            if path.is_file() and path.stat().st_size <= MAX_GEOMETRY_BYTES:
                artifacts.append(store.import_artifact(path, "structure_execution", run_id=run.id,
                                                       source=_source(run, call)).id)
        geometry_path = directory / "prepared.xyz"
        artifact = None
        raw_geometry = None
        if geometry_path.is_file() and geometry_path.stat().st_size <= MAX_GEOMETRY_BYTES:
            raw_geometry = geometry_path.read_bytes()
            artifact = store.import_artifact(geometry_path, "prepared_geometry_candidate", run_id=run.id,
                                             source=_source(run, call),
                                             expected_sha256=hashlib.sha256(raw_geometry).hexdigest())
            artifacts.append(artifact.id)
        if outcome.get("state") != "completed":
            status = outcome.get("state") if outcome.get("state") in {"unknown", "timed_out", "cancelled"} else "failed"
            return _finish(store, run, call, _failure(run, call, "preparation_execution_incomplete", artifacts,
                                                     status=status, **source))
        if artifact is None:
            raise ValueError("prepared geometry missing or oversized")
        worker = _read_json(directory / "worker-result.json", MAX_GEOMETRY_BYTES)
        xyz = raw_geometry.decode("utf-8")
        if worker.get("sha256") != artifact.sha256:
            raise ValueError("worker geometry hash mismatch")
        if worker.get("versions") != source["versions"]:
            raise ValueError("worker dependency versions differ")
        geometry = validate_prepared_geometry(xyz, requirement["canonical_name"], document["elements"],
                                             graph=document.get("graph"),
                                             smiles=document["smiles"] if general_admission_enabled(store, run) else None)
        source.update(**geometry, sha256=artifact.sha256)
        rule = requirement["check_version"]
        check = {"name": "initial_geometry_identity", "status": "passed", "rule_version": rule,
                 "detail": "Finite angstrom coordinates preserve supported identity and explicit hydrogen atom mapping; not optimized.",
                 "source": source}
        data = {"operation_status": "completed", "artifact_ids": artifacts,
                "qualified_outputs": {"prepared_geometry": {"artifact_id": artifact.id, "unit": "angstrom",
                    "checks": [check], "source": source}}, "source": _source(run, call, **source)}
        return _finish(store, run, call, data)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, ImportError) as exc:
        status = "unknown" if generation_entered and outcome is None else "failed"
        if not generation_entered:
            source["not_started"] = True
        return _finish(store, run, call, _failure(run, call, type(exc).__name__, artifacts, status=status, **source))


def _worker(path):
    data = _read_json(Path(path), 4096)
    params = PrepareParameters(system_id="worker", charge=data["charge"], multiplicity=data["multiplicity"])
    if not data.get("general_admission") and (params.charge != 0 or params.multiplicity != 1):
        raise ValueError("worker requires neutral singlet")
    versions = _versions()
    graph = _general_graph(data["smiles"]) if data.get("identity_rule") in GENERAL_IDENTITY_RULES else None
    elements = graph["elements"] if graph else _validate_smiles(data["smiles"], data["canonical_name"])
    if graph:
        if graph["unspecified_stereochemistry"]:
            raise ValueError("initial geometry cannot silently choose unspecified stereochemistry")
        if data.get("general_admission"):
            _validate_preparation_state({"charge": graph["formal_charge"], "elements": elements}, params)
    from opi.input.structures import Structure
    structure = Structure.from_smiles(data["smiles"], charge=params.charge, multiplicity=params.multiplicity)
    xyz = structure.to_xyz_block()
    raw = xyz.encode("utf-8")
    if len(raw) > MAX_GEOMETRY_BYTES:
        raise ValueError("prepared geometry exceeds size bound")
    directory = Path(path).parent
    # Retain bounded raw generation evidence even when validation rejects it.
    # Only successful validation publishes the worker receipt used for input qualification.
    _publish(directory / "prepared.xyz", raw)
    validate_prepared_geometry(xyz, data["canonical_name"], elements, graph=graph,
                               smiles=data["smiles"] if data.get("general_admission") else None)
    _publish(directory / "worker-result.json", _json_bytes({"sha256": hashlib.sha256(raw).hexdigest(),
                                                           "versions": versions}))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Only the fixed managed structure worker entry is supported")
    _worker(sys.argv[2])
