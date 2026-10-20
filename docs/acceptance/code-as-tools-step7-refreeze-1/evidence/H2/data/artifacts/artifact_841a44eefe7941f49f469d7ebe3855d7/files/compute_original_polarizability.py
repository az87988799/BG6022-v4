import json
import shutil
from pathlib import Path

from orca_task_sdk import run_orca, receipt_source

SEED_DECLARED = 1701
COMMENT_ORIG = "unoptimized prepared geometry; coordinates in angstrom"


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


atoms = parse_xyz("prepared.xyz")
write_xyz("original.xyz", atoms, COMMENT_ORIG)
make_inp("orca_original.inp", "original.xyz")

rec = run_orca("orca_original.inp", dependencies=["original.xyz"], request_key="polar_original_1701", purpose="new_candidate", retry_of=None)

files = rec.get("files", {})
for key, dest in (("stdout.out", "original_stdout.out"), ("stderr.txt", "original_stderr.txt"), ("job.inp", "original_job.inp"), ("job.xyz", "original_job.xyz")):
    if key in files:
        shutil.copyfile(files[key], dest)

text = Path("original_stdout.out").read_text()
tensor = parse_tensor(text)
iso = (tensor[0][0] + tensor[1][1] + tensor[2][2]) / 3.0

limitations = [
    "RHF/def2-SVP, gas phase, neutral singlet, fixed unoptimized geometry prepared by OPI/RDKit; no geometry optimization.",
    "Static electric-dipole/static-electric-dipole polarizability from ORCA %elprop Polar true only; no frequency-dependent response, no permanent dipole moment substituted.",
    "Tensor values are atomic units as printed by ORCA; the XYZ geometry file is in angstrom; SCF/default numerical settings of ORCA apply.",
    "No stochastic sampling is used; ORCA exposes no random-seed control, so seed 1701 cannot be imposed at the ORCA level.",
]

src = receipt_source(rec, "stdout.out", location="STATIC POLARIZABILITY TENSOR (Dipole/Dipole), raw cartesian tensor (atomic units)")
seed_src = receipt_source(rec, "stdout.out", location="job stdout (deterministic SCF, no stochastic sampling)")

obs = [
    {
        "name": "static_polarizability_tensor",
        "value": tensor,
        "unit": "atomic units",
        "sources": [src],
        "formula": "raw cartesian static electric dipole-dipole polarizability tensor printed by ORCA %elprop Polar true at the fixed original geometry",
        "artifacts": ["original.xyz", "orca_original.inp", "original_stdout.out"],
        "limitations": limitations,
    },
    {
        "name": "static_polarizability_isotropic",
        "value": iso,
        "unit": "atomic units",
        "sources": [src],
        "formula": "alpha_iso = (alpha_xx + alpha_yy + alpha_zz)/3 computed from the same raw cartesian tensor",
        "artifacts": ["original_stdout.out"],
        "limitations": limitations,
    },
    {
        "name": "random_seed",
        "value": "seed 1701 declared for the task; no stochastic sampling occurs in this preparation/ORCA step; ORCA provides no random-seed control (not fixable)",
        "unit": None,
        "sources": [seed_src],
        "formula": None,
        "artifacts": ["compute_original_polarizability.py"],
        "limitations": ["Library-level seed control is not exposed by ORCA; determinism comes from fixed geometry and deterministic SCF."],
    },
]

Path("observations.json").write_text(json.dumps({"version": 1, "observations": obs}, indent=2) + "\n")
print(json.dumps({"request_key": rec.get("request_key"), "isotropic": iso, "tensor": tensor}))
