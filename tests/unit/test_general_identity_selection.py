"""Synthetic intake/source receipts and RDKit graph checks; no HTTP or calculation."""

import copy
import hashlib
import json

import pytest
from test_semantic_control import candidate
from test_structure_tools import tool_call
from test_text_entry import defaults, text_environment

from orca_agent.condition_selection import (
    commit_selections,
    selectable_conditions,
    selected_request,
)
from orca_agent.model_usage import current_basis
from orca_agent.models import EvidenceRef, OutputBinding, Plan, Result, Step, ToolCall, fingerprint
from orca_agent.natural import initialize_text
from orca_agent.proposals import ProposalError
from orca_agent.semantic import commit_candidate
from orca_agent.store import ControlChanged, StoreError
from orca_agent.tools import structure


def intake(tmp_path, name="ethanol", *, qualification="", automatic=True):
    store, config = text_environment(tmp_path,
        defaults={"charge": 0, "multiplicity": 1, "electronic_state": "RKS", "environment": "gas_phase"},
        permission={"model_execution": True, "artifact_writes": True, "external_identity_queries": True,
                    "geometry_preparation": True,
                    "allowed_tools": ["structure.resolve", "structure.prepare", "knowledge.search"]})
    text = f"Optimize {name} and report polarizability{qualification}."
    if automatic:
        text += " Choose the method and basis."
    run = initialize_text(store, config, text)
    message = store.read_control(run.id)["messages"][0]
    parameters = candidate(store, run, kind="normalize", conditions=defaults(config),
        identities=[{"key": "target", "name": name, "text_basis": text, "message_id": message["id"]}],
        goals=[{"key": "property", "port": "polarizability", "quantity": "polarizability",
                "text_basis": text, "message_id": message["id"], "system_refs": ["target"]}],
        automatic_selection=({name: {"text_basis": "Choose the method and basis.", "message_id": message["id"]}
                              for name in ("method", "basis")} if automatic else {}))
    return store, run, parameters


def normalized(tmp_path, **kwargs):
    store, run, parameters = intake(tmp_path, **kwargs)
    run = commit_candidate(store, run, parameters, decision_id="normalize", basis=current_basis(store, run))
    return store, run


def source(store, run, *, kind="official_document", excerpt="PBE0 with def2-SVP provides a specified model chemistry."):
    call = ToolCall(tool="knowledge.search", request_version=run.request_version,
                    parameters={"document": "orca_dft", "query": "PBE0"})
    run.calls.append(call)
    store.save_run(run)
    digest = hashlib.sha256(b"synthetic official document fixture; not a real retrieval").hexdigest()
    result = Result(run_id=run.id, call_id=call.id, operation_status="completed",
        observations={"document_excerpt": {"status": "found", "document_sha256": digest,
            "excerpt": excerpt, "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(), "kind": kind,
            "document_id": "orca_dft", "url": "https://example.invalid/synthetic", "title": "Synthetic evidence"}})
    store.finish_call(run, call, result)
    return {"result_id": result.id, "document_sha256": digest, "quote": excerpt}


def selections(store, run, reference):
    return {"request_version": run.request_version, "choices": {
        name: {"value": value, "reason": "Finite illustrative selection backed by the fixture excerpt.",
               "sources": [reference]}
        for name, value in (("method", "PBE0"), ("basis", "def2-SVP"))}}


@pytest.mark.parametrize("name", ["ethanol", "acetone", "benzene", "cyclohexane", "N,N-dimethylformamide"])
def test_general_named_intake_preserves_original_goal_without_a_molecule_allowlist(tmp_path, name):
    store, run = normalized(tmp_path, name=name)
    request = store.load_request(run)
    assert request.systems[0].identity["requested_names"] == [name]
    assert request.systems[0].identity["contract"] == "general-identity-2"
    goal = request.goals[0]
    assert goal.original_text == request.original_text
    assert goal.quantity == "polarizability" and goal.verification == "reported_with_limitations"
    assert goal.minimum_check_version == "generic-observation-1" and not goal.unresolved
    assert request.charge == 0 and request.conditions_source["charge"] == "default"
    assert selectable_conditions(request) == ["method", "basis"]
    assert not run.calls and not run.attempts


