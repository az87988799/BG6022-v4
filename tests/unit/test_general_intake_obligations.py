"""Compound intake regressions; no HTTP, model, generated code or ORCA execution."""

import copy

import pytest
from test_general_intake_correction import commit, environment, proposal

from orca_agent.models import Goal, Request
from orca_agent.proposals import ProposalError
from orca_agent.semantic import _text_geometry_relation


@pytest.mark.parametrize("text", [
    "在固定几何上计算性质，不分别优化两个结构。",
    "在固定几何上计算性质，不要分别优化两套几何。",
    "做单点计算，禁止重新优化。",
    "Compute properties at fixed geometry; do not separately optimize the structures.",
    "Compute single-point properties without optimizing the structure.",
])
def test_negative_optimization_preserves_independent_fixed_geometry(text):
    request = Request(goals=[Goal(id="raw", port="unresolved")])
    assert _text_geometry_relation(request, [{"text": text}], []) == "fixed_initial"


@pytest.mark.parametrize("text", [
    "不分别优化两个结构。", "禁止优化。", "Do not optimize the structure.",
    "先优化结构，不分别优化两个结构。",
])
def test_negative_optimization_never_establishes_positive_optimized_relation(text):
    request = Request(goals=[Goal(id="raw", port="unresolved")])
    assert _text_geometry_relation(request, [{"text": text}], []) is None


def test_compound_goals_keep_own_geometry_obligations_and_reporting_scope(tmp_path):
    text = "请为乙醇生成结构，在固定几何上计算极化率。不分别优化两个结构。记录随机种子。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation="fixed_initial", quantity="极化率")
    parameters["goals"].append({
        "key": "seed", "port": "reproducibility", "quantity": "随机种子",
        "text_basis": text, "system_refs": ["target"],
    })
    before = copy.deepcopy(parameters)
    run = commit(store, run, parameters)
    goals = store.load_request(run).goals
    assert goals[0].conditions["geometry_relation"] == "fixed_initial"
    assert "geometry_relation" not in goals[1].conditions
    assert parameters == before and not run.attempts and not run.result_ids
    assert all(goal.verification == "reported_with_limitations" for goal in goals)


@pytest.mark.parametrize("relation", [None, "optimized"])
def test_explicit_fixed_geometry_cannot_be_dropped_or_reversed_in_compound_goal(tmp_path, relation):
    text = "请为乙醇生成结构，在固定几何上计算极化率。不分别优化两个结构。记录随机种子。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=relation, quantity="极化率")
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    assert any(row["reason"] == "preserve_explicit_geometry_relation"
               and row["required_relation"] == "fixed_initial" for row in error.value.detail["corrections"])
    assert store.load_request(run).version == 1 and not run.attempts


@pytest.mark.parametrize("text", [
    "请为乙醇保持固定几何。计算极化率。",
    "请为乙醇保持完全相同的原子顺序和坐标。计算极化率。",
    "请为乙醇优化结构。计算极化率。",
])
def test_geometry_directive_in_prior_sentence_cannot_be_bypassed_by_omission(tmp_path, text):
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="极化率")
    # A short authenticated quote must not clip off the preceding condition.
    parameters["goals"][0]["text_basis"] = "计算极化率"
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    expected = "optimized" if "优化" in text else "fixed_initial"
    assert any(row["reason"] == "preserve_explicit_geometry_relation"
               and row["required_relation"] == expected for row in error.value.detail["corrections"])
    parameters["goals"][0]["geometry_relation"] = expected
    run = commit(store, run, parameters)
    assert store.load_request(run).goals[0].conditions["geometry_relation"] == expected


def test_distinct_scientific_goals_keep_distinct_geometry_scopes(tmp_path):
    text = "请分析乙醇初始几何的极化率。优化后计算振动频率。记录随机种子。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation="fixed_initial", quantity="极化率")
    parameters["goals"] += [
        {"key": "frequencies", "port": "spectrum", "quantity": "振动频率",
         "text_basis": text, "system_refs": ["target"], "geometry_relation": "optimized"},
        {"key": "seed", "port": "reproducibility", "quantity": "随机种子",
         "text_basis": text, "system_refs": ["target"]},
    ]
    run = commit(store, run, parameters)
    goals = store.load_request(run).goals
    assert [goal.conditions.get("geometry_relation") for goal in goals] == ["fixed_initial", "optimized", None]
    assert not run.result_ids and not run.attempts


