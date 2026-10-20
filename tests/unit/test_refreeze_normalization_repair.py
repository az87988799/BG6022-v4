"""Offline retained-candidate replay; repairs below are explicit test authorship."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_general_intake_correction import commit, environment, proposal

from orca_agent.proposals import DecisionIntent, ProposalError
from orca_agent.semantic import _mentions, _text_geometry_relation
from orca_agent.store import StoreError

EVIDENCE = Path(__file__).resolve().parents[2] / "docs/acceptance/code-as-tools-step7-refreeze-1/evidence"
REPLIES = {
    ("T1", 1): ("model_c0182229b11d49ba8a45f826b9154124", "cde707e2a1955605534e48ff39908f5531b4b351715e922c8b63362f716a724a"),
    ("T1", 2): ("model_b0a671c8d0ff467a9c628d34a6322fa0", "92acea65689b4f2405fb67192e7909582f379d1b0e905301eecbce9daebf62b1"),
    ("T3", 1): ("model_78dfb7b531aa41baad726f25ccfda3a9", "1047ac674f7d2673dd2252038982f929555be09aa7126701ba350b1339d18cb4"),
    ("T3", 3): ("model_9da67adbd553410aae1472458e803b8c", "ac8d2c70d2799a9857f576a63eb530ca1e444a563a26948478c4b207457a49d5"),
    ("T5", 1): ("model_749a0e1f6f09458fb0b287e76f2c72af", "19e77b21de3c349d3463fb8d0523fca6018d34545ac81f62a2e29c9c1daf29cc"),
    ("T5", 2): ("model_5076347769a84047821c1fbaabad2c14", "a0792ddec5879e4b1025050defe20a8d9765740fafd698580fb5aa042418c31e"),
    ("T5", 3): ("model_d87c6730ae82416fb6a5c6c2a7474327", "b9ed495e69f42672d1c0138026f0367bda503e2233de38bf0dec1bb1ff975cd8"),
}
NOTICE = "保留这些初始结构交付和最低证据要求；没有对应的注册最低证据评估器，不能宣称科学检查通过。"


def retained(tmp_path, task, ordinal):
    identifier, digest = REPLIES[task, ordinal]
    paths = list((EVIDENCE / task).rglob(identifier + ".response.json"))
    assert len(paths) == 1
    raw = paths[0].read_bytes()
    assert hashlib.sha256(raw).hexdigest() == digest
    response = json.loads(raw)
    request_path = paths[0].parent.parent / "request-revisions/1.json"
    original = json.loads(request_path.read_text(encoding="utf-8"))["original_text"]
    store, config, run = environment(tmp_path, original)
    message_id = store.read_control(run.id)["messages"][0]["id"]

    def rebind(value):
        # Only transport identity changes; science fields and historical errors
        # remain byte-equivalent JSON values until each test's explicit repair.
        if isinstance(value, dict):
            return {key: message_id if key == "message_id" else [message_id] if key == "message_ids"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value

    return store, config, run, rebind(response["proposal"]), original


def unchanged_run(store, run, before):
    assert store.load_run(run.id).model_dump_json() == before
    assert run.request_version == 1 and not run.attempts and not run.model_records
    assert not run.calls and not run.decisions


def rows(error):
    return error.value.detail["corrections"]


def test_t1_redaction_marker_remains_invalid_original_evidence(tmp_path):
    store, _, run, reply, _ = retained(tmp_path, "T1", 1)
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(StoreError, match="semantic evidence must quote"):
        commit(store, run, reply["parameters"])
    unchanged_run(store, run, before)


def test_t1_full_original_warning_preserves_optimized_relation_and_reports_minimum_gap(tmp_path):
    store, _, run, reply, original = retained(tmp_path, "T1", 2)
    parameters = reply["parameters"]
    before = store.load_run(run.id).model_dump_json()
    original_parameters = copy.deepcopy(parameters)
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    reasons = {row["reason"] for row in rows(error)}
    assert reasons == {"new_minimum_evidence_requires_notice"}
    assert rows(error)[0]["new_unresolved"] == ["unsupported_minimum_evidence:交付初始结构"]
    assert parameters == original_parameters
    unchanged_run(store, run, before)

    parameters["notices"] = [NOTICE]
    updated = commit(store, run, parameters)
    request = store.load_request(updated)
    assert request.original_text == original
    assert [goal.conditions.get("geometry_relation") for goal in request.goals[1:3]] == ["optimized", "optimized"]
    assert request.goals[0].minimum_evidence == original_parameters["goals"][0]["minimum_evidence"]
    assert "unsupported_minimum_evidence:交付初始结构" in request.goals[0].unresolved
    assert not updated.attempts and not updated.result_ids and not updated.model_records


def test_t3_original_errors_are_aggregated_without_seed_geometry_misclassification(tmp_path):
    store, _, run, reply, original = retained(tmp_path, "T3", 1)
    parameters = reply["parameters"]
    before = store.load_run(run.id).model_dump_json()
    original_parameters = copy.deepcopy(parameters)
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    reasons = {row["reason"] for row in rows(error)}
    assert {"authorized_default_requires_explicit_adoption", "geometry_relation_is_unknown_or_ambiguous_in_user_text",
            "new_minimum_evidence_requires_notice"} <= reasons
    geometry_rows = [row for row in rows(error) if row["reason"] == "preserve_general_geometry_obligation"]
    assert geometry_rows and all("固定种子" not in row["user_evidence"] and "不可固定" not in row["user_evidence"]
                                 for row in geometry_rows)
    assert parameters == original_parameters
    unchanged_run(store, run, before)

    # No algorithm or science values are supplied. Preserve the whole original
    # quote and every minimum, adopt the disclosed configured reference, and
    # disclose the unsupported input minimum. Per-point geometry stays textual.
    parameters["conditions"]["electronic_state"] = {
        "source": "default", "default_rule": "local-hf-1", "value": "RKS"}
    parameters["notices"] = [NOTICE]
    for goal in parameters["goals"]:
        goal["text_basis"] = original
        goal["geometry_relation"] = None
    updated = commit(store, run, parameters)
    request = store.load_request(updated)
    assert request.original_text == original and len(request.goals) == len(original_parameters["goals"])
    assert [g.minimum_evidence for g in request.goals] == [g["minimum_evidence"] for g in original_parameters["goals"]]
    assert all(g.conditions.get("geometry_relation") is None for g in request.goals)
    assert not updated.attempts and not updated.model_records and not updated.result_ids


def test_t3_extra_field_still_rejected_without_guessing_a_schema_mapping(tmp_path):
    store, _, run, reply, _ = retained(tmp_path, "T3", 3)
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError) as error:
        commit(store, run, reply["parameters"])
    assert any(entry["type"] == "extra_forbidden" and entry["loc"][-1] == "system_refs_note"
               for entry in error.value.detail["errors"])
    unchanged_run(store, run, before)


def test_t5_default_and_ambiguous_field_quote_reported_together(tmp_path):
    store, _, run, reply, _ = retained(tmp_path, "T5", 1)
    before = store.load_run(run.id).model_dump_json()
    with pytest.raises(ProposalError) as error:
        commit(store, run, reply["parameters"])
    assert "authorized_default_requires_explicit_adoption" in {row["reason"] for row in rows(error)}
    assert "semantic_quote_not_unique" in {row["reason"] for row in rows(error)}
    unchanged_run(store, run, before)


def test_t5_overlong_reason_remains_rejected_without_silent_truncation(tmp_path):
    _, _, _, reply, _ = retained(tmp_path, "T5", 2)
    before = copy.deepcopy(reply)
    assert len(reply["reason"]) == 1163
    with pytest.raises(ValidationError) as error:
        DecisionIntent.model_validate({key: reply[key] for key in ("action", "parameters", "reason")})
    assert any(item["loc"] == ("reason",) and item["type"] == "string_too_long" for item in error.value.errors())
    assert reply == before


def test_t5_same_message_quote_diagnostic_and_explicit_fuller_quote_repair(tmp_path):
    store, _, run, reply, original = retained(tmp_path, "T5", 3)
    parameters = reply["parameters"]
    before = store.load_run(run.id).model_dump_json()
    original_parameters = copy.deepcopy(parameters)
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    repeated = next(row for row in rows(error) if row["reason"] == "semantic_quote_not_unique")
    assert repeated["path"] == ["parameters", "goals", 1, "conditions", "environment", "text_basis"]
    assert repeated["matching_occurrence_count"] == 2
    assert len({item["message_id"] for item in repeated["occurrences"]}) == 1
    assert all(original[item["start"]:item["end"]] == "气相" for item in repeated["occurrences"])
    assert "repeating that ID cannot disambiguate" in repeated["instruction"]
    lexical = next(row for row in rows(error) if row["reason"] == "condition_evidence_invalid")
    assert lexical["path"] == ["parameters", "goals", 2, "conditions", "environment"]
    assert lexical["diagnostic"] == "explicit condition lacks a supported lexical value basis"
    assert parameters == original_parameters
    unchanged_run(store, run, before)

    parameters["goals"][1]["conditions"]["environment"]["text_basis"] = "分别计算气相"
    # The candidate's alias water_solvent was not authenticated by its Chinese
    # source quote. Use the literal specified solvent/model, not a new alias or
    # any program-selected environment/parameter.
    parameters["goals"][2]["conditions"]["environment"]["value"] = "C-PCM水环境"
    updated = commit(store, run, parameters)
    request = store.load_request(updated)
    assert request.original_text == original
    assert [goal.minimum_evidence for goal in request.goals] == [goal.get("minimum_evidence", [])
                                                               for goal in original_parameters["goals"]]
    assert request.goals[1].conditions["environment"] == "gas_phase"
    assert request.goals[2].conditions["environment"] == "C-PCM水环境"
    assert not updated.attempts and not updated.model_records and not updated.result_ids


@pytest.mark.parametrize("text, expected", [
    ("计算乙醇在C-PCM水环境中的能量", ["ethanol"]),
    ("Calculate ethanol energy in a water environment.", ["ethanol"]),
    ("分别计算乙醇和水分子的气相单点能量", ["ethanol", "water"]),
    ("Calculate ethanol and water energies in gas phase.", ["ethanol", "water"]),
    ("分别计算水在甲烷溶剂中的能量", ["water"]),
])
def test_solvent_environment_is_not_an_additional_molecular_target(text, expected):
    assert [mention[2] for mention in _mentions(text, targets=True)] == expected


@pytest.mark.parametrize("warning", [
    "不要把程序正常结束当成优化收敛或极小值确认",
    "不得将成功退出视为几何优化收敛",
    "Do not treat normal termination as optimization convergence",
])
def test_result_caveats_do_not_withdraw_explicit_geometry_request(tmp_path, warning):
    text = "请为乙醇优化结构并计算振动频率。" + warning + "。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text)
    updated = commit(store, run, parameters)
    assert store.load_request(updated).goals[0].conditions["geometry_relation"] == "optimized"
    assert store.load_request(updated).original_text == text and not updated.attempts


@pytest.mark.parametrize("text, expected", [
    ("不要优化结构。不要把程序正常结束当成优化收敛。", None),
    ("先优化结构，但不要优化结构。", None),
    ("在固定几何上计算性质，不分别优化结构。", "fixed_initial"),
    ("尚不确定是否优化结构。", None),
    ("请优化结构并且不要把程序正常结束当成优化收敛。", "optimized"),
    ("不要优化结构且不要把程序正常结束当成优化收敛。", None),
    ("先优化结构，但不要优化结构且不要把程序正常结束当成优化收敛。", None),
])
def test_actual_negative_and_uncertain_geometry_constraints_remain(tmp_path, text, expected):
    store, _, run = environment(tmp_path, text)
    assert _text_geometry_relation(store.load_request(run), [{"text": text}], []) == expected


@pytest.mark.parametrize("text", [
    "先优化几何。不要把正常结束当成收敛并且不要优化结构或确认极小值。",
    "First optimize the geometry. Do not treat normal termination as convergence "
    "and do not optimize the structure or confirm a minimum.",
    "先优化几何。不要把正常结束当成证明并且不要优化结构或确认极小值。",
    "First optimize the geometry. Do not treat normal termination as proof "
    "and do not optimize the structure or confirm a minimum.",
])
def test_outcome_warning_cannot_swallow_later_geometry_prohibition(tmp_path, text):
    store, _, run = environment(tmp_path, text)
    assert _text_geometry_relation(store.load_request(run), [{"text": text}], []) is None


@pytest.mark.parametrize("seed_clause", [
    "固定随机种子 42；若不支持固定种子，明确记为不可固定。",
    "Use a fixed random seed; mark an unsupported seed as not fixed.",
])
def test_seed_language_is_not_an_additional_geometry_obligation(tmp_path, seed_clause):
    text = "请为乙醇生成多个构象，每个结构分别计算单点电子能。" + seed_clause
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="单点电子能")
    parameters["goals"][0]["text_basis"] = text.split("。", 1)[0]
    updated = commit(store, run, parameters)
    assert "geometry_relation" not in store.load_request(updated).goals[0].conditions
    assert store.load_request(updated).original_text == text


@pytest.mark.parametrize("notice", ["", "\u200b", " \t\n"])
def test_invisible_notice_does_not_resolve_unsupported_minimum(tmp_path, notice):
    store, _, run, reply, _ = retained(tmp_path, "T1", 2)
    reply["parameters"]["notices"] = [notice]
    with pytest.raises(ProposalError) as error:
        commit(store, run, reply["parameters"])
    assert "new_minimum_evidence_requires_notice" in {row["reason"] for row in rows(error)}
    assert run.request_version == 1 and not run.attempts
