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