def test_explicit_qualification_is_not_downgraded(tmp_path):
    store, run, parameters = intake(tmp_path, qualification="; qualified scientific evidence is required")
    parameters["goals"][0]["verification"] = "reported_with_limitations"
    with pytest.raises(StoreError, match="cannot be downgraded"):
        commit_candidate(store, run, parameters, decision_id="bad", basis=current_basis(store, run))
    parameters["goals"][0].pop("verification")
    run = commit_candidate(store, run, parameters, decision_id="good", basis=current_basis(store, run))
    goal = store.load_request(run).goals[0]
    assert goal.verification == "qualified" and goal.minimum_evidence


def test_selection_preserves_history_permission_goals_and_persists_sources(tmp_path):
    store, run = normalized(tmp_path)
    prior = store.load_request(run)
    raw = store.path(f"runs/{run.id}/decisions/normalize.json").read_bytes()
    reference = source(store, run)
    before = run.permission.model_dump(), run.budget.model_dump()
    run = commit_selections(store, run, selections(store, run, reference), decision_id="select",
                            basis=current_basis(store, run), related_results=[reference["result_id"]])
    request = store.load_request(run)
    assert request.version == prior.version + 1
    assert request.goals == prior.goals and request.messages == prior.messages
    assert (run.permission.model_dump(), run.budget.model_dump()) == before
    assert request.method == "PBE0" and request.basis == "def2-SVP"
    assert request.conditions_source["method"] == "auto_selected" and not selectable_conditions(request)
    assert request.condition_evidence["request.method"]["sources"][0]["result_fingerprint"] == fingerprint(
        store.load_result(run.id, reference["result_id"]))
    assert store.path(f"runs/{run.id}/decisions/normalize.json").read_bytes() == raw
    assert run.usage.plan_revisions == 0 and len(run.decisions) == 2 and not run.attempts


@pytest.mark.parametrize("mutation", ["hash", "quote", "result", "stale", "unauthorized", "goal", "permission", "pending", "prepared"])
def test_selection_rejects_unbound_evidence_or_authority_expansion(tmp_path, mutation):
    store, run = normalized(tmp_path)
    reference = source(store, run)
    parameters = selections(store, run, reference)
    prior = store.load_request(run)
    if mutation == "hash":
        parameters["choices"]["method"]["sources"][0]["document_sha256"] = "0" * 64
    elif mutation == "quote":
        parameters["choices"]["method"]["sources"][0]["quote"] = "A fabricated recommendation"
    elif mutation == "result":
        parameters["choices"]["method"]["sources"][0]["result_id"] = "result_missing"
    elif mutation == "stale":
        parameters["request_version"] -= 1
    elif mutation == "unauthorized":
        parameters["choices"]["environment"] = copy.deepcopy(parameters["choices"]["method"])
    elif mutation == "permission":
        run.permission.code_execution = True
    elif mutation == "pending":
        store.enqueue_message(run.id, "Wait; change the requested target.")
    elif mutation == "prepared":
        run.calls.append(ToolCall(tool="structure.resolve", request_version=run.request_version,
                                  parameters={"system_id": "system_target"}))
        store.save_run(run)
    with pytest.raises((StoreError, ControlChanged, ValueError)):
        if mutation == "goal":
            proposed = selected_request(store, run, prior, parameters)
            proposed.goals[0].verification = "qualified"
            store.commit_revision(run, None, request=proposed, decision_id="bad", basis=current_basis(store, run),
                selection_record={"schema": "condition-selection-1", "parameters": parameters})
        else:
            commit_selections(store, run, parameters, decision_id="bad", basis=current_basis(store, run))
    assert store.load_request(run) == prior


def test_project_guide_does_not_masquerade_as_method_documentation(tmp_path):
    store, run = normalized(tmp_path)
    reference = source(store, run, kind="project_guide")
    with pytest.raises(ProposalError) as rejected:
        commit_selections(store, run, selections(store, run, reference), decision_id="bad", basis=current_basis(store, run))
    assert all(item["reason"] == "official_documentation_required"
               for item in rejected.value.detail["corrections"])


