"""Estimate equilibrium populations from completed, comparable ORCA Opt Freq outputs."""

import argparse
from collections import Counter
from decimal import Decimal
import json
import math
from pathlib import Path
import re
import sys

from orca_job import read_job

# CODATA 2022 Hartree energy in J divided by the exact Boltzmann constant in J/K.
HARTREE_IN_K = 4.3597447222060e-18 / 1.380649e-23
SCALAR = (r"(?:maxiter|nprocs|temp|pressure|scalfreq|scalefreq|cutofffreq|quasirrho|qrrhoref_freq|"
          r"epsilon|refrac|smd|tolenergy|tolrmsp|tolmaxp)\s+(?:[+\-\d.e]+|true|false)")
OPTION = rf'(?:smdsolvent\s+(?:"[\w -]+"|[\w-]+)|{SCALAR})'
SELF_CONTAINED_OPTIONS = re.compile(rf"{OPTION}(?:\s+{OPTION})*")


def model_lines(echo):
    """Compare echoed electronic/thermochemical input, excluding only geometry and resources."""
    result = []
    geometry = False
    saw_geometry = False
    for raw in echo:
        line = " ".join(raw.partition("#")[0].strip().lower().split())
        if not line or line == "****end of input****":
            continue
        if geometry:
            if line == "*":
                geometry = False
            continue
        if re.fullmatch(r"\*\s*xyz\s+[+-]?\d+\s+\d+", line):
            geometry = saw_geometry = True
        elif re.fullmatch(r'\*\s*xyzfile\s+[+-]?\d+\s+\d+\s+(?:"[^"]+"|\S+)', line):
            # The output already supplies the evaluated geometry and charge/spin.
            saw_geometry = True
        elif re.fullmatch(r"%maxcore\s+\d+|%pal nprocs \d+ end|%freq temp [\d.e+-]+ end", line):
            continue
        else:
            result.append(line)
    if geometry or not saw_geometry or not any(line.startswith("!") for line in result):
        raise ValueError("Model comparison requires echoed XYZ or XYZFILE input and method keywords.")
    # Accept common self-contained blocks. A matching filename (basis, orbitals,
    # charges, embedding, etc.) does not identify its historical contents.
    in_block = False
    allowed_blocks = {"%freq", "%scf", "%geom", "%pal", "%cpcm"}
    for line in result:
        words = line.split()
        if line.startswith("!"):
            if any(re.search(r"read|file|extern", word) for word in words):
                raise ValueError("Model input references external data; compare its historical evidence first.")
            continue
        if words[0].startswith("%") and words[0] not in allowed_blocks:
            raise ValueError(f"Cannot establish a self-contained model for {words[0]}.")
        if words[0] in allowed_blocks:
            in_block = True
        # Recognize scalar options, including a quoted solvent name. All other
        # block contents require review (a bare filename need not have a suffix).
        body = words[1:] if words[0] in allowed_blocks else words
        if body and body[-1] == "end":
            body = body[:-1]
            in_block = False
        if body and not SELF_CONTAINED_OPTIONS.fullmatch(" ".join(body)):
            raise ValueError(f"Cannot verify the model option in {line!r} from its echo alone.")
    if in_block:
        raise ValueError("Incomplete echoed model block.")
    return result


def dot(v, w):
    return sum(x * y for x, y in zip(v, w))


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def body_coordinates(atoms):
    """Express ordered atoms in a proper-rotation frame. Reflections retain opposite handedness."""
    origin = atoms[0][1:]
    points = [[x - o for x, o in zip(a[1:], origin)] for a in atoms]
    baseline = next((p for p in points if math.hypot(*p) > 1e-4), None)
    if baseline is None:  # A single atom has no orientation.
        return [(a[0], (0, 0, 0)) for a in atoms]
    axis = [x / math.hypot(*baseline) for x in baseline]
    normals = (cross(axis, p) for p in points)
    normal = next((v for v in normals if math.hypot(*v) > 1e-3), None)
    if normal is None:  # A linear molecule needs only an axial coordinate.
        return [(a[0], (dot(p, axis), math.hypot(*cross(axis, p)), 0)) for a, p in zip(atoms, points)]
    z = [x / math.hypot(*normal) for x in normal]
    y = cross(z, axis)
    return [(a[0], (dot(p, axis), dot(p, y), dot(p, z))) for a, p in zip(atoms, points)]


