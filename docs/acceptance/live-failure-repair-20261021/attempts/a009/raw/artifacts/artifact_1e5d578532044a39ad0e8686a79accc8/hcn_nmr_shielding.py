import json
import re
from pathlib import Path
from orca_task_sdk import run_orca, receipt_source

def dump(path, obj):
    Path(path).write_text(json.dumps(obj, indent=2, allow_nan=False), encoding='utf-8')

input_text = '! PBE0 def2-TZVP RKS NoAutoStart\n%maxcore 192\n%scf\n  MaxIter 256\n  AutoTRAH false\nend\n* xyzfile 0 1 hcn.xyz\n%eprnmr\n  NMR true\nend\n'
Path('job.inp').write_text(input_text, encoding='utf-8')
initial_geometry = Path('hcn.xyz').read_text(encoding='utf-8')
err = None
receipt = None
try:
    receipt = run_orca('job.inp', dependencies=('hcn.xyz',), request_key='nmr_shielding', purpose='new_candidate', retry_of=None)
except Exception as exc:
    err = f'{type(exc).__name__}: {exc}'

float_re = re.compile(r'[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[Ee][-+]?\d+)?')
atom_re = re.compile(r'^\s*(?:Nucleus|Atom)\s*[:\-]?\s*(\d+)\s*[:\-]?\s*([A-Za-z]{1,3})?', re.I)

def get_floats(line):
    return [float(x) for x in float_re.findall(line)]

atoms = []
stdout_text = ''
files = {}
if receipt:
    files = receipt.get('files', {})
    if 'stdout.out' in files:
        try:
            stdout_text = Path(files['stdout.out']).read_text(encoding='utf-8', errors='replace')
        except Exception as exc:
            err = (err or '') + f'; read stdout failed: {type(exc).__name__}: {exc}'
    lines = stdout_text.splitlines()
    i = 0
    while i < len(lines):
        m = atom_re.search(lines[i])
        if not m:
            i += 1
            continue
        idx = int(m.group(1))
        elem = (m.group(2) or '').upper()
        tensor = None
        reported_iso = None
        j = i + 1
        limit = min(len(lines), i + 80)
        while j < limit:
            low = lines[j].lower()
            if 'isotropic' in low and reported_iso is None:
                vals = get_floats(lines[j])
                if vals:
                    reported_iso = vals[0]
            if 'total shielding' in low and 'tensor' in low:
                rows = []
                k = j + 1
                while k < min(len(lines), j + 30) and len(rows) < 3:
                    vals = get_floats(lines[k])
                    if len(vals) >= 3:
                        rows.append(vals[:3])
                    k += 1
                if len(rows) == 3:
                    tensor = rows
                    break
            j += 1
        if tensor is not None and not any(a['atom_index'] == idx for a in atoms):
            atoms.append({'atom_index': idx, 'element': elem, 'tensor_ppm': tensor, 'reported_isotropic_ppm': reported_iso})
        i = j + 1 if tensor is not None else i + 1
    atoms.sort(key=lambda a: a['atom_index'])

checks = []
iso_constants = []
tensors = []
for a in atoms:
    t = a['tensor_ppm']
    trace = t[0][0] + t[1][1] + t[2][2]
    iso = trace / 3.0
    rep = a.get('reported_isotropic_ppm')
    diff = None if rep is None else iso - rep
    tensors.append({'atom_index': a['atom_index'], 'element': a['element'], 'tensor_ppm': t})
    iso_constants.append({'atom_index': a['atom_index'], 'element': a['element'], 'isotropic_ppm': iso})
    checks.append({'atom_index': a['atom_index'], 'element': a['element'], 'trace_ppm': trace, 'isotropic_from_tensor_ppm': iso, 'reported_isotropic_ppm': rep, 'difference_ppm': diff})

