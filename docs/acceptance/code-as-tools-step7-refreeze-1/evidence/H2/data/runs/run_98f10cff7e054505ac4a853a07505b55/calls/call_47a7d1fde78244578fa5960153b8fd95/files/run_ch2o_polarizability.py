import json, math
from pathlib import Path


def parse_xyz(path):
    lines = Path(path).read_text().splitlines()
    n = int(lines[0].strip())
    atoms = []
    for line in lines[2:2+n]:
        p = line.split()
        atoms.append((p[0], [float(p[1]), float(p[2]), float(p[3])]))
    if len(atoms) != n:
        raise ValueError(f"expected {n} atoms, found {len(atoms)}")
    return atoms


def write_xyz(path, atoms):
    lines = [str(len(atoms)), ""]
    for sym, xyz in atoms:
        lines.append(f"{sym:2s} {xyz[0]: .15f} {xyz[1]: .15f} {xyz[2]: .15f}")
    Path(path).write_text("\n".join(lines) + "\n")


def rotation_matrix():
    axis = [1.0, 2.0, 3.0]
    norm = math.sqrt(sum(v*v for v in axis))
    u = [v/norm for v in axis]
    theta = math.radians(37.0)
    c = math.cos(theta)
    s = math.sin(theta)
    ux, uy, uz = u
    R = [
        [c + ux*ux*(1-c), ux*uy*(1-c) - uz*s, ux*uz*(1-c) + uy*s],
        [uy*ux*(1-c) + uz*s, c + uy*uy*(1-c), uy*uz*(1-c) - ux*s],
        [uz*ux*(1-c) - uy*s, uz*uy*(1-c) + ux*s, c + uz*uz*(1-c)],
    ]
    return axis, u, theta, R


def rotate_xyz(xyz, R):
    return [sum(R[i][j]*xyz[j] for j in range(3)) for i in range(3)]


def make_inp(path, xyz_name):
    content = f"""%maxcore 192
! RHF def2-SVP NoAutoStart
%scf MaxIter 256 AutoTRAH false end
%elprop Polar true end
* xyzfile 0 1 {xyz_name}
"""
    Path(path).write_text(content)


atoms = parse_xyz("prepared.xyz")
write_xyz("original.xyz", atoms)

axis, u, theta, R = rotation_matrix()
rotated_atoms = [(sym, rotate_xyz(xyz, R)) for sym, xyz in atoms]
write_xyz("rotated.xyz", rotated_atoms)

make_inp("orca_original.inp", "original.xyz")
make_inp("orca_rotated.inp", "rotated.xyz")

record = {
    "version": 1,
    "rotation_convention": "active right-handed rotation about an axis through the original coordinate origin; coordinates transformed as r_prime = R r",
    "axis_direction": axis,
    "axis_unit": u,
    "angle_degrees": 37.0,
    "angle_radians": theta,
    "origin": [0.0, 0.0, 0.0],
    "rotation_matrix": R,
    "formula": "R = cos(theta) I + sin(theta) [u]_x + (1-cos(theta)) u u^T",
    "atom_order_preserved": True,
    "internal_geometry_unchanged": True,
    "source_geometry": "prepared.xyz",
    "original_xyz": "original.xyz",
    "rotated_xyz": "rotated.xyz",
    "orca_inputs": {"original": "orca_original.inp", "rotated": "orca_rotated.inp"},
    "original_atoms": [{"symbol": sym, "xyz": xyz} for sym, xyz in atoms],
    "rotated_atoms": [{"symbol": sym, "xyz": xyz} for sym, xyz in rotated_atoms],
}
Path("rotation_record.json").write_text(json.dumps(record, indent=2) + "\n")

import shutil
from orca_task_sdk import run_orca, receipt_source


def copy_receipt_file(receipt, key, dest):
    files = receipt.get("files", {})
    if key not in files:
        raise RuntimeError("missing " + key + " in files keys: " + str(sorted(files)))
    shutil.copyfile(files[key], dest)
    return dest


