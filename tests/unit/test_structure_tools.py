"""Synthetic PubChem/geometry evidence; no molecular HTTP or OPI embedding runs.

The JSON fixtures imitate the documented property shape and are not retained
PubChem responses. Fixed XYZ strings test checks only, never production fallback.
"""

import copy
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from orca_agent.models import (
    BudgetLimits,
    Goal,
    PermissionSnapshot,
    Request,
    Result,
    Step,
    SystemInput,
    ToolCall,
    fingerprint,
)
from orca_agent.store import Store
from orca_agent.tools import structure

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/structure"
XYZ = {
    "water": "3\nsynthetic not generated\nO 0 0 0\nH 0 .8 .6\nH 0 -.8 .6\n",
    "methane": "5\nsynthetic not generated\nC 0 0 0\nH .63 .63 .63\nH -.63 -.63 .63\nH -.63 .63 -.63\nH .63 -.63 -.63\n",
}


def response(name="water"):
    return (FIXTURES / f"pubchem-{name}.synthetic.json").read_bytes()


def environment(tmp_path, name="water"):
    store = Store(tmp_path / "data", environment_root=tmp_path / "environment")
    quote = f"Optimize {name} and report its energy."
    proof = {"message_id": "message_name", "text_basis": quote}
    identity = {"canonical_names": [name], "requested_names": [name], "text_evidence": proof}
    request = Request(original_text=quote, messages=[{"id": "message_name", "text": quote}],
        systems=[SystemInput(id=name, identity=identity, geometry_source="prepare")], goals=[
            Goal(id="energy", port="energy", system_ids=[name], minimum_check_version="orca-hf-2",
                 identity=identity, text_evidence=proof, conditions={"geometry_relation": "optimized"})])
    permission = PermissionSnapshot(allowed_tools=["structure.resolve", "structure.prepare"],
        artifact_writes=True, external_identity_queries=True, geometry_preparation=True)
    run = store.create_run(request, None, permission, BudgetLimits(identity_queries=2, structure_preparations=2))
    return store, run


def tool_call(store, run, tool, parameters, consumption=None):
    from orca_agent.input_bindings import input_purpose
    from orca_agent.tools.registry import validate_parameters
    step = Step(id="resolve" if tool == "structure.resolve" else "prepare", tool=tool,
                logical_id="structure", system_id=parameters["system_id"],
                parameters=validate_parameters(tool, parameters))
    call = ToolCall(tool=tool, step_id=step.id, request_version=run.request_version, frozen_step=step,
                    parameters=parameters, consumption={"_input_purpose": input_purpose(
                        store.load_request(run), parameters["system_id"]), **(consumption or {})})
    run.calls.append(call)
    store.save_run(run)
    return call


def resolve_identity(store, run, monkeypatch, name="water"):
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (200, response(name), True))
    call = tool_call(store, run, "structure.resolve", {"system_id": name})
    data = structure.resolve(store, run, call)
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id,
        operation_status=data["operation_status"], qualified_outputs=data["qualified_outputs"],
        checks={"resolved_identity": data["qualified_outputs"]["resolved_identity"]["checks"]},
        artifact_ids=data["artifact_ids"], source={"tool": call.tool, **data["source"]})
    store.finish_call(run, call, result)
    binding = {"run_id": run.id, "result_id": result.id, "port": "resolved_identity", "status": "qualified",
        "result_fingerprint": fingerprint(result), "artifact_hashes": {
            artifact_id: store.load_artifact(artifact_id).sha256 for artifact_id in result.artifact_ids}}
    return result, {"identity": binding}


