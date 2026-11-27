"""Prepare a local, reproducible starting geometry or conformer set for ORCA."""

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen


def pubchem(name):
    base = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/"
    lookup = base + "name/" + quote(name, safe="") + "/cids/JSON?name_type=complete"
    try:
        with urlopen(lookup, timeout=25) as response:
            cids = json.load(response)["IdentifierList"]["CID"]
        if not isinstance(cids, list) or not cids or not all(type(cid) is int and cid > 0 for cid in cids):
            raise ValueError("PubChem returned an invalid CID list.")
        requested = cids[:16]
        detail = base + "cid/" + ",".join(map(str, requested)) + "/property/SMILES,IUPACName/JSON"
        with urlopen(detail, timeout=25) as response:
            rows = json.load(response)["PropertyTable"]["Properties"]
        choices = [{"cid": row["CID"], "name": row.get("IUPACName"), "smiles": row["SMILES"]} for row in rows]
        if {r["cid"] for r in choices} != set(requested) or not all(
            isinstance(r["smiles"], str) and r["smiles"] for r in choices
        ):
            raise ValueError("PubChem returned incomplete identity information.")
    except HTTPError as exc:
        raise ValueError(f"PubChem returned HTTP {exc.code} for the public name {name!r}.") from exc
    except (KeyError, TypeError, URLError, TimeoutError) as exc:
        raise ValueError(f"Could not resolve {name!r} through PubChem: {exc}") from exc
    return {"source": "PubChem", "public_name": name, "query_url": lookup, "property_url": detail,
            "match_count": len(cids), "options": choices}


def cluster(mol, ids, cutoff):
    """Keep ranked representatives under proper rotations, preserving mirror geometries."""
    from rdkit.Chem import rdMolAlign

    # Include polar hydrogens so an OH rotation can affect the distance.
    atoms = [a.GetIdx() for a in mol.GetAtoms() if a.GetAtomicNum() != 1 or
             any(neighbor.GetAtomicNum() != 6 for neighbor in a.GetNeighbors())]
    mapping = [(idx, idx) for idx in atoms]
    groups = {}
    for cid in ids:
        for rep, members in groups.items():
            rmsd, _ = rdMolAlign.GetAlignmentTransform(mol, mol, cid, rep, atomMap=mapping, reflect=False)
            if rmsd <= cutoff:
                members.append(cid)
                break
        else:
            groups[cid] = [cid]
    return groups


def optimize_xtb(mol, cid, exe, directory, charge, uhf):
    from rdkit import Chem

    directory.mkdir()
    (directory / "input.xyz").write_text(Chem.MolToXYZBlock(mol, confId=cid), encoding="ascii")
    args = [str(exe), "input.xyz", "--gfn", "2", "--opt", "normal", "--chrg", str(charge), "--uhf", str(uhf)]
    log = directory / "xtb.out"
    with log.open("wb") as stream:
        result = subprocess.run(args, cwd=directory, stdout=stream, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env={**os.environ, "OMP_NUM_THREADS": "1",
                                                               "MKL_NUM_THREADS": "1"})
    if result.returncode or "GEOMETRY OPTIMIZATION CONVERGED" not in log.read_text(errors="replace"):
        raise ValueError(f"xTB optimization did not converge for conformer {cid}; inspect {log}.")
    xyz = (directory / "xtbopt.xyz").read_text(encoding="ascii")
    energy = re.search(r"\benergy:\s*([-+\d.eEdD]+)", xyz.splitlines()[1], re.I)
    optimized = Chem.MolFromXYZBlock(xyz)
    if not optimized or [a.GetSymbol() for a in optimized.GetAtoms()] != [a.GetSymbol() for a in mol.GetAtoms()]:
        raise ValueError(f"xTB atom ordering is inconsistent: {directory / 'xtbopt.xyz'}")
    if not energy:
        raise ValueError(f"Missing optimized energy in {directory / 'xtbopt.xyz'}")
    value = float(energy.group(1).upper().replace("D", "E"))
    if not math.isfinite(value):
        raise ValueError(f"Invalid optimized energy in {directory / 'xtbopt.xyz'}")
    for i, pos in enumerate(optimized.GetConformer().GetPositions()):
        mol.GetConformer(cid).SetAtomPosition(i, pos)
    return value


