"""Rank ORCA Hessian normal modes by displacement along a selected bond."""

import argparse
import json
import math
from pathlib import Path


def finite(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("non-finite Hessian entry")
    return value


def sections(path):
    """Keep source line numbers; only retain the sections this analysis uses."""
    wanted = {"$atoms", "$normal_modes", "$vibrational_frequencies", "$ir_spectrum",
              "$frequency_scale_factor"}
    found = {}
    active = None
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for number, raw in enumerate(stream, 1):
            line = raw.strip()
            if line.startswith("$"):
                active = line if line in wanted else None
                if active:
                    if active in found:
                        raise ValueError(f"multiple {active} sections")
                    found[active] = []
            elif active and line and not line.startswith("#"):
                found[active].append((number, line.split()))
    for name in ("$atoms", "$normal_modes", "$vibrational_frequencies"):
        if not found.get(name):
            raise ValueError(f"missing {name} section")
    return found


def mode_columns(block, size):
    if block[0][1] != [str(size), str(size)]:
        raise ValueError("normal-mode dimensions disagree with the atom count")
    columns = []
    cursor = 1
    while len(columns) < size:
        if cursor >= len(block):
            raise ValueError("incomplete normal-mode matrix")
        header_line, labels = block[cursor]
        indices = list(map(int, labels))
        start = len(columns)
        if indices != list(range(start, start + len(labels))) or start + len(labels) > size:
            raise ValueError(f"unexpected mode numbers at line {header_line}")
        vectors = [[] for _ in indices]
        rows = block[cursor + 1:cursor + size + 1]
        if len(rows) != size:
            raise ValueError("incomplete normal-mode column block")
        for coordinate, (line, fields) in enumerate(rows):
            if len(fields) != len(vectors) + 1 or int(fields[0]) != coordinate:
                raise ValueError(f"incomplete displacements at line {line}")
            for vector, field in zip(vectors, fields[1:]):
                vector.append(finite(field))
        columns.extend((vector, header_line) for vector in vectors)
        cursor += size + 1
    if cursor != len(block):
        raise ValueError("unexpected rows after normal-mode matrix")
    return columns


def bond_modes(path, atom_a, atom_b):
    data = sections(path)
    atoms = data["$atoms"]
    count = int(atoms[0][1][0])
    if len(atoms) != count + 1 or any(len(fields) != 5 for _, fields in atoms[1:]):
        raise ValueError("incomplete atom table")
    if not (1 <= atom_a <= count and 1 <= atom_b <= count and atom_a != atom_b):
        raise ValueError("supply two distinct, one-based atom numbers from $atoms")
    a = atoms[atom_a][1]
    b = atoms[atom_b][1]
    delta = [finite(y) - finite(x) for x, y in zip(a[2:], b[2:])]
    length = math.hypot(*delta)
    if length == 0:
        raise ValueError("coincident atoms have no bond axis")
    axis = [x / length for x in delta]
    size = 3 * count
    frequencies = data["$vibrational_frequencies"]
    if frequencies[0][1] != [str(size)] or len(frequencies) != size + 1:
        raise ValueError("incomplete frequency table")
    ir = data.get("$ir_spectrum", [])
    ir_complete = len(ir) == size + 1 and ir[0][1] == [str(size)]
    ranked = []
    for mode, (vector, line) in enumerate(mode_columns(data["$normal_modes"], size)):
        freq_line, row = frequencies[mode + 1]
        if len(row) != 2 or int(row[0]) != mode:
            raise ValueError(f"unexpected frequency index at line {freq_line}")
        frequency = finite(row[1])
        norm = math.hypot(*vector)
        if frequency <= 0 or norm == 0:
            continue
        offset_a, offset_b = 3 * (atom_a - 1), 3 * (atom_b - 1)
        u = vector[offset_a:offset_a + 3]
        v = vector[offset_b:offset_b + 3]
        axial_a = sum(x * y for x, y in zip(u, axis))
        axial_b = sum(x * y for x, y in zip(v, axis))
        record = dict(mode=mode, wavenumber_cm1=frequency, displacement_a=u, displacement_b=v,
                      axial_a=axial_a, axial_b=axial_b, score=abs(axial_b - axial_a) / norm,
                      frequency_line=freq_line, mode_columns_line=line, intensity_km_mol=None)
        if ir_complete:
            ir_line, values = ir[mode + 1]
            if len(values) == 6 and abs(finite(values[0]) - frequency) <= 0.011:
                record.update(intensity_km_mol=finite(values[2]), ir_line=ir_line)
        ranked.append(record)
    if not ranked:
        raise ValueError("no positive-frequency mode with displacements")
    scale = data.get("$frequency_scale_factor")
    return dict(file=str(path), atom_numbers=[atom_a, atom_b], elements=[a[0], b[0]],
                scale_factor=finite(scale[0][1][0]) if scale else None,
                modes=sorted(ranked, key=lambda row: row["score"], reverse=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hess", type=Path, required=True)
    parser.add_argument("--atoms", type=int, nargs=2, required=True, metavar=("A", "B"))
    args = parser.parse_args()
    try:
        print(json.dumps(bond_modes(args.hess, *args.atoms), indent=2, allow_nan=False))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
