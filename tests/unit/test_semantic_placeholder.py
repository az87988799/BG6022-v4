"""Raw placeholders are replaced; real user uncertainty remains unresolved."""

import copy
import json
from pathlib import Path

import pytest
from test_context import payload
from test_natural import bundle_at, store_at
from test_semantic_control import candidate

from orca_agent.config import Config
from orca_agent.context import build_context
from orca_agent.model_usage import current_basis
from orca_agent.natural import initialize_bundle
from orca_agent.proposals import ProposalError
from orca_agent.semantic import RULES, action_parameters, commit_candidate
from orca_agent.store import Store, StoreError, sha256_file
from tests.helpers.phase_b_model_cases import create_request
from tests.helpers.semantic_replay import current_candidate

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/phase_b/semantic-proposal-v3.json"
V3 = json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_placeholder_resolution_rejection_explains_automatic_normalization_without_mutation(tmp_path):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-marker")
    proposed = copy.deepcopy(V3["proposal"]["parameters"])
    proposed["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
    proposed = current_candidate(proposed)
    proposed["goals"] = proposed["goals"][:1]
    proposed["resolves"] = ["missing:goal_definition"]
    before = store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()
    with pytest.raises(ProposalError, match="automatically") as caught:
        commit_candidate(store, run, proposed, decision_id="bad_marker", basis=current_basis(store, run))
    assert caught.value.detail["path"] == ["parameters", "resolves"]
    assert "Omit that marker" in caught.value.detail["requirement"]
    assert (store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()) == before
    proposed["resolves"] = []
    updated = commit_candidate(store, run, proposed, decision_id="normalize_marker", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert [goal.port for goal in request.goals] == ["energy"]
    assert not request.unresolved and not request.goals[0].unresolved
    assert not updated.calls and not updated.attempts and not updated.model_records


def test_actual_v3_placeholder_shape_still_rejects_and_only_test_authored_correction_normalizes(tmp_path):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-v3-replay")
    proposed = copy.deepcopy(V3["proposal"]["parameters"])
    messages = store.read_control(run.id)["messages"]
    proposed["message_ids"] = [messages[0]["id"]]
    proposed = current_candidate(proposed)
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError, match="visible question") as caught:
        commit_candidate(store, run, proposed, decision_id="offline_v3_placeholder", basis=current_basis(store, run))
    assert caught.value.detail["new_unresolved"] == ["unsupported_quantity:unresolved"]
    assert store.load_run(run.id).model_dump_json() == before
    prepared = build_context(store.load_request(run), run, relevant_tools=[], user_messages=messages,
        action_parameters=action_parameters(),
        feedback={"validation_error": {"category": "ProposalError", "requirement": caught.value.detail}})
    data = payload(prepared)
    contract = data["ACTION_PARAMETERS"]["normalize_request"]
    assert prepared.input_token_bound <= 12000
    assert "PLAN_RULES" not in data and "PLAN_REFERENCES" not in data
    assert "normalize defines goals and retires raw_request/missing:goal_definition" in contract[
        "instruction"]
    assert "Registration/no-execution is not a Goal" in contract["instruction"]
    assert "Preserve unknown/unsupported requirements" in contract["instruction"]
    assert "preserve goals.unresolved" not in contract["instruction"]
    assert "normalize defines raw_request" not in contract["instruction"]
    assert "Ports:minimum_evidence_rules.port_rules" in contract["instruction"]
    assert set(contract["minimum_evidence_rules"]["port_rules"]) == set(RULES) - {"unresolved"}
    # Removing the unjustified extra Goal is a developer-authored correction;
    # production must neither silently remove it nor count this as a live pass.
    proposed["goals"] = proposed["goals"][:1]
    updated = commit_candidate(store, run, proposed, decision_id="offline_v3_corrected", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "normalized" and not request.unresolved
    assert [goal.port for goal in request.goals] == ["energy"]
    assert request.goals[0].conditions["geometry_relation"] == "fixed_initial"
    assert not request.goals[0].unresolved and request.goals[0].minimum_evidence == ["converged_scf@1"]
    assert not updated.attempts and not updated.calls and not updated.model_records
    assert updated.usage.orca_starts_actual == updated.usage.model_calls == 0


@pytest.mark.parametrize("uncertainty", ["missing_condition", "unsupported_quantity", "unsupported_minimum"])
def test_actual_user_gaps_remain_goals_and_clarification_after_placeholder_replacement(tmp_path, uncertainty):
    store = store_at(tmp_path)
    (tmp_path / "water.xyz").write_text("3\nsynthetic geometry\nO 0 0 0\nH 0 0.8 0.6\nH 0 -0.8 0.6\n")
    text = "登记水的单点电子能。采用气相 RHF/STO-3G，中性单重态。本轮只登记，不启动计算。"
    if uncertainty == "missing_condition":
        text = text.replace("中性单重态", "单重态，电荷待确认")
    elif uncertainty == "unsupported_quantity":
        text += "还需要水的偶极矩。"
    else:
        text += "还要求实验独立复测。"
    run = initialize_bundle(store, Config(), bundle_at(tmp_path, goals=None, text=text,
        geometries=[{"id": "water", "file": "water.xyz"}], allowed_tools=[]))
    fields = {"method": ("HF", "RHF"), "basis": ("STO-3G", "STO-3G"),
              "charge": (0, "中性"), "multiplicity": (1, "单重态"),
              "environment": ("gas", "气相"), "electronic_state": ("RHF", "RHF")}
    conditions = {name: {"value": value, "source": "explicit", "text_basis": quote}
                  for name, (value, quote) in fields.items()}
    goals = [{"key": "energy", "port": "energy", "text_basis": "水的单点电子能", "system_refs": ["water"],
              "geometry_relation": "fixed_initial"}]
    if uncertainty == "missing_condition":
        conditions["charge"] = {"source": "unknown", "value": None}
        expected_gap = "missing:charge:water"
    elif uncertainty == "unsupported_quantity":
        goals.append({"key": "dipole", "port": "dipole_moment", "text_basis": "水的偶极矩", "system_refs": ["water"]})
        expected_gap = "unsupported_quantity:dipole_moment"
    else:
        goals[0]["minimum_evidence"] = ["实验独立复测"]
        expected_gap = "unsupported_minimum_evidence:实验独立复测"
    proposed = candidate(store, run, kind="normalize", goals=goals, conditions=conditions,
        unresolved=["missing:goal_definition"],
        questions=["请确认待定电荷或保留未支持的用户要求。"])
    if uncertainty != "missing_condition":
        proposed["questions"] = []
        proposed["notices"] = ["已登记未支持的用户要求，科学目标仍未满足。"]
    updated = commit_candidate(store, run, proposed, decision_id="offline_actual_gap", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert request.normalization_status == "clarification"
    assert "missing:goal_definition" not in request.unresolved
    assert expected_gap in {gap for goal in request.goals for gap in goal.unresolved}
    assert all(goal.id != "raw_request" and goal.id != "goal_raw_request" for goal in request.goals)
    if uncertainty == "missing_condition":
        assert request.charge is None and request.conditions_source["charge"] == "unknown"
    elif uncertainty == "unsupported_quantity":
        assert request.goals[1].port == "unresolved" and request.goals[1].required
        assert request.goals[1].original_text == "水的偶极矩"
        assert "missing:goal_definition" in request.goals[1].unresolved
    else:
        assert request.goals[0].minimum_evidence == ["实验独立复测"]
    assert not updated.attempts and not updated.calls and not updated.model_records


def _water_candidate(tmp_path):
    store = store_at(tmp_path)
    run, _ = create_request(store, "N-01/raw-water-sp", 1, category="development", freeze_label="offline-sentinel")
    proposed = copy.deepcopy(V3["proposal"]["parameters"])
    proposed["message_ids"] = [store.read_control(run.id)["messages"][0]["id"]]
    proposed = current_candidate(proposed)
    proposed["goals"] = proposed["goals"][:1]
    return store, run, proposed


@pytest.mark.parametrize("location", ["request", "goal", "both"])
def test_successful_normalization_retires_only_exact_sentinel_and_preserves_candidate(tmp_path, location):
    store, run, proposed = _water_candidate(tmp_path)
    original = store.load_request(run).model_dump_json()
    marker = "missing:goal_definition"
    if location in {"request", "both"}:
        proposed["unresolved"] = [marker, "missing:goal_definition:user_qualification"]
        proposed["notices"] = ["保留用户补充限定的未决项。"]
    if location in {"goal", "both"}:
        proposed["goals"][0]["unresolved"] = [marker, "unsupported:user_requirement"]
        proposed["notices"] = ["保留未支持的用户要求。"]
    before = copy.deepcopy(proposed)
    updated = commit_candidate(store, run, proposed, decision_id="retire_sentinel", basis=current_basis(store, run))
    request = store.load_request(updated)
    assert marker not in request.unresolved and marker not in request.goals[0].unresolved
    if location in {"request", "both"}:
        assert request.unresolved == ["missing:goal_definition:user_qualification"]
    if location in {"goal", "both"}:
        assert "unsupported:user_requirement" in request.goals[0].unresolved
    assert proposed == before
    audited = updated.decisions[-1]["semantics"]["candidate"]
    assert audited["unresolved"] == proposed.get("unresolved", [])
    assert audited["goals"][0]["unresolved"] == proposed["goals"][0].get("unresolved", [])
    assert store.load_request_revision(updated, 1).model_dump_json() == original
    assert updated.usage == run.usage and updated.permission == run.permission


@pytest.mark.parametrize("extra_gap", [None, "field:temperature_K"])
@pytest.mark.parametrize("crash", [False, True])
def test_raw_clarification_resume_retires_sentinel_atomically_but_keeps_unanswered_question(tmp_path, extra_gap, crash):
    from orca_agent.agent import _mark_decision
    from orca_agent.models import Proposal

    store, run, proposed = _water_candidate(tmp_path)
    gaps = ["missing:goal_definition", *([extra_gap] if extra_gap else [])]
    waiting = commit_candidate(store, run, candidate(store, run, kind="clarify",
        unresolved=gaps, questions=["请明确目标及仍未确定的条件。"],
        question_gaps={"请明确目标及仍未确定的条件。": gaps}),
        decision_id="raw_clarification", basis=current_basis(store, run))
    assert "missing:goal_definition" in store.load_request(waiting).unresolved
    # A durable ordinary clarification can coexist with the unresolved raw
    # Request in a restored history. It is not a new model/HTTP observation.
    _mark_decision(store, waiting, "ordinary_question", Proposal(action="clarify",
        **current_basis(store, waiting), reason="synthetic restored question",
        parameters={"questions": ["请明确目标及仍未确定的条件。"], "unresolved": gaps}),
        current_basis(store, waiting))
    waiting = store.load_run(waiting.id)
    question = store.active_clarification(waiting)
    original = store.load_request(waiting).model_dump_json()
    message = store.enqueue_message(waiting.id, "仍按原文登记水的单点电子能。")
    proposed.update(message_ids=[message], text_basis="仍按原文登记水的单点电子能。")
    proposed["unresolved"] = []
    basis = current_basis(store, waiting)

    def interrupt(point):
        if point == "after_revision_saved":
            raise KeyboardInterrupt()

    if crash:
        with pytest.raises(KeyboardInterrupt):
            commit_candidate(store, waiting, proposed, decision_id="resume_normalize", basis=basis, fault=interrupt)
        unchanged = store.load_run(waiting.id)
        assert store.load_request(unchanged).model_dump_json() == original
        assert store.active_clarification(unchanged) == question and message not in unchanged.processed_messages
    updated = commit_candidate(store, waiting, proposed, decision_id="resume_normalize", basis=basis)
    request = store.load_request(updated)
    assert "missing:goal_definition" not in request.unresolved
    assert [goal.port for goal in request.goals] == ["energy"]
    assert message in updated.processed_messages and updated.usage == waiting.usage
    assert updated.permission == waiting.permission
    if extra_gap:
        assert extra_gap in request.unresolved and store.active_clarification(updated) == question
    else:
        assert not request.unresolved and store.active_clarification(updated) is None
        assert updated.decisions[-1]["semantics"]["resolved_clarification_id"] == question["id"]
    replayed = store.commit_revision(updated, None, decision_id="resume_normalize", basis=basis)
    assert replayed == updated
    if extra_gap:
        # The immutable old question still contains the sentinel. Answering its
        # remaining real gap must not resurrect the already retired placeholder.
        answer = store.enqueue_message(updated.id, "temperature_K=300")
        resolved = commit_candidate(store, updated, candidate(store, updated, kind="amend",
            conditions={"temperature_K": {"value": 300, "source": "explicit", "text_basis": "temperature_K=300"}},
            resolves=[extra_gap]), decision_id="answer_remaining_gap", basis=current_basis(store, updated))
        assert answer in resolved.processed_messages
        assert not store.load_request(resolved).unresolved
        assert store.active_clarification(resolved) is None
        assert resolved.usage == waiting.usage and resolved.permission == waiting.permission
        assert store.load_request_revision(resolved, waiting.request_version).model_dump_json() == original


@pytest.mark.parametrize("goals", [None, []])
def test_missing_goals_cannot_retire_sentinel(tmp_path, goals):
    store, run, proposed = _water_candidate(tmp_path)
    proposed.update(goals=goals, unresolved=["missing:goal_definition"])
    before = store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()
    with pytest.raises((StoreError, ProposalError)):
        commit_candidate(store, run, proposed, decision_id="no_goals", basis=current_basis(store, run))
    assert (store.load_run(run.id).model_dump_json(), store.load_request(run).model_dump_json()) == before


def test_actual_n06_sentinel_replay_keeps_bad_questions_and_original_failed_review(tmp_path):
    project = Path(__file__).resolve().parents[2]
    run_dir = project / "data/phase-b/reference/runs/run_bb258f6450294d4ab220ab370ebbd1f2"
    slot = project / "data/phase-b/model-evaluations/development/repair-revalidation-v15/N-06__raw-unsupported-system/1"
    if not (run_dir / "run.json").exists() or not (slot / "review.json").exists():
        pytest.skip("retained v15 N06 archive absent; real-input replay unverified")
    protected = [p for p in run_dir.rglob("*") if p.is_file()] + list(slot.glob("*.json"))
    before = {p: sha256_file(p) for p in protected}
    history = object.__new__(Store)
    history.root = project / "data/phase-b/reference"
    old = history.load_run(run_dir.name)
    from orca_agent.model_usage import read_model_reply
    reply, _ = read_model_reply(history, old, old.model_records[0])
    assert json.loads(reply.raw_content) == reply.proposal
    assert "missing:goal_definition" in history.load_request(old).unresolved
    review = json.loads((slot / "review.json").read_text(encoding="utf-8"))
    assert review["semantic_review_passed"] is False
    store = store_at(tmp_path)
    fresh, _ = create_request(store, "N-06/raw-unsupported-system", 1,
                              category="development", freeze_label="offline-n06-sentinel")
    parameters = copy.deepcopy(reply.proposal["parameters"])
    parameters["message_ids"] = [store.read_control(fresh.id)["messages"][0]["id"]]
    parameters = current_candidate(parameters)
    # The old accepted state is evidence, not permission to reactivate its
    # unsupported-resource confirmation under the strengthened current rule.
    current_before = store.load_run(fresh.id).model_dump_json(), store.load_request(fresh).model_dump_json()
    with pytest.raises(ProposalError, match="current delivery scope"):
        commit_candidate(store, fresh, parameters, decision_id="replay_n06_sentinel",
                         basis=current_basis(store, fresh))
    assert (store.load_run(fresh.id).model_dump_json(), store.load_request(fresh).model_dump_json()) == current_before
    assert parameters["questions"] == reply.proposal["parameters"]["questions"]
    assert "missing:goal_definition" in parameters["unresolved"]
    assert {p: sha256_file(p) for p in before} == before
