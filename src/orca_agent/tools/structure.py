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

import httpx
from pydantic import Field

from orca_agent.models import ElectronicInteger, Identifier, Record, fingerprint, utc_now

IDENTITY_RULE = "structure-identity-1"
PREPARATION_RULE = "structure-prepare-1"
RECEIPT_SCHEMA = "structure-call-1"
OPI_VERSION = "2.0.0"
RDKIT_VERSION = "2025.9.6"
MAX_RESPONSE_BYTES = 256 * 1024
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
    # The currently enabled preparation capability supports neutral singlets
    # only. Preflight still requires these values to match confirmed Request
    # conditions; defaults never resolve an unknown user charge or spin.
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


def _requirement(store, run, system_id):
    request = store.load_request(run)
    system = next((item for item in request.systems if item.id == system_id), None)
    identity = getattr(system, "identity", {}) if system else {}
    names = identity.get("canonical_names", [])
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
        from orca_agent.applicability import effective_conditions
        if run.permission.max_cores < PREPARE_CORES or run.permission.max_memory_mb < PREPARE_MEMORY_MB:
            raise ValueError("preparation resources exceed the frozen permission snapshot")
        conditions = effective_conditions(request, system_id=system.id)
        if (params.charge != 0 or params.multiplicity != 1
                or conditions["conditions"].get("charge") != params.charge
                or conditions["conditions"].get("multiplicity") != params.multiplicity
                or any("charge" in reason or "multiplicity" in reason
                       for reason in conditions["reasons"])):
            raise ValueError("preparation requires confirmed neutral singlet conditions matching its parameters")
        _bound_identity(store, run, consumption or {}, canonical)
    return {"canonical_name": canonical, "system_id": system.id, "text_evidence": evidence}


def _bound_identity(store, run, consumption, canonical):
    binding = consumption.get("identity", {})
    if binding.get("port") != "resolved_identity" or binding.get("status") != "qualified":
        raise ValueError("preparation requires the qualified resolved_identity input")
    source_run, result_id = binding.get("run_id"), binding.get("result_id")
    if source_run != run.id and result_id not in run.permission.result_ids:
        raise ValueError("identity Result is outside frozen permissions")
    result = store.load_result(source_run, result_id)
    from orca_agent.input_bindings import validate_input_result
    validate_input_result(store, run, store.load_request(run), result, "resolved_identity")
    output = result.qualified_outputs.get("resolved_identity")
    if (result.operation_status != "completed" or output is None
            or output.checks != result.checks.get("resolved_identity")
            or any(check.status != "passed" or check.rule_version != IDENTITY_RULE for check in output.checks)
            or result.source.get("tool") != "structure.resolve"
            or binding.get("result_fingerprint") != fingerprint(result)):
        raise ValueError("identity Result does not carry the required immutable check guarantee")
    path = store.artifact_path(output.artifact_id)
    artifact = store.load_artifact(output.artifact_id)
    if binding.get("artifact_hashes", {}).get(artifact.id) != artifact.sha256:
        raise ValueError("identity input Artifact changed after reservation")
    document = _read_json(path, MAX_GEOMETRY_BYTES)
    if (document.get("schema") != IDENTITY_RULE or document.get("canonical_name") != canonical
            or document.get("charge") != 0):
        raise ValueError("identity snapshot conflicts with current requested identity")
    if document.get("elements") != _validate_smiles(document.get("smiles"), canonical):
        raise ValueError("identity atom mapping differs from its complete graph")
    return document, artifact


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


def validate_pubchem_response(raw, canonical):
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


def _http_get(url, *, send_guard=None):
    """One exact official endpoint, no redirects/proxies/retries or user payload."""
    if url not in {PUBCHEM_ROOT + name + "/property/" + ",".join(PROPERTIES) + "/JSON"
                   for name in IDENTITY_PROFILE}:
        raise ValueError("unregistered identity query endpoint")
    started = time.monotonic()
    data = bytearray()
    status = 0
    try:
        with httpx.Client(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False) as client:
            manager = client.stream("GET", url, headers={"Accept": "application/json", "Accept-Encoding": "identity"})
            # The control generation is held stable through request submission,
            # then released for messages/cancel while the bounded body arrives.
            with send_guard or nullcontext():
                response = manager.__enter__()
            try:
                status = response.status_code
                # Preserve actual response bytes, never an unbounded decompressor.
                for chunk in response.iter_raw():
                    available = MAX_RESPONSE_BYTES - len(data)
                    data.extend(chunk[:available])
                    if len(chunk) > available or time.monotonic() - started > 20:
                        return status, bytes(data), False
                return status, bytes(data), response.headers.get("content-encoding", "identity") == "identity"
            finally:
                manager.__exit__(*sys.exc_info())
    except httpx.HTTPError:
        return status, bytes(data), False


@contextmanager
def _send_guard(store, run):
    with store.control_lock(run.id):
        store.check_execution_control(run)
        yield


def _paced_query(store, url, *, run=None):
    from orca_agent.store import atomic_write, controlled_path
    lock = store._lock(controlled_path(store.environment_root, "identity-query.lock"))
    with lock.acquire(timeout=30):
        pace = controlled_path(store.environment_root, "identity-query-time.json")
        previous = _read_json(pace).get("started_at", 0) if pace.exists() else 0
        delay = previous + 1 - time.time()
        if delay > 2:
            raise ValueError("identity query clock moved backwards; quota cannot be assumed free")
        if delay > 0:
            time.sleep(delay)
        atomic_write(pace, _json_bytes({"started_at": time.time()}))
        return _http_get(url, send_guard=_send_guard(store, run)) if run is not None else _http_get(url)


