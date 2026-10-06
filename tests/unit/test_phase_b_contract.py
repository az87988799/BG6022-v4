"""Offline checks of the frozen acceptance artifacts, not Agent capability tests."""

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests/fixtures/phase_b"
ACCEPTANCE = ROOT / "docs/acceptance/phase-b"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


@pytest.fixture(scope="module")
def contract():
    return read_json(FIXTURES / "cases.json")


@pytest.fixture(scope="module")
def profile():
    return read_json(ACCEPTANCE / "profile.json")


def case_by_id(contract, case_id):
    return next(case for case in contract["cases"] if case["id"] == case_id)


def variant_by_id(contract, case_id, variant_id):
    case = case_by_id(contract, case_id)
    return next(variant for variant in case["variants"] if variant["id"] == variant_id)


def effective_budget(case, variant):
    return case["budget"] | variant.get("budget_override", {})


def assert_repo_file_hash(path, expected_hash):
    source = (ROOT / path).resolve()
    assert source.is_relative_to(ROOT), path
    assert source.is_file(), path
    assert len(expected_hash) == 64
    assert hashlib.sha256(source.read_bytes()).hexdigest() == expected_hash, path


def test_all_blueprint_scenarios_have_unrun_positive_and_negative_evidence(contract):
    assert [case["id"] for case in contract["cases"]] == [
        f"V-{number:02d}" for number in range(1, 12)
    ]
    required = {
        "input", "goals", "preconditions", "allowed_actions", "minimum_evidence",
        "stop_conditions", "reference_sources", "budget", "variants",
    }
    variants = []
    for case in contract["cases"]:
        assert required <= case.keys()
        assert all(case[field] for field in required), case["id"]
        assert case["runtime_validation_status"] == "not_verified"
        assert "negative" in {variant["kind"] for variant in case["variants"]}
        ids = [variant["id"] for variant in case["variants"]]
        assert len(ids) == len(set(ids)), case["id"]
        for variant in case["variants"]:
            assert variant["evaluation_status"] == "not_run"
            assert variant["evaluation_repeats"] == 3
            assert variant["input"] and variant["expected"]
            assert variant["evidence_requirement"] in {
                "offline_fault_injection",
                "real_model_with_frozen_evidence",
                "joint_real_model_orca",
            }
            assert all(
                item["metric"] and item["operator"] in {"eq", "lte"}
                for item in variant["expected"]
            )
        variants.extend(case["variants"])
    assert len(variants) == 58
    assert {variant["kind"] for variant in variants} == {"positive", "negative"}


def test_frozen_input_bytes_and_sampling_selection_resolve(contract, profile):
    for source in contract["common"]["fixed_input_files"].values():
        assert_repo_file_hash(source["path"], source["sha256"])
    prompt = (ROOT / profile["model"]["prompt_path"]).resolve()
    assert prompt.is_relative_to(ROOT) and prompt.is_file()
    manifest = read_json(FIXTURES / "sampling-candidates.json")
    assert_repo_file_hash(manifest["source"]["fixture"], manifest["source"]["sha256"])
    candidates = {}
    for window in manifest["windows"]:
        for candidate in window["candidates"]:
            assert candidate["id"] not in candidates
            candidates[candidate["id"]] = candidate
            assert_repo_file_hash(candidate["path"], candidate["sha256"])
        initial = set(window["initial_candidate_ids"])
        optional = set(window["optional_candidate_ids"])
        assert len(initial) == 3 and len(optional) == 2 and initial.isdisjoint(optional)
        assert initial | optional == {candidate["id"] for candidate in window["candidates"]}
    assert len(candidates) == 15
    for side in ("left", "right"):
        variant = variant_by_id(contract, "V-05", side)
        initial = variant["input"]["initial_candidates"]
        optional = variant["input"]["allowed_additional_candidates"]
        assert set(initial + optional) <= candidates.keys()
        selected = next(
            item["value"] for item in variant["expected"]
            if item["metric"] == "model.selected_candidate"
        )
        assert selected in optional and selected not in initial


def test_every_variant_budget_respects_frozen_profile(contract, profile):
    mapping = {
        "orca_starts": "orca_starts",
        "extra_orca_starts": "extra_orca_starts",
        "attempts_per_logical_step": "attempts_per_logical_step",
        "native_postprocessing_starts": "postprocess_starts",
        "analysis_calls": "analysis_executions",
        "evidence_reads": "evidence_reads",
        "autonomous_plan_revisions": "plan_revisions",
        "model_http_requests": "model_http_requests",
        "model_tokens_total": "total_model_tokens",
        "decision_rounds": "decision_rounds",
        "run_seconds": "run_seconds",
    }
    for case in contract["cases"]:
        for variant in case["variants"]:
            budget = effective_budget(case, variant)
            for case_key, profile_key in mapping.items():
                assert 0 <= budget[case_key] <= profile["run_limits"][profile_key], (
                    case["id"], variant["id"], case_key,
                )
            assert budget["extra_orca_starts"] <= budget["orca_starts"]
            if variant["evidence_requirement"] == "real_model_with_frozen_evidence":
                assert budget["orca_starts"] == budget["native_postprocessing_starts"] == 0
                assert budget["model_http_requests"] == 4
                assert budget["model_tokens_total"] == 32000