def butane_dihedral(atoms):
    """Recognize an n-butane carbon backbone in its final geometry and measure C-C-C-C."""
    if Counter(a[0] for a in atoms) != {"C": 4, "H": 10}:
        raise ValueError("Butane grouping requires C4H10.")
    carbons = [i for i, atom in enumerate(atoms) if atom[0] == "C"]
    neighbors = {i: [j for j in carbons if 1.2 < math.dist(atoms[i][1:], atoms[j][1:]) < 1.8]
                 for i in carbons}
    ends = [i for i in carbons if len(neighbors[i]) == 1]
    if len(ends) != 2 or sorted(map(len, neighbors.values())) != [1, 1, 2, 2]:
        raise ValueError("Final carbon geometry does not show the linear n-butane backbone.")
    h_counts = Counter()
    for atom in atoms:
        if atom[0] == "H":
            attached = [i for i in carbons if 0.8 < math.dist(atom[1:], atoms[i][1:]) < 1.3]
            if len(attached) != 1:
                raise ValueError("Ambiguous C-H connectivity in final geometry.")
            h_counts[attached[0]] += 1
    if any(h_counts[i] + len(neighbors[i]) != 4 for i in carbons):
        raise ValueError("C-H geometry does not match n-butane.")
    backbone = [ends[0], neighbors[ends[0]][0], neighbors[ends[1]][0], ends[1]]
    a, b, c, d = (atoms[i][1:] for i in backbone)
    axis = [y - x for x, y in zip(b, c)]
    length = math.hypot(*axis)
    axis = [value / length for value in axis]
    ba = [x - y for x, y in zip(a, b)]
    cd = [y - x for x, y in zip(c, d)]
    ba_parallel, cd_parallel = dot(ba, axis), dot(cd, axis)
    first = [x - ba_parallel * y for x, y in zip(ba, axis)]
    last = [x - cd_parallel * y for x, y in zip(cd, axis)]
    angle = math.degrees(math.atan2(dot(cross(axis, first), last), dot(first, last)))
    if abs(angle) > 140:
        basin = "anti"
    elif 35 < abs(angle) < 95:
        basin = "gauche+" if angle > 0 else "gauche-"
    else:
        raise ValueError("Final torsion is outside the resolved anti/gauche basins.")
    return {"basin": basin, "atoms_1based": [i + 1 for i in backbone], "dihedral_deg": angle}


