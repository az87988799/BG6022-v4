"""Offline replay of frozen real replies; no new model or scientific execution."""

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_simplified_semantic_selection import intake, normal, rebind, submit
from test_simplified_semantic_selection import retained as previous_retained

from orca_agent.proposals import DecisionIntent, ProposalError
from orca_agent.semantic import source_fragments
from orca_agent.store import StoreError

PACK = Path(__file__).resolve().parents[2] / "docs/acceptance/code-as-tools-step7-refreeze-3"


def retained(tmp_path, task, ordinal):
    manifest = json.loads((PACK / "evidence/index.json").read_bytes())
    files = next(row["files"] for row in manifest["tasks"] if row["task"] == task)

    def read(suffix):
        entry = next(row for row in files if row["path"].endswith(suffix))
        path = PACK / entry["published_path"]
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        return json.loads(raw), path, raw

    run_record, _, _ = read("/run.json")
    identifier = run_record["model_records"][ordinal]["id"]
    reply, path, raw = read(identifier + ".response.json")
    request, _, _ = read("/request-revisions/1.json")
    control, _, _ = read("/control.json")
    text = request["original_text"]
    store, config, run = intake(tmp_path, text)
    messages = store.read_control(run.id)["messages"]
    previous = source_fragments(control["messages"])
    current = source_fragments(messages)
    assert [(row["start"], row["end"], row["text"]) for row in previous] == [
        (row["start"], row["end"], row["text"]) for row in current]
    refs = {old["id"]: new["id"] for old, new in zip(previous, current, strict=True)}
    message_id = messages[0]["id"]

    def rebind(value):
        # Only transport/source identities change. Every quoted quantity,
        # condition, constraint and scientific parameter remains from the reply.
        if isinstance(value, dict):
            return {key: message_id if key == "message_id"
                    else [message_id] if key == "message_ids"
                    else [refs[ref] for ref in child] if key == "source_refs"
                    else rebind(child) for key, child in value.items()}
        return [rebind(child) for child in value] if isinstance(value, list) else value

    return store, config, run, rebind(reply["proposal"]), path, raw


@pytest.mark.parametrize("task,ordinal", [
    (task, ordinal) for task in ("T1", "T2", "H2") for ordinal in range(3)
])
def test_retained_intake_activates_without_rewriting_quantity_or_requirements(tmp_path, task, ordinal):
    store, _, run, reply, path, raw = retained(tmp_path, task, ordinal)
    original = copy.deepcopy(reply)
    intent = DecisionIntent.model_validate(reply)
    updated = submit(store, run, intent.parameters)
    request = store.load_request(updated)
    proposed = intent.parameters["goals"]
    assert request.normalization_status == "normalized"
    assert len(request.goals) == len(proposed)
    assert [goal.quantity for goal in request.goals] == [goal["quantity"] for goal in proposed]
    for goal, authored in zip(request.goals, proposed, strict=True):
        assert set(authored.get("minimum_evidence", [])) <= set(goal.minimum_evidence)
        assert goal.quantity in request.original_text
        assert goal.text_evidence["message_id"] == store.read_control(run.id)["messages"][0]["id"]
    assert path.read_bytes() == raw and reply == original
    assert not updated.calls and not updated.attempts and not updated.model_records
    assert not updated.result_ids and not updated.goal_evidence
    if task == "T1":
        assert (request.charge, request.multiplicity, request.conditions["environment"]) == (0, 1, "gas_phase")
        assert set(request.automatic_selection) == {"method", "basis"}
    elif task == "T2":
        assert all(goal.conditions.get("geometry_relation") is None for goal in request.goals)
        assert any("绝对能量" in goal.quantity for goal in request.goals)
    else:
        initial = next(goal for goal in request.goals if goal.port == "static_polarizability_fixed_geometry")
        rotated = next(goal for goal in request.goals if goal.port == "static_polarizability_rotated_copy")
        assert initial.conditions["geometry_relation"] == "fixed_initial"
        assert "geometry_relation" not in rotated.conditions
        assert (request.method, request.basis, request.conditions["electronic_state"]) == ("HF", "def2-SVP", "RHF")


