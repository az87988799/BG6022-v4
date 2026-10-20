import json
import math
import shutil
from pathlib import Path

from orca_task_sdk import run_orca, receipt_source

SEED_DECLARED = 1701
AXIS = [1.0, 2.0, 3.0]
ANGLE_DEGREES = 37.0
ORIGIN = [0.0, 0.0, 0.0]
COMMENT_ORIG = "unoptimized prepared geometry; coordinates in angstrom"
COMMENT_ROT = "active rotation of prepared geometry: 37 deg about (1,2,3) through origin; coordinates in angstrom"


def parse_xyz(path):
    lines = Path(path).read_text().splitlines()
    n = int(lines[0].strip())
    atoms = []
    for line in lines[2:2 + n]:
        p = line.split()
        atoms.append((p[0], [float(p[1]), float(p[2]), float(p[3])]))
    if len(atoms) != n:
        raise ValueError("expected %d atoms, found %d" % (n, len(atoms)))
    return atoms


def write_xyz(path, atoms, comment):
    lines = [str(len(atoms)), comment]
    for sym, xyz in atoms:
        lines.append("%-2s % .15f % .15f % .15f" % (sym, xyz[0], xyz[1], xyz[2]))
    Path(path).write_text("\n".join(lines) + "\n")


def make_inp(path, xyz_name):
    content = ("%maxcore 192\n"
               "! RHF def2-SVP NoAutoStart\n"
               "%scf MaxIter 256 AutoTRAH false end\n"
               "%elprop Polar true end\n"
               "* xyzfile 0 1 " + xyz_name + "\n")
    Path(path).write_text(content)


def parse_tensor(text):
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "raw cartesian tensor" in line.lower():
            start = i + 1
            break
    if start is None:
        raise RuntimeError("raw cartesian tensor block not found in ORCA output")
    rows = []
    for line in lines[start:]:
        parts = line.replace(":", " ").split()
        if len(parts) < 3:
            if rows:
                break
            continue
        try:
            vals = [float(parts[0]), float(parts[1]), float(parts[2])]
        except ValueError:
            if rows:
                break
            continue
        rows.append(vals)
        if len(rows) == 3:
            break
    if len(rows) != 3:
        raise RuntimeError("incomplete raw cartesian tensor")
    return rows


def rotation_matrix():
    norm = math.sqrt(sum(v * v for v in AXIS))
    u = [v / norm for v in AXIS]
    theta = math.radians(ANGLE_DEGREES)
    c = math.cos(theta)
    s = math.sin(theta)
    ux, uy, uz = u
    R = [
        [c + ux * ux * (1.0 - c), ux * uy * (1.0 - c) - uz * s, ux * uz * (1.0 - c) + uy * s],
        [uy * ux * (1.0 - c) + uz * s, c + uy * uy * (1.0 - c), uy * uz * (1.0 - c) - ux * s],
        [uz * ux * (1.0 - c) - uy * s, uz * uy * (1.0 - c) + ux * s, c + uz * uz * (1.0 - c)],
    ]
    return u, theta, R


def apply_rotation(R, xyz):
    return [sum(R[i][j] * xyz[j] for j in range(3)) for i in range(3)]


def max_distance_deviation(a, b):
    worst = 0.0
    n = len(a)
    for i in range(n):
        for j in range(i + 1, n):
            da = sum((a[i][1][k] - a[j][1][k]) ** 2 for k in range(3)) ** 0.5
            db = sum((b[i][1][k] - b[j][1][k]) ** 2 for k in range(3)) ** 0.5
            worst = max(worst, abs(da - db))
    return worst


atoms = parse_xyz("prepared.xyz")
write_xyz("original.xyz", atoms, COMMENT_ORIG)
u, theta, R = rotation_matrix()
rotated = [(sym, apply_rotation(R, xyz)) for sym, xyz in atoms]
write_xyz("rotated.xyz", rotated, COMMENT_ROT)
deviation = max_distance_deviation(atoms, rotated)

record = {
    "rotation_convention": "active right-handed rotation of the coordinate vectors about an axis through the original coordinate origin: r_prime = R r",
    "axis_direction": AXIS,
    "axis_unit_vector": u,
    "angle_degrees": ANGLE_DEGREES,
    "angle_radians": theta,
    "origin": ORIGIN,
    "rotation_matrix": R,
    "formula": "R = cos(theta) I + sin(theta) [u]_x + (1 - cos(theta)) u u^T",
    "atom_order_preserved": True,
    "max_pairwise_distance_deviation_angstrom": deviation,
    "source_geometry": "prepared.xyz",
    "original_xyz": "original.xyz",
    "rotated_xyz": "rotated.xyz",
    "orca_input": "orca_rotated.inp",
}
Path("rotation_record.json").write_text(json.dumps(record, indent=2) + "\n")