def test_independent_wording_and_binding_errors_are_reported_without_default_recopy(tmp_path):
    text = "请为乙醇生成结构，在固定几何上计算极化率。交付全部张量。记录随机种子。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation="fixed_initial", quantity="极化率")
    parameters["conditions"].pop("electronic_state")
    parameters["goals"][0]["minimum_evidence"] = ["全部分量交付"]
    parameters["goals"].insert(0, {
        "key": "input", "port": "prepared_geometry", "quantity": "结构文件",
        "text_basis": text, "system_refs": ["target"],
    })
    parameters["goals"].append({
        "key": "seed", "port": "reproducibility", "quantity": "随机种子", "text_basis": text,
    })
    before = copy.deepcopy(parameters)
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    rows = error.value.detail["corrections"]
    assert {tuple(row["path"]) for row in rows} == {
        ("parameters", "goals", 0, "quantity"),
        ("parameters", "goals", 1, "minimum_evidence", 0),
        ("parameters", "goals", 2, "system_refs"),
    }
    assert parameters == before and store.load_request(run).version == 1
    parameters["goals"][0]["quantity"] = "生成结构"
    parameters["goals"][1]["minimum_evidence"] = ["交付全部张量"]
    parameters["goals"][2]["system_refs"] = ["target"]
    run = commit(store, run, parameters)
    goals = store.load_request(run).goals
    assert store.load_request(run).conditions_source["electronic_state"] == "default"
    assert len(goals) == 3 and goals[1].minimum_evidence == ["交付全部张量"]
    assert not run.attempts and not run.model_records and not run.result_ids


@pytest.mark.parametrize("port", ["value_observation", "field_observation", "artifact_metadata"])
def test_general_quantity_named_like_read_port_does_not_become_legacy_query(tmp_path, port):
    text = "请分析乙醇极化率，核对张量迹并交付全部分量。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="极化率")
    parameters["goals"][0].update(port=port, minimum_evidence=["核对张量迹并交付全部分量"])
    run = commit(store, run, parameters)
    goal = store.load_request(run).goals[0]
    assert goal.evidence_contract_version == goal.minimum_check_version == "generic-observation-1"
    assert goal.minimum_evidence == ["核对张量迹并交付全部分量"]
    assert not goal.unresolved and goal.verification == "reported_with_limitations"
    assert not run.result_ids and not run.calls and not run.attempts


def test_general_electronic_energy_quantity_needs_no_registered_energy_port(tmp_path):
    text = "请为乙醇计算单点能量比较。记录随机种子。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="单点能量比较")
    parameters["goals"][0]["port"] = "energy_comparison"
    run = commit(store, run, parameters)
    goal = store.load_request(run).goals[0]
    assert goal.port == "energy_comparison" and goal.quantity == "单点能量比较"
    assert goal.evidence_contract_version == "generic-observation-1"
    assert "geometry_relation" not in goal.conditions
    assert not run.attempts and not run.result_ids and not run.goal_evidence


@pytest.mark.parametrize("quantity,port", [
    ("结构", "energy_comparison"), ("自由能", "energy"), ("自由能", "free_energy"),
])
def test_general_wrong_quantity_cannot_cover_electronic_energy_even_with_energy_port(tmp_path, quantity, port):
    text = "请为乙醇计算单点电子能。保留结构，另外报告自由能。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity=quantity)
    parameters["goals"][0]["port"] = port
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, run, parameters)
    assert store.load_request(run).version == 1 and not run.attempts


def test_energy_difference_in_another_clause_does_not_cover_requested_absolute_energy(tmp_path):
    text = "请为乙醇计算单点电子能。再报告电子能差。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="电子能差")
    parameters["goals"][0]["port"] = "energy_difference"
    with pytest.raises(ProposalError, match="electronic-energy"):
        commit(store, run, parameters)
    assert not run.attempts and not run.result_ids


@pytest.mark.parametrize("prefix", ["", "请生成多个乙醇候选。"])
@pytest.mark.parametrize("restriction", [
    "仅使用初始XYZ计算极化率",
    "所有计算必须保持初始XYZ的hash不变，计算极化率",
])
def test_explicit_initial_xyz_binding_is_not_relaxed_by_other_candidates(tmp_path, prefix, restriction):
    text = "请为乙醇生成结构。" + prefix + restriction + "。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="极化率")
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    assert any(row["reason"] == "preserve_explicit_geometry_relation"
               and row["required_relation"] == "fixed_initial" for row in error.value.detail["corrections"])
    parameters["goals"][0]["geometry_relation"] = "fixed_initial"
    run = commit(store, run, parameters)
    goal = store.load_request(run).goals[0]
    assert goal.conditions["geometry_relation"] == "fixed_initial"
    assert goal.original_text == text and not run.attempts


