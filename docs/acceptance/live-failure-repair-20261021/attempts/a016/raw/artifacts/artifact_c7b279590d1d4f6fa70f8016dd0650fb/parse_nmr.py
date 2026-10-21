import json, re, sys
from pathlib import Path

out = Path('stdout.out').read_text(errors='replace')

# Locate the total shielding tensor block from ORCA NMR output.
# ORCA prints per-nucleus blocks like:
#   Nucleus  0H :
#   ...
#   Total shielding tensor (ppm):
#   ...
# We parse the 'Total shielding tensor' 3x3 matrices and isotropic values.

lines = out.splitlines()

# Find atom order from the input geometry echo (C, N, H order as in xyz).
atom_order = []
for i, ln in enumerate(lines):
    if 'CARTESIAN COORDINATES (ANGSTROEM)' in ln:
        j = i + 1
        while j < len(lines) and lines[j].strip() == '':
            j += 1
        # skip dashes line
        while j < len(lines) and set(lines[j].strip()) <= set('-'):
            j += 1
        while j < len(lines) and lines[j].strip():
            parts = lines[j].split()
            if len(parts) >= 4:
                atom_order.append(parts[0])
            j += 1
        break

# Parse total shielding tensors.
# ORCA format (per nucleus):
#   Nucleus   0C :
#   ...
#   Total shielding tensor (ppm):
#   ...
#   <row1>
#   <row2>
#   <row3>
#   ...
#   Isotropic shielding (ppm): <value>

results = []
idx = 0
while idx < len(lines):
    ln = lines[idx]
    m = re.match(r'\s*Nucleus\s+(\d+)([A-Za-z]+)\s*:', ln)
    if m:
        nuc_num = int(m.group(1))
        elem = m.group(2)
        # search forward for 'Total shielding tensor'
        k = idx + 1
        tensor = None
        iso = None
        while k < len(lines):
            if 'Total shielding tensor' in lines[k]:
                # next non-empty lines are 3 rows
                rows = []
                kk = k + 1
                while kk < len(lines) and len(rows) < 3:
                    s = lines[kk].strip()
                    if s and not set(s) <= set('-'):
                        nums = re.findall(r'-?\d+\.\d+', s)
                        if len(nums) >= 3:
                            rows.append([float(x) for x in nums[:3]])
                    kk += 1
                if len(rows) == 3:
                    tensor = rows
                k = kk
                continue
            if 'Isotropic shielding' in lines[k]:
                nums = re.findall(r'-?\d+\.\d+', lines[k])
                if nums:
                    iso = float(nums[0])
                break
            if re.match(r'\s*Nucleus\s+\d+', lines[k]):
                break
            k += 1
        if tensor is not None:
            trace = tensor[0][0] + tensor[1][1] + tensor[2][2]
            iso_from_trace = trace / 3.0
            results.append({
                'nucleus_index': nuc_num,
                'element': elem,
                'tensor_ppm': tensor,
                'isotropic_ppm': iso,
                'isotropic_from_trace_ppm': iso_from_trace,
                'trace_ppm': trace,
                'trace_consistency_abs_diff': (abs(iso - iso_from_trace) if iso is not None else None),
            })
        idx = k
    else:
        idx += 1

# Build observation records.
obs = []
if results:
    obs.append({
        'name': 'nmr_shielding',
        'value': results,
        'unit': 'ppm',
        'sources': [{'filename': 'stdout.out', 'location': 'Total shielding tensor / Isotropic shielding blocks'}],
        'formula': 'isotropic = trace(tensor)/3',
        'artifacts': [],
        'limitations': ['Parsed from ORCA stdout; total shielding only, no reference/chemical shift.'],
    })
    # shielding_tensor_table observation: same data, explicit table form
    table = []
    for r in results:
        table.append({
            'nucleus_index': r['nucleus_index'],
            'element': r['element'],
            'tensor_ppm': r['tensor_ppm'],
            'isotropic_ppm': r['isotropic_ppm'],
            'trace_ppm': r['trace_ppm'],
            'trace_consistency_abs_diff_ppm': r['trace_consistency_abs_diff'],
        })
    obs.append({
        'name': 'shielding_tensor_table',
        'value': table,
        'unit': 'ppm',
        'sources': [{'filename': 'stdout.out', 'location': 'Total shielding tensor / Isotropic shielding blocks'}],
        'formula': 'isotropic = trace(tensor)/3',
        'artifacts': [],
        'limitations': ['Cartesian total shielding tensors in ppm; isotropic from trace/3; no reference standard.'],
    })
    # delivery_bundle observation: summary of files
    obs.append({
        'name': 'delivery_bundle',
        'value': {'files': ['hcn.xyz', 'hcn_nmr.inp', 'stdout.out', 'parse_nmr.py'], 'note': 'initial geometry, input, raw output, parser'},
        'unit': None,
        'sources': [{'filename': 'stdout.out', 'location': 'whole file'}],
        'formula': None,
        'artifacts': [],
        'limitations': ['Bundle lists task-local files; geometry is initial prepared geometry, not optimized.'],
    })

Path('observations.json').write_text(json.dumps({'version': 1, 'observations': obs}, indent=2))
print(json.dumps({'n_nuclei': len(results), 'observations': [o['name'] for o in obs]}))
