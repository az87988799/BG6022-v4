"""Independent developer-only Opt/Freq audit, never a production qualifier.

Reads raw ORCA text, the separately written Hessian, and final XYZ. No model
observations or production scientific parsers supply reference numbers.
"""

import hashlib
import math
import re
from pathlib import Path

import numpy as np

BOHR_TO_ANGSTROM = 0.529177210544
FREQUENCY_TOLERANCE_CM = 0.006  # stdout is rounded to two decimal places
GEOMETRY_TOLERANCE_ANGSTROM = 1e-5
# SI constants: Eh, a0 and u; Hz converted to wavenumber (cm^-1).
HESSIAN_TO_WAVENUMBER = math.sqrt(4.3597447222071e-18 / (
    5.29177210544e-11 ** 2 * 1.66053906892e-27)) / (2 * math.pi * 299792458 * 100)


def _number(value):
    result = float(value.replace("D", "E").replace("d", "e"))
    if not np.isfinite(result):
        raise ValueError("nonfinite scientific value")
    return result


def _section(text, name):
    matches = re.findall(r"(?ms)^\$" + re.escape(name) + r"\s*\n(.*?)(?=^\$|\Z)", text)
    if len(matches) != 1:
        raise ValueError("missing or repeated Hessian section: " + name)
    return [line.strip() for line in matches[0].splitlines() if line.strip()]


def _xyz(text):
    lines = text.splitlines()
    size = int(lines[0])
    if len(lines) != size + 2:
        raise ValueError("final XYZ is truncated or contains multiple frames")
    rows = [line.split() for line in lines[2:]]
    if any(len(row) != 4 for row in rows):
        raise ValueError("invalid final XYZ atom row")
    return [row[0] for row in rows], np.array([[_number(v) for v in row[1:]] for row in rows])


def _actual_output(stdout):
    """Exclude echoed input, which cannot establish the executed Hamiltonian."""
    return re.sub(r"(?ms)^\s*INPUT FILE\s*$.*?^.*?\*{4}END OF INPUT\*{4}.*?$", "", stdout)


def _method_state(stdout, *, method, basis, charge, multiplicity, electrons):
    # This intentionally narrow developer oracle recognizes only the methods
    # exercised by the frozen HF probe and autonomous B3LYP calculation.
    if method not in {"HF", "B3LYP", "B3LYP/G"}:
        raise ValueError("independent output method oracle does not cover requested method")
    blocks = re.findall(r"(?ms)^SCF SETTINGS\s*\n-+\s*\n(.*?)(?=^Convergence Acceleration:|\Z)", stdout)
    if not blocks:
        raise ValueError("actual SCF SETTINGS evidence is missing")
    basis_names = re.findall(r"(?m)^Your calculation utilizes the basis: (\S+)\s*$", stdout)
    if not basis_names or any(name != basis for name in basis_names):
        raise ValueError("output basis differs from expected basis")

    def value(block, key):
        matches = re.findall(r"(?m)^\s*[^\n]*?\b" + re.escape(key)
                             + r"\s+\.{4}\s+([^\n]+?)\s*$", block)
        if len(matches) != 1:
            raise ValueError("missing or repeated SCF setting: " + key)
        return matches[0]

    records = []
    for block in blocks:
        actual = {key: value(block, key) for key in ("Method", "HFTyp", "Charge", "Mult", "NEL")}
        if actual["HFTyp"] != "RHF":
            raise ValueError("output electronic state is not restricted closed shell")
        for key, expected in (("Charge", charge), ("Mult", multiplicity), ("NEL", electrons)):
            if not re.fullmatch(r"-?\d+", actual[key]) or int(actual[key]) != expected:
                raise ValueError("output electronic state differs at " + key)
        if method == "HF":
            if actual["Method"] != "Hartree-Fock(GTOs)":
                raise ValueError("output method differs from HF")
        else:
            expected_values = {"Method": "DFT(GTOs)", "Exchange": "B88", "Correlation": "LYP",
                               "LDAOpt": "VWN-5" if method == "B3LYP" else "VWN-3"}
            for key, expected in expected_values.items():
                actual[key] = value(block, key)
                if actual[key] != expected:
                    raise ValueError("output functional differs at " + key)
            expected_scales = {"XAlpha": .666667, "XBeta": .0042,
                               "ScalHFX": .2, "ScalDFX": .72, "ScalDFC": .81, "ScalLDAC": 1.}
            for key, expected in expected_scales.items():
                actual[key] = _number(value(block, key))
                if not math.isclose(actual[key], expected, rel_tol=0, abs_tol=5e-7):
                    raise ValueError("output functional coefficient differs at " + key)
            if len(re.findall(r"(?m)^\s*Hybrid DFT is turned on\s*$", block)) != 1:
                raise ValueError("actual hybrid DFT activation evidence is missing")
            correction = re.findall(r"(?m)^\s*Perturbative correction\s+\.{4}\s+(\S+)\s*$", block)
            if len(correction) != 1 or _number(correction[0]) != 0:
                raise ValueError("unexpected perturbative functional correction")
        records.append(actual)
    if any(record != records[0] for record in records[1:]):
        raise ValueError("conflicting actual SCF SETTINGS blocks")
    return {"method": method, "basis": basis, "charge": charge, "multiplicity": multiplicity,
            "electrons": electrons, "restricted_state": "RHF" if method == "HF" else "RKS",
            "scf_settings_blocks": len(records), "actual_scf_settings": records}