def pubchem(smiles, formula, *, charge=0, stereo=0):
    return json.dumps({"PropertyTable": {"Properties": [{"CID": 123, "SMILES": smiles,
        "MolecularFormula": formula, "Charge": charge, "IsotopeAtomCount": 0,
        "AtomStereoCount": stereo, "BondStereoCount": 0, "CovalentUnitCount": 1}]}}).encode()


@pytest.mark.parametrize(("smiles", "formula", "count"), [("CCO", "C2H6O", 9), ("CC(=O)C", "C3H6O", 10),
                                                         ("c1ccccc1", "C6H6", 12)])
def test_general_graph_validation_is_not_ethanol_specific(smiles, formula, count):
    document = structure.validate_pubchem_response(pubchem(smiles, formula), "original name", general=True)
    assert document["schema"] == "structure-identity-2" and len(document["elements"]) == count
    assert document["graph"]["formal_charge"] == 0
    assert "multiplicity" not in document and "electronic_state" not in document


def test_unknown_stereochemistry_and_radicals_are_facts_not_spin_inference():
    stereo = structure.validate_pubchem_response(pubchem("CC(F)Cl", "C2H4ClF", stereo=1), "query", general=True)
    assert stereo["graph"]["unspecified_stereochemistry"]
    radical = structure.validate_pubchem_response(pubchem("[CH3]", "CH3"), "query", general=True)
    assert radical["graph"]["radical_electrons"] == 1 and "multiplicity" not in radical
    with pytest.raises(ValueError, match="formula"):
        structure.validate_pubchem_response(pubchem("CCO", "CH4"), "query", general=True)


def test_general_resolve_uses_frozen_rule_and_cannot_relabel_v1(tmp_path, monkeypatch):
    store, run = normalized(tmp_path, automatic=False)
    monkeypatch.setattr(structure, "_paced_query", lambda *args, **kwargs: (200, pubchem("CCO", "C2H6O"), True))
    call = tool_call(store, run, "structure.resolve", {"system_id": "system_target"})
    data = structure.resolve(store, run, call)
    output = data["qualified_outputs"]["resolved_identity"]
    assert output["checks"][0]["rule_version"] == "structure-identity-2"
    result = Result(run_id=run.id, call_id=call.id, step_id=call.step_id, operation_status="completed",
        artifact_ids=data["artifact_ids"], qualified_outputs=data["qualified_outputs"],
        checks={"resolved_identity": output["checks"]}, source={"tool": call.tool, **data["source"]})
    wrong = result.model_copy(deep=True)
    wrong.qualified_outputs["resolved_identity"].checks[0].rule_version = "structure-identity-1"
    wrong.checks["resolved_identity"][0].rule_version = "structure-identity-1"
    with pytest.raises(StoreError, match="declared successful checks"):
        store.save_result(wrong)
    store.finish_call(run, call, result)
    assert run.input_bindings["system_target"]["resolved_identity"]["rule_version"] == "structure-identity-2"


