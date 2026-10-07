"""Known names, registered resources and capability are separate prompt facts.

Synthetic candidates verify control boundaries, not new live model understanding.
Retained live receipts are read only and never rewritten as successful evidence.
"""

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_context import payload
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent.applicability import PROFILE
from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis, read_model_reply
from orca_agent.models import Goal, Request, SystemInput
from orca_agent.natural import initialize_bundle
from orca_agent.semantic import (
    SYSTEM_ALIASES,
    _grounded_systems,
    action_parameters,
    commit_candidate,
)
from orca_agent.store import Store, StoreError
from orca_agent.tools.registry import SCIENCE_COMPOSITIONS


@pytest.mark.parametrize("system_id", ["water", "methane", "h2o", "ch4"])
def test_readable_scope_and_registered_binding_share_existing_names(system_id):
    request = Request(original_text="registered molecule", goals=[
        Goal(id="energy", port="energy", minimum_check_version="orca-hf-2")], systems=[
        SystemInput(id=system_id, geometry_artifact_id="registered_geometry")])
    for name in (system_id, *SYSTEM_ALIASES[system_id]):
        assert _grounded_systems(request, [{"text": f"优化 {name}。"}]) == {system_id}
    assert _grounded_systems(request, [{"text": "优化 ammonia。"}]) == set()
    assert _grounded_systems(request, [{"text": "waterford methane_extra h2o_extra ch4_extra"}]) == set()
    scope = action_parameters(request=request)["normalize_request"]["science_scope"]
    assert scope["systems"] == list(SCIENCE_COMPOSITIONS) == ["H2O", "CH4"]
    assert scope["conditions"] == PROFILE
    assert scope["names"] == {formula: list(SYSTEM_ALIASES[formula.casefold()])
                              for formula in SCIENCE_COMPOSITIONS}
    scope["names"]["H2O"].append("ammonia")
    assert "ammonia" not in SYSTEM_ALIASES["h2o"]
    assert _grounded_systems(request, [{"text": "ammonia"}]) == set()


def unregistered_run(tmp_path, target):
    store = store_at(tmp_path)
    text = f"登记{target}的单点电子能需求，气相 RHF/STO-3G，中性单重态。没有几何；本轮仅登记，不计算。"
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        allowed_tools=[], scientific_execution=False))
    return store, run


def named_parameters(store, run, target, notice):
    fields = {"method": ("HF", "RHF"), "basis": ("STO-3G", "STO-3G"),
              "charge": (0, "中性"), "multiplicity": (1, "单重态"),
              "electronic_state": ("RHF", "RHF"), "environment": ("gas_phase", "气相")}
    return candidate(store, run, kind="normalize", conditions={name: {
        "value": value, "source": "explicit", "text_basis": quote}
        for name, (value, quote) in fields.items()}, goals=[{
            "key": "energy", "port": "energy", "text_basis": f"{target}的单点电子能",
            "geometry_relation": "fixed_initial"}],
        unresolved=["missing:geometry"], questions=[notice])