def _response_convergence(stdout, dimension):
    blocks = re.findall(r"(?ms)^SHARK CP-SCF DRIVER\s*\n(.*?)^CP-SCF equations solved[^\n]*$", stdout)
    if len(blocks) != 1:
        raise ValueError("one completed analytic CP-SCF response is required")
    block = blocks[0]
    tolerances = re.findall(r"Convergence Tolerance\s+\.{3}\s+(\S+)", block)
    counts = re.findall(r"Number of perturbations\s+\.{3}\s+(\d+)", block)
    iterations = re.findall(r"ITERATION\s+\d+:\s+\|\|err\|\|_max\s*=\s*(\S+)"
                            r"\s*\([^\n]*?\s+(\d+)/\s*(\d+)\s+done\)", block)
    if len(tolerances) != 1 or counts != [str(dimension)] or not iterations:
        raise ValueError("response tolerance, perturbation count or iteration evidence is missing")
    tolerance = _number(tolerances[0])
    error, done, total = iterations[-1]
    error = _number(error)
    if tolerance <= 0 or error < 0 or error > tolerance or int(done) != dimension or int(total) != dimension:
        raise ValueError("not all analytic response perturbations independently converged")
    return {"perturbations": dimension, "completed": int(done), "max_error": error,
            "tolerance": tolerance}