def test_model_sampling_inputs_hide_grader_answers_and_keep_exact_geometry():
    manifest = read_json(FIXTURES / "sampling-candidates.json")
    user_messages = set()
    aliases = set()
    for window in manifest["windows"]:
        path = window["model_context_fixture"]
        assert_repo_file_hash(path, window["model_context_sha256"])
        context = read_json(ROOT / path)
        serialized = json.dumps(context, ensure_ascii=False).lower()
        assert not re.search(r"left|right|stop|expected|reference|energy_eh|sampling-", serialized)
        assert set(context) == {"source_id", "user_message", "conditions", "scan",
                                "registered_candidates"}
        assert context["source_id"] == window["model_window_id"]
        assert re.fullmatch(r"system_[0-9a-f]{12}", context["source_id"])
        assert context["source_id"] not in aliases
        aliases.add(context["source_id"])
        user_messages.add(context["user_message"])
        actual = {candidate["source_id"]: candidate
                  for candidate in context["registered_candidates"]}
        assert len(actual) == 5
        assert sum(candidate["required_initial"] for candidate in actual.values()) == 3
        assert set(actual) == {candidate["model_candidate_id"] for candidate in window["candidates"]}
        for candidate in window["candidates"]:
            visible = actual[candidate["model_candidate_id"]]
            assert set(visible) == {"source_id", "sha256", "required_initial", "atoms"}
            assert re.fullmatch(r"geom_[0-9a-f]{12}", visible["source_id"])
            assert visible["sha256"] == candidate["sha256"]
            assert visible["required_initial"] == candidate["required_initial"]
            lines = (ROOT / candidate["path"]).read_text(encoding="utf-8").splitlines()
            assert len(visible["atoms"]) == int(lines[0]) == 3
            for atom, line in zip(visible["atoms"], lines[2:], strict=True):
                element, *coordinates = line.split()
                assert atom["element"] == element
                assert atom["position_angstrom"] == [float(value) for value in coordinates]
    assert len(user_messages) == 1, "Window-specific user wording must not disclose its answer"


def test_science_allocations_count_unique_runs_not_reused_evidence(contract, profile):
    batch = contract["batch_budget"]
    allocation = batch["formal_allocations"]
    groups = {}
    for case in contract["cases"]:
        for variant in case["variants"]:
            group = variant["input"].get("formal_allocation")
            if group:
                assert group not in groups
                groups[group] = (case, variant)
    assert set(groups) == {item["id"] for item in allocation}
    for item in allocation:
        case, variant = groups[item["id"]]
        assert item["repeats"] == variant["evaluation_repeats"] == 3
        assert item["starts_per_repeat"] == effective_budget(case, variant)["orca_starts"]
        assert item["total"] == item["repeats"] * item["starts_per_repeat"]
    assert sum(item["total"] for item in allocation) == 48
    caps = batch["orca_starts"]
    assert sum(caps[key] for key in (
        "independent_reference", "formal_joint_evaluation", "development_and_reruns"
    )) == caps["total"] == profile["batch_limits"]["orca_starts"]["total"] == 96
    assert caps["independent_reference"] == profile["batch_limits"]["orca_starts"]["reference"]
    assert caps["formal_joint_evaluation"] == profile["batch_limits"]["orca_starts"]["formal"]
    assert caps["development_and_reruns"] == profile["batch_limits"]["orca_starts"]["development"]