@pytest.mark.parametrize("target", ["水", "methane", "氨", "carbon dioxide"])
def test_named_targets_without_geometry_keep_identity_and_registration_notice(tmp_path, target):
    store, run = unregistered_run(tmp_path, target)
    request = store.load_request(run)
    before = request.model_dump_json(), run.model_dump_json()
    contract = action_parameters(request=request)
    prepared = build_context(request, run, relevant_tools=[], action_parameters=contract,
        user_messages=store.read_control(run.id)["messages"])
    visible = payload(prepared)["ACTION_PARAMETERS"]["normalize_request"]
    assert "Named identity != registered System/geometry" in visible["questions_policy"]
    assert "no geometry != unknown identity" in visible["questions_policy"]
    assert "named out-of-scope targets/missing geometry" in visible["questions_policy"]
    assert "do not request resources or reconfirm/change explicit choices" in visible["questions_policy"]
    assert (request.model_dump_json(), run.model_dump_json()) == before
    assert prepared.input_token_bound <= run.budget.input_tokens == 12000
    assert not payload(prepared)["AUTHORITY"]["request"]["systems"]
    assert not payload(prepared)["TOOL_CATALOG"]

    known_names = {name for aliases in visible["science_scope"]["names"].values() for name in aliases}
    outside_scope = target not in known_names
    scope_notice = "该已命名体系超出当前H2O/CH4支持范围；" if outside_scope else ""
    notice = f"已登记{target}需求；{scope_notice}几何未登记，本轮不执行。"
    parameters = named_parameters(store, run, target, notice)
    if outside_scope:
        parameters["unresolved"].append(f"unsupported_scope:{target}")
    updated = commit_candidate(store, run, parameters, decision_id="named_notice", basis=current_basis(store, run))
    normalized = store.load_request(updated)
    assert normalized.original_text == request.original_text
    assert normalized.goals[0].original_text == f"{target}的单点电子能"
    assert not normalized.systems and normalized.geometry_artifact_id is None
    assert "missing:geometry" in normalized.goals[0].unresolved
    assert updated.decisions[-1]["semantics"]["questions"] == [notice]
    assert not updated.permission.scientific_execution and not updated.permission.allowed_tools
    assert not updated.calls and not updated.attempts and not updated.model_records


def test_readable_names_do_not_authorize_an_unregistered_binding(tmp_path):
    store, run = unregistered_run(tmp_path, "水")
    parameters = named_parameters(store, run, "水", "仅登记水需求，几何缺失。")
    parameters["goals"][0]["system_refs"] = ["water"]
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(StoreError):
        commit_candidate(store, run, parameters, decision_id="invented_binding", basis=current_basis(store, run))
    assert store.load_run(run.id).model_dump_json() == before


def test_ambiguous_identity_still_requires_a_real_question(tmp_path):
    store, run = unregistered_run(tmp_path, "那个分子")
    parameters = candidate(store, run, kind="clarify", unresolved=["ambiguous_system"],
                           questions=["那个分子具体指哪个体系？"])
    updated = commit_candidate(store, run, parameters, decision_id="unknown_identity", basis=current_basis(store, run))
    assert "ambiguous_system" in store.load_request(updated).unresolved
    assert updated.decisions[-1]["semantics"]["questions"] == parameters["questions"]
    assert not updated.calls and not updated.attempts


def test_retained_v12_failure_stays_exact_while_new_context_explains_named_identity():
    root = Path(__file__).resolve().parents[2] / "data/phase-b/reference"
    run_id = "run_746719fe131148d9a24493283418f5ee"
    directory = root / "runs" / run_id
    if not (directory / "run.json").is_file():
        pytest.skip("retained development receipt unavailable; no live evidence fabricated")
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    before = {path: digest(path) for path in directory.rglob("*") if path.is_file()}
    store = object.__new__(Store)
    store.root = root
    run = store.load_run(run_id)
    reply, receipt_hash = read_model_reply(store, run, run.model_records[0])
    assert receipt_hash == "dd8235c45d46535ae389e9f244f3eab362e3dfa102bcf392ad0bc6afd58a92fb"
    assert reply.proposal["parameters"]["questions"] == [
        "乙醇的初始几何结构（原子坐标或几何文件）尚未登记，请提供以完成登记。"]
    assert "missing:system_identity:ethanol" in reply.proposal["parameters"]["unresolved"]
    assert json.loads(reply.raw_content) == reply.proposal
    request = store.load_request_revision(run, 1)
    projection = copy.deepcopy(run)
    projection.request_version = 1
    projection.processed_messages = []
    prepared = build_context(request, projection, relevant_tools=[],
        now=run.created_at + timedelta(seconds=1), user_messages=store.read_control(run.id)["messages"],
        action_parameters=action_parameters(request=request))
    visible = payload(prepared)["ACTION_PARAMETERS"]["normalize_request"]
    assert visible["science_scope"]["names"] == {"H2O": ["水", "water"], "CH4": ["甲烷", "methane"]}
    assert "no geometry != unknown identity" in visible["questions_policy"]
    assert prepared.input_token_bound <= 12000
    assert {path: digest(path) for path in before} == before
