import json, re, math
from pathlib import Path
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolAlign
from orca_task_sdk import run_orca
m = Chem.AddHs(Chem.MolFromSmiles("CCCCCC"))
ids = list(AllChem.EmbedMultipleConfs(m,numConfs=6,randomSeed=1701,pruneRmsThresh=-1))
assert len(ids)==6
# Copy only after conformers exist; use actual IDs, never positional assumptions.
probe = Chem.Mol(m)
rms = rdMolAlign.GetBestRMS(probe,m,prbId=ids[0],refId=ids[1])
assert math.isfinite(rms)
rows = []
for i,cid in enumerate(ids):
    Path(f"candidate_{i}.xyz").write_text(Chem.MolToXYZBlock(m,confId=cid))
for i in range(2):
    geometry=f"candidate_{i}.xyz"
    Path(f"candidate_{i}.inp").write_text('! HF STO-3G RHF TightSCF NoAutoStart\n%maxcore 192\n%pal nprocs 4 end\n%scf MaxIter 128 AutoTRAH false end\n* xyzfile 0 1 GEOMETRY\n'.replace("GEOMETRY",geometry))
    r=run_orca(f"candidate_{i}.inp",dependencies=[geometry],request_key=f"candidate_{i}",purpose="new_candidate")
    assert r["operation_status"]=="completed",r
    text=Path(r["files"]["stdout.out"]).read_text()
    energy=float(re.findall(r"FINAL SINGLE POINT ENERGY\s+([-+0-9.]+)",text)[-1])
    assert math.isfinite(energy) and "ORCA TERMINATED NORMALLY" in text
    rows.append({"candidate":i,"energy_hartree":energy,"receipt":r})
Path("conformers.json").write_text(json.dumps({"generated":6,"computed":2,"excluded_by_scope":4,
    "rms_probe":rms,"rows":rows,"scientific_validation":"unverified",
    "limitations":["developer probe; no global minimum or full task claim"]}))
