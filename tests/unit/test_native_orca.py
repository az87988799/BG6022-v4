"""Native input admission/staging tests; no scientific or OS isolation claims."""

import hashlib
import json
import os

import pytest

from orca_agent.models import (
    CodeRef,
    InputManifest,
    InputManifestEntry,
    NativeOrcaParameters,
    ResourceLimits,
)
from orca_agent.orca.native import prepare_native_input, read_pinned_input, validate_native_input

XYZ = b"2\ninitial hydrogen\nH 0 0 0\nH 0 0 0.74\n"
INPUT = "! PBE0 def2-SVP TightSCF NoAutoStart\n%maxcore 192\n* xyzfile 0 1 geometry.xyz\n"


def digest(value):
    return hashlib.sha256(value).hexdigest()


def parameters(tmp_path, text=INPUT):
    (tmp_path / "source.inp").write_bytes(text.encode())
    (tmp_path / "source.xyz").write_bytes(XYZ)
    refs = {"source": tmp_path / "source.inp", "geometry": tmp_path / "source.xyz"}
    params = NativeOrcaParameters(
        input=CodeRef(artifact_id="source", sha256=digest(text.encode()), language="orca"),
        inputs=InputManifest(entries=[InputManifestEntry(
            artifact_id="geometry", sha256=digest(XYZ), target_path="geometry.xyz", purpose="geometry")]))
    return params, refs.__getitem__


def validate(text=INPUT, dependencies=None, **kwargs):
    return validate_native_input(text, dependencies if dependencies is not None else {"geometry.xyz": XYZ},
                                 ResourceLimits(), **kwargs)


def test_native_input_staging_pins_exact_code_and_dependent_bytes(tmp_path):
    params, resolve = parameters(tmp_path)
    prepared = prepare_native_input(tmp_path / "attempt", params, resolve, ResourceLimits())
    assert (tmp_path / "attempt/job.inp").read_text() == INPUT
    assert (tmp_path / "attempt/geometry.xyz").read_bytes() == XYZ
    assert prepared["input_sha256"] == params.input.sha256
    assert prepared["dependencies"]["geometry.xyz"]["sha256"] == digest(XYZ)
    assert prepared["resources"]["cores"] == 1
    assert prepared["execution_impact"]["orca_starts"] == 1
    assert prepared["execution_impact"]["electronic_evaluation_upper_bound"] == 1
    assert prepared["actual_conditions"]["scientific_validation"] == "unverified"
    persisted = json.loads((tmp_path / "attempt/input-manifest.json").read_text())
    assert persisted["input_artifact"] == params.input.model_dump(mode="json")
    with pytest.raises(FileExistsError):
        prepare_native_input(tmp_path / "attempt", params, resolve, ResourceLimits())


@pytest.mark.parametrize("target", ["source.inp", "source.xyz"])
def test_changed_artifact_rejected_before_attempt_write(tmp_path, target):
    params, resolve = parameters(tmp_path)
    (tmp_path / target).write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        prepare_native_input(tmp_path / "attempt", params, resolve, ResourceLimits())
    assert not (tmp_path / "attempt").exists()


def test_arbitrary_method_and_basis_labels_do_not_require_property_registration():
    result = validate(INPUT.replace("PBE0 def2-SVP", "B97M-V def2-TZVPPD"))
    assert result["actual_conditions"]["input_keywords"][:2] == ["B97M-V", "def2-TZVPPD"]
    # Unknown scientific labels are ORCA's syntax responsibility; no scientific
    # validity or success is inferred by this execution admission grammar.
    assert validate(INPUT.replace("PBE0", "UnregisteredFunctional"))["actual_conditions"]["scientific_validation"] == "unverified"


def test_resource_values_are_derived_from_actual_input():
    result = validate(INPUT.replace("%maxcore 192", "%maxcore 128\n%pal nprocs=2 end"))
    assert result["resources"] == {"cores": 2, "memory_mb": 1024, "maxcore_mb": 128, "timeout_seconds": 900}


@pytest.mark.parametrize("directive, reason", [
    ("%pal nprocs 5 end", "resource envelope"),
    ("%pal nprocs 2 nprocs 3 end", "duplicate"),
    ("%pal nprocs 2 end\n%pal nprocs 3 end", "duplicate"),
    ("%pal nprocs 2 nprocs_group 2 end", "unsupported"),
    ("%maxcore 193", "duplicate"),
    ("%base \"../outside\"", "unsupported"),
    ("%method ProgExt \"C:/Windows/System32/cmd.exe\" end", "unsupported"),
    ("%compound \"script.cmp\"", "unsupported"),
    ("$new_job", "multi-job"),
    ("%geom Scan B 0 1 = 1,2,5 end end", "unsupported"),
    ("%scf maxiter 5 # ignored # progext payload", "closed comments"),
    ("%scf maxiter 5 end extra", "unterminated"),
    ("%scf guess moread end", "unsupported"),
    ("%output JSONGBWFile true end", "conversion"),
    ("%freq restart true end", "unsupported"),
    ("%coords CTyp xyz CFile \"../secret\" end", "unsupported"),
])
def test_unmanaged_or_ambiguous_directive_rejected(directive, reason):
    with pytest.raises(ValueError, match=reason):
        validate(INPUT + directive + "\n")


