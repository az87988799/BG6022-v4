import json
from orca_task_sdk import run_orca, receipt_source

input_content = """! B3LYP def2-SVP RKS NoAutoStart
%maxcore 192
%scf
  MaxIter 256
  AutoTRAH false
end
* xyzfile 0 1 ethanol.xyz
"""
with open('job.inp', 'w') as f:
    f.write(input_content)

receipt = run_orca('job.inp', dependencies=['ethanol.xyz'], request_key='gas_sp', purpose='new_candidate')
files = receipt['files']
if 'stdout.out' in files:
    with open(files['stdout.out'], 'r') as src, open('gas_stdout.out', 'w') as dst:
        dst.write(src.read())
with open('job.inp', 'r') as src, open('gas_job.inp', 'w') as dst:
    dst.write(src.read())

energy = None
with open('gas_stdout.out', 'r') as f:
    lines = f.readlines()
for line in reversed(lines):
    if 'FINAL SINGLE POINT ENERGY' in line:
        energy = float(line.split()[-1])
        break

if energy is None:
    raise RuntimeError('Energy not found in ORCA output')

with open('gas_energy.json', 'w') as f:
    json.dump({'energy': energy}, f)

obs = {
    "version": 1,
    "observations": [
        {
            "name": "energy",
            "value": energy,
            "unit": "Eh",
            "sources": [
                receipt_source(receipt, 'stdout.out', location='FINAL SINGLE POINT ENERGY line')
            ],
            "formula": None,
            "artifacts": ["gas_stdout.out", "gas_job.inp"],
            "limitations": ["Unverified raw ORCA output; no independent check."]
        }
    ]
}
with open('observations.json', 'w') as f:
    json.dump(obs, f)