def mock_generator(monkeypatch, name="water", *, state="completed", xyz=None):
    calls = []
    def generate(store, run, call, document, parameters):
        calls.append((document, parameters))
        directory = store.path(f"runs/{run.id}/calls/{call.id}")
        outcome = {"state": state, "exit_code": 0 if state == "completed" else None}
        (directory / "worker-outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
        if state == "completed":
            content = (XYZ[name] if xyz is None else xyz).encode("utf-8")
            (directory / "prepared.xyz").write_bytes(content)
            (directory / "worker-result.json").write_text(json.dumps({
                "sha256": hashlib.sha256(content).hexdigest(), "versions": structure._versions()}), encoding="utf-8")
        return outcome
    monkeypatch.setattr(structure, "_generate", generate)
    return calls


@pytest.mark.parametrize("name", ["water", "methane"])
def test_supported_full_graph_and_isomeric_metadata(name):
    data = structure.validate_pubchem_response(response(name), name)
    assert data["canonical_name"] == name and data["charge"] == 0
    assert data["elements"] == (["O", "H", "H"] if name == "water" else ["C", "H", "H", "H", "H"])


@pytest.mark.parametrize("mutation", [
    {"SMILES": "OO"}, {"SMILES": "[O]"}, {"SMILES": "[OH-]"}, {"SMILES": "[18OH2]"},
    {"SMILES": "O.O"}, {"SMILES": "C"}, {"MolecularFormula": "CH4"},
    {"Charge": True}, {"Charge": -1}, {"IsotopeAtomCount": 1}, {"AtomStereoCount": 1},
    {"BondStereoCount": 1}, {"CovalentUnitCount": 2}, {"CID": "962"}, {"SMILES": "[H][H].[O]"},
])
def test_formula_or_cid_alone_does_not_certify_identity(mutation):
    data = json.loads(response())
    data["PropertyTable"]["Properties"][0].update(mutation)
    with pytest.raises(ValueError):
        structure.validate_pubchem_response(json.dumps(data).encode(), "water")


@pytest.mark.parametrize("raw", [b"broken", b"{}", b'{"PropertyTable":{"Properties":[]}}',
    b'{"PropertyTable":{"Properties":[{},{}]}}', b"x" * (structure.MAX_RESPONSE_BYTES + 1),
    b'{"PropertyTable":[]}', b'{"PropertyTable":{"Properties":[3]}}', b'[]'],
    ids=["broken", "empty", "no_candidates", "multiple", "oversized", "invalid_table", "invalid_row", "invalid_root"])
def test_damaged_missing_or_multiple_candidates_rejected(raw):
    with pytest.raises((ValueError, KeyError)):
        structure.validate_pubchem_response(raw, "water")


def test_resolve_archives_exact_response_url_time_hash_and_receipt(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    queried = []
    def query(_, url, **kwargs):
        queried.append(url)
        return 200, response(), True
    monkeypatch.setattr(structure, "_paced_query", query)
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    data = structure.resolve(store, run, call)
    assert data["operation_status"] == "completed" and len(queried) == 1
    assert queried[0].startswith(structure.PUBCHEM_ROOT + "water/property/")
    assert "Optimize" not in queried[0] and run.id not in queried[0]
    raw_id = data["source"]["raw_artifact_id"]
    assert store.artifact_path(raw_id).read_bytes() == response()
    assert store.load_artifact(raw_id).sha256 == hashlib.sha256(response()).hexdigest()
    assert data["source"]["fetched_at"] and data["source"]["text_evidence"]["message_id"] == "message_name"
    assert structure.recover_receipt(store, run, call) == data
    assert structure.resolve(store, run, call) == data and len(queried) == 1


@pytest.mark.parametrize("status,raw,complete", [(503, b"capacity", True), (200, b"{", False),
    (200, b'{"PropertyTable":{"Properties":[{},{}]}}', True), (200, b"not json", True)])
def test_failed_resolve_retains_raw_bytes_and_never_falls_back(tmp_path, monkeypatch, status, raw, complete):
    store, run = environment(tmp_path)
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (status, raw, complete))
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    data = structure.resolve(store, run, call)
    assert data["operation_status"] == "failed" and not data["qualified_outputs"]
    assert data["diagnostics"] and len(data["artifact_ids"]) == 1
    assert store.artifact_path(data["artifact_ids"][0]).read_bytes() == raw
    assert structure.recover_receipt(store, run, call) == data


@pytest.mark.parametrize("effect", ["external_identity_queries", "artifact_writes"])
def test_unallowed_query_effect_rejected_before_intent_or_http(tmp_path, monkeypatch, effect):
    store, run = environment(tmp_path)
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: pytest.fail("unexpected HTTP"))
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    setattr(run.permission, effect, False)
    with pytest.raises(ValueError, match="authoriz|allow"):
        structure.resolve(store, run, call)
    assert not store.path(f"runs/{run.id}/calls/{call.id}/intent.json").exists()


