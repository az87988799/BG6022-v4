import json, re, math
from pathlib import Path
import numpy as np
from orca_task_sdk import run_orca
lines = Path("initial.xyz").read_text().splitlines()
count = int(lines[0])
records = lines[2:2+count]
assert len(records) == count
atoms = [line.split()[0] for line in records]
xyz = np.array([[float(x) for x in line.split()[1:4]] for line in records])
u = np.array([1.,2.,3.]); u /= np.linalg.norm(u)
t = math.radians(37); K = np.array([[0,-u[2],u[1]],[u[2],0,-u[0]],[-u[1],u[0],0]])
R = np.eye(3)*math.cos(t)+(1-math.cos(t))*np.outer(u,u)+math.sin(t)*K
rot = xyz @ R.T
Path("rotated.xyz").write_text(str(len(atoms))+"\nactive right handed 37 deg r'=Rr\n"+
    "\n".join(a+" "+" ".join(f"{v:.12f}" for v in row) for a,row in zip(atoms,rot))+"\n")
assert np.allclose(np.linalg.norm(xyz[:,None]-xyz[None,:],axis=2),
                   np.linalg.norm(rot[:,None]-rot[None,:],axis=2),atol=1e-10)
tensors, receipts = [], []
for key, geometry in [("original","initial.xyz"),("rotated","rotated.xyz")]:
    Path(key+".inp").write_text('! HF def2-SVP RHF TightSCF NoAutoStart\n%maxcore 192\n%pal nprocs 4 end\n%scf MaxIter 128 AutoTRAH false end\n%elprop Polar true end\n* xyzfile 0 1 GEOMETRY\n'.replace("GEOMETRY",geometry))
    r = run_orca(key+".inp",dependencies=[geometry],request_key=key,purpose="new_candidate")
    assert r["operation_status"] == "completed",r
    text = Path(r["files"]["stdout.out"]).read_text()
    assert "ORCA TERMINATED NORMALLY" in text
    section = text.split("STATIC POLARIZABILITY TENSOR (Dipole/Dipole)",1)[1]
    block = section.split("The raw cartesian tensor (atomic units):",1)[1].split("diagonalized",1)[0]
    A = np.array([[float(x) for x in line.split()] for line in block.strip().splitlines()[:3]])
    assert A.shape == (3,3) and np.isfinite(A).all()
    tensors.append(A); receipts.append(r)
error = float(np.max(np.abs(tensors[1] - R @ tensors[0] @ R.T)))
assert error < 1e-3,error
assert abs(np.trace(tensors[1])-np.trace(tensors[0]))/3 < 1e-4
Path("rotation.json").write_text(json.dumps({"tensors":[x.tolist() for x in tensors],
    "R":R.tolist(),"max_abs_covariance_error_au":error,"receipts":receipts,
    "scientific_validation":"unverified"}))