@pytest.mark.parametrize("mutation", ["wrong_value", "wrong_value_basis", "foreign_quote", "foreign_message"])
def test_explicit_value_and_source_cannot_be_forged_after_envelope_normalization(tmp_path, mutation):
    store, _, run, reply, _, _ = retained(tmp_path, "T1", 1)
    field = reply["parameters"]["conditions"]["charge"]
    if mutation == "wrong_value":
        field["value"] = 1
    elif mutation == "wrong_value_basis":
        field["value_basis"] = "单重态"
    elif mutation == "foreign_quote":
        field["text_basis"] = "总电荷为2"
    else:
        field["message_id"] = "foreign"
    before = store.load_request(run).model_dump_json()
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, reply["parameters"])
    assert store.load_request(run).model_dump_json() == before
    assert not run.calls and not run.attempts


@pytest.mark.parametrize("mutation", ["remove_energy", "free_energy", "optional_energy", "wrong_scope"])
def test_delivery_reference_does_not_replace_a_missing_or_different_energy_quantity(tmp_path, mutation):
    store, _, run, reply, _, _ = retained(tmp_path, "T2", 1)
    goals = reply["parameters"]["goals"]
    energy = next(goal for goal in goals if goal["key"] == "orca_sp_energy")
    if mutation == "remove_energy":
        goals.remove(energy)
    elif mutation == "free_energy":
        energy["quantity"] = "自由能最低"
        # This phrase is authentic but it is an explicitly prohibited claim,
        # not the electronic-energy obligation.
        fragments = source_fragments(store.read_control(run.id)["messages"])
        energy["source_refs"] = [row["id"] for row in fragments if "自由能最低" in row["text"]]
    elif mutation == "optional_energy":
        energy["required"] = False
    else:
        fragments = source_fragments(store.read_control(run.id)["messages"])
        energy["source_refs"] = [row["id"] for row in fragments if "绝对能量" in row["text"]]
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, reply["parameters"])
    assert store.load_request(run).normalization_status == "pending"


@pytest.mark.parametrize("extra", [{"type": "execute"}, {"execute": True}, {"budget": {"orca_starts": 999}}])
def test_transport_normalization_does_not_ignore_unknown_execution_fields(extra):
    value = {"action": "normalize_request", "parameters": {}, "reason": "", **extra}
    with pytest.raises(ValidationError):
        DecisionIntent.model_validate(value)


def test_conflicting_or_unrecognized_bare_envelope_is_not_guessed():
    for value in (
        {"schema_version": "other", "kind": "normalize", "message_ids": ["one"]},
        {"schema_version": "request-semantics-3", "kind": "normalize", "parameters": {}},
    ):
        with pytest.raises(ValidationError):
            DecisionIntent.model_validate(value)


@pytest.mark.parametrize("text,quantity", [
    ("请为乙醇计算单点电子能。再报告电子能差。", "电子能差"),
    ("请为乙醇报告电子能差。也报告电子能。", "电子能"),
    ("请为乙醇计算单点电子能。保留结构。", "结构"),
])
def test_shared_source_range_does_not_make_distinct_quantities_equivalent(tmp_path, text, quantity):
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=quantity)
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"