@pytest.mark.parametrize("keyword", [
    "ExtOpt", "NumFreq", "AnFreq", "Freq", "NumGrad", "EnGrad", "Opt", "OptTS", "TightOpt",
    "GOAT", "NEB-TS", "MD", "Docker", "Solvator", "QMMM", "DMRG", "MRCC", "NBO", "NMScan",
    "IRC", "AutoStart", "MORead", "ReadInts", "PAL4", "PropertiesOnly", "VPT2", "XTB2", "ESD",
    "A;ExtOpt", "A>outside", "A|cmd", "A$run", "A%pal", "C:/secret", "../../secret",
])
def test_external_unbounded_and_implicit_dependency_keywords_rejected(keyword):
    with pytest.raises(ValueError):
        validate(INPUT.replace("PBE0", keyword))


@pytest.mark.parametrize("name", ["../geometry.xyz", "C:/geometry.xyz", "a/geometry.xyz", "job.xyz", "geometry.xyz:ads"])
def test_native_path_redirection_rejected(name):
    with pytest.raises(ValueError):
        validate(INPUT.replace("geometry.xyz", name), {name: XYZ})


def test_missing_and_unused_dependencies_rejected():
    with pytest.raises(ValueError, match="absent"):
        validate(dependencies={})
    with pytest.raises(ValueError, match="unreferenced"):
        validate(dependencies={"geometry.xyz": XYZ, "unused.xyz": XYZ})


def test_multi_frame_xyz_cannot_hide_multiple_calculations():
    with pytest.raises(ValueError, match="exactly one frame"):
        validate(dependencies={"geometry.xyz": XYZ + XYZ})


def test_explicit_checkpoint_is_pinned_and_noautostart_remains_required():
    value = validate(INPUT + '%moinp "initial.gbw"\n! MORead\n',
                     {"geometry.xyz": XYZ, "initial.gbw": b"not a scientific fixture"})
    assert "initial.gbw" in value["dependencies"]
    with pytest.raises(ValueError, match="NoAutoStart"):
        validate(INPUT.replace("NoAutoStart", ""))


def test_inline_geometry_and_scalar_property_options():
    text = INPUT.replace("* xyzfile 0 1 geometry.xyz", "* xyz 0 1\nH 0 0 0\nH 0 0 0.74\n*")
    value = validate(text + "%tddft nroots 3 tda true end\n%cpcm epsilon 80 refrac 1.3 end\n", {})
    assert value["actual_conditions"]["atom_count"] == 2
    assert value["actual_conditions"]["input_blocks"]["tddft"]["nroots"] == "3"
    with pytest.raises(ValueError, match="multiple coordinate"):
        validate(text + "* xyz 0 1\nH 0 0 0\n*", {})


def test_only_legacy_bounded_optimization_is_admitted():
    text = INPUT + "! TightOpt\n%geom maxiter 5 enforcestrictconvergence true end\n"
    with pytest.raises(ValueError, match="optimization"):
        validate(text)
    result = validate(text, legacy_optimization=True)
    assert result["execution_impact"]["electronic_evaluation_upper_bound"] == 6


def test_raw_artifact_hardlink_rejected(tmp_path):
    original = tmp_path / "original"
    original.write_bytes(XYZ)
    os.link(original, tmp_path / "linked")
    with pytest.raises(ValueError, match="hard-linked"):
        read_pinned_input(original, digest(XYZ), limit=1024)


def test_stale_working_directory_rejected(tmp_path):
    params, resolve = parameters(tmp_path)
    directory = tmp_path / "attempt"
    directory.mkdir()
    (directory / "preloaded.dll").write_bytes(b"unknown")
    with pytest.raises(ValueError, match="fresh attempt"):
        prepare_native_input(directory, params, resolve, ResourceLimits())
    assert not (directory / "job.inp").exists()


def test_library_payload_cannot_enter_manifest(tmp_path):
    params, resolve = parameters(tmp_path)
    params.inputs.entries[0].target_path = "library.dll"
    with pytest.raises(ValueError, match="XYZ and GBW"):
        prepare_native_input(tmp_path / "attempt", params, resolve, ResourceLimits())


@pytest.mark.parametrize("text", [INPUT.replace("%maxcore 192\n", ""), INPUT.replace("192", "0"),
                                 INPUT.replace("192", "193"), INPUT + "\x00", INPUT + "é"])
def test_missing_resource_or_non_ascii_syntax_rejected(text):
    with pytest.raises(ValueError):
        validate(text)