def _failure(run, call, reason, artifacts=(), *, status="failed", **facts):
    return {"operation_status": status, "artifact_ids": list(artifacts), "qualified_outputs": {},
            "diagnostics": [{"category": "structure_input_unavailable", "reason": reason}],
            "source": _source(run, call, **facts)}


def resolve(store, run, call):
    requirement = validate_call_inputs(store, run, call.parameters, call.tool, call.consumption)
    if recovered := _begin(store, run, call):
        return recovered
    canonical = requirement["canonical_name"]
    url = PUBCHEM_ROOT + canonical + "/property/" + ",".join(PROPERTIES) + "/JSON"
    artifacts = []
    source = {**requirement, "url": url, "requested_at": utc_now().isoformat(),
              "normalization_rule": IDENTITY_RULE, "http_requests": 1}
    try:
        status, raw, complete = _paced_query(store, url, run=run)
        raw_artifact = _artifact(store, run, call, "pubchem-response.json", "identity_raw_response", raw)
        artifacts.append(raw_artifact.id)
        source.update(http_status=status, response_complete=complete, raw_sha256=raw_artifact.sha256,
                      raw_artifact_id=raw_artifact.id, fetched_at=utc_now().isoformat() if status else None)
        if status != 200 or not complete:
            return _finish(store, run, call, _failure(run, call, "pubchem_http_or_response_bound", artifacts, **source))
        document = {**validate_pubchem_response(raw, canonical), "query": source}
        artifact = _artifact(store, run, call, "identity.json", "resolved_identity", _json_bytes(document))
        artifacts.append(artifact.id)
        check = {"name": "supported_identity_graph", "status": "passed", "rule_version": IDENTITY_RULE,
                 "detail": "Name, complete graph, neutral state, one component and no isotope/stereo labels checked.",
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
            source.update(not_started=True, http_requests=0)
        return _finish(store, run, call, _failure(run, call, type(exc).__name__, artifacts,
                                                 status="cancelled" if cancelled else "failed", **source))


def validate_prepared_geometry(xyz, canonical, expected_elements):
    from orca_agent.models import CalculationParameters
    from orca_agent.tools.registry import validate_geometry
    if len(xyz.encode("utf-8")) > MAX_GEOMETRY_BYTES:
        raise ValueError("prepared geometry exceeds size bound")
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
    directory = _call_directory(store, run, call)
    worker_input = directory / "worker-input.json"
    _publish(worker_input, _json_bytes({"smiles": document["smiles"], "canonical_name": document["canonical_name"],
                                      "charge": params.charge, "multiplicity": params.multiplicity}))
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
            cores=PREPARE_CORES, total_memory_mb=PREPARE_MEMORY_MB, timeout_s=PREPARE_TIMEOUT_SECONDS,
            job_name=new_job_name(), cancel_requested=lambda: store.read_signal(run.id) == "cancel",
            environment=environment, on_started=started, fault=release)
    finally:
        release()
    _publish(directory / "worker-outcome.json", _json_bytes(outcome))
    return outcome


def prepare(store, run, call):
    requirement = validate_call_inputs(store, run, call.parameters, call.tool, call.consumption)
    if recovered := _begin(store, run, call):
        return recovered
    params = PrepareParameters.model_validate(call.parameters)
    document, identity_artifact = _bound_identity(store, run, call.consumption, requirement["canonical_name"])
    source = {**requirement, "identity_artifact_id": identity_artifact.id,
              "identity_sha256": identity_artifact.sha256, "smiles": document["smiles"],
              "charge": params.charge, "multiplicity": params.multiplicity,
              "generator": "opi.Structure.from_smiles", "random_seed": None,
              "generation_reproducibility": "no seed exposed; reuse this immutable XYZ", "timeout_seconds": 30}
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
        geometry = validate_prepared_geometry(xyz, requirement["canonical_name"], document["elements"])
        source.update(**geometry, sha256=artifact.sha256)
        check = {"name": "initial_geometry_identity", "status": "passed", "rule_version": PREPARATION_RULE,
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
    params = PrepareParameters(system_id=data["canonical_name"], charge=data["charge"], multiplicity=data["multiplicity"])
    if params.charge != 0 or params.multiplicity != 1:
        raise ValueError("worker requires neutral singlet")
    versions = _versions()
    elements = _validate_smiles(data["smiles"], data["canonical_name"])
    from opi.input.structures import Structure
    structure = Structure.from_smiles(data["smiles"], charge=params.charge, multiplicity=params.multiplicity)
    xyz = structure.to_xyz_block()
    validate_prepared_geometry(xyz, data["canonical_name"], elements)
    directory = Path(path).parent
    _publish(directory / "prepared.xyz", xyz.encode("utf-8"))
    _publish(directory / "worker-result.json", _json_bytes({"sha256": hashlib.sha256(xyz.encode("utf-8")).hexdigest(),
                                                           "versions": versions}))


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit("Only the fixed managed structure worker entry is supported")
    _worker(sys.argv[2])