with open('nmr_tensors.csv', 'w', encoding='utf-8') as fh:
    fh.write('atom_index,element,tensor_xx_ppm,tensor_xy_ppm,tensor_xz_ppm,tensor_yx_ppm,tensor_yy_ppm,tensor_yz_ppm,tensor_zx_ppm,tensor_zy_ppm,tensor_zz_ppm,isotropic_ppm,trace_ppm,reported_isotropic_ppm,difference_ppm\n')
    for a, c in zip(atoms, checks):
        t = a['tensor_ppm']
        rep = a.get('reported_isotropic_ppm')
        vals = [a['atom_index'], a['element']] + [t[r][col] for r in range(3) for col in range(3)] + [c['isotropic_from_tensor_ppm'], c['trace_ppm'], rep, c['difference_ppm']]
        fh.write(','.join('' if v is None else str(v) for v in vals) + '\n')

summary = {
    'request_key': 'nmr_shielding',
    'purpose': 'new_candidate',
    'operation_status': None if receipt is None else receipt.get('operation_status'),
    'scientific_validation': None if receipt is None else receipt.get('scientific_validation'),
    'actual_conditions': None if receipt is None else receipt.get('actual_conditions'),
    'files_keys': sorted(files.keys()),
    'initial_geometry': initial_geometry,
    'actual_input': input_text,
    'parsed_atoms': atoms,
    'isotropic_constants_ppm': iso_constants,
    'trace_checks': checks,
    'error': err,
}
dump('nmr_summary.json', summary)

limitations = []
if err:
    limitations.append(err)
limitations.append('Shielding values are raw ORCA GIAO outputs; no reference-standard calibration or experimental comparison was performed.')
limitations.append('The geometry is the prepared fixed initial geometry, not a PBE0/def2-TZVP reoptimization.')
limitations.append('No random numbers are used in this script; any randomness in structure preparation is outside this script and was not reseeded here.')

sources = []
if receipt is not None:
    try:
        sources = [receipt_source(receipt, 'stdout.out', location='ORCA stdout.out: total shielding tensor blocks')]
    except Exception as exc:
        limitations.append(f'receipt_source failed: {type(exc).__name__}: {exc}')

observations = [
    {'name': 'total_cartesian_shielding_tensor', 'value': {'frame': 'cartesian', 'tensors_ppm': tensors}, 'unit': 'ppm', 'sources': sources, 'formula': None, 'artifacts': ['nmr_tensors.csv', 'nmr_summary.json'], 'limitations': limitations},
    {'name': 'isotropic_nmr_shielding_constant', 'value': {'constants_ppm': iso_constants}, 'unit': 'ppm', 'sources': sources, 'formula': 'isotropic_ppm = (s_xx + s_yy + s_zz) / 3', 'artifacts': ['nmr_tensors.csv', 'nmr_summary.json'], 'limitations': limitations},
    {'name': 'shielding_trace_consistency_check', 'value': {'checks_ppm': checks}, 'unit': 'ppm', 'sources': sources, 'formula': 'difference_ppm = isotropic_from_tensor_ppm - reported_isotropic_ppm', 'artifacts': ['nmr_tensors.csv', 'nmr_summary.json'], 'limitations': limitations},
    {'name': 'delivery_report', 'value': 'HCN GIAO NMR shielding calculation using PBE0/def2-TZVP RKS, charge 0, multiplicity 1, gas phase, on the prepared fixed initial geometry. Files: job.inp (actual input), hcn.xyz (initial/computational geometry), nmr_tensors.csv (machine-readable 3x3 tensors and isotropic values), nmr_summary.json (parsed summary and receipt metadata), observations.json. Seed 1701 is not applicable in this analysis script because no random numbers are used here.', 'unit': None, 'sources': sources, 'formula': None, 'artifacts': ['job.inp', 'hcn.xyz', 'nmr_tensors.csv', 'nmr_summary.json', 'observations.json'], 'limitations': limitations},
]
dump('observations.json', {'version': 1, 'observations': observations})
