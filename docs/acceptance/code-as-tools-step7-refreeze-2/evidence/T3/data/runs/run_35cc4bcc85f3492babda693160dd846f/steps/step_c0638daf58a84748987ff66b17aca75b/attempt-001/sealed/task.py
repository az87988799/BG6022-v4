import os, json, math, shutil, re
from orca_task_sdk import run_orca

INITIAL_XYZ = "initial.xyz"
ANGLES = [-180, -120, -60, 0, 60, 120, 180]
LABELS = {-180:"m180", -120:"m120", -60:"m60", 0:"0", 60:"60", 120:"120", 180:"180"}
HARTREE_TO_KJ = 2625.499639


def parse_xyz(path):
    with open(path, "r") as f:
        lines = [ln.rstrip("\n") for ln in f]
    n = int(lines[0].split()[0])
    atoms = []
    for ln in lines[2:2+n]:
        p = ln.split()
        if len(p) < 4:
            continue
        atoms.append((p[0], float(p[1]), float(p[2]), float(p[3])))
    return atoms


def sub(a,b): return (a[0]-b[0], a[1]-b[1], a[2]-b[2])
def dot(a,b): return a[0]*b[0]+a[1]*b[1]+a[2]*b[2]
def cross(a,b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def norm(a):
    n = math.sqrt(dot(a,a))
    return (a[0]/n, a[1]/n, a[2]/n)


def dihedral_deg(a,b,c,d):
    p0 = (a[1],a[2],a[3]); p1 = (b[1],b[2],b[3]); p2 = (c[1],c[2],c[3]); p3 = (d[1],d[2],d[3])
    b0 = sub(p1,p0); b1 = sub(p2,p1); b2 = sub(p3,p2); b1n = norm(b1)
    v = sub(b0, tuple(b1n[k]*dot(b0,b1n) for k in range(3)))
    w = sub(b2, tuple(b1n[k]*dot(b2,b1n) for k in range(3)))
    x = dot(v,w); y = dot(cross(b1n,v),w)
    return math.degrees(math.atan2(y,x))


def find_dihedral_indices(atoms):
    carbons = [i for i,a in enumerate(atoms) if a[0].upper() == "C"]
    adj = {i:[] for i in carbons}
    for ii,i in enumerate(carbons):
        for j in carbons[ii+1:]:
            d = math.sqrt(sum((atoms[i][k+1]-atoms[j][k+1])**2 for k in range(3)))
            if d <= 1.75:
                adj[i].append(j); adj[j].append(i)
    best = []
    for s in carbons:
        stack = [(s, [s])]
        while stack:
            cur, path = stack.pop()
            if len(path) > len(best):
                best = path
            for nxt in adj[cur]:
                if nxt not in path:
                    stack.append((nxt, path+[nxt]))
    if len(best) < 4:
        raise RuntimeError("Could not find a four-carbon chain; carbons=%s adj=%s" % (carbons, adj))
    return best[:4]


def write_input(angle, idxs):
    i,j,k,l = idxs
    inp_name = "scan_%s.inp" % LABELS[angle]
    lines = [
      "! B3LYP def2-TZVP RKS Opt NoAutoStart",
      "%maxcore 192",
      "%scf",
      "  MaxIter 256",
      "  AutoTRAH false",
      "end",
      "%geom",
      "  MaxIter 128",
      "  Constraints",
      "    { D %d %d %d %d %.1f C }" % (i,j,k,l,angle),
      "  end",
      "end",
      "* xyzfile 0 1 initial.xyz",
    ]
    with open(inp_name,"w") as f:
        f.write("\n".join(lines)+"\n")
    return inp_name


atoms = parse_xyz(INITIAL_XYZ)
idxs = find_dihedral_indices(atoms)
points = []
all_artifacts = []

for angle in ANGLES:
    label = LABELS[angle]
    inp = write_input(angle, idxs)
    receipt = run_orca(inp, dependencies=["initial.xyz"], request_key="scan_%s" % label, purpose="new_candidate")
    receipt_name = "scan_%s_receipt.json" % label
    with open(receipt_name,"w") as f:
        json.dump(receipt, f, indent=2, default=str)
    all_artifacts.append(receipt_name)
    files = receipt.get("files", {}) or {}
    copied = {}
    for key, src in files.items():
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(key))
        dst = "scan_%s_%s" % (label, safe)
        try:
            shutil.copyfile(src, dst)
            copied[key] = dst
            all_artifacts.append(dst)
        except Exception:
            copied[key] = None
    stdout_path = copied.get("stdout.out")
    txt = ""
    if stdout_path and os.path.exists(stdout_path):
        txt = open(stdout_path, errors="replace").read()
    energies = re.findall(r"FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)", txt)
    energy = float(energies[-1]) if energies else None
    converged = bool(re.search(r"HURRAY|OPTIMIZATION HAS CONVERGED|The optimization has converged", txt, re.IGNORECASE))
    actual = None
    xyz_path = copied.get("job.xyz")
    if xyz_path and os.path.exists(xyz_path):
        final_atoms = parse_xyz(xyz_path)
        if len(final_atoms) >= 4:
            actual = dihedral_deg(final_atoms[idxs[0]], final_atoms[idxs[1]], final_atoms[idxs[2]], final_atoms[idxs[3]])
    fail = None
    if receipt.get("operation_status") != "completed":
        fail = receipt.get("error") or receipt.get("message") or json.dumps({k:receipt.get(k) for k in ("operation_status","scientific_validation")}, default=str)
    points.append({
        "requested_angle_deg": angle,
        "actual_dihedral_deg": actual,
        "energy_hartree": energy,
        "converged": converged,
        "request_key": "scan_%s" % label,
        "input_filename": inp,
        "files": copied,
        "failure_reason": fail
    })