@pytest.mark.parametrize("quantity", [
    "总能量收敛阈值", "总能量计算次数", "电子能量的相对误差",
    "总能量方差", "电子能量一阶导数", "电子能量误差的均方根",
    "total energy convergence threshold", "total energy evaluation count",
    "electronic energy error", "electronic energy variance",
    "convergence threshold for total energy", "number of total energy evaluations",
    "variance in electronic energy", "standard deviation of total energy",
    "first derivative of electronic energy", "units of total energy",
    "平均电子能量", "电子能量平均值", "mean total energy", "average electronic energy",
    "absolute value of electronic energy", "squared values of total energy",
    "squared total energy", "normalized electronic energy", "root mean square total energy",
    "square-root transformed total energy", "custom_transformed electronic energy",
    "平方总能量", "归一化电子能量", "未登记变换的电子能量",
])
def test_energy_parameter_or_derived_metric_does_not_cover_actual_energy(tmp_path, quantity):
    text = f"请为乙醇计算单点电子能。再报告{quantity}。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=quantity)
    before = store.load_request(run).model_dump_json()
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    assert store.load_request(run).model_dump_json() == before
    # Retain the requested metric and add the genuinely missing physical output.
    energy = copy.deepcopy(parameters["goals"][0])
    energy.update(key="electronic_energy", port="energy", quantity="单点电子能")
    parameters["goals"].append(energy)
    updated = submit(store, run, parameters)
    assert [goal.quantity for goal in store.load_request(updated).goals] == [quantity, "单点电子能"]


@pytest.mark.parametrize("quantity", [
    "电子能量收敛阈值", "电子能量计算次数", "电子能量的相对误差",
    "electronic energy error", "electronic energy evaluation count",
    "variance in electronic energy", "first derivative of electronic energy",
    "平均电子能量", "mean total energy",
    "squared total energy", "normalized electronic energy", "root mean square total energy",
])
def test_metric_only_request_does_not_invent_an_absolute_energy_obligation(tmp_path, quantity):
    text = f"请为乙醇报告{quantity}。"
    store, config, run = intake(tmp_path, text)
    updated = submit(store, run, normal(store, config, run, quantity=quantity))
    assert [goal.quantity for goal in store.load_request(updated).goals] == [quantity]


@pytest.mark.parametrize("quantity", [
    "绝对能量和相对最低项的kJ/mol排名", "总能量数值与排名", "绝对电子能量（Hartree）",
    "absolute energies and ranking", "total electronic energy values",
    "values of absolute energies", "absolute energies in Hartree",
    "ethanol electronic energy", "乙醇的电子能", "original total energy", "computed electronic energy",
])
def test_direct_energy_values_and_rankings_cover_the_energy_obligation(tmp_path, quantity):
    text = f"请为乙醇计算单点电子能。再报告{quantity}。"
    store, config, run = intake(tmp_path, text)
    updated = submit(store, run, normal(store, config, run, quantity=quantity))
    assert [goal.quantity for goal in store.load_request(updated).goals] == [quantity]


@pytest.mark.parametrize("instruction", [
    "请为乙醇计算单点电子能用于比较。",
    "请为乙醇计算单点电子能来比较候选。",
    "Please calculate electronic energy for ethanol.",
    "Please calculate electronic energy of ethanol.",
    "Please calculate electronic energy from ORCA.",
    "请为乙醇 compute electronic energy using HF.",
    "Calculate electronic energy in water solvent.",
    "Report electronic energy without executing calculations.",
])
def test_energy_purpose_and_condition_clauses_cannot_remove_the_physical_obligation(tmp_path, instruction):
    text = instruction + "再报告乙醇结构。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="结构")
    if "water solvent" in instruction:
        parameters["conditions"]["environment"] = {
            "source": "explicit", "value": "water_solvent", "text_basis": "water solvent"}
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"


def test_absolute_and_relative_energy_can_be_preserved_as_separate_goals(tmp_path):
    text = "请为乙醇报告电子能和相对电子能差。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="电子能")
    difference = copy.deepcopy(parameters["goals"][0])
    difference.update(key="relative", port="relative_energy", quantity="相对电子能差")
    parameters["goals"].append(difference)
    updated = submit(store, run, parameters)
    assert [goal.quantity for goal in store.load_request(updated).goals] == ["电子能", "相对电子能差"]