def test_unsupported_or_ungrounded_identity_does_not_use_registered_label(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    request = store.load_request(run)
    request.systems[0].identity["canonical_names"] = ["ethanol"]
    monkeypatch.setattr(store, "load_request", lambda _: request)
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: pytest.fail("unexpected HTTP"))
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    with pytest.raises(ValueError, match="frozen supported identity"):
        structure.resolve(store, run, call)
    request.systems[0].identity["canonical_names"] = ["water"]
    request.systems[0].identity["text_evidence"]["message_id"] = "forged"
    with pytest.raises(ValueError, match="authenticated"):
        structure.resolve(store, run, call)


def test_intent_without_complete_receipt_never_requeries(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    count = []
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (count.append(1) or 200, response(), True))
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    def crash(*_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(structure, "_finish", crash)
    with pytest.raises(KeyboardInterrupt):
        structure.resolve(store, run, call)
    assert structure.recover_receipt(store, run, call) is None
    for _ in range(2):
        with pytest.raises(ValueError, match="unknown"):
            structure.resolve(store, run, call)
    assert len(count) == 1


def test_modified_artifact_or_receipt_cannot_recover(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    monkeypatch.setattr(structure, "_paced_query", lambda *_, **__: (200, response(), True))
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    data = structure.resolve(store, run, call)
    store.artifact_path(data["artifact_ids"][0]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash changed"):
        structure.recover_receipt(store, run, call)


@pytest.mark.parametrize("name", ["water", "methane"])
def test_prepare_uses_one_frozen_identity_and_reuses_exact_receipt(tmp_path, monkeypatch, name):
    store, run = environment(tmp_path, name)
    _, consumption = resolve_identity(store, run, monkeypatch, name)
    generated = mock_generator(monkeypatch, name)
    call = tool_call(store, run, "structure.prepare", {"system_id": name, "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    assert data["operation_status"] == "completed", data
    assert list(data["qualified_outputs"]) == ["prepared_geometry"]
    output = data["qualified_outputs"]["prepared_geometry"]
    assert store.artifact_path(output["artifact_id"]).read_text() == XYZ[name]
    assert output["source"]["unit"] == "angstrom" and output["source"]["random_seed"] is None
    assert structure.prepare(store, run, call) == data and len(generated) == 1
    assert structure.recover_receipt(store, run, call) == data
    assert not run.attempts and run.usage.orca_starts_actual == 0


@pytest.mark.parametrize("state", ["failed", "timed_out", "unknown", "cancelled"])
def test_prepare_retains_execution_failure_without_geometry(tmp_path, monkeypatch, state):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    generated = mock_generator(monkeypatch, state=state)
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    assert data["operation_status"] == state and not data["qualified_outputs"]
    assert data["artifact_ids"] and data["diagnostics"]
    assert structure.prepare(store, run, call) == data and len(generated) == 1


@pytest.mark.parametrize("change", [{"charge": 1}, {"multiplicity": 3}, {"charge": True}, {"multiplicity": "1"}])
def test_prepare_cannot_override_identity_charge_or_electronic_state(tmp_path, monkeypatch, change):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    monkeypatch.setattr(structure, "_generate", lambda *_, **__: pytest.fail("unexpected generation"))
    with pytest.raises(ValueError):
        call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1, **change}, consumption)
        structure.prepare(store, run, call)


def test_prepare_rejects_wrong_input_identity_and_tampered_binding(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    altered = copy.deepcopy(consumption)
    altered["identity"]["result_fingerprint"] = "0" * 64
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, altered)
    with pytest.raises(ValueError, match="immutable check"):
        structure.prepare(store, run, call)


@pytest.mark.parametrize("xyz", [
    "3\nsynthetic\nO 0 0 0\nH 0 0 0\nH 0 0 1\n",
    "3\nsynthetic\nO 0 0 0\nH 0 0 10\nH 0 0 -10\n",
    "3\nsynthetic\nO nan 0 0\nH 0 0 1\nH 0 0 -1\n",
    XYZ["methane"],
])
def test_bad_generated_geometry_is_withheld(tmp_path, monkeypatch, xyz):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    mock_generator(monkeypatch, xyz=xyz)
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    assert data["operation_status"] == "failed" and not data["qualified_outputs"]


@pytest.mark.parametrize("stage,expected", [("versions", "failed"), ("generation", "unknown")])
def test_exception_before_start_or_without_outcome_has_distinct_recovery_state(tmp_path, monkeypatch, stage, expected):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, consumption)
    def fail(*_):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(structure, "_versions" if stage == "versions" else "_generate", fail)
    data = structure.prepare(store, run, call)
    assert data["operation_status"] == expected
    assert data["source"].get("not_started") is (True if stage == "versions" else None)
    assert structure.recover_receipt(store, run, call) == data
    assert structure.prepare(store, run, call) == data


def test_known_completed_outcome_with_invalid_geometry_is_failed_not_unknown(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    mock_generator(monkeypatch, xyz="not xyz")
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, consumption)
    data = structure.prepare(store, run, call)
    assert data["operation_status"] == "failed"
    assert data["source"]["execution"]["state"] == "completed"
    assert "not_started" not in data["source"]


def test_fixed_worker_invokes_only_opi_with_explicit_electronic_state(tmp_path, monkeypatch):
    from opi.input.structures import Structure
    calls = []
    class Generated:
        def to_xyz_block(self):
            return XYZ["water"]
    def from_smiles(smiles, *, charge, multiplicity):
        calls.append((smiles, charge, multiplicity))
        return Generated()
    monkeypatch.setattr(Structure, "from_smiles", from_smiles)
    path = tmp_path / "worker-input.json"
    path.write_text(json.dumps({"smiles": "O", "canonical_name": "water", "charge": 0, "multiplicity": 1}))
    structure._worker(path)
    assert calls == [("O", 0, 1)]
    assert (tmp_path / "prepared.xyz").read_text() == XYZ["water"]
    receipt = json.loads((tmp_path / "worker-result.json").read_text())
    assert receipt["sha256"] == hashlib.sha256(XYZ["water"].encode()).hexdigest()


def test_managed_generation_records_handle_and_checks_control_before_resume(tmp_path, monkeypatch):
    from orca_agent.backends import local
    store, run = environment(tmp_path)
    events = []
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1})
    directory = store.path(f"runs/{run.id}/calls/{call.id}")
    monkeypatch.setattr(store, "update_lease_handle", lambda *args: events.append(("lease", args)))
    monkeypatch.setattr(store, "check_execution_control", lambda *args: events.append(("control", args)))
    def managed(executable, args, workdir, **kwargs):
        assert args[:3] == ["-m", "orca_agent.tools.structure", "--worker"]
        assert workdir == directory and kwargs["cores"] == 1 and kwargs["total_memory_mb"] == 1024
        assert kwargs["timeout_s"] == 30
        assert "DEEPSEEK_API_KEY" not in kwargs["environment"]
        kwargs["on_started"]({"pid": 123, "atomic_job_assignment": True})
        assert (directory / "worker-handle.json").exists()
        return {"state": "completed"}
    monkeypatch.setattr(local, "run_managed", managed)
    structure._generate(store, run, call, {"smiles": "O", "canonical_name": "water"},
                         structure.PrepareParameters.model_validate(call.parameters))
    assert [name for name, _ in events] == ["control", "lease", "control"]


