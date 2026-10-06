import json
from pathlib import Path

import pytest

from orca_agent.models import CalculationParameters, InputRef, PermissionSnapshot, Step, fingerprint
from orca_agent.tools.registry import (
    EvidenceFieldParameters,
    EvidenceListParameters,
    EvidenceTextParameters,
    SinglePointParameters,
    catalog,
    dispatch_evidence,
    get_tool,
    validate_geometry,
    validate_parameters,
)

WATER = "3\nWater coordinates, angstrom\nO 0 0 0\nH 0 0.757 0.587\nH 0 -0.757 0.587\n"


def test_catalog_schema_and_execution_share_one_model():
    models = {"orca.sp": SinglePointParameters, "orca.opt": CalculationParameters}
    for tool in catalog():
        if tool["name"].startswith("orca."):
            assert tool["parameter_schema"] == models[tool["name"]].model_json_schema()
            assert "execute_orca" in tool["effects"]
    assert get_tool("orca.sp").output_ports == ["energy"]
    assert get_tool("orca.opt").output_ports == ["energy", "optimized_geometry"]
    with pytest.raises(ValueError, match="unregistered"):
        get_tool("shell.run")


@pytest.mark.parametrize("name,limit", [("orca.sp", 300), ("orca.opt", 900)])
def test_calculation_deadline_schema_matches_registered_validation(name, limit):
    field = get_tool(name).parameter_schema["properties"]["timeout_seconds"]
    assert field["maximum"] == limit
    assert field["exclusiveMinimum"] == 0
    assert field["default"] == 300
    assert validate_parameters(name, {"timeout_seconds": limit}).timeout_seconds == limit
    for invalid in (0, limit + 0.001, float("inf")):
        with pytest.raises(ValueError, match="timeout_seconds"):
            validate_parameters(name, {"timeout_seconds": invalid})


def test_single_point_revalidates_general_calculation_parameter_objects():
    with pytest.raises(ValueError, match="300"):
        validate_parameters("orca.sp", CalculationParameters(timeout_seconds=900))
    # The inherited resource combination check remains active on the subtype.
    with pytest.raises(ValueError, match="memory"):
        validate_parameters("orca.sp", {"memory_mb": 512, "cores": 4})


@pytest.mark.parametrize("name", ["orca.sp", "orca.opt"])
def test_calculation_step_defaults_and_serialized_identity_remain_compatible(name):
    parameters = validate_parameters(name, {})
    assert isinstance(parameters, CalculationParameters)
    assert parameters.model_dump() == CalculationParameters().model_dump()
    assert fingerprint(parameters) == fingerprint(CalculationParameters())
    step = Step(id="calculation", logical_id="calculation", tool=name,
                geometry=InputRef(artifact_id="geometry"), parameters=parameters)
    stored = step.model_dump_json()
    loaded = Step.model_validate_json(stored)
    assert loaded.model_dump_json() == stored
    assert fingerprint(loaded) == fingerprint(step)
    assert type(loaded.parameters) is type(parameters)


_FIXTURES = Path(__file__).parents[1] / "fixtures" / "phase_a"


@pytest.mark.parametrize("path", sorted(_FIXTURES.glob("*/request.json")),
                         ids=lambda path: path.parent.name)
def test_historical_request_parameters_preserve_serialized_values(path):
    request = json.loads(path.read_text(encoding="utf-8"))
    for step in request["steps"]:
        actual = validate_parameters(step["tool"], step["parameters"])
        legacy = CalculationParameters.model_validate(step["parameters"])
        assert actual.model_dump(mode="json") == legacy.model_dump(mode="json")
        assert fingerprint(actual) == fingerprint(legacy)


@pytest.mark.parametrize("path", sorted(_FIXTURES.glob("*/input-manifest.json")),
                         ids=lambda path: path.parent.name)
def test_historical_input_manifest_parameters_preserve_serialized_values(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    actual = validate_parameters(manifest["tool"], manifest["parameters"])
    assert actual.model_dump(mode="json") == manifest["parameters"]
    assert fingerprint(actual) == fingerprint(manifest["parameters"])


def test_catalog_copy_cannot_mutate_the_runtime_contract():
    get_tool("orca.sp").output_ports.append("optimized_geometry")
    assert get_tool("orca.sp").output_ports == ["energy"]


@pytest.mark.parametrize("name,model", [
    ("evidence.list", EvidenceListParameters), ("evidence.text", EvidenceTextParameters),
    ("evidence.field", EvidenceFieldParameters),
])
def test_readonly_tools_share_schema_and_never_declare_scientific_ports(name, model):
    tool = get_tool(name)
    assert tool.parameter_schema == model.model_json_schema()
    assert tool.output_ports == []
    assert tool.observation_outputs
    assert tool.effects == ["read_registered_artifact"]
    assert tool.implementation.startswith("orca_agent.tools.evidence.")
    with pytest.raises(ValueError):
        Step(id="inspect", logical_id="inspect", tool=name, geometry=InputRef(artifact_id="g"))
    assert PermissionSnapshot(allowed_tools=[name]).scientific_execution is False
    with pytest.raises(ValueError):
        validate_parameters(name, {})


def test_evidence_dispatch_cannot_be_used_as_an_execution_channel():
    with pytest.raises(ValueError, match="cannot execute scientific"):
        dispatch_evidence(None, "orca.sp", {})
    with pytest.raises(ValueError, match="unregistered"):
        dispatch_evidence(None, "os.system", {"command": "unsafe"})
    with pytest.raises(ValueError):
        dispatch_evidence(None, "evidence.text", {"artifact_id": "a", "implementation": "os.system"})


def test_geometry_is_typed_and_comment_is_data():
    atoms = validate_geometry(WATER.replace("Water coordinates, angstrom", "run arbitrary command"),
                              CalculationParameters())
    assert len(atoms) == 3
    assert atoms[0] == ("O", 0.0, 0.0, 0.0)


@pytest.mark.parametrize("xyz", [
    "", "0\nempty\n", "2\ncount mismatch\nO 0 0 0\n",
    "1\nunsupported\nFe 0 0 0\n", "1\nodd electrons\nH 0 0 0\n",
    "2\nnonfinite\nH NaN 0 0\nH 0 0 1\n",
    "2\nnonfinite\nH inf 0 0\nH 0 0 1\n",
    "2\ncoincident\nH 0 0 0\nH 0 0 0\n",
    "2\nextra input\nH 0 0 0\nH 0 0 1 extra\n",
    "2\nextra lines\nH 0 0 0\nH 0 0 1\n! HF\n",
])
def test_geometry_rejects_illegal_input_before_execution(xyz):
    with pytest.raises(ValueError):
        validate_geometry(xyz, CalculationParameters())


def test_only_frozen_water_and_methane_compositions_are_admitted():
    methane = "5\nCH4\nC 0 0 0\nH 0.6 0.6 0.6\nH -0.6 -0.6 0.6\nH -0.6 0.6 -0.6\nH 0.6 -0.6 -0.6\n"
    assert len(validate_geometry(methane, CalculationParameters())) == 5
    peroxide = "4\nH2O2\nO 0 0 0\nO 0 0 1.4\nH 0 1 0\nH 0 1 1.4\n"
    with pytest.raises(ValueError, match="H2O and CH4"):
        validate_geometry(peroxide, CalculationParameters())
