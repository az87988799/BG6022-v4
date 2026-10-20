"""Single-query name contracts; HTTP is mocked and no LLM or ORCA is called."""

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from orca_agent import semantic
from orca_agent.input_bindings import prepared_geometry, validate_input_result
from orca_agent.model_usage import current_basis
from orca_agent.models import EvidenceRef, OutputBinding, Plan, Step
from orca_agent.store import StoreError
from orca_agent.tools import structure
from orca_agent.tools.dispatch import execute_call
from tests.unit.test_general_identity_selection import intake, pubchem
from tests.unit.test_general_preparation_admission import execution

ROOT = Path(__file__).resolve().parents[2]
T2 = ROOT / "docs/acceptance/code-as-tools-step7-refreeze-1/evidence/T2/data"
T2_RAW = T2 / "artifacts/artifact_8366135c809c4cefbdb384be27ae7f74/files/pubchem-response.json"
T2_RUN = T2 / "runs/run_536ee867c7324e4693757b8e35290e4f"
T2_RESULT = T2_RUN / "results/result_fc49e56f59564ecf92ffbfb5e4a14377.json"
T2_REQUEST = T2_RUN / "decisions/model_c87f6457f7b045bba3301426462a0e56.json"
T2_DIGESTS = {
    T2_RAW: "b3cdd931d4ce768e2eb37c0f3047583a76405006e7feb34d683945db06422d82",
    T2_RESULT: "17a4403ccbd44121f0ee7234cbfcb3cadb31938b3f5f61af78d12a87dbcfc61e",
    T2_REQUEST: "d3db07fd31d061bb2199b7527fc9c119e721eff62bcf2075b0d0f19a7512b200",
}


def name_run(tmp_path, monkeypatch, original, query, *, contract="general-identity-3", ambiguities=()):
    store, run, candidate = intake(tmp_path, name=original, automatic=False, execution=execution())
    candidate["identities"][0].update(query_name=query, ambiguities=list(ambiguities))
    # Historical fixtures choose the old default only while creating their
    # Request. All resolution/consumption then runs with the current default.
    with monkeypatch.context() as historical:
        historical.setattr(semantic, "CURRENT_IDENTITY_CONTRACT", contract)
        run = semantic.commit_candidate(store, run, candidate, decision_id="normalize",
                                        basis=current_basis(store, run))
    request = store.load_request(run)
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="system_target",
                   parameters={"system_id": "system_target"})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="system_target",
        parameters={"system_id": "system_target", "charge": 0, "multiplicity": 1}, depends_on=[resolve.id],
        inputs={"identity": EvidenceRef(producer_step_id=resolve.id, port="resolved_identity",
            rule_version="structure-identity-3" if contract == "general-identity-3" else "structure-identity-2")})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[resolve, prepare],
        goal_map={goal.id: OutputBinding(port=goal.port, gap="Initial inputs precede scientific observations.")
                  for goal in request.goals})
    run = store.commit_revision(run, plan, decision_id="plan", basis=current_basis(store, run))
    return store, run, request, plan


def mock_http(monkeypatch, replies):
    """Keep the production endpoint, pacing, byte bounds and send-time guard."""
    original_client = httpx.Client
    calls = []

    def receive(request):
        calls.append(str(request.url))
        assert len(calls) <= len(replies), "unexpected extra identity HTTP request"
        status, raw = replies[len(calls) - 1]
        return httpx.Response(status, stream=httpx.ByteStream(raw))

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        return original_client(**kwargs, transport=httpx.MockTransport(receive))

    monkeypatch.setattr(structure.httpx, "Client", client)
    return calls


def resolve(store, run, plan):
    step = plan.steps[0]
    return execute_call(store, run, step.tool, step.parameters.model_dump(), step=step)