def prepare(args):
    import rdkit
    from rdkit import Chem
    from rdkit.Chem import AllChem, Lipinski, rdMolTransforms
    from rdkit.Chem.EnumerateStereoisomers import EnumerateStereoisomers, StereoEnumerationOptions

    if args.conformers < 1 or not (0 <= args.seed <= 2147483647):
        raise ValueError("Specify positive --conformers and a nonnegative 32-bit --seed.")
    if not math.isfinite(args.rmsd) or args.rmsd <= 0:
        raise ValueError("Specify a positive, finite RMSD cutoff in angstrom.")
    directory = args.out_dir.resolve()
    if directory.exists():
        raise ValueError(f"Choose a new preparation directory: {directory}")
    if args.name:
        identity = pubchem(args.name)
        if identity["match_count"] != 1:
            return {"status": "clarify_identity", "identity": identity}
        identity.update(identity["options"][0])
    else:
        identity = {"source": "local_smiles", "smiles": args.smiles}
    parent = Chem.MolFromSmiles(identity["smiles"])
    if parent is None or not parent.GetNumAtoms():
        raise ValueError("RDKit could not parse this SMILES.")
    if len(Chem.GetMolFrags(parent)) != 1 or any(a.GetIsotope() or a.GetAtomicNum() == 0 for a in parent.GetAtoms()):
        raise ValueError("Specify one connected, isotope-unlabelled molecule for this XYZ preparation.")
    if any(info.specified == Chem.StereoSpecified.Unspecified for info in Chem.FindPotentialStereo(parent)):
        isomers = EnumerateStereoisomers(parent, options=StereoEnumerationOptions(maxIsomers=8, onlyUnassigned=True))
        return {"status": "clarify_stereochemistry", "identity": identity,
                "example_options_smiles": [Chem.MolToSmiles(m) for m in isomers]}

    mol = Chem.AddHs(parent)
    params = AllChem.ETKDGv3()
    params.randomSeed = args.seed
    params.numThreads = 1
    ids = list(AllChem.EmbedMultipleConfs(mol, numConfs=args.conformers, params=params))
    if not ids:
        raise ValueError("ETKDGv3 did not produce a geometry.")
    if AllChem.MMFFHasAllMoleculeParams(mol):
        force_field = "MMFF94"
        opt = AllChem.MMFFOptimizeMoleculeConfs(mol, numThreads=1, maxIters=1000)
    elif AllChem.UFFHasAllMoleculeParams(mol):
        force_field = "UFF"
        opt = AllChem.UFFOptimizeMoleculeConfs(mol, numThreads=1, maxIters=1000)
    else:
        raise ValueError("No complete MMFF94 or UFF parameters; supply a local XYZ geometry.")
    samples = {cid: {"converged": code == 0 and math.isfinite(e),
                     "energy_kcal_mol": e if math.isfinite(e) else None} for cid, (code, e) in zip(ids, opt)}
    ranked = sorted((cid for cid in ids if samples[cid]["converged"]), key=lambda cid: samples[cid]["energy_kcal_mol"])
    if not ranked:
        raise ValueError(f"No {force_field} optimization converged; supply a local XYZ geometry.")
    groups = cluster(mol, ranked, args.rmsd)
    charge = Chem.GetFormalCharge(mol)
    if args.xtb:
        if not args.xtb.is_file() or args.uhf is None or args.uhf < 0:
            raise ValueError("xTB requires an executable path and explicit --uhf (unpaired electron count).")
        electrons = sum(a.GetAtomicNum() for a in mol.GetAtoms()) - charge
        if args.uhf > electrons or (electrons - args.uhf) % 2:
            raise ValueError("--uhf conflicts with the electron count.")
    directory.mkdir(parents=True)
    xtb_energies = {}
    if args.xtb:
        for cid in groups:
            xtb_energies[cid] = optimize_xtb(mol, cid, args.xtb.resolve(), directory / f"xtb-{cid}", charge, args.uhf)
        post = cluster(mol, sorted(groups, key=xtb_energies.get), args.rmsd)
        groups = {rep: [cid for old in reps for cid in groups[old]] for rep, reps in post.items()}

    torsions = []
    for j, k in parent.GetSubstructMatches(Lipinski.RotatableBondSmarts):
        left = [a.GetIdx() for a in parent.GetAtomWithIdx(j).GetNeighbors() if a.GetIdx() != k]
        right = [a.GetIdx() for a in parent.GetAtomWithIdx(k).GetNeighbors() if a.GetIdx() != j]
        if left and right:
            torsions.append((min(left), j, k, min(right)))
    candidates = []
    for n, (rep, members) in enumerate(groups.items(), start=1):
        path = directory / f"candidate-{n:02d}.xyz"
        path.write_text(Chem.MolToXYZBlock(mol, confId=rep, precision=10), encoding="ascii")
        angles = [{"atoms_1based": [i + 1 for i in tor],
                   "degrees": rdMolTransforms.GetDihedralDeg(mol.GetConformer(rep), *tor)} for tor in torsions]
        candidates.append({"file": path.name, "representative_id": rep, "sample_ids": members, "dihedrals": angles})
    return {"status": "prepared", "identity": identity, "formal_charge": charge,
            "canonical_isomeric_smiles": Chem.MolToSmiles(parent), "rdkit_version": rdkit.__version__,
            "generator": "ETKDGv3", "seed": args.seed, "requested": args.conformers, "embedded": len(ids),
            "force_field": force_field, "samples": samples, "rmsd_cutoff_angstrom": args.rmsd,
            "selection": "Lowest preoptimization energy per aligned atom-index RMSD cluster; no reflection. "
                         "Carbon-bound H omitted. Sample hits are not thermodynamic degeneracies.",
            "xtb": ({"executable": str(args.xtb.resolve()), "model": "GFN2-xTB", "uhf": args.uhf,
                     "energies_Eh": xtb_energies} if args.xtb else None), "candidates": candidates}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--name", help="Public molecular name to send to PubChem")
    identity.add_argument("--smiles", help="Local isomeric SMILES (offline)")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--conformers", type=int, default=1)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--rmsd", type=float, default=0.25, help="Deduplication cutoff in angstrom")
    parser.add_argument("--xtb", type=Path, help="Optional local xTB executable for representative preoptimization")
    parser.add_argument("--uhf", type=int, help="Unpaired electrons for xTB (0 for a singlet)")
    args = parser.parse_args()
    if args.uhf is not None and not args.xtb:
        parser.error("--uhf applies only with --xtb")
    try:
        result = prepare(args)
        data = json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False)
        if result["status"] == "prepared":
            (args.out_dir / "preparation.json").write_text(data + "\n", encoding="utf-8")
        print(data)
    except ModuleNotFoundError as exc:
        print(f"Structure preparation requires RDKit in this Python environment: {exc}", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0 if result["status"] == "prepared" else 2


if __name__ == "__main__":
    sys.exit(main())
