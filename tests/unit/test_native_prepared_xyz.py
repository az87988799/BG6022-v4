"""Real archived OPI bytes through offline preparation/native admission only."""

import base64
import hashlib
import json
from pathlib import Path

import pytest

from orca_agent.models import ResourceLimits
from orca_agent.orca.native import validate_native_input
from orca_agent.orca.native_profile import admit_profile
from orca_agent.tools.structure import _general_graph, validate_prepared_geometry
from tests.unit import test_native_analytic as native_case

FIXTURE = Path(__file__).parents[1] / "fixtures/structure/opi-2.0-ethanol-managed-xyz.json"


def opi_bytes():
    record = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data = base64.b64decode(record["base64"], validate=True)
    assert len(data) == record["size"] == 859
    assert hashlib.sha256(data).hexdigest() == record["sha256"]
    assert data.endswith(b"\n\n")
    return data


def bound(data):
    return {**native_case.binding(), "geometry_sha256": native_case.sha(data)}


@pytest.mark.parametrize("trailing", [b"", b" \t\n\r\n"])
def test_real_opi_prepared_bytes_pass_native_staging_with_exact_bindings(tmp_path, monkeypatch, trailing):
    data = opi_bytes() + trailing
    graph = _general_graph("CCO")
    validated = validate_prepared_geometry(data.decode("ascii"), "ethanol", graph["elements"], graph=graph)
    assert validated["atom_mapping"] == list(range(9))
    monkeypatch.setattr(native_case, "XYZ", data)
    store, run, step, prepared, params = native_case.setup_native(tmp_path, monkeypatch)
    digest = native_case.sha(data)
    geometry_id = store.load_request(run).geometry_artifact_id
    assert store.artifact_path(geometry_id).read_bytes() == data
    assert (tmp_path / "staged/initial.xyz").read_bytes() == data
    assert params.inputs.entries[0].sha256 == digest
    assert prepared["input_manifest"]["entries"][0]["sha256"] == digest
    assert prepared["input_binding"]["geometry_sha256"] == digest
    assert prepared["input_binding"]["step_id"] == step.id
    assert prepared["actual_conditions"]["input_geometry_sha256"] == digest
    assert prepared["actual_conditions"]["atom_count"] == 9
    assert prepared["actual_conditions"]["scientific_validation"] == "unverified"
    assert not run.attempts and run.usage.orca_starts_actual == 0


def test_profile_uses_only_the_already_validated_number_of_atom_rows():
    data = opi_bytes()
    trimmed = data.rstrip() + b"\n"
    launch = native_case.admit(data=trimmed, bound=bound(trimmed))
    result = admit_profile(launch, {"initial.xyz": data}, "initial.xyz", native_case.POLICY, bound(data))
    assert result["actual_conditions"]["atom_count"] == 9
    assert result["actual_conditions"]["geometry_sha256"] == native_case.sha(data)


def test_legacy_single_point_admission_accepts_same_serializer_bytes():
    text = "! HF STO-3G NoAutoStart\n%maxcore 192\n* xyzfile 0 1 initial.xyz\n"
    result = validate_native_input(text, {"initial.xyz": opi_bytes()}, ResourceLimits())
    assert result["actual_conditions"]["atom_count"] == 9


@pytest.mark.parametrize("malformation", ["interior_blank", "extra_atom", "extra_frame", "nan"])
@pytest.mark.parametrize("new_profile", [False, True])
def test_trailing_blank_compatibility_does_not_relax_geometry_validation(malformation, new_profile):
    data = opi_bytes()
    if malformation == "interior_blank":
        rows = data.splitlines(keepends=True)
        rows.insert(4, b"\n")
        data = b"".join(rows)
    elif malformation == "extra_atom":
        data += b"H 0 0 0\n"
    elif malformation == "extra_frame":
        data += data
    else:
        data = data.replace(b"-0.9534551842756148", b"NaN", 1)
    text = (native_case.INPUT if new_profile else
            "! HF STO-3G NoAutoStart\n%maxcore 192\n* xyzfile 0 1 initial.xyz\n")
    with pytest.raises(ValueError, match="one frame|finite Cartesian"):
        validate_native_input(text, {"initial.xyz": data}, ResourceLimits(),
                              policy=native_case.POLICY if new_profile else None, binding=bound(data))


def test_normalizing_prepared_bytes_still_breaks_exact_request_hash_binding():
    data = opi_bytes()
    with pytest.raises(ValueError, match="xyzfile bytes differ"):
        native_case.admit(data=data.rstrip() + b"\n", bound=bound(data))
