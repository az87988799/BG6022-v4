"""Developer-only DFT output checks using pinned autonomous-run raw excerpts."""
import hashlib
import json
import re
from pathlib import Path

import pytest

from tests.helpers import analytic_oracle as ORACLE

FIXTURES = Path(__file__).parents[1] / "fixtures"
DFT = FIXTURES / "native_analytic_b3lyp"
HF = FIXTURES / "native_analytic_hf"


def paths(tmp_path, source=DFT):
    names = ("stdout-excerpts.out", "job.hess", "job.xyz")
    provenance = json.loads((source / "provenance.json").read_text(encoding="utf-8"))
    for name in names:
        data = (source / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == provenance["fixture_files"][name]["sha256"]
        (tmp_path / name).write_bytes(data)
    return [tmp_path / name for name in names]


def test_full_actual_dft_and_hf_outputs(tmp_path):
    result = ORACLE.inspect_optfreq(*paths(tmp_path), method="B3LYP", basis="def2-SVP")
    assert result["actual_conditions"]["scf_settings_blocks"] == 2
    assert result["actual_conditions"]["electrons"] == 26
    assert result["full_mode_count"] == 27
    assert result["minimum_assessment"] == "consistent_local_harmonic_minimum"
    assert result["product_qualification"] == "unverified"
    assert result["hessian_reconstructed_spectrum_max_error_cm_minus_1"] < .006
    assert result["geometry_max_error_angstrom"] < 1e-5
    assert ORACLE.inspect_optfreq(*paths(tmp_path, HF))["actual_conditions"]["method"] == "HF"


@pytest.mark.parametrize("case", ["vwn", "exchange", "correlation", "hfx", "gga_x", "gga_c", "lda_c",
                                 "alpha", "beta", "pt", "charge", "multiplicity", "electrons",
                                 "unrestricted", "basis", "missing_scf", "echo_only", "conflicting_block",
                                 "concatenated", "frequency_sign", "frequency_count", "unit", "geometry",
                                 "convergence", "matrix", "scaling", "response", "response_count",
                                 "response_incomplete", "response_residual"])
def test_wrong_scientific_evidence_rejected(tmp_path, case):
    files = paths(tmp_path)
    target = files[0]
    text = target.read_text(encoding="utf-8")
    changes = {"vwn": ("VWN-5", "VWN-3"), "exchange": (".... B88", ".... PBE"),
               "correlation": (".... LYP", ".... P86"), "hfx": ("ScalHFX         ....  0.200000", "ScalHFX         ....  0.250000"),
               "gga_x": ("ScalDFX         ....  0.720000", "ScalDFX         ....  0.700000"),
               "gga_c": ("ScalDFC         ....  0.810000", "ScalDFC         ....  0.800000"),
               "lda_c": ("ScalLDAC        ....  1.000000", "ScalLDAC        ....  0.900000"),
               "alpha": ("XAlpha          ....  0.666667", "XAlpha          ....  0.700000"),
               "beta": ("XBeta           ....  0.004200", "XBeta           ....  0.003900"),
               "pt": ("Perturbative correction              ....  0.000000", "Perturbative correction              ....  0.100000"),
               "charge": ("Charge          ....    0", "Charge          ....    1"),
               "multiplicity": ("Mult            ....    1", "Mult            ....    3"),
               "electrons": ("NEL             ....   26", "NEL             ....   25"),
               "unrestricted": ("HFTyp           .... RHF", "HFTyp           .... UHF"),
               "basis": ("basis: def2-SVP", "basis: def2-TZVP"),
               "frequency_sign": ("276.12 cm", "-276.12 cm"),
               "frequency_count": ("26:    3804.59 cm**-1", "missing mode"),
               "unit": ("cm**-1", "Hz"), "scaling": ("1.000000000  (already applied!)", "0.900000000  (already applied!)"),
               "response": ("CP-SCF equations solved", "CP-SCF failed"),
               "response_count": ("Number of perturbations                 ...     27", "Number of perturbations                 ...     26"),
               "response_incomplete": ("27/ 27 done", "26/ 27 done"),
               "response_residual": ("6.5124e-05", "6.5124e-03")}
    if case in changes:
        text = text.replace(*changes[case])
    elif case == "convergence":
        text = text.replace("Energy change      -0.0000012405            0.0000050000      YES",
                            "Energy change      -0.0000012405            0.0000050000      NO")
    elif case in {"missing_scf", "echo_only"}:
        pattern = r"(?ms)^SCF SETTINGS\s*\n-+\s*\n.*?(?=^Convergence Acceleration:)"
        blocks = re.findall(pattern, text)
        text = re.sub(pattern, "", text)
        if case == "echo_only":
            text = "INPUT FILE\n" + "\n".join(blocks) + "\n****END OF INPUT****\n" + text
    elif case == "conflicting_block":
        text = text.replace("VWN-5", "VWN-3", 1)
    elif case == "concatenated":
        text = text + text
    elif case == "geometry":
        target = files[2]
        text = target.read_text().replace("-0.91686440846145", "-0.89686440846145")
    else:
        target = files[1]
        text = target.read_text()
        row = ORACLE._section(text, "hessian")[2]
        token = row.split()[1]
        text = text.replace(token, "9.2670848884E-01", 1)
    assert text != target.read_text(encoding="utf-8"), case
    target.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        ORACLE.inspect_optfreq(*files, method="B3LYP", basis="def2-SVP")


def test_b3lyp_gaussian_variant_is_not_alias(tmp_path):
    files = paths(tmp_path)
    with pytest.raises(ValueError, match="LDAOpt"):
        ORACLE.inspect_optfreq(*files, method="B3LYP/G", basis="def2-SVP")
    # A synthetic method block exercises identification only. It does not
    # constitute a real B3LYP/G calculation or reuse its altered frequencies.
    stdout = files[0].read_text(encoding="utf-8").replace("VWN-5", "VWN-3")
    state = ORACLE._method_state(ORACLE._actual_output(stdout), method="B3LYP/G", basis="def2-SVP",
                                 charge=0, multiplicity=1, electrons=26)
    assert state["method"] == "B3LYP/G"