@pytest.mark.parametrize("text,quantity", [
    ("请生成多个乙醇候选，从中选择至少3个不同构象，分别计算单点电子能。"
     "禁止优化这些候选，每个能量必须来自独立真实ORCA计算。", "单点电子能"),
    ("请为乙醇生成一份结构，在固定几何上计算极化率。对结构作刚体旋转，保持原子顺序和内部几何。"
     "对旋转副本独立计算极化率，不分别优化两个结构，不得用第一份数值替代第二次真实ORCA结果。", "极化率"),
    ("请为乙醇生成不同构象，对每个候选优化结构，再计算单点电子能。保留每次真实ORCA结果。", "单点电子能"),
    ("请为乙醇扫描一个二面角，逐点约束优化并记录电子能量。交付所有点的结构和每次真实ORCA结果。", "电子能量"),
])
def test_per_geometry_calculations_do_not_acquire_one_initial_hash_relation(tmp_path, text, quantity):
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity=quantity)
    before = copy.deepcopy(parameters)
    run = commit(store, run, parameters)
    request = store.load_request(run)
    goal = request.goals[0]
    assert "geometry_relation" not in goal.conditions
    assert request.original_text == goal.original_text == text
    assert goal.quantity == quantity and goal.verification == "reported_with_limitations"
    assert parameters == before and not run.attempts and not run.result_ids


@pytest.mark.parametrize("relation", ["fixed_initial", "optimized"])
def test_scalar_labels_cannot_replace_authorized_derived_geometry_requirements(tmp_path, relation):
    text = ("请为乙醇生成一份结构，在固定几何上计算极化率。对结构作刚体旋转，保持内部几何。"
            "对旋转副本独立计算极化率，不分别优化两个结构。")
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=relation, quantity="极化率")
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    assert any(row["reason"] == "geometry_relation_is_unknown_or_ambiguous_in_user_text"
               for row in error.value.detail["corrections"])
    assert store.load_request(run).version == 1 and not run.attempts


def test_short_quote_cannot_drop_derived_geometry_and_independent_real_calculation_obligations(tmp_path):
    text = ("请为乙醇生成结构，对结构作刚体旋转并计算极化率。"
            "保持原子顺序和内部几何，不分别优化两个结构，第二次真实ORCA结果不得由第一份数值变换代替。")
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="极化率")
    parameters["goals"][0]["text_basis"] = "计算极化率"
    before = copy.deepcopy(parameters)
    run = commit(store, run, parameters)
    goal = store.load_request(run).goals[0]
    shared = goal.text_evidence["shared_constraints"]
    assert {row["text_basis"] for row in shared} >= {
        "不分别优化两个结构", "第二次真实ORCA结果不得由第一份数值变换代替"}
    assert all(text[row["start"]:row["end"]] == row["text_basis"] for row in shared)
    assert parameters == before
    assert goal.minimum_evidence == parameters["goals"][0].get("minimum_evidence", [])
    assert "geometry_relation" not in goal.conditions and not run.attempts


def test_negated_candidate_generation_does_not_relax_one_fixed_geometry(tmp_path):
    text = "请为乙醇生成一份结构，在固定几何上计算极化率。不要生成多个候选。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="极化率")
    with pytest.raises(ProposalError) as error:
        commit(store, run, parameters)
    assert any(row["reason"] == "preserve_explicit_geometry_relation"
               and row["required_relation"] == "fixed_initial" for row in error.value.detail["corrections"])


def test_short_general_energy_quote_retains_single_point_operation_without_initial_hash(tmp_path):
    text = "请为乙醇计算单点电子能。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation=None, quantity="电子能")
    parameters["goals"][0]["text_basis"] = "电子能"
    run = commit(store, run, parameters)
    goal = store.load_request(run).goals[0]
    assert "geometry_relation" not in goal.conditions
    assert goal.text_evidence["shared_constraints"][0]["text_basis"] == "请为乙醇计算单点电子能"
    assert not run.attempts and not run.result_ids


def test_shared_coordinates_across_environments_keep_one_initial_xyz_binding(tmp_path):
    text = ("请为乙醇生成一份结构，保持完全相同的原子顺序和坐标，分别计算两个环境中的单点电子能。"
            "不要分别优化两套几何。交付两份原始输入输出、共用XYZ及hash。")
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation="fixed_initial", quantity="单点电子能")
    # This fixture deliberately leaves both environments unnamed; do not
    # overwrite that request with its configured single gas-phase default.
    parameters["conditions"]["environment"] = {"source": "unknown"}
    parameters["questions"] = ["两个计算环境分别是什么？"]
    run = commit(store, run, parameters)
    request = store.load_request(run)
    goal = request.goals[0]
    assert goal.conditions["geometry_relation"] == "fixed_initial"
    assert "unconfirmed:environment" in request.unresolved
    assert goal.original_text == text and not run.attempts


def test_many_atom_coordinates_do_not_mean_many_geometry_candidates(tmp_path):
    text = "请为乙醇优化结构并计算振动频率。交付全部9个原子坐标。"
    store, config, run = environment(tmp_path, text)
    parameters = proposal(store, config, run, text=text, relation="optimized", quantity="振动频率")
    run = commit(store, run, parameters)
    assert store.load_request(run).goals[0].conditions["geometry_relation"] == "optimized"
    assert not run.attempts