def test_real_t2_properties_need_one_http_and_reach_local_opi_preparation(tmp_path, monkeypatch):
    original = {path: path.read_bytes() for path in T2_DIGESTS}
    assert {path: hashlib.sha256(raw).hexdigest() for path, raw in original.items()} == T2_DIGESTS
    prior_result = json.loads(original[T2_RESULT])
    prior_request = json.loads(original[T2_REQUEST])["request"]
    old_identity = prior_request["systems"][0]["identity"]
    assert old_identity["contract"] == "general-identity-2"
    assert prior_result["operation_status"] == "failed" and not prior_result["qualified_outputs"]

    store, run, request, plan = name_run(tmp_path, monkeypatch,
        old_identity["requested_names"][0], old_identity["query_name"])
    calls = mock_http(monkeypatch, [(200, original[T2_RAW])])
    result = resolve(store, run, plan)
    assert result.operation_status == "completed", result.diagnostics
    assert len(calls) == result.source["http_requests"] == run.usage.identity_queries == 1
    assert "/name/n-hexane/property/" in calls[0]
    document = result.observations["input_preparation"]["identity"]
    assert (document["cid"], document["formula"], document["smiles"], document["charge"]) == (
        8058, "C6H14", "CCCCCC", 0)
    assert document["original_name"] == old_identity["requested_names"][0]
    assert document["query_name"] == old_identity["query_name"]
    assert document["query_name_source"] == "inferred_translation_or_alias"
    assert document["name_mapping_independently_verified"] is False
    assert "alias_verified" not in document and "name_cross_reference" not in document
    assert any("not independently matched" in limitation for limitation in document["limitations"])
    binding = validate_input_result(store, run, request, result, "resolved_identity")
    assert run.input_bindings["system_target"]["resolved_identity"] == binding
    assert binding["rule_version"] == document["schema"] == "structure-identity-3"
    assert store.artifact_path(result.source["raw_artifact_id"]).read_bytes() == original[T2_RAW]

    def local_worker(store, run, call, document, parameters):
        # Actual local OPI/RDKit embedding, with a simulated managed boundary.
        # This tests preparation/geometry checks, not process isolation.
        worker = store.path(f"runs/{run.id}/calls/{call.id}/worker-input.json")
        worker.write_text(json.dumps({"smiles": document["smiles"],
            "canonical_name": document["canonical_name"], "identity_rule": document["schema"],
            "charge": parameters.charge, "multiplicity": parameters.multiplicity,
            "general_admission": True}), encoding="utf-8")
        structure._worker(worker)
        return {"state": "completed", "exit_code": 0}

    monkeypatch.setattr(structure, "_generate", local_worker)
    step = plan.steps[1]
    geometry = execute_call(store, run, step.tool, step.parameters.model_dump(), step=step,
                            results={plan.steps[0].id: result})
    assert geometry.operation_status == "completed", geometry.diagnostics
    assert geometry.checks["prepared_geometry"][0].rule_version == "structure-prepare-3"
    artifact_id = prepared_geometry(store, run, request, "system_target")
    assert artifact_id == geometry.qualified_outputs["prepared_geometry"].artifact_id
    assert len(geometry.source["elements"]) == 20
    assert run.usage.structure_preparations == 1 and len(calls) == 1
    assert not run.attempts and run.usage.orca_starts_actual == run.usage.model_calls == 0
    assert store.load_request(run).systems[0].geometry_artifact_id is None
    assert {path: path.read_bytes() for path in T2_DIGESTS} == original


@pytest.mark.parametrize("original,query,smiles,formula", [
    ("甲醛CH2O", "formaldehyde", "C=O", "CH2O"),
    ("乙醇（C2H6O）", "ethanol", "CCO", "C2H6O"),
    ("丙酮 C3H6O", "acetone", "CC(=O)C", "C3H6O"),
])
def test_v3_formula_matches_do_not_require_synonyms_or_certify_translation(
    tmp_path, monkeypatch, original, query, smiles, formula,
):
    store, run, _, plan = name_run(tmp_path, monkeypatch, original, query)
    calls = mock_http(monkeypatch, [(200, pubchem(smiles, formula))])
    result = resolve(store, run, plan)
    assert result.operation_status == "completed", result.diagnostics
    document = result.observations["input_preparation"]["identity"]
    assert document["original_name"] == original
    assert document["name_evidence"]["formula_matches_graph"] is True
    assert document["name_mapping_independently_verified"] is False and "alias_verified" not in document
    assert len(calls) == 1 and len(result.artifact_ids) == 2


