import json, re, math
from pathlib import Path
from orca_task_sdk import run_orca
Path("excitation.inp").write_text('! PBE0 def2-SVP RKS TightSCF NoAutoStart\n%maxcore 192\n%pal nprocs 4 end\n%scf MaxIter 128 AutoTRAH false end\n%tddft NRoots 5 MaxIter 128 TDA false Triplets false end\n* xyzfile 0 1 initial.xyz\n')
r = run_orca("excitation.inp", dependencies=["initial.xyz"], request_key="excitation")
assert r["operation_status"] == "completed", r
text = Path(r["files"]["stdout.out"]).read_text()
assert "ORCA TERMINATED NORMALLY" in text
section = text.split("ABSORPTION SPECTRUM VIA TRANSITION ELECTRIC DIPOLE MOMENTS", 1)[1]
rows = []
for line in section.splitlines():
    m = re.match(r"\s*0-1A\s*->\s*(\d+)-1A\s+(.+)", line)
    if m:
        values = [float(x) for x in m.group(2).split()]
        assert len(values) >= 6 and all(math.isfinite(x) for x in values)
        rows.append({"root": int(m.group(1)), "values": values})
        if len(rows) == 5: break
assert [r["root"] for r in rows] == [1,2,3,4,5], rows
assert all(row["values"][0] > 0 and row["values"][3] >= 0 for row in rows)
Path("parsed.json").write_text(json.dumps({"rows": rows, "receipt": r,
    "basis": "actual stdout length-gauge table; raw columns retained", "scientific_validation": "unverified"}))
