"""Native grammar disclosure and exact live027 offline admission controls."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from orca_agent.models import NativeInputPolicy, ResourceLimits
from orca_agent.orca.native import validate_native_input
from orca_agent.orca.native_profile import BLOCK_FIELDS, profile_contract

FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live027-native-output-flag.json"
BOUND_FIXTURE = Path(__file__).parents[1] / "fixtures/code_contracts/live038-missing-response-bound.json"


def case():
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == (
        "9511081a0cbe6c752ca60eb55d3eb92e7c184aa7a0fc76309f23abdd5dc7bd38")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    files = {"job.inp": data["input"], **data["dependencies"]}
    for entry in data["input_manifest"]:
        assert hashlib.sha256(files[entry["target_path"]].encode()).hexdigest() == entry["sha256"]
    return data


def admit(data, text):
    return validate_native_input(text, {name: text.encode() for name, text in data["dependencies"].items()},
        ResourceLimits.model_validate(data["resources"]),
        policy=NativeInputPolicy.model_validate(data["native_input_policy"]), binding=data["binding"])


def test_actual_live027_bare_output_flag_is_still_rejected_without_changing_evidence():
    data = case()
    before = copy.deepcopy(data)
    with pytest.raises(ValueError, match="%output requires admitted scalar key/value pairs; no nested blocks"):
        admit(data, data["input"])
    assert data == before


def test_synthetic_true_scalar_only_correction_passes_existing_admission_not_execution():
    data = case()
    before = copy.deepcopy(data)
    assert data["input"].count("  jsonpropfile\n") == 1
    text = data["input"].replace("  jsonpropfile\n", "  jsonpropfile true\n")
    result = admit(data, text)
    assert text.replace("  jsonpropfile true\n", "  jsonpropfile\n") == data["input"]
    assert result["actual_conditions"]["input_blocks"]["output"] == {"jsonpropfile": "true"}
    assert result["actual_conditions"]["atom_count"] == 9
    assert result["actual_conditions"]["run_type"] == "optimization_and_analytic_frequencies"
    assert result["actual_conditions"]["scientific_validation"] == "unverified"
    assert result["execution_impact"]["internal_work_units"] == 157
    assert data == before
    # This is an in-memory positive control, not a model repair, modified
    # Artifact, ORCA launch, parsed result or scientific validation.


@pytest.mark.parametrize("value", ["true", "false", "0", "1"])
def test_disclosed_optional_output_values_match_existing_admission(value):
    data = case()
    contract = profile_contract(NativeInputPolicy.model_validate(data["native_input_policy"]))
    output = contract["optional_blocks"]["output"]
    assert output["required"] is False
    assert set(output["fields"]) == BLOCK_FIELDS["output"]
    assert value in output["fields"]["jsonpropfile"]
    text = data["input"].replace("%output\n  jsonpropfile\nend",
        output["example"].replace("true", value))
    result = admit(data, text)
    assert result["actual_conditions"]["input_blocks"]["output"] == {"jsonpropfile": value}
    assert "no bare flags or nested blocks" in contract["block_syntax"]


def test_disclosed_output_block_is_optional_and_does_not_create_a_requirement():
    data = case()
    text = data["input"].replace("%output\n  jsonpropfile\nend\n", "")
    result = admit(data, text)
    assert "output" not in result["actual_conditions"]["input_blocks"]


@pytest.mark.parametrize("replacement", ["jsonpropfile yes", "jsonpropfile true jsonpropfile false",
                                         "jsonpropfile true\n  %scf MaxIter 1 end"])
def test_guidance_does_not_relax_boolean_duplicate_or_nested_block_rejections(replacement):
    data = case()
    before = copy.deepcopy(data)
    with pytest.raises(ValueError):
        admit(data, data["input"].replace("jsonpropfile\n", replacement + "\n"))
    assert data == before


def bound_case():
    assert hashlib.sha256(BOUND_FIXTURE.read_bytes()).hexdigest() == (
        "3f93d5a0841ef0ee73464f7977acff49abf40ad57411583cb706bda27a5745a8")
    data = json.loads(BOUND_FIXTURE.read_text(encoding="utf-8"))
    assert hashlib.sha256(data["input"].encode()).hexdigest() == data["input_source"]["sha256"]
    assert hashlib.sha256(data["dependencies"]["prepared.xyz"].encode()).hexdigest() == (
        data["geometry_source"]["sha256"])
    return data


def with_response_bound(data, value=128):
    return data["input"].replace("* xyzfile", f"%method Z_MaxIter {value} end\r\n* xyzfile")


def test_actual_live038_omitted_response_bound_still_rejects_and_old_guidance_was_visible():
    data = bound_case()
    before = copy.deepcopy(data)
    assert "Z_MaxIter" not in data["input"]
    assert all("Z_MaxIter <bound>" in row["visible_required_block_fragment"]
        for row in data["old_guidance_visible_in_actual_requests"])
    with pytest.raises(ValueError, match="explicit %method Z_MaxIter exceeds or omits the frozen bound"):
        admit(data, data["input"])
    # The fixture hash pins the historical source identity. Current admission
    # may evolve; the actual missing-bound rejection above remains mandatory.
    assert data == before


def test_only_synthetic_missing_method_block_addition_passes_existing_admission():
    data = bound_case()
    before = copy.deepcopy(data)
    text = with_response_bound(data)
    assert text.replace("%method Z_MaxIter 128 end\r\n", "") == data["input"]
    result = admit(data, text)
    assert result["actual_conditions"]["input_blocks"]["method"] == {"z_maxiter": "128"}
    assert result["execution_impact"]["response_maxiter"] == 128
    assert result["execution_impact"]["internal_work_units"] == 157
    assert result["actual_conditions"]["scientific_validation"] == "unverified"
    assert data == before  # No file, model repair, SDK or actual ORCA execution.


@pytest.mark.parametrize("block,field,policy_field,maximum", [
    ("scf", "MaxIter", "max_scf_iterations", 31),
    ("geom", "MaxIter", "max_geom_iterations", 5),
    ("method", "Z_MaxIter", "max_response_iterations", 17),
])
def test_required_iteration_guidance_tracks_frozen_policy_and_actual_bound(block, field, policy_field, maximum):
    data = bound_case()
    original_maximum = data["native_input_policy"][policy_field]
    data["native_input_policy"][policy_field] = maximum
    policy = NativeInputPolicy.model_validate(data["native_input_policy"])
    text = with_response_bound(data).replace(f"%{block} {field} {original_maximum}", f"%{block} {field} {maximum}")
    contract = profile_contract(policy)
    guidance = contract["required_blocks"][block]
    assert f"%{block} {field} <integer 1..{maximum}>" in guidance
    assert "required" in guidance
    if block in {"geom", "method"}:
        assert "required for Opt+Freq; forbidden for SP" in guidance
    result = admit(data, text)
    assert result["actual_conditions"]["input_blocks"][block][field.lower()] == str(maximum)
    for invalid in (0, maximum + 1):
        with pytest.raises(ValueError):
            admit(data, text.replace(f"%{block} {field} {maximum}", f"%{block} {field} {invalid}"))


def test_single_point_prohibits_response_block_instead_of_making_it_unconditionally_required():
    data = bound_case()
    text = with_response_bound(data).replace(" Opt Freq", " SP").replace(
        "%geom MaxIter 128 EnforceStrictConvergence true end\r\n", "")
    with pytest.raises(ValueError, match="single point cannot carry optimization/response control blocks"):
        admit(data, text)
    result = admit(data, text.replace("%method Z_MaxIter 128 end\r\n", ""))
    assert result["actual_conditions"]["run_type"] == "single_point"