@pytest.mark.parametrize("quantity", [
    "HF电子能", "每个构象的电子能", "HF electronic energy",
    "electronic energy evaluated for each conformer", "未知限定的电子能",
    "normalized electronic energy", "custom_transformed total energy",
])
def test_unclassified_source_energy_phrase_must_be_preserved_verbatim(tmp_path, quantity):
    text = f"请为乙醇报告{quantity}。再报告结构。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="结构")
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    parameters = normal(store, config, run, quantity=quantity)
    updated = submit(store, run, parameters)
    assert [goal.quantity for goal in store.load_request(updated).goals] == [quantity]


@pytest.mark.parametrize("quantity,purpose", [
    ("HF电子能", "用于比较"),
    ("每个构象的电子能", "以便比较候选"),
    ("归一化电子能", "用来绘图"),
    ("normalized electronic energy", " in order to compare candidates"),
    ("electronic energy error", " so that the precision can be assessed"),
])
def test_unknown_quantity_preserves_its_modifiers_without_turning_purpose_into_output(tmp_path, quantity, purpose):
    text = f"请为乙醇报告{quantity}{purpose}。再报告结构。"
    store, config, run = intake(tmp_path, text)
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, normal(store, config, run, quantity="结构"))
    updated = submit(store, run, normal(store, config, run, quantity=quantity))
    assert [goal.quantity for goal in store.load_request(updated).goals] == [quantity]


@pytest.mark.parametrize("kept", ["电子能", "归一化电子能"])
def test_raw_and_unclassified_energy_in_one_clause_are_separate_obligations(tmp_path, kept):
    text = "请为乙醇报告电子能和归一化电子能。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=kept)
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    other = copy.deepcopy(parameters["goals"][0])
    other.update(key="other_energy", port="other_energy",
                 quantity="归一化电子能" if kept == "电子能" else "电子能")
    parameters["goals"].append(other)
    assert len(store.load_request(submit(store, run, parameters)).goals) == 2


@pytest.mark.parametrize("change", [
    "correct", "remove_gas_goal", "remove_water_goal", "gas_condition", "missing_condition",
])
@pytest.mark.parametrize("full_quantity", [False, True])
def test_condition_qualified_energy_requires_the_matching_goals_own_condition(tmp_path, change, full_quantity):
    text, proposal, _, _ = previous_retained("T5")
    store, _, run = intake(tmp_path, text)
    parameters = rebind(proposal, store.read_control(run.id)["messages"][0]["id"])
    water = next(goal for goal in parameters["goals"] if goal["key"] == "water_energy")
    if full_quantity:
        water["quantity"] = "ORCA C-PCM水环境下的单点电子能量"
    if change == "remove_gas_goal":
        parameters["goals"] = [goal for goal in parameters["goals"] if goal["key"] != "gas_energy"]
    elif change == "remove_water_goal":
        parameters["goals"].remove(water)
    elif change == "gas_condition":
        water["conditions"]["environment"] = copy.deepcopy(next(
            goal["conditions"]["environment"] for goal in parameters["goals"] if goal["key"] == "gas_energy"))
    elif change == "missing_condition":
        water.pop("conditions")
    if change == "correct":
        updated = submit(store, run, parameters)
        actual = next(goal for goal in store.load_request(updated).goals if goal.id == "goal_water_energy")
        assert actual.conditions["environment"] == "CPCM(water)"
        return
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)


@pytest.mark.parametrize("quantity", [
    "normalized electronic energy", "squared total energy", "电子能量收敛阈值", "总能量计算次数",
])
def test_authenticated_environment_does_not_erase_energy_transforms_or_metrics(tmp_path, quantity):
    prefix = "ORCA C-PCM水环境下的"
    text = f"请为乙醇计算{prefix}单点电子能。再报告{prefix}{quantity}。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=prefix + quantity)
    parameters["conditions"].pop("environment")
    parameters["goals"][0]["conditions"] = {
        "environment": {"source": "explicit", "value": "water_solvent", "text_basis": text}}
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)
    energy = copy.deepcopy(parameters["goals"][0])
    energy.update(key="actual_energy", port="energy", quantity="单点电子能")
    parameters["goals"].append(energy)
    updated = submit(store, run, parameters)
    assert [goal.quantity for goal in store.load_request(updated).goals] == [prefix + quantity, "单点电子能"]
    assert {goal.conditions["environment"] for goal in store.load_request(updated).goals} == {"CPCM(water)"}