def parse_tensor(text):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "raw cartesian tensor" in line.lower():
            start = i + 1
            break
    if start is None:
        raise RuntimeError("raw cartesian tensor block not found")
    rows = []
    for line in lines[start:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            vals = [float(parts[0]), float(parts[1]), float(parts[2])]
        except ValueError:
            continue
        rows.append(vals)
        if len(rows) == 3:
            break
    if len(rows) != 3:
        raise RuntimeError("incomplete raw cartesian tensor")
    return rows


def parse_isotropic(text):
    for line in text.splitlines():
        if "isotropic polarizability" in line.lower():
            parts = line.replace(":", " ").split()
            for token in reversed(parts):
                try:
                    return float(token)
                except ValueError:
                    pass
    raise RuntimeError("isotropic polarizability not found")


rec1 = run_orca("orca_original.inp", dependencies=["original.xyz"], request_key="polar_original", purpose="new_candidate", retry_of=None)
rec2 = run_orca("orca_rotated.inp", dependencies=["rotated.xyz"], request_key="polar_rotated", purpose="new_candidate", retry_of=None)

for tag, rec in [("original", rec1), ("rotated", rec2)]:
    for key in ["stdout.out", "stderr.txt", "job.inp", "job.xyz"]:
        if key in rec.get("files", {}):
            copy_receipt_file(rec, key, tag + "_" + Path(key).name)

text1 = Path("original_stdout.out").read_text()
text2 = Path("rotated_stdout.out").read_text()
alpha1 = parse_tensor(text1)
alpha2 = parse_tensor(text2)
iso1 = parse_isotropic(text1)
iso2 = parse_isotropic(text2)

alpha2_pred = [[sum(R[i][k] * alpha1[k][l] * R[j][l] for k in range(3) for l in range(3)) for j in range(3)] for i in range(3)]
max_abs_error = max(abs(alpha2[i][j] - alpha2_pred[i][j]) for i in range(3) for j in range(3))
iso_diff = abs(iso2 - iso1)

data = {
    "version": 1,
    "units": "atomic units",
    "rotation": record,
    "original_tensor": alpha1,
    "rotated_tensor": alpha2,
    "original_isotropic": iso1,
    "rotated_isotropic": iso2,
    "rotated_tensor_predicted_from_original": alpha2_pred,
    "tensor_transform_max_abs_error": max_abs_error,
    "isotropic_difference": iso_diff,
    "formulas": {
        "tensor_transform": "alpha_rotated = R alpha_original R^T for active rotation r' = R r",
        "isotropic": "alpha_iso = (alpha_xx + alpha_yy + alpha_zz)/3",
    },
    "request_keys": {"original": "polar_original", "rotated": "polar_rotated"},
}
Path("polarizability_data.json").write_text(json.dumps(data, indent=2) + "\n")

src1 = receipt_source(rec1, "stdout.out", location="STATIC POLARIZABILITY TENSOR (Dipole/Dipole)")
src2 = receipt_source(rec2, "stdout.out", location="STATIC POLARIZABILITY TENSOR (Dipole/Dipole)")

common_limitations = [
    "RHF/def2-SVP gas phase, fixed geometry, static electric-field response only; no geometry optimization and no frequency-dependent response.",
    "Tensor values are the raw cartesian static dipole-dipole polarizability printed by ORCA %elprop Polar true.",
]

obs = [
    {
        "name": "original_tensor",
        "value": alpha1,
        "unit": "atomic units",
        "sources": [src1],
        "formula": "ORCA static dipole-dipole polarizability raw cartesian tensor",
        "artifacts": ["original.xyz", "orca_original.inp", "original_stdout.out", "rotation_record.json"],
        "limitations": common_limitations,
    },
    {
        "name": "rotated_tensor",
        "value": alpha2,
        "unit": "atomic units",
        "sources": [src2],
        "formula": "ORCA static dipole-dipole polarizability raw cartesian tensor for independently computed rotated geometry",
        "artifacts": ["rotated.xyz", "orca_rotated.inp", "rotated_stdout.out", "rotation_record.json"],
        "limitations": common_limitations,
    },
    {
        "name": "original_isotropic",
        "value": iso1,
        "unit": "atomic units",
        "sources": [src1],
        "formula": "ORCA isotropic polarizability = trace(alpha)/3",
        "artifacts": ["orca_original.inp", "original_stdout.out", "polarizability_data.json"],
        "limitations": common_limitations,
    },
    {
        "name": "rotated_isotropic",
        "value": iso2,
        "unit": "atomic units",
        "sources": [src2],
        "formula": "ORCA isotropic polarizability = trace(alpha)/3",
        "artifacts": ["orca_rotated.inp", "rotated_stdout.out", "polarizability_data.json"],
        "limitations": common_limitations,
    },
    {
        "name": "tensor_transform_max_abs_error",
        "value": max_abs_error,
        "unit": "atomic units",
        "sources": [src1, src2],
        "formula": "max_ij |alpha_rotated,ij - (R alpha_original R^T)_ij| for active rotation r' = R r about the requested axis through the origin",
        "artifacts": ["polarizability_data.json", "rotation_record.json"],
        "limitations": ["Comparison is numerical, not a substitute for the independent ORCA calculation.", "R is the exact active right-handed rotation matrix recorded in rotation_record.json."],
    },
    {
        "name": "isotropic_difference",
        "value": iso_diff,
        "unit": "atomic units",
        "sources": [src1, src2],
        "formula": "abs(alpha_iso_rotated - alpha_iso_original)",
        "artifacts": ["polarizability_data.json"],
        "limitations": ["Isotropic values are rotation invariants; the small numerical difference is a check, not an exact-zero assumption."],
    },
    {
        "name": "rotation_record",
        "value": record,
        "unit": None,
        "sources": [{"filename": "rotation_record.json", "location": "generated by run_ch2o_polarizability.py"}],
        "formula": "R = cos(theta) I + sin(theta) [u]_x + (1-cos(theta)) u u^T; active rotation r' = R r",
        "artifacts": ["rotation_record.json", "original.xyz", "rotated.xyz"],
        "limitations": ["Rotation is defined about the original coordinate origin; atom order and internal geometry are preserved."],
    },
    {
        "name": "random_seed",
        "value": 1701,
        "unit": None,
        "sources": [{"filename": "run_ch2o_polarizability.py", "location": "deterministic geometry transformation and ORCA inputs; no random sampling"}],
        "formula": None,
        "artifacts": ["run_ch2o_polarizability.py", "polarizability_data.json"],
        "limitations": ["No random numbers are used in this transformation or analysis; ORCA does not expose a random seed control. Seed 1701 is recorded for completeness."],
    },
]

Path("observations.json").write_text(json.dumps({"version": 1, "observations": obs}, indent=2) + "\n")