def inspect_optfreq(stdout_path, hessian_path, geometry_path, *, expected_atoms=9,
                    method="HF", basis="STO-3G", charge=0, multiplicity=1, electrons=26):
    paths = {"stdout": Path(stdout_path), "hessian": Path(hessian_path), "geometry": Path(geometry_path)}
    if any(path.stat().st_size > 16 * 1024 * 1024 for path in paths.values()):
        raise ValueError("developer oracle read bound exceeded")
    raw = {key: path.read_bytes() for key, path in paths.items()}
    if any(len(value) > 16 * 1024 * 1024 for value in raw.values()):
        raise ValueError("developer oracle read bound exceeded")
    stdout, hessian = _actual_output(raw["stdout"].decode("utf-8")), raw["hessian"].decode("ascii")
    if stdout.count("ORCA TERMINATED NORMALLY") != 1:
        raise ValueError("one complete ORCA job is required; concatenated jobs are not accepted")
    if "ORCA TERMINATED NORMALLY" not in stdout or "THE OPTIMIZATION HAS CONVERGED" not in stdout:
        raise ValueError("normal termination and explicit optimization convergence are required")
    if "SCF CONVERGED AFTER" not in stdout or re.search(r"SCF NOT CONVERGED|SCF DID NOT CONVERGE", stdout):
        raise ValueError("SCF convergence evidence missing or contradicted")
    if "CP-SCF equations solved" not in stdout:
        raise ValueError("analytic response completion evidence is missing")
    response = _response_convergence(stdout, 3 * expected_atoms)
    optimization_text = stdout.split("THE OPTIMIZATION HAS CONVERGED")[0]
    convergence = optimization_text.rsplit("|Geometry convergence|", 1)[-1]
    criteria = re.findall(r"(?m)^\s*(Energy change|RMS gradient|MAX gradient|RMS step|MAX step)"
                          r"\s+([-+\d.eE]+)\s+([-+\d.eE]+)\s+(YES|NO)\s*$", convergence)
    if (len(criteria) != 5 or any(flag != "YES" or abs(_number(value)) > _number(tolerance)
                                 for _, value, tolerance, flag in criteria)):
        raise ValueError("all five final optimization criteria must independently satisfy their tolerances")
    factors = re.findall(r"Scaling factor for frequencies\s*=\s*([\d.]+)", stdout)
    if len(factors) != 1 or _number(factors[0]) != 1:
        raise ValueError("independent frequency comparison requires explicit unit scaling")
    actual_conditions = _method_state(stdout, method=method, basis=basis, charge=charge,
                                     multiplicity=multiplicity, electrons=electrons)
    atoms, final_xyz = _xyz(raw["geometry"].decode("ascii"))
    if len(atoms) != expected_atoms:
        raise ValueError("unexpected final atom count")
    if np.linalg.matrix_rank(final_xyz - final_xyz.mean(axis=0), tol=1e-7) < 2:
        raise ValueError("this oracle's 3N-6 mode contract requires a nonlinear geometry")
    h_atoms = _section(hessian, "atoms")
    if int(h_atoms[0]) != expected_atoms or len(h_atoms) != expected_atoms + 1:
        raise ValueError("Hessian atoms differ from expected count")
    h_rows = [line.split() for line in h_atoms[1:]]
    if any(len(row) != 5 for row in h_rows) or [row[0] for row in h_rows] != atoms:
        raise ValueError("Hessian/final XYZ atom ordering differs")
    h_xyz = np.array([[_number(v) for v in row[2:]] for row in h_rows]) * BOHR_TO_ANGSTROM
    if any(_number(row[1]) <= 0 for row in h_rows):
        raise ValueError("Hessian atomic mass must be positive")
    # ORCA recentres the Hessian geometry; absolute origins are not physical.
    # Preserve atom order and chirality: proper rigid rotation, no reflection.
    x = h_xyz - h_xyz.mean(axis=0)
    y = final_xyz - final_xyz.mean(axis=0)
    left, _, right = np.linalg.svd(x.T @ y)
    correction = np.diag([1., 1., np.linalg.det(left @ right)])
    rotation = left @ correction @ right
    geom_error = float(np.max(np.abs(x @ rotation - y)))
    h_distances = np.linalg.norm(h_xyz[:, None] - h_xyz[None, :], axis=2)
    xyz_distances = np.linalg.norm(final_xyz[:, None] - final_xyz[None, :], axis=2)
    distance_error = float(np.max(np.abs(h_distances - xyz_distances)))
    if max(geom_error, distance_error) > GEOMETRY_TOLERANCE_ANGSTROM:
        raise ValueError("Hessian geometry differs from final optimized XYZ")
    dimension = 3 * expected_atoms
    h_matrix = _section(hessian, "hessian")
    if int(h_matrix[0]) != dimension:
        raise ValueError("Hessian dimension is not 3N")
    matrix = np.full((dimension, dimension), np.nan)
    columns = None
    for line in h_matrix[1:]:
        values = line.split()
        if all(re.fullmatch(r"\d+", token) for token in values):
            columns = [int(token) for token in values]
        else:
            if columns is None or len(values) != len(columns) + 1:
                raise ValueError("truncated Hessian matrix block")
            row = int(values[0])
            if not 0 <= row < dimension or any(not 0 <= c < dimension for c in columns):
                raise ValueError("Hessian matrix index outside dimensions")
            if any(np.isfinite(matrix[row, col]) for col in columns):
                raise ValueError("repeated Hessian matrix cell")
            matrix[row, columns] = [_number(value) for value in values[1:]]
    if not np.isfinite(matrix).all() or not np.allclose(matrix, matrix.T, atol=1e-8, rtol=0):
        raise ValueError("incomplete or asymmetric Hessian")
    section = _section(hessian, "vibrational_frequencies")
    h_freq = [line.split() for line in section[1:]]
    if int(section[0]) != dimension or len(h_freq) != dimension or [int(row[0]) for row in h_freq] != list(range(dimension)):
        raise ValueError("Hessian frequency count or mode indices differ from 3N")
    h_frequencies = np.array([_number(row[1]) for row in h_freq])
    output_rows = re.findall(r"(?m)^\s*(\d+):\s+([-+]?\d+(?:\.\d+)?)\s+cm\*\*-1", stdout)
    if len(output_rows) != dimension or [int(row[0]) for row in output_rows] != list(range(dimension)):
        raise ValueError("stdout frequency count or unit is not the complete cm**-1 3N table")
    output_frequencies = np.array([_number(row[1]) for row in output_rows])
    freq_error = float(np.max(np.abs(output_frequencies - h_frequencies)))
    if freq_error > FREQUENCY_TOLERANCE_CM:
        raise ValueError("stdout/Hessian frequency value or sign mismatch")
    if np.max(np.abs(h_frequencies[:6])) > FREQUENCY_TOLERANCE_CM:
        raise ValueError("expected six projected translation/rotation modes")
    # Independently diagonalize the mass-weighted Cartesian Hessian after
    # projecting the six rigid translations/rotations (ORCA 6.1 default).
    masses = np.array([_number(row[1]) for row in h_rows])
    centered = h_xyz - np.sum(h_xyz * masses[:, None], axis=0) / masses.sum()
    rigid = []
    for axis in np.eye(3):
        rigid.append((np.ones_like(centered) * axis * np.sqrt(masses[:, None])).ravel())
        rigid.append((np.cross(centered, axis) * np.sqrt(masses[:, None])).ravel())
    q = np.linalg.qr(np.array(rigid).T)[0]
    projection = np.eye(dimension) - q @ q.T
    weights = np.repeat(masses, 3)
    dynamical = matrix / np.sqrt(weights[:, None] * weights[None, :])
    eigenvalues = np.linalg.eigvalsh(projection @ dynamical @ projection)
    reconstructed = np.sign(eigenvalues) * np.sqrt(np.abs(eigenvalues)) * HESSIAN_TO_WAVENUMBER
    # Sort both full spectra so negative vibrational modes are not discarded
    # merely because the printed six projected zeros occur first in the file.
    spectrum_error = float(np.max(np.abs(np.sort(reconstructed) - np.sort(h_frequencies))))
    if spectrum_error > FREQUENCY_TOLERANCE_CM:
        raise ValueError("Hessian matrix/masses independently reconstructed spectrum disagrees")
    vibrations = h_frequencies[6:]
    assessment = ("inconsistent_with_minimum" if np.any(vibrations < -20) else
                  "inconclusive_soft_modes" if np.any(vibrations <= 20) else
                  "consistent_local_harmonic_minimum")
    energies = re.findall(r"(?m)^FINAL SINGLE POINT ENERGY\s+([-+\d.]+)\s*$", stdout)
    if not energies:
        raise ValueError("final electronic energy is missing from actual output")
    final_energy = _number(energies[-1])
    return {"version": "developer-analytic-oracle-3", "product_qualification": "unverified",
            "source_sha256": {key: hashlib.sha256(value).hexdigest() for key, value in raw.items()},
            "method": method, "basis": basis, "atom_count": expected_atoms,
            "actual_conditions": actual_conditions,
            "response_convergence": response,
            "final_electronic_energy_hartree": final_energy,
            "final_electronic_energy_decimal": energies[-1],
            "full_mode_count": dimension, "vibrational_mode_count": len(vibrations),
            "frequencies_cm_minus_1": h_frequencies.tolist(), "minimum_assessment": assessment,
            "geometry_max_error_angstrom": geom_error, "frequency_max_error_cm_minus_1": freq_error,
            "geometry_comparison": "same atom order, Bohr conversion, proper rigid alignment without reflection",
            "pairwise_distance_max_error_angstrom": distance_error,
            "mean_hessian_minus_xyz_offset_angstrom": (h_xyz - final_xyz).mean(axis=0).tolist(),
            "hessian_reconstructed_spectrum_max_error_cm_minus_1": spectrum_error,
            "hessian_frequency_conversion_cm_minus_1": HESSIAN_TO_WAVENUMBER,
            "optimization_criteria": [{"name": name, "value": _number(value), "tolerance": _number(tolerance)}
                                      for name, value, tolerance, _ in criteria],
            "tolerances": {"geometry_angstrom": GEOMETRY_TOLERANCE_ANGSTROM,
                           "frequency_cm_minus_1": FREQUENCY_TOLERANCE_CM},
            "limitations": ["narrow HF/B3LYP output method oracle; no general DFT qualification",
                            "harmonic local stationarity; not global minimum",
                            "no production frequency checker; independent developer evidence only"]}