def test_arbitrary_transform_word_cannot_be_reinterpreted_as_a_method_condition(tmp_path):
    text = "请为乙醇报告normalized electronic energy。再报告electronic energy。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity="electronic energy")
    parameters["goals"][0]["conditions"] = {
        "method": {"source": "explicit", "value": "normalized", "text_basis": text}}
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)


@pytest.mark.parametrize("scope,accepted", [
    ("request", True), ("system", True), ("goal", True),
    ("request_gas_system_water", True), ("request_water_system_gas", False),
    ("request_water_goal_gas", False),
])
@pytest.mark.parametrize("full_quantity", [False, True])
@pytest.mark.parametrize("position", ["prefix", "suffix"])
def test_environment_evidence_uses_the_execution_resolvers_effective_scope(
        tmp_path, scope, accepted, full_quantity, position):
    literal = ("ORCA C-PCM水环境下的单点电子能" if position == "prefix"
               else "单点电子能 in ORCA C-PCM水环境")
    text = f"请为乙醇计算{literal}。气相作为对照。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=literal if full_quantity else "单点电子能")
    parameters["conditions"].pop("environment")
    water = {"source": "explicit", "value": "water_solvent", "text_basis": "C-PCM水环境"}
    gas = {"source": "explicit", "value": "gas_phase", "text_basis": "气相"}
    if scope == "request":
        parameters["conditions"]["environment"] = water
    elif scope == "system":
        parameters["system_conditions"] = {"target": {"environment": water}}
    elif scope == "goal":
        parameters["goals"][0]["conditions"] = {"environment": water}
    else:
        parameters["conditions"]["environment"] = gas if scope == "request_gas_system_water" else water
        if scope == "request_water_goal_gas":
            parameters["goals"][0]["conditions"] = {"environment": gas}
        else:
            parameters["system_conditions"] = {"target": {
                "environment": water if scope == "request_gas_system_water" else gas}}
    if accepted:
        updated = submit(store, run, parameters)
        assert store.load_request(updated).normalization_status == "normalized"
    else:
        with pytest.raises(ProposalError, match="electronic-energy"):
            submit(store, run, parameters)


@pytest.mark.parametrize("full_quantity", [False, True])
def test_suffix_environment_cannot_disappear_into_a_default(tmp_path, full_quantity):
    literal = "electronic energy in ORCA C-PCM水环境"
    store, config, run = intake(tmp_path, f"请为乙醇计算{literal}。")
    parameters = normal(store, config, run, quantity=literal if full_quantity else "electronic energy")
    parameters["conditions"].pop("environment")
    with pytest.raises(ProposalError, match="electronic-energy"):
        submit(store, run, parameters)


@pytest.mark.parametrize("full_quantity", [False, True])
@pytest.mark.parametrize("keep_water", [False, True])
def test_classified_suffix_environment_still_requires_its_own_matching_goal(tmp_path, full_quantity, keep_water):
    literal = "electronic energy in ORCA C-PCM水环境"
    text = f"请为乙醇计算{literal}。再报告气相electronic energy。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run, quantity=literal if full_quantity else "electronic energy")
    parameters["conditions"].pop("environment")
    parameters["goals"][0]["conditions"] = {
        "environment": {"source": "explicit", "value": "gas_phase", "text_basis": "气相"}}
    if not keep_water:
        with pytest.raises(ProposalError, match="electronic-energy"):
            submit(store, run, parameters)
        return
    gas = copy.deepcopy(parameters["goals"][0])
    gas.update(key="gas_energy", port="gas_energy", quantity="electronic energy")
    parameters["goals"].append(gas)
    parameters["goals"][0]["conditions"]["environment"] = {
        "source": "explicit", "value": "water_solvent", "text_basis": "C-PCM水环境"}
    updated = submit(store, run, parameters)
    assert {goal.conditions["environment"] for goal in store.load_request(updated).goals} == {
        "CPCM(water)", "gas_phase"}


