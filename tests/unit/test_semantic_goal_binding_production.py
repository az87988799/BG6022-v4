"""Synthetic proposals exercise actual intake, atomic revisions and the Agent loop."""

import pytest
from test_agent import ScriptedTransport, initial_proposal
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent import agent
from orca_agent.config import Config
from orca_agent.model_usage import current_basis
from orca_agent.natural import initialize_bundle
from orca_agent.report import build_report, render_report
from orca_agent.semantic import LEGACY_VERSION, SemanticCandidate, commit_candidate


def molecular_run(tmp_path, text, *, geometries=True):
    store = store_at(tmp_path)
    items = []
    if geometries:
        for name, xyz in {
            "water": "3\nwater\nO 0 0 0\nH 0 .8 .6\nH 0 -.8 .6\n",
            "methane": "5\nmethane\nC 0 0 0\nH 1 1 1\nH -1 -1 1\nH -1 1 -1\nH 1 -1 -1\n",
        }.items():
            (tmp_path / f"{name}.xyz").write_text(xyz)
            items.append({"id": name, "file": f"{name}.xyz"})
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        geometries=items, scientific_execution=True, allowed_tools=["orca.sp"],
        conditions={"method": "HF", "basis": "STO-3G", "charge": 0, "multiplicity": 1}))
    return store, run


def goal(text, refs):
    return {"key": "energy", "port": "energy", "text_basis": text,
            "geometry_relation": "fixed_initial", "system_refs": refs}


def commit(store, run, **values):
    return commit_candidate(store, run, candidate(store, run, **{"kind": "normalize", **values}),
                            decision_id=f"semantic_{run.request_version}", basis=current_basis(store, run))


@pytest.mark.parametrize("text,refs", [
    ("计算水的单点电子能", ["methane"]),
    ("Calculate methane energy in water solvent", ["water"]),
    ("Calculate methane energy in water", ["water"]),
    ("以水为溶剂计算甲烷电子能", ["water"]),
    ("Calculate ethanol energy", ["water"]),
])
def test_wrong_initial_target_is_rejected_without_any_activation(tmp_path, text, refs):
    store, run = molecular_run(tmp_path, text)
    before = run.model_dump_json(), store.load_request(run).model_dump_json()
    with pytest.raises(ValueError, match="binding contradicts"):
        commit(store, run, goals=[goal(text, refs)])
    after = store.load_run(run.id)
    assert (after.model_dump_json(), store.load_request(after).model_dump_json()) == before
    assert not after.attempts and after.usage.orca_starts_actual == 0


def test_two_goal_quotes_in_one_sentence_keep_their_own_targets(tmp_path):
    text = "Calculate water energy and methane energy"
    store, run = molecular_run(tmp_path, text)
    other = {**goal("methane energy", ["methane"]), "key": "methane"}
    updated = commit(store, run, goals=[goal("water energy", ["water"]), other])
    goals = store.load_request(updated).goals
    assert [item.system_ids for item in goals] == [["water"], ["methane"]]
    assert [item.identity["canonical_names"] for item in goals] == [["water"], ["methane"]]
    assert all(item.text_evidence["message_id"] == updated.processed_messages[0] for item in goals)


def test_solvent_role_is_not_the_goal_target(tmp_path):
    text = "以水为溶剂计算甲烷电子能"
    store, run = molecular_run(tmp_path, text)
    updated = commit(store, run, goals=[goal(text, ["methane"])])
    assert store.load_request(updated).goals[0].identity["canonical_names"] == ["methane"]


@pytest.mark.parametrize("operation", ["replace_goals", "amend"])
def test_replacement_and_later_binding_share_original_identity_guard(tmp_path, operation):
    store, run = molecular_run(tmp_path, "计算水的单点电子能")
    run = commit(store, run, goals=[goal("水的单点电子能", [])], notices=["水目标尚未绑定注册体系。"])
    text = "将目标替换为水的单点电子能" if operation == "replace_goals" else "目标绑定甲烷"
    store.enqueue_message(run.id, text)
    values = ({"kind": operation, "replaces": ["goal_energy"], "goals": [goal(text, ["methane"])]}
              if operation == "replace_goals" else {"kind": operation,
                  "goal_bindings": {"goal_energy": ["methane"]}})
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ValueError, match="identity|binding contradicts"):
        commit(store, run, **values)
    assert store.load_run(run.id).model_dump_json() == before