def populations(specs, temperature, butane=False):
    if len(specs) < 2 or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Specify at least two states and a finite, positive temperature in kelvin.")
    states = []
    seen = set()
    geometries = {}
    reference = None
    for label, degeneracy, output in specs:
        degeneracy = int(degeneracy)
        path = Path(output).resolve()
        if degeneracy < 1 or path in seen or any(s["label"] == label for s in states):
            raise ValueError("Each state requires a distinct label, output and positive integer degeneracy.")
        seen.add(path)
        job = read_job(path, "optfreq")
        quantities = job["quantities"]
        if not job["checks"]["task_verified"] or job["checks"]["local_minimum"] is not True:
            raise ValueError(f"{path}: requires a converged optimized geometry with complete positive internal modes.")
        values = quantities.get("gibbs_free_energies", [])
        if "gibbs_free_energy" in quantities:
            values = [quantities["gibbs_free_energy"]]
        matched = [v for v in values if v["temperature_K"] == temperature and v["unit"] == "Eh"]
        if len(matched) != 1:
            raise ValueError(f"{path}: requires one final Gibbs free energy at {temperature:g} K.")
        gibbs = matched[0]
        geometry = quantities["final_geometry"]
        conditions = job["conditions"]
        model = {"input": model_lines(conditions.get("input_echo_lines", [])), "orca_version": job["orca_version"],
                 "formula": dict(Counter(a[0] for a in geometry["atoms"])), "charge": conditions.get("charge"),
                 "multiplicity": conditions.get("multiplicity"), "pressure_atm": gibbs["pressure_atm"],
                 "frequency_scale": quantities["frequencies"]["scale_factor"], "quasi_rrho": gibbs.get("quasi_rrho"),
                 "cutoff_cm1": gibbs.get("cutoff_cm1"), "mass_amu": gibbs.get("mass_amu")}
        if any(v is None for v in model.values()):
            raise ValueError(f"{path}: incomplete charge/spin, thermochemistry or model evidence.")
        if reference is not None and model != reference:
            differing = ", ".join(key for key in model if model[key] != reference[key])
            raise ValueError(f"{path}: comparison conditions differ ({differing}).")
        reference = model
        frame = body_coordinates(geometry["atoms"])
        for prior, other in geometries.items():
            if len(frame) == len(other) and all(
                a == b and math.dist(p, q) <= 1e-3 for (a, p), (b, q) in zip(frame, other)
            ):
                raise ValueError(f"{label} and {prior} have the same optimized geometry; count this basin once.")
        geometries[label] = frame
        state = {"label": label, "degeneracy": degeneracy, "output": str(path), "gibbs_Eh": gibbs["value"],
                 "gibbs_line": gibbs["line"], "geometry_line": geometry["line"],
                 "rotational_symmetry_number": gibbs.get("symmetry_number")}
        if butane:
            if conditions.get("charge") != 0 or conditions.get("multiplicity") != 1:
                raise ValueError("The n-butane grouping assumes neutral singlet n-butane.")
            state.update(butane_dihedral(geometry["atoms"]))
        states.append(state)
    if butane:
        rotations = {s["rotational_symmetry_number"] for s in states}
        if None in rotations or len(rotations) != 1:
            raise ValueError("n-Butane anti and gauche require consistent rotational symmetry numbers. "
                             "Inspect the thermochemistry point groups and rotational entropy before comparing.")
        counts = {s["basin"]: s["degeneracy"] for s in states}
        gauche = {key: val for key, val in counts.items() if key.startswith("gauche")}
        if len(counts) != len(states) or counts.get("anti") != 1 or sum(gauche.values()) != 2:
            raise ValueError("Supply anti once (g=1), plus both gauche signs (g=1 each) or one equivalent representative (g=2).")
    minimum = min(Decimal(state["gibbs_Eh"]) for state in states)
    weights = []
    for state in states:
        delta = float(Decimal(state["gibbs_Eh"]) - minimum)
        state["delta_G_Eh"] = delta
        weights.append(state["degeneracy"] * math.exp(-delta * HARTREE_IN_K / temperature))
    total = sum(weights)
    for state, weight in zip(states, weights):
        state["percent"] = 100 * weight / total
    result = {"temperature_K": temperature, "conditions": reference, "states": states}
    if butane:
        result["class_percent"] = {
            "anti": sum(s["percent"] for s in states if s["basin"] == "anti"),
            "gauche": sum(s["percent"] for s in states if s["basin"].startswith("gauche")),
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", action="append", nargs=3, metavar=("LABEL", "DEGENERACY", "OUT"), required=True)
    parser.add_argument("--temperature", type=float, required=True, help="Kelvin, matched to ORCA thermochemistry")
    parser.add_argument("--butane", action="store_true", help="Verify n-butane geometry, anti/gauche assignments and counting")
    args = parser.parse_args()
    try:
        print(json.dumps(populations(args.state, args.temperature, args.butane), indent=2, allow_nan=False))
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
