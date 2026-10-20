"""Rigid active rotation of the prepared CH2O geometry about the coordinate origin.

Convention: active (alibi) rotation of the Cartesian coordinates, right-hand rule
about the axis (1,2,3) normalized to unit length, origin fixed at the origin of the
input coordinates, angle 37 degrees. Atom order and internal geometry are preserved.
Coordinates in angstrom; the rotation matrix is dimensionless.
No random numbers are used; the requested seed 1701 is recorded but not applicable.
"""
import json
import math
from pathlib import Path

import numpy as np

AXIS_RAW = [1.0, 2.0, 3.0]
ANGLE_DEG = 37.0
SEED = 1701

axis = np.array(AXIS_RAW, dtype=float)
axis = axis / np.linalg.norm(axis)
theta = math.radians(ANGLE_DEG)
K = np.array([[0.0, -axis[2], axis[1]],
              [axis[2], 0.0, -axis[0]],
              [-axis[1], axis[0], 0.0]])
R = np.eye(3) + math.sin(theta) * K + (1.0 - math.cos(theta)) * (K @ K)

src = Path("ch2o_original.xyz")
lines = src.read_text(encoding="utf-8").splitlines()
n = int(lines[0].split()[0])
atoms, coords = [], []
for ln in lines[2:2 + n]:
    parts = ln.split()
    atoms.append(parts[0])
    coords.append([float(parts[1]), float(parts[2]), float(parts[3])])
X = np.array(coords, dtype=float)
Y = X @ R.T

rot_path = Path("ch2o_rotated.xyz")
hdr = ("active rigid rotation about origin, axis (1,2,3) normalized, 37 deg, "
       "right-hand rule; atom order preserved; angstrom")
rot_lines = [str(n), hdr]
for a, y in zip(atoms, Y):
    rot_lines.append("%-2s %20.12f %20.12f %20.12f" % (a, y[0], y[1], y[2]))
rot_path.write_text("\n".join(rot_lines) + "\n", encoding="utf-8")

def dists(P):
    return np.linalg.norm(P[:, None, :] - P[None, :, :], axis=-1)

dev = float(np.max(np.abs(dists(X) - dists(Y))))

record = {
    "version": 1,
    "source_file": "ch2o_original.xyz",
    "rotated_file": "ch2o_rotated.xyz",
    "units": "angstrom",
    "axis_raw": AXIS_RAW,
    "axis_unit": axis.tolist(),
    "angle_deg": ANGLE_DEG,
    "origin": [0.0, 0.0, 0.0],
    "rotation_kind": "active (alibi) rotation of coordinates, right-hand rule",
    "rotation_matrix": R.tolist(),
    "atom_order_preserved": [a for a in atoms],
    "max_pairwise_distance_deviation_angstrom": dev,
    "random_seed": SEED,
    "random_seed_note": "no random numbers used in geometry generation or rotation; seed recorded only as requested",
}
Path("rotation_record.json").write_text(json.dumps(record, indent=2), encoding="utf-8")

obs = {
    "version": 1,
    "observations": [
        {
            "name": "rotation_relation",
            "value": R.tolist(),
            "unit": "dimensionless",
            "sources": [
                {"filename": "ch2o_original.xyz",
                 "location": "lines 3-%d (Cartesian coordinates, angstrom)" % (2 + n)}
            ],
            "formula": "R = I + sin(theta) K + (1-cos(theta)) K^2 with K = skew(axis/|axis|), axis=(1,2,3), theta=37 deg; active rotation y = R x about the coordinate origin",
            "artifacts": ["ch2o_rotated.xyz", "rotation_record.json"],
            "limitations": ["Input coordinates in angstrom; rotation matrix dimensionless.",
                            "No random numbers used; requested seed 1701 recorded but not applicable."]
        }
    ]
}
Path("observations.json").write_text(json.dumps(obs, indent=2), encoding="utf-8")
print(json.dumps({"n_atoms": n, "max_pairwise_distance_deviation_angstrom": dev}))