def test_general_prepare_dispatch_consumes_exact_v2_input_and_preserves_initial_geometry(tmp_path, monkeypatch):
    from orca_agent.input_bindings import prepared_geometry
    from orca_agent.tools.dispatch import execute_call

    store, run = normalized(tmp_path)
    reference = source(store, run)
    run = commit_selections(store, run, selections(store, run, reference), decision_id="select",
                            basis=current_basis(store, run))
    request = store.load_request(run)
    resolve = Step(id="resolve", logical_id="resolve", tool="structure.resolve", system_id="system_target",
                   parameters={"system_id": "system_target"})
    prepare = Step(id="prepare", logical_id="prepare", tool="structure.prepare", system_id="system_target",
        parameters={"system_id": "system_target"}, depends_on=["resolve"],
        inputs={"identity": EvidenceRef(producer_step_id="resolve", port="resolved_identity",
                                       rule_version="structure-identity-2")})
    plan = Plan(request_id=request.id, request_version=request.version, steps=[resolve, prepare],
                goal_map={request.goals[0].id: OutputBinding(port="polarizability", gap="Preparation precedes observations.")})
    run = store.commit_revision(run, plan, decision_id="plan", basis=current_basis(store, run))
    monkeypatch.setattr(structure, "_paced_query", lambda *args, **kwargs: (200, pubchem("CCO", "C2H6O"), True))
    identity = execute_call(store, run, "structure.resolve", {"system_id": "system_target"}, step=resolve)
    assert identity.operation_status == "completed"
    xyz = "9\nsynthetic initial geometry, not optimized\nC 0 0 0\nC 1.5 0 0\nO 2.9 0 0\nH -.5 .9 0\nH -.5 -.9 0\nH 0 0 1\nH 1.5 1 0\nH 1.5 -1 0\nH 3.3 .85 0\n"
    def generate(store, run, call, document, params):
        directory = store.path(f"runs/{run.id}/calls/{call.id}")
        (directory / "prepared.xyz").write_text(xyz, encoding="utf-8", newline="\n")
        (directory / "worker-result.json").write_text(json.dumps({
            "sha256": hashlib.sha256(xyz.encode()).hexdigest(), "versions": structure._versions()}))
        return {"state": "completed", "exit_code": 0}
    monkeypatch.setattr(structure, "_generate", generate)
    result = execute_call(store, run, "structure.prepare", {"system_id": "system_target", "charge": 0, "multiplicity": 1},
                          step=prepare, results={"resolve": identity})
    assert result.operation_status == "completed", result.diagnostics
    assert result.checks["prepared_geometry"][0].rule_version == "structure-prepare-2"
    artifact_id = prepared_geometry(store, run, request, "system_target")
    assert store.artifact_path(artifact_id).read_text(encoding="utf-8") == xyz
    assert store.load_request(run).systems[0].geometry_artifact_id is None
    assert not run.attempts and run.usage.orca_starts_actual == 0


def test_clipped_goal_quote_cannot_hide_required_scientific_qualification(tmp_path):
    store, run, parameters = intake(tmp_path, qualification="; qualified scientific evidence is required")
    parameters["goals"][0]["text_basis"] = "report polarizability"
    parameters["goals"][0]["verification"] = "reported_with_limitations"
    with pytest.raises(StoreError, match="cannot be downgraded"):
        commit_candidate(store, run, parameters, decision_id="bad", basis=current_basis(store, run))


def test_unrelated_generic_goal_does_not_remove_explicit_electronic_energy_obligation(tmp_path):
    from orca_agent.proposals import ProposalError
    store, run, parameters = intake(tmp_path, qualification=" and electronic energy")
    with pytest.raises(ProposalError, match="explicit electronic-energy request"):
        commit_candidate(store, run, parameters, decision_id="bad", basis=current_basis(store, run))


def wikidata(name="乙醇", cid="123"):
    search = {"search": [{"id": "Q123"}]}
    entities = {"entities": {"Q123": {"id": "Q123", "type": "item", "lastrevid": 17,
        "labels": {"zh": {"language": "zh", "value": name}}, "aliases": {},
        "claims": {"P662": [{"rank": "normal", "mainsnak": {"snaktype": "value", "property": "P662",
            "datavalue": {"type": "string", "value": cid}}}]}}}}
    return search, entities


@pytest.mark.parametrize("mutation", ["different_cid", "similar_label", "multiple_cids", "deprecated", "wrong_property",
                                       "extra_entity", "duplicate_entity", "oversized"])
def test_name_cross_reference_cannot_confirm_approximate_or_ambiguous_identity(mutation):
    search, entities = wikidata()
    entity = entities["entities"]["Q123"]
    statement = entity["claims"]["P662"][0]
    if mutation == "different_cid":
        statement["mainsnak"]["datavalue"]["value"] = "124"
    elif mutation == "similar_label":
        entity["labels"]["zh"]["value"] = "乙醇胺"
    elif mutation == "multiple_cids":
        other = copy.deepcopy(statement)
        other["mainsnak"]["datavalue"]["value"] = "124"
        entity["claims"]["P662"].append(other)
    elif mutation == "deprecated":
        statement["rank"] = "deprecated"
    elif mutation == "wrong_property":
        statement["mainsnak"]["property"] = "P235"
    elif mutation == "extra_entity":
        entities["entities"]["Q124"] = copy.deepcopy(entity)
    elif mutation == "duplicate_entity":
        search["search"].append({"id": "Q123"})
    search_raw = json.dumps(search).encode()
    if mutation == "oversized":
        search_raw = b"x" * (structure.MAX_SYNONYM_BYTES + 1)
    with pytest.raises(ValueError):
        structure.validate_wikidata_alias(search_raw, json.dumps(entities).encode(), "乙醇", 123)