@pytest.mark.parametrize("mode", ["complete", "redirect", "oversized", "partial_timeout", "compressed"])
def test_http_boundary_has_one_fixed_request_and_preserves_bounded_raw_bytes(monkeypatch, mode):
    observed = []
    class Response:
        status_code = 302 if mode == "redirect" else 200
        headers = {"content-encoding": "gzip"} if mode == "compressed" else {}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def iter_raw(self):
            yield b"head"
            if mode == "partial_timeout":
                raise httpx.ReadTimeout("synthetic")
            yield b"x" * structure.MAX_RESPONSE_BYTES if mode == "oversized" else b"tail"
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def stream(self, method, url, **kwargs):
            observed.append((method, url, kwargs))
            return Response()
    monkeypatch.setattr(structure.httpx, "Client", Client)
    url = structure.PUBCHEM_ROOT + "water/property/" + ",".join(structure.PROPERTIES) + "/JSON"
    status, raw, complete = structure._http_get(url)
    assert len(observed) == 1 and observed[0][0] == "GET"
    assert observed[0][2]["headers"]["Accept-Encoding"] == "identity"
    assert raw.startswith(b"head") and len(raw) <= structure.MAX_RESPONSE_BYTES
    assert complete == (mode in {"complete", "redirect"})
    assert status == (302 if mode == "redirect" else 200)
    with pytest.raises(ValueError, match="endpoint"):
        structure._http_get("https://example.com/" + url)
    assert len(observed) == 1