def test_legacy_candidate_parses_read_only_but_cannot_activate(tmp_path):
    store, run = molecular_run(tmp_path, "计算水的单点电子能")
    parameters = candidate(store, run, kind="normalize", goals=[goal("水的单点电子能", ["water"])],
                           schema_version=LEGACY_VERSION)
    assert SemanticCandidate.model_validate(parameters).schema_version == LEGACY_VERSION
    with pytest.raises(ValueError, match="read-only"):
        commit_candidate(store, run, parameters, decision_id="legacy", basis=current_basis(store, run))
    assert store.load_run(run.id) == run


def test_registration_notice_completes_without_science_and_resume_stays_bounded(tmp_path):
    text = "登记乙醇的单点电子能；本轮只登记需求，不执行。"
    store, run = molecular_run(tmp_path, text, geometries=False)
    parameters = candidate(store, run, kind="normalize", goals=[goal("乙醇的单点电子能", [])],
        unresolved=["unsupported_system:ethanol"], notices=["已登记乙醇需求；当前不支持乙醇，几何尚未提供。"])
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport(
        {"action": "normalize_request", "parameters": parameters}))
    assert stopped.state == "paused", stopped.diagnostics
    assert not stopped.attempts and stopped.usage.orca_starts_actual == 0
    report = build_report(store, stopped)
    assert report["communication"]["registration_complete"]
    assert not report["communication"]["awaiting_reply"] and not report["user_goal_complete"]
    assert "乙醇" in render_report(report)
    assert store.load_request(stopped).goals[0].identity["support_status"] == "unsupported"
    resumed = agent.execute(store, Config(), run.id, resume=True, transport=ScriptedTransport())
    assert resumed.usage == stopped.usage and resumed.state == "paused"


@pytest.mark.parametrize("text", ["使用 RHF，但不执行", "Use RHF but do not execute", "看看结果，不要运行"])
def test_no_execution_scope_overrides_existing_science_permission(tmp_path, text):
    store, run = molecular_run(tmp_path, "计算水的单点电子能")
    run = commit(store, run, goals=[goal("水的单点电子能", ["water"])])
    store.enqueue_message(run.id, text)
    fields = {"method": {"source": "explicit", "value": "HF", "text_basis": "RHF"}} if "RHF" in text else {}
    parameters = candidate(store, run, kind="amend", conditions=fields)
    stopped = agent.execute(store, Config(), run.id, transport=ScriptedTransport(
        {"action": "normalize_request", "parameters": parameters}))
    assert stopped.state == "paused", stopped.diagnostics
    assert stopped.permission.scientific_execution and not stopped.attempts
    assert stopped.usage.orca_starts_actual == 0
    store.enqueue_message(run.id, "使用 RHF")
    next_parameters = candidate(store, stopped, kind="amend", conditions={
        "method": {"source": "explicit", "value": "HF", "text_basis": "RHF"}})
    again = agent.execute(store, Config(), run.id, transport=ScriptedTransport(
        {"action": "normalize_request", "parameters": next_parameters}))
    assert again.state == "paused" and again.usage.orca_starts_actual == 0


def test_read_only_instruction_still_allows_registered_evidence_query(tmp_path):
    store = store_at(tmp_path)
    raw = tmp_path / "raw.json"
    raw.write_text('{"a": 3}')
    artifact = store.import_artifact(raw, "synthetic_test_evidence")
    text = "Read field a from the existing file; do not run calculations."
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text, artifact_ids=[artifact.id]))
    parameters = candidate(store, run, kind="normalize", goals=[{
        "key": "read", "port": "value_observation", "text_basis": "Read field a from the existing file",
        "query": {"artifact_id": artifact.id, "path": [{"kind": "key", "key": "a"}]}}])
    completed = agent.execute(store, Config(), run.id, transport=ScriptedTransport(
        {"action": "normalize_request", "parameters": parameters}, initial_proposal))
    assert completed.state == "completed", completed.diagnostics
    assert completed.usage.evidence_reads == 1 and completed.usage.orca_starts_actual == 0