@pytest.mark.parametrize("case", ["http", "oversize", "missing", "multiple", "smiles", "formula", "charge", "explicit_formula"])
def test_v3_unavailable_or_conflicting_candidate_stays_failed(tmp_path, monkeypatch, case):
    name = "甲醛CH2O2" if case == "explicit_formula" else "甲醛"
    store, run, _, plan = name_run(tmp_path, monkeypatch, name, "formaldehyde")
    payload = json.loads(pubchem("C=O", "CH2O"))
    rows = payload["PropertyTable"]["Properties"]
    if case == "missing":
        rows.clear()
    elif case == "multiple":
        rows.append(dict(rows[0], CID=124))
    elif case in {"smiles", "formula", "charge"}:
        rows[0].update({"smiles": {"SMILES": "invalid!"},
                        "formula": {"MolecularFormula": "CH4"},
                        "charge": {"Charge": 1}}[case])
    raw = b"x" * (structure.MAX_RESPONSE_BYTES + 1) if case == "oversize" else json.dumps(payload).encode()
    calls = mock_http(monkeypatch, [(404 if case == "http" else 200, raw)])
    result = resolve(store, run, plan)
    assert result.operation_status == "failed" and not result.qualified_outputs
    assert not run.input_bindings.get("system_target")
    assert len(calls) == run.usage.identity_queries == 1
    assert not run.attempts and run.usage.orca_starts_actual == 0


def test_persisted_v2_still_requires_name_evidence_and_recovers_failure_unchanged(tmp_path, monkeypatch):
    store, run, request, plan = name_run(tmp_path, monkeypatch, "未知名称CH2O", "formaldehyde",
                                       contract="general-identity-2")
    assert semantic.CURRENT_IDENTITY_CONTRACT == "general-identity-3"
    replies = [(200, pubchem("C=O", "CH2O")),
        (200, b'{"InformationList":{"Information":[{"CID":123,"Synonym":["formaldehyde","CH2O"]}]}}'),
        (200, b'{"search":[]}')]
    calls = mock_http(monkeypatch, replies)
    result = resolve(store, run, plan)
    assert result.operation_status == "failed" and not result.qualified_outputs
    assert result.diagnostics[0]["reason"] == "original_name_evidence_unverified"
    assert len(calls) == 3 and not run.input_bindings.get("system_target")
    assert request.systems[0].identity["contract"] == "general-identity-2"
    raw = store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes()
    receipt = structure.resolve(store, run, run.calls[0])
    assert receipt["operation_status"] == "failed" and receipt["identity"]["alias_verified"] is False
    assert receipt["identity"]["schema"] == "structure-identity-2" and len(calls) == 3
    assert store.path(f"runs/{run.id}/results/{result.id}.json").read_bytes() == raw
    assert store.load_request(run) == request


@pytest.mark.parametrize("contract,wrong_rule", [
    ("general-identity-2", "structure-identity-3"), ("general-identity-3", "structure-identity-2"),
])
def test_identity_check_versions_cannot_cross_frozen_request_contracts(tmp_path, monkeypatch, contract, wrong_rule):
    store, run, request, plan = name_run(tmp_path, monkeypatch, "ethanol", "ethanol", contract=contract)
    mock_http(monkeypatch, [(200, pubchem("CCO", "C2H6O"))])
    result = resolve(store, run, plan)
    assert result.operation_status == "completed"
    altered = result.model_copy(deep=True)
    for check in altered.checks["resolved_identity"]:
        check.rule_version = wrong_rule
    for check in altered.qualified_outputs["resolved_identity"].checks:
        check.rule_version = wrong_rule
    with pytest.raises(StoreError, match="declared successful checks"):
        store.save_result(altered)
    with pytest.raises(ValueError, match="required_checks_missing"):
        validate_input_result(store, run, request, altered, "resolved_identity")
    assert store.load_result(run.id, result.id) == result