@pytest.mark.parametrize("text", [
    "请为乙醇计算极化率，不要将电荷设为0。",
    "请为乙醇计算极化率，电荷可能为0。",
    "请为乙醇计算极化率，电荷为0或1。",
])
def test_short_value_quote_cannot_hide_negation_or_uncertainty(tmp_path, text):
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    parameters["conditions"]["charge"] = {
        "source": "explicit", "value": 0, "text_basis": "0", "value_basis": "0",
        "source_refs": [source_fragments(store.read_control(run.id)["messages"])[0]["id"]],
    }
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)
    assert store.load_request(run).normalization_status == "pending"


def test_broad_reference_cannot_replace_a_contradictory_literal_field_quote(tmp_path):
    text = "请为乙醇计算极化率，中性，随机种子1701。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    parameters["conditions"]["charge"] = {
        "source": "explicit", "value": 1701, "text_basis": "中性",
        "source_refs": [source_fragments(store.read_control(run.id)["messages"])[0]["id"]],
    }
    with pytest.raises((ProposalError, StoreError)):
        submit(store, run, parameters)


@pytest.mark.parametrize("relation", [None, "fixed_initial"])
def test_multiple_candidates_cannot_erase_an_explicit_initial_hash_restriction(tmp_path, relation):
    text = "请为乙醇生成多个候选，所有计算必须保持初始XYZ的hash不变，计算极化率。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    parameters["goals"][0]["geometry_relation"] = relation
    updated = submit(store, run, parameters)
    assert store.load_request(updated).goals[0].conditions["geometry_relation"] == "fixed_initial"


def test_unrequested_optimization_is_not_normalized_away(tmp_path):
    store, _, run, reply, _, _ = retained(tmp_path, "H2", 2)
    goal = next(item for item in reply["parameters"]["goals"] if item["key"] == "rotated_static_polarizability")
    goal["geometry_relation"] = "optimized"
    with pytest.raises(ProposalError):
        submit(store, run, reply["parameters"])


@pytest.mark.parametrize("field,value,quote", [
    ("charge", 0, "中性"), ("multiplicity", 1, "单重态"),
    ("method", "HF", "RHF"), ("basis", "def2-SVP", "def2-SVP"),
    ("electronic_state", "RHF", "RHF"), ("environment", "gas_phase", "气相"),
    ("temperature_K", 298.15, "298.15"), ("standard_state", "1 atm", "1 atm"),
])
def test_value_basis_has_the_same_meaning_for_every_explicit_condition(tmp_path, field, value, quote):
    text = "请计算乙醇极化率，中性、单重态、气相，RHF/def2-SVP，温度298.15 K，标准态1 atm。"
    store, config, run = intake(tmp_path, text)
    parameters = normal(store, config, run)
    parameters["conditions"] = {
        name: {"source": "explicit", "value": item, "text_basis": word}
        for name, item, word in (
            ("charge", 0, "中性"), ("multiplicity", 1, "单重态"),
            ("method", "HF", "RHF"), ("basis", "def2-SVP", "def2-SVP"),
            ("electronic_state", "RHF", "RHF"), ("environment", "gas_phase", "气相"),
            ("temperature_K", 298.15, "298.15"), ("standard_state", "1 atm", "1 atm"))
    }
    parameters["conditions"][field]["value_basis"] = quote
    updated = submit(store, run, parameters)
    assert store.load_request(updated).condition_evidence[f"request.{field}"]["value_basis"] == quote
