"""One property reader inside the OPI adapter; no execution or conversion."""

import math
import re

DIPOLE_RULE = "dipole-binding-1"
AU_TO_DEBYE = 2.541746473
ORCA_AU_TO_DEBYE = 2.541798
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"


def read_dipole(parsed, text, facts, *, tool_name):
    """Require one SCF property on the final, already identified geometry."""
    values = parsed.get_dipole(index=-1)
    if not values or len(values) != 1:
        raise ValueError("final JSON geometry needs exactly one dipole")
    item = values[0]
    if item.method != "SCF" or item.mult != 1 or item.state != -1:
        raise ValueError("dipole method/state is outside the admitted ground-state SCF profile")
    vector = item.dipoletotal
    if not vector or len(vector) != 3 or any(len(row) != 1 for row in vector):
        raise ValueError("dipole vector must have three components")
    vector = [row[0] for row in vector]
    if not all(math.isfinite(v) for v in vector):
        raise ValueError("nonfinite dipole vector")
    magnitude = math.sqrt(sum(v * v for v in vector))
    if item.dipolemagnitude is None or abs(magnitude - item.dipolemagnitude) > 5e-8:
        raise ValueError("dipole norm disagrees with JSON magnitude")
    segment = facts.get("energy_fragment", {})
    if not segment.get("unique_binding"):
        raise ValueError("dipole lacks unique energy/geometry segment")
    lines = text.split("\n")
    start, end = segment["segment_start_line"], segment["segment_end_line"]
    headers = [i for i in range(start - 1, min(end, len(lines)))
               if lines[i].strip() == "DIPOLE MOMENT"]
    if len(headers) != 1:
        raise ValueError("dipole text segment missing or ambiguous")
    header = headers[0]
    block = "\n".join(lines[header:min(header + 30, end)])

    def one(pattern):
        matches = list(re.finditer(pattern, block, re.M))
        if len(matches) != 1:
            raise ValueError("dipole text field missing or duplicated")
        return matches[0]

    one(r"^Method\s*:\s*SCF\s*$")
    one(r"^Multiplicity\s*:\s*1\s*$")
    energy = float(one(rf"^Energy\s*:\s*({NUMBER})\s+Eh\s*$")[1])
    if abs(energy - facts["energy_eh"]) > 5e-8:
        raise ValueError("dipole energy differs from bound geometry energy")
    raw = one(rf"^Total Dipole Moment\s*:\s*({NUMBER})\s+({NUMBER})\s+({NUMBER})\s*$")
    if any(abs(float(raw[i + 1]) - value) > 5e-8 for i, value in enumerate(vector)):
        raise ValueError("JSON/text dipole components conflict")
    au = float(one(rf"^Magnitude \(a\.u\.\)\s*:\s*({NUMBER})\s*$")[1])
    debye = float(one(rf"^Magnitude \(Debye\)\s*:\s*({NUMBER})\s*$")[1])
    if abs(au - magnitude) > 5e-8 or abs(debye - magnitude * ORCA_AU_TO_DEBYE) > 5e-6:
        raise ValueError("dipole units/magnitudes conflict")
    structure = parsed.get_structure(with_fragments=False)
    from orca_agent.models import CalculationParameters
    from orca_agent.tools.registry import validate_geometry
    atoms = validate_geometry(structure.to_xyz_block(), CalculationParameters()) if structure else []
    bound = facts.get("energy_geometry", {}).get("atoms", [])
    if len(atoms) != len(bound) or not atoms or any(
            a[0] != b[0] or any(abs(x - y) > 3e-6 for x, y in zip(a[1:], b[1:], strict=True))
            for a, b in zip(atoms, bound, strict=True)):
        raise ValueError("dipole coordinate frame differs between JSON and text")
    index = len(parsed.results_properties.geometries) - 1
    return {"vector_au": vector, "magnitude_au": magnitude,
            "magnitude_debye": magnitude * AU_TO_DEBYE,
            "source": {"rule_version": DIPOLE_RULE, "file": "job.property.json",
                       "path": ["Geometries", index, "Dipole_Moment", 0],
                       "text_file": "stdout.out", "start_line": header + 1,
                       "end_line": min(header + 30, end),
                       "geometry_line": segment["geometry_line"],
                       "geometry_index": index, "geometry_atoms_angstrom": bound,
                       "coordinate_frame": "ORCA reported Cartesian XYZ axes; not rotational axes",
                       "origin_convention": "neutral total molecular dipole; translation invariant",
                       "stage": "optimized" if tool_name == "orca.opt" else "single_point",
                       "vector_unit": "e*a0", "conversion_au_to_debye": AU_TO_DEBYE,
                       "orca_printed_debye": debye, "orca_conversion_au_to_debye": ORCA_AU_TO_DEBYE,
                       "unit_note": "Reported value uses modern e*a0 to Debye conversion; ORCA printed Debye uses 2.541798"}}
