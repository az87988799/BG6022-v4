from pathlib import Path
import json
from orca_task_sdk import run_orca
from rdkit import Chem
assert Chem.MolFromSmiles("O").GetNumAtoms() == 1
Path("water.inp").write_text("""! HF STO-3G TightSCF NoAutoStart
%maxcore 192
%scf maxiter 100 end
* xyz 0 1
O 0 0 0
H 0 .757 .587
H 0 -.757 .587
*
""")
receipt = run_orca("water.inp", request_key="water_sp")
Path("receipt.json").write_text(json.dumps(receipt, indent=2))
assert receipt["operation_status"] == "completed", receipt
text = Path(receipt["files"]["stdout.out"]).read_text()
assert "ORCA TERMINATED NORMALLY" in text
assert "FINAL SINGLE POINT ENERGY" in text
Path("smoke.json").write_text(json.dumps({"real_orca_completed": True, "scientific_validation": "unverified"}))