@pytest.mark.parametrize("valid_mapping", [True, False])
def test_general_alias_uses_independent_named_entity_and_same_pubchem_cid(tmp_path, monkeypatch, valid_mapping):
    store, run, parameters = intake(tmp_path, name="乙醇", automatic=False)
    parameters["identities"][0]["query_name"] = "ethanol"
    run = commit_candidate(store, run, parameters, decision_id="normalize", basis=current_basis(store, run))
    search, entities = wikidata()
    if not valid_mapping:
        entities["entities"]["Q123"]["claims"]["P662"][0]["mainsnak"]["datavalue"]["value"] = "124"
    responses = [pubchem("CCO", "C2H6O"), json.dumps({"InformationList": {"Information": [
        {"CID": 123, "Synonym": ["ethanol"]}]}}).encode(), json.dumps(search).encode(), json.dumps(entities).encode()]
    urls = []
    def query(_, url, **kwargs):
        urls.append(url)
        assert kwargs["deadline"] > 0
        return 200, responses[len(urls) - 1], True
    monkeypatch.setattr(structure, "_paced_query", query)
    call = tool_call(store, run, "structure.resolve", {"system_id": "system_target"})
    result = structure.resolve(store, run, call)
    if not valid_mapping:
        assert result["operation_status"] == "failed" and not result["qualified_outputs"]
        assert "unambiguous matching PubChem cross-reference" in result["diagnostics"][0]["detail"]
        assert len(result["artifact_ids"]) == 4 and result["source"]["http_requests"] == 4
        for response, artifact_id in zip(responses, result["artifact_ids"], strict=True):
            assert store.artifact_path(artifact_id).read_bytes() == response
        return
    assert result["operation_status"] == "completed" and result["source"]["http_requests"] == 4
    assert len(result["artifact_ids"]) == 5
    assert result["identity"]["alias_verified"] is True
    mapping = result["identity"]["name_cross_reference"]
    assert mapping["original_name"] == "乙醇" and mapping["pubchem_cid"] == 123
    assert mapping["source_kind"] == "community_maintained_cross_reference"
    assert urls[2] == structure._wikidata_search_url("乙醇")
    assert urls[3] == structure._wikidata_entities_url(["Q123"])
    for response, artifact_id in zip(responses, result["artifact_ids"]):
        assert store.artifact_path(artifact_id).read_bytes() == response


@pytest.mark.parametrize("url", ["https://www.wikidata.org/w/api.php?action=edit", "https://www.wikidata.org/wiki/Q123",
    "https://evil.invalid/w/api.php?action=wbsearchentities", "https://www.wikidata.org/w/api.php?action=wbgetentities&ids=../../secret"])
def test_only_fixed_read_identity_endpoints_are_allowed(url):
    with pytest.raises(ValueError, match="endpoint"):
        structure._http_get(url)


@pytest.mark.parametrize("kind", ["legacy_property", "synonyms", "wikidata"])
def test_identity_http_limits_are_explicit_and_legacy_limit_is_unchanged(monkeypatch, kind):
    limit = structure.MAX_RESPONSE_BYTES if kind == "legacy_property" else structure.MAX_SYNONYM_BYTES
    class Response:
        status_code = 200
        headers = {}
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def iter_raw(self):
            yield b"x" * limit
            yield b"!"
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False and kwargs["trust_env"] is False
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
        def stream(self, method, url, **kwargs):
            assert "https://github.com/" in kwargs["headers"]["User-Agent"]
            return Response()
    monkeypatch.setattr(structure.httpx, "Client", Client)
    url = (structure.PUBCHEM_ROOT + "water/property/" + ",".join(structure.PROPERTIES) + "/JSON"
           if kind == "legacy_property" else "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/cid/123/synonyms/JSON"
           if kind == "synonyms" else structure._wikidata_search_url("乙醇"))
    status, raw, complete = structure._http_get(url)
    assert status == 200 and len(raw) == limit and not complete
    assert structure.MAX_RESPONSE_BYTES == 262144
