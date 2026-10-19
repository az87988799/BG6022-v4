"""Developer-only independent oracle regressions, using pinned raw excerpts."""

import hashlib
import json
from pathlib import Path

import pytest

from tests.helpers.analytic_oracle import inspect_optfreq

FIXTURE = Path(__file__).parents[1] / "fixtures" / "native_analytic_hf"


def copies(tmp_path):
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    for name in ("stdout-excerpts.out", "job.hess", "job.xyz"):
        data = (FIXTURE / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == provenance["fixture_files"][name]["sha256"]
        (tmp_path / name).write_bytes(data)
    return [tmp_path / name for name in ("stdout-excerpts.out", "job.hess", "job.xyz")]


def test_independent_hessian_matrix_geometry_and_spectrum(tmp_path):
    result = inspect_optfreq(*copies(tmp_path))
    assert result["version"] == "developer-analytic-oracle-3"
    assert result["full_mode_count"] == 27 and result["vibrational_mode_count"] == 21
    assert result["product_qualification"] == "unverified"
    assert result["minimum_assessment"] == "consistent_local_harmonic_minimum"
    assert result["pairwise_distance_max_error_angstrom"] < 1e-5
    assert result["hessian_reconstructed_spectrum_max_error_cm_minus_1"] < .006


@pytest.mark.parametrize("case", ["frequency_sign", "frequency_count", "unit", "geometry",
                                  "convergence", "method", "basis", "matrix", "scaling", "response"])
def test_wrong_missing_or_inconsistent_scientific_evidence_is_rejected(tmp_path, case):
    paths = copies(tmp_path)
    target = paths[0]
    text = target.read_text()
    if case == "frequency_sign":
        text = text.replace("235.96 cm", "-235.96 cm")
    elif case == "frequency_count":
        text = text.replace("26:    4224.27 cm**-1", "missing mode")
    elif case == "unit":
        text = text.replace("cm**-1", "Hz")
    elif case == "geometry":
        target = paths[2]
        text = target.read_text().replace("-0.92084506702809", "-0.90084506702809")
    elif case == "convergence":
        text = text.replace("YES", "NO", 1)
    elif case == "method":
        text = text.replace("Hartree-Fock(GTOs)", "DFT")
    elif case == "basis":
        text = text.replace("basis: STO-3G", "basis: def2-SVP")
    elif case == "matrix":
        target = paths[1]
        text = target.read_text().replace("7.2670848884E-01", "9.2670848884E-01", 1)
    elif case == "scaling":
        text = text.replace("1.000000000", "0.900000000")
    else:
        text = text.replace("CP-SCF equations solved", "CP-SCF failed")
    assert text != target.read_text(), case
    target.write_text(text)
    with pytest.raises(ValueError):
        inspect_optfreq(*paths)
