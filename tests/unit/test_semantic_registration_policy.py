"""Offline prompt/control regressions are not a new successful model evaluation."""

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest
from test_context import payload
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis, read_model_reply
from orca_agent.natural import initialize_bundle
from orca_agent.proposals import ProposalError
from orca_agent.semantic import action_parameters, commit_candidate
from orca_agent.store import Store, sha256_file
from tests.helpers.phase_b_model_cases import create_request
from tests.helpers.semantic_replay import current_candidate

PROJECT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("scope", ["本轮只登记需求，不执行。", "请执行计算。"])
@pytest.mark.parametrize("text,gap,question", [
    ("那个分子的单点电子能。", "ambiguous_system", "那个分子具体指哪个体系？"),
    ("水的单点电子能，电荷未知。", "field:charge", "水的总电荷是多少？"),
    ("水的某个性质。", "unknown:quantity", "需要水的哪个物理量？"),
])
def test_registration_notice_does_not_suppress_real_unknowns_or_grant_execution(tmp_path, scope, text, gap, question):
    store = store_at(tmp_path)
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text + scope,
        allowed_tools=[], scientific_execution=False))
    permission = run.permission.model_dump_json()
    parameters = candidate(store, run, kind="clarify", unresolved=[gap], questions=[question])
    updated = commit_candidate(store, run, parameters, decision_id="critical_unknown", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.original_text == text + scope
    assert gap in request.unresolved
    assert updated.decisions[-1]["semantics"]["questions"] == [question]
    assert updated.permission.model_dump_json() == permission
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert updated.usage.model_calls == updated.usage.orca_starts_actual == 0


def test_actual_n06_request_rebuilt_with_separate_scope_and_registration_without_rewriting_reply(tmp_path):
    root = PROJECT / "data/phase-b/reference"
    run_id = "run_a25aaf548dac432e943aca69fee212e9"
    directory = root / "runs" / run_id
    slot = (PROJECT / "data/phase-b/model-evaluations/development/repair-supplement-v1"
            / "N-06__raw-unsupported-system/1")
    if not (directory / "run.json").is_file() or not (slot / "review.json").is_file():
        pytest.skip("retained real N06 receipt absent; real-input replay unverified")
    protected = [path for path in directory.rglob("*") if path.is_file()]
    protected.extend(path for path in slot.glob("*.json") if path.is_file())
    before = {path: sha256_file(path) for path in protected}
    store = object.__new__(Store)
    store.root = root
    run = store.load_run(run_id)
    reply, receipt_hash = read_model_reply(store, run, run.model_records[0])
    assert receipt_hash == "417498d18619c052d3210ba3172209ecb490230a2fcc59c3a6cdd6ff5fce51ef"
    assert json.loads(reply.raw_content) == reply.proposal
    original_question = "乙醇尚未登记为 System，且没有几何结构；请确认是否登记乙醇（含几何来源），本轮不启动计算。"
    assert reply.proposal["parameters"]["questions"] == [original_question]
    assert json.loads((slot / "review.json").read_text(encoding="utf-8"))["semantic_review_passed"] is False

    request = store.load_request_revision(run, 1)
    projection = copy.deepcopy(run)
    projection.request_version = 1
    projection.processed_messages = []
    prepared = build_context(request, projection, relevant_tools=[],
        now=run.created_at + timedelta(seconds=1), user_messages=store.read_control(run.id)["messages"],
        action_parameters=action_parameters(request=request))
    data = payload(prepared)
    policy = action_parameters(request=request)["normalize_request"]["questions_policy"]
    assert prepared.body()["messages"][0]["content"].count(policy) == 1
    assert "questions_policy" not in data["ACTION_PARAMETERS"]["normalize_request"]
    assert "disclose scope and geometry limits via notices" in policy
    assert "without asking for resources/confirmation" in policy
    assert "No execution permission alone is not registration-only intent" in policy
    assert "critical gaps blocking the requested scope" in policy
    assert data["AUTHORITY"]["user_originals"][0]["text"] == request.original_text
    assert "本轮只登记需求，不启动计算" in request.original_text
    assert data["ACTION_PARAMETERS"]["normalize_request"]["science_scope"]["systems"] == ["H2O", "CH4"]
    assert not data["AUTHORITY"]["request"]["systems"]
    assert not data["AUTHORITY"]["permission"]["scientific_execution"]
    assert not data["TOOL_CATALOG"]
    assert prepared.input_token_bound <= 12000

    # Historical acceptance/review stays immutable. Current activation now
    # rejects a resource-confirmation question outside registration-only scope.
    isolated = store_at(tmp_path)
    fresh, _ = create_request(isolated, "N-06/raw-unsupported-system", 1,
        category="development", freeze_label="offline-registration-policy")
    parameters = copy.deepcopy(reply.proposal["parameters"])
    parameters["message_ids"] = [isolated.read_control(fresh.id)["messages"][0]["id"]]
    parameters = current_candidate(parameters)
    current_before = isolated.load_run(fresh.id).model_dump_json(), isolated.load_request(fresh).model_dump_json()
    with pytest.raises(ProposalError, match="current delivery scope"):
        commit_candidate(isolated, fresh, parameters, decision_id="unchanged_bad_notice",
                         basis=current_basis(isolated, fresh))
    assert (isolated.load_run(fresh.id).model_dump_json(), isolated.load_request(fresh).model_dump_json()) == current_before
    assert parameters["questions"] == [original_question]
    assert {path: sha256_file(path) for path in before} == before