def test_environment_rate_limit_is_shared_across_store_roots(tmp_path, monkeypatch):
    store, _ = environment(tmp_path)
    other = Store(tmp_path / "other", environment_root=store.environment_root)
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(structure.time, "time", lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    monkeypatch.setattr(structure.time, "sleep", sleep)
    monkeypatch.setattr(structure, "_http_get", lambda _: (200, response(), True))
    structure._paced_query(store, "controlled-test-url")
    structure._paced_query(other, "controlled-test-url")
    assert sleeps == [1.0]


def test_prepare_interruption_without_receipt_does_not_regenerate(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    _, consumption = resolve_identity(store, run, monkeypatch)
    generated = mock_generator(monkeypatch)
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1}, consumption)
    def crash(*_):
        raise KeyboardInterrupt()
    monkeypatch.setattr(structure, "_finish", crash)
    with pytest.raises(KeyboardInterrupt):
        structure.prepare(store, run, call)
    assert structure.recover_receipt(store, run, call) is None
    for _ in range(2):
        with pytest.raises(ValueError, match="unknown"):
            structure.prepare(store, run, call)
    assert len(generated) == 1


def test_http_submission_guard_releases_before_reading_body(monkeypatch):
    events = []
    @contextmanager
    def guard():
        events.append("locked")
        yield
        events.append("released")
    class Response:
        status_code = 200
        headers = {}
        def __enter__(self):
            events.append("sent")
            return self
        def __exit__(self, *_):
            pass
        def iter_raw(self):
            events.append("body")
            yield response()
    class Client:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def stream(self, *_, **__):
            return Response()
    monkeypatch.setattr(structure.httpx, "Client", Client)
    url = structure.PUBCHEM_ROOT + "water/property/" + ",".join(structure.PROPERTIES) + "/JSON"
    assert structure._http_get(url, send_guard=guard())[2]
    assert events == ["locked", "sent", "released", "body"]


def test_user_message_before_http_prevents_network_submission(tmp_path, monkeypatch):
    store, run = environment(tmp_path)
    call = tool_call(store, run, "structure.resolve", {"system_id": "water"})
    store.enqueue_message(run.id, "Pause and clarify the identity first")
    class Client:
        def __init__(self, **_):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        @contextmanager
        def stream(self, *_, **__):
            pytest.fail("unexpected request submission")
            yield
    monkeypatch.setattr(structure.httpx, "Client", Client)
    data = structure.resolve(store, run, call)
    assert data["operation_status"] == "cancelled"
    assert data["source"]["not_started"] and data["source"]["http_requests"] == 0


def test_user_message_before_managed_worker_proves_no_start(tmp_path, monkeypatch):
    from orca_agent.backends import local
    store, run = environment(tmp_path)
    call = tool_call(store, run, "structure.prepare", {"system_id": "water", "charge": 0, "multiplicity": 1})
    store.enqueue_message(run.id, "Do not generate geometry")
    monkeypatch.setattr(local, "run_managed", lambda *_, **__: pytest.fail("unexpected managed generation"))
    outcome = structure._generate(store, run, call, {"smiles": "O", "canonical_name": "water"},
                                   structure.PrepareParameters.model_validate(call.parameters))
    assert outcome["state"] == "cancelled" and outcome["not_started"]