def test_model_allocation_and_peak_fee_fit_same_batch(contract, profile):
    batch = contract["batch_budget"]
    model = batch["model_allocation"]
    fixed, joint = model["fixed_evidence"], model["joint"]
    selected = [
        (case, variant) for case in contract["cases"] for variant in case["variants"]
        if variant["evidence_requirement"] == "real_model_with_frozen_evidence"
    ]
    assert set(fixed["variant_ids"]) == {
        f"{case['id']}/{variant['id']}" for case, variant in selected
    }
    assert fixed["variants"] == len(selected) == 25
    assert fixed["runs"] == fixed["variants"] * fixed["repeats"] == 75
    assert fixed["http_requests"] == sum(
        variant["evaluation_repeats"] * effective_budget(case, variant)["model_http_requests"]
        for case, variant in selected
    )
    assert fixed["tokens"] == sum(
        variant["evaluation_repeats"] * effective_budget(case, variant)["model_tokens_total"]
        for case, variant in selected
    )
    assert joint["runs"] == sum(item["repeats"] for item in batch["formal_allocations"]) == 18
    assert joint["http_requests"] == joint["runs"] * joint["max_requests_per_run"]
    assert joint["tokens"] == joint["runs"] * joint["max_tokens_per_run"]
    assert joint["evidence_reuse_without_new_run"] == ["V-03/water-sp-via-other-closed-loop"]
    totals, remaining = model["formal_totals"], model["development_and_reruns_remaining"]
    assert totals["runs"] == fixed["runs"] + joint["runs"]
    for key in ("http_requests", "tokens"):
        assert totals[key] == fixed[key] + joint[key]
    assert totals["http_requests"] + remaining["http_requests"] == (
        batch["model_http_requests"]
    ) == profile["batch_limits"]["model_http_requests"] == 600
    assert totals["tokens"] + remaining["tokens"] == batch["model_tokens"] == (
        profile["batch_limits"]["model_total_tokens"]
    ) == 6000000

    pricing = read_json(ACCEPTANCE / "pricing-basis.json")
    rate = pricing["reservation_rates"]
    per_request = (
        Decimal(profile["run_limits"]["input_tokens_per_request"])
        * Decimal(rate["input_cache_miss_peak"])
        + Decimal(profile["run_limits"]["output_tokens_per_request"])
        * Decimal(rate["output_peak"])
    ) / Decimal(pricing["unit_tokens"])
    assert per_request == Decimal(model["request_cost_upper_bound_usd"])
    assert per_request * totals["http_requests"] == Decimal(totals["conservative_cost_usd"])
    assert per_request * remaining["http_requests"] == Decimal(
        remaining["conservative_request_limited_cost_usd"]
    )
    maximum = per_request * batch["model_http_requests"]
    assert maximum == Decimal(model["full_600_request_cost_upper_bound_usd"])
    assert maximum <= Decimal(profile["batch_limits"]["model_cost_usd"]) == (
        Decimal(batch["model_cost_usd"])
    )


@pytest.mark.parametrize(("case_id", "variant_id", "metric", "value"), [
    ("V-01", "ambiguous-pronoun", "orca_starts", 0),
    ("V-02", "free-energy-protected", "all_required_goals_satisfied", False),
    ("V-03", "handwritten-plan-with-explanation", "initial_planning_evidence.accepted", False),
    ("V-04", "repair-exhaustion", "attempt_2.scf_converged", False),
    ("V-04", "repair-exhaustion", "third_start.rejected", True),
    ("V-05", "left", "model.selected_candidate", "left-minus_half"),
    ("V-05", "right", "model.selected_candidate", "right-plus_half"),
    ("V-06", "sufficient-stop", "stop.reason", "goal_satisfied"),
    ("V-06", "sufficient-stop", "model.continue_proposal", False),
    ("V-07", "discover-and-read", "scientific_dipole_port.published", False),
    ("V-08", "missing-conditions", "historical_cost.fabricated", False),
    ("V-09", "different-system", "energy_comparison.science_passed", False),
    ("V-10", "json-missing", "native_postprocessing_starts", 0),
    ("V-11", "result-written-run-not-linked", "reservation.double_settled", False),
])
def test_frozen_counterexamples_cannot_be_weakened(contract, case_id, variant_id, metric, value):
    """Guard the scientific/authority boundaries future implementations must prove."""
    expected = variant_by_id(contract, case_id, variant_id)["expected"]
    assert {"metric": metric, "operator": "eq", "value": value} in expected


def test_profile_keeps_model_transport_and_science_permissions_separate(profile):
    gates = {gate["mark"]: gate for gate in profile["test_gates"]["planned_B-04"]}
    assert gates["offline"]["flags"] == []
    assert gates["offline"]["network"] == "deny"
    assert gates["model"]["flags"] == ["--live-model"]
    assert gates["model"]["science"].startswith("deny")
    assert gates["live"]["flags"] == ["--live-orca"]
    assert gates["live"]["network"] == "deny"
    assert set(gates["e2e"]["flags"]) == {"--live-model", "--live-orca"}
    assert profile["model"]["sdk_max_retries"] == 0
    assert profile["model"]["credential_environment_variable"] == "DEEPSEEK_API_KEY"
    assert profile["run_limits"]["postprocess_starts"] == 0
    assert profile["resources"]["environment_concurrency"] == 1