conv_energies = [p["energy_hartree"] for p in points if p["converged"] and p["energy_hartree"] is not None]
all_energies = [p["energy_hartree"] for p in points if p["energy_hartree"] is not None]
ref = min(conv_energies) if conv_energies else (min(all_energies) if all_energies else None)
for p in points:
    if p["energy_hartree"] is not None and ref is not None:
        p["relative_energy_kj_mol"] = (p["energy_hartree"] - ref) * HARTREE_TO_KJ
    else:
        p["relative_energy_kj_mol"] = None

sources = []
for p in points:
    sources.append({"internal_request_key": p["request_key"], "filename": "stdout.out", "location": "FINAL SINGLE POINT ENERGY; job.xyz final geometry"})

rel_points = [{"requested_angle_deg": p["requested_angle_deg"], "actual_dihedral_deg": p["actual_dihedral_deg"], "relative_energy_kj_mol": p["relative_energy_kj_mol"], "converged": p["converged"]} for p in points]

rationale = "B3LYP/def2-TZVP. ORCA 6.1 manual: B3LYP is the popular hybrid functional with 20% HF exchange (result_39c1b61f90a54983910f5843208f55e2); def2-TZVP is the Karlsruhe polarized valence triple-zeta basis for H-Rn (result_d087eafacb914c6fb00979050e3579a0). No dispersion correction is applied because the retrieved official excerpts did not document a B3LYP dispersion correction. Gas phase, neutral (charge 0), singlet RKS."

criterion = "Criterion stated before scanning: use the 60-degree grid; if the lowest-energy converged grid point has at least one neighboring interval with |DeltaE| > 5 kJ/mol, or any adjacent converged pair differs by > 10 kJ/mol, propose at most two additional constrained optimizations at midpoint angles adjacent to the minimum or steepest interval. If the minimum is bracketed by neighbor differences <= 5 kJ/mol on both sides, or no converged points exist, stop without extra angles."
sorted_conv = sorted([p for p in points if p["converged"] and p["relative_energy_kj_mol"] is not None], key=lambda p: p["requested_angle_deg"])
max_adj_delta = None
if len(sorted_conv) >= 2:
    deltas = [abs(sorted_conv[i+1]["relative_energy_kj_mol"] - sorted_conv[i]["relative_energy_kj_mol"]) for i in range(len(sorted_conv)-1)]
    max_adj_delta = max(deltas)
if len(sorted_conv) >= 2 and max_adj_delta is not None and max_adj_delta > 10:
    recommendation = "provisional: the converged grid shows a steep adjacent energy change (>10 kJ/mol); propose up to two midpoint angles adjacent to the minimum or steepest interval, with final choice made in the main decision loop after inspecting the full curve."
elif len(sorted_conv) >= 2:
    recommendation = "provisional: no additional angles according to the stated criterion; the converged grid's adjacent changes are <=10 kJ/mol and the minimum is bracketed without a >5 kJ/mol neighbor trigger."
else:
    recommendation = "provisional: insufficient converged points for a defensible refinement decision."
decision = {"criterion": criterion, "max_adjacent_converged_delta_kj_mol": max_adj_delta, "n_converged": len(sorted_conv), "recommendation": recommendation, "status": "preliminary_main_loop_decides"}

seed_text = "No random number generation is used. Seed 1701 is recorded per request; ORCA SCF/optimization and this Python workflow expose no stochastic random-seed control, so determinism is not claimed for ORCA's internal numerical algorithms."

bundle = {
    "initial_geometry": INITIAL_XYZ,
    "points": points,
    "failures": [p for p in points if p["failure_reason"] or not p["converged"]],
    "files": sorted(set(all_artifacts)),
    "method_basis_rationale": rationale,
    "scan_refinement_decision": decision,
    "random_seed_record": seed_text
}

observations = {
  "version": 1,
  "observations": [
    {"name":"constrained_dihedral_scan", "value":points, "unit":"mixed (deg, hartree, boolean)", "sources":sources, "formula":"E_rel = (E - E_min)*2625.499639 kJ/mol per hartree", "artifacts":sorted(set(all_artifacts)), "limitations":["Energies are raw ORCA final single point energies.", "Nonconverged points are flagged and are not treated as successful points."]},
    {"name":"relative_energy_curve", "value":rel_points, "unit":"kJ/mol", "sources":sources, "formula":"E_rel = (E - E_min)*2625.499639; E_min is the lowest converged scan point", "artifacts":sorted(set(all_artifacts)), "limitations":["Nonconverged points carry values relative to the converged reference but are flagged."]},
    {"name":"method_basis_rationale", "value":rationale, "unit":None, "sources":[], "formula":None, "artifacts":[], "limitations":["Rationale cites archived ORCA 6.1 manual excerpts; scientific qualification is not claimed."]},
    {"name":"scan_refinement_decision", "value":decision, "unit":None, "sources":sources, "formula":None, "artifacts":sorted(set(all_artifacts)), "limitations":["This is a preliminary decision aid; the final decision is made in the main decision loop after reviewing results."]},
    {"name":"random_seed_record", "value":seed_text, "unit":None, "sources":[], "formula":None, "artifacts":[], "limitations":[]},
    {"name":"delivery_bundle", "value":bundle, "unit":None, "sources":sources, "formula":None, "artifacts":sorted(set(all_artifacts)), "limitations":["Bundle is a preliminary manifest; nonconverged points remain failures."]}
  ]
}
with open("observations.json","w") as f:
    json.dump(observations, f, indent=2, default=str)
print("wrote observations.json with", len(points), "scan points")