make_inp("orca_original.inp", "original.xyz")
make_inp("orca_rotated.inp", "rotated.xyz")

rec = run_orca("orca_rotated.inp", dependencies=["rotated.xyz"], request_key="polar_rotated_1701", purpose="new_candidate", retry_of=None)

files = rec.get("files", {})
for key, dest in (("stdout.out", "rotated_stdout.out"), ("stderr.txt", "rotated_stderr.txt"), ("job.inp", "rotated_job.inp"), ("job.xyz", "rotated_job.xyz")):
    if key in files:
        shutil.copyfile(files[key], dest)

text = Path("rotated_stdout.out").read_text()
tensor = parse_tensor(text)
iso = (tensor[0][0] + tensor[1][1] + tensor[2][2]) / 3.0

limitations = [
    "RHF/def2-SVP, gas phase, neutral singlet, fixed rotated copy of the unoptimized prepared geometry; no geometry optimization.",
    "Static electric-dipole/static-electric-dipole polarizability from ORCA %elprop Polar true only; no frequency-dependent response.",
    "Tensor values are atomic units; XYZ files are in angstrom; rotation matrix and axis vector are dimensionless.",
    "Same scientific settings as the original-geometry run (same input keywords, %maxcore 192, MaxIter 256, AutoTRAH false).",
    "No stochastic sampling; ORCA exposes no random-seed control.",
]

src = receipt_source(rec, "stdout.out", location="STATIC POLARIZABILITY TENSOR (Dipole/Dipole), raw cartesian tensor (atomic units)")
seed_src = receipt_source(rec, "stdout.out", location="job stdout (deterministic SCF, no stochastic sampling)")

obs = [
    {
        "name": "static_polarizability_tensor",
        "value": tensor,
        "unit": "atomic units",
        "sources": [src],
        "formula": "raw cartesian static electric dipole-dipole polarizability tensor printed by ORCA %elprop Polar true at the independently computed rotated geometry",
        "artifacts": ["rotated.xyz", "orca_rotated.inp", "rotated_stdout.out", "rotation_record.json"],
        "limitations": limitations,
    },
    {
        "name": "static_polarizability_isotropic",
        "value": iso,
        "unit": "atomic units",
        "sources": [src],
        "formula": "alpha_iso = (alpha_xx + alpha_yy + alpha_zz)/3 computed from the same raw cartesian tensor",
        "artifacts": ["rotated_stdout.out"],
        "limitations": limitations,
    },
    {
        "name": "rotation_relation",
        "value": record,
        "unit": "dimensionless rotation matrix and unit axis vector; angles labelled in degrees and radians; distance deviation in angstrom",
        "sources": [{"internal_request_key": rec.get("request_key"), "filename": "stdout.out", "location": "rotated-geometry ORCA job whose input XYZ is the recorded rotated copy"}],
        "formula": "r_prime = R r with R = cos(theta) I + sin(theta)[u]_x + (1-cos(theta)) u u^T, u = (1,2,3)/|(1,2,3)|, theta = 37 deg, origin = (0,0,0)",
        "artifacts": ["rotation_record.json", "original.xyz", "rotated.xyz", "compute_rotated_polarizability.py"],
        "limitations": ["Rigid active rotation only; the two geometries were not optimized and no internal-coordinate relaxation was applied."],
    },
    {
        "name": "random_seed",
        "value": "seed 1701 declared for the task; no stochastic sampling occurs in this preparation/ORCA step; ORCA provides no random-seed control (not fixable)",
        "unit": None,
        "sources": [seed_src],
        "formula": None,
        "artifacts": ["compute_rotated_polarizability.py"],
        "limitations": ["Library-level seed control is not exposed by ORCA; determinism comes from fixed geometry and deterministic SCF."],
    },
]

Path("observations.json").write_text(json.dumps({"version": 1, "observations": obs}, indent=2) + "\n")
print(json.dumps({"request_key": rec.get("request_key"), "isotropic": iso, "tensor": tensor, "geometry_deviation": deviation}))