def test_every_case_reference_resolves_without_promoting_historical_evidence(contract):
    review = read_json(ACCEPTANCE / "reference-review.json")
    index = review["source_index"]
    for case in contract["cases"]:
        for reference in case["reference_sources"]:
            assert reference["review"] == "docs/acceptance/phase-b/reference-review.json"
            assert reference["source_id"] in index, (case["id"], reference["source_id"])
    assert review["uses_product_science_parser_for_expected"] is False
    assert review["human_expert_approval"] is False
    for name in ("phase-a/water_sp", "phase-a/methane_opt"):
        assert index[name]["historical_rules_not_upgraded"] is True
    for name in ("phase-a/real_water_sp", "phase-a/real_water_scf_limit"):
        provenance = index[name]["provenance"]
        assert_repo_file_hash(provenance["path"], provenance["sha256"])
        fixture_dir = Path(provenance["path"]).parent
        for filename, digest in index[name]["files_sha256"].items():
            assert_repo_file_hash(fixture_dir / filename, digest)
    for record in index["phase-a/r03-recovery"]["records"]:
        assert_repo_file_hash(record["path"], record["sha256"])


def test_all_independent_scientific_inputs_and_stdout_are_portable_and_hash_bound():
    review = read_json(ACCEPTANCE / "reference-review.json")
    manifest = read_json(FIXTURES / "sampling-candidates.json")
    records = review["sampling"]["raw_records"]
    expected = {candidate["id"]: candidate
                for window in manifest["windows"] for candidate in window["candidates"]}
    assert records.keys() == expected.keys()
    for candidate_id, record in records.items():
        assert record["reference_id"] == expected[candidate_id]["reference_id"]
        assert record["geometry"]["sha256"] == expected[candidate_id]["sha256"]
        for field in ("raw_stdout", "input", "geometry"):
            assert_repo_file_hash(record[field]["path"], record[field]["sha256"])
        for field in ("run_id", "step_id", "attempt_id", "result_id", "artifact_ids"):
            assert record[field]
        assert len(record["result"]["sha256"]) == 64
        assert record["raw_observation"]["uses_product_parser"] is False
        assert record["product_check_versions"] == ["orca-hf-2"]
        lines = (ROOT / record["raw_stdout"]["path"]).read_bytes().split(b"\n")
        observation = record["raw_observation"]
        assert b"FINAL SINGLE POINT ENERGY" in lines[observation["energy_line"] - 1]
        assert b"SCF CONVERGED AFTER" in lines[observation["scf_converged_line"] - 1]
    failure = review["source_index"]["phase-b/scf-maxiter-2"]
    for field in ("raw_stdout", "input", "geometry"):
        assert_repo_file_hash(failure[field]["path"], failure[field]["sha256"])
    assert failure["raw_observation"]["status"] == "scf_not_converged"
    assert failure["raw_observation"]["energy_eh"] is None
    assert failure["product_energy_qualified"] is False
    assert failure["resources_passed"] is True
    lines = (ROOT / failure["raw_stdout"]["path"]).read_bytes().split(b"\n")
    observation = failure["raw_observation"]
    assert b"SCF NOT CONVERGED AFTER" in lines[observation["failure_line"] - 1]
    for iteration in observation["iterations"]:
        assert int(lines[iteration["line"] - 1].split()[0]) == iteration["number"]


def test_energy_comparison_has_two_exact_sources_and_independent_signed_expected(contract):
    review = read_json(ACCEPTANCE / "reference-review.json")
    pair = review["source_index"]["phase-b/compatible-water-pair"]
    case = case_by_id(contract, "V-09")
    energies = {}
    for role in ("A", "B"):
        binding = case["input"]["source_bindings"][f"qualified_water_geometry_{role}"]
        source = pair[role]
        assert binding["source_index"] == f"phase-b/compatible-water-pair/{role}"
        assert binding["reference_id"] == source["reference_id"]
        assert source["product_energy_qualified"] is True
        assert source["product_check_versions"] == ["orca-hf-2"]
        # Read only the frozen raw stdout, never the tested analysis Tool/Result value.
        text = (ROOT / source["raw_stdout"]["path"]).read_bytes().decode("utf-8")
        values = re.findall(r"FINAL SINGLE POINT ENERGY\s+([-+0-9.Ee]+)", text)
        assert len(values) == 1
        energies[role] = Decimal(values[0])
        assert energies[role] == Decimal(str(source["raw_observation"]["energy_eh"]))
    assert pair["A"]["geometry"]["sha256"] != pair["B"]["geometry"]["sha256"]
    assert pair["formula"] == "E(B) - E(A)" and pair["unit"] == "Eh"
    assert abs(energies["B"] - energies["A"] - Decimal(str(pair["expected_difference_eh"]))) <= (
        Decimal(str(pair["absolute_recompute_tolerance_eh"]))
    )
    assert pair["conditions"]["required_source_rule"] == "orca-hf-2"
    assert pair["analysis_tool_status"] == "not_implemented_B-07"
