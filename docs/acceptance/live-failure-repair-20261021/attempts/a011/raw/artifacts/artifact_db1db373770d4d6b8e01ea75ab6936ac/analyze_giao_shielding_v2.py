import json
import math

def read_lines(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as fh:
        return fh.read().splitlines()

prop = read_lines('job.property.txt')
xyz = read_lines('hcn.xyz')

n_atoms = int(xyz[0].split()[0])
elements = []
for ln in xyz[2:2 + n_atoms]:
    elements.append(ln.split()[0])
assert len(elements) == n_atoms, elements

blocks = []
i = 0
while i < len(prop):
    if '&STot' in prop[i]:
        rows = {}
        start = i + 1
        j = i + 1
        while j < len(prop):
            s = prop[j].strip()
            if s.startswith('&'):
                break
            parts = s.split()
            if len(parts) == 4 and parts[0] in ('0', '1', '2'):
                try:
                    vals = [float(parts[1]), float(parts[2]), float(parts[3])]
                except ValueError:
                    vals = None
                if vals is not None:
                    rows[int(parts[0])] = vals
            j += 1
        if sorted(rows.keys()) == [0, 1, 2]:
            blocks.append({'start': start, 'end': j, 'tensor': [rows[0], rows[1], rows[2]]})
        i = j if j > i else i + 1
    else:
        i += 1

assert len(blocks) == n_atoms, [len(blocks), n_atoms]

coord = 'Cartesian x,y,z in the ORCA input geometry frame (Angstrom coordinates); tensor components in ppm'

tensor_items = []
iso_items = []
check_items = []
max_abs_diff = 0.0
src_tensors = []
src_iso = []
src_check = []

for idx in range(len(elements)):
    el = elements[idx]
    blk = blocks[idx]
    t = blk['tensor']
    tr = t[0][0] + t[1][1] + t[2][2]
    iso = tr / 3.0
    three_iso = 3.0 * iso
    diff = three_iso - tr
    abs_diff = abs(diff)
    max_abs_diff = max(max_abs_diff, abs_diff)
    for v in t[0] + t[1] + t[2] + [tr, iso, three_iso, diff, abs_diff]:
        assert math.isfinite(v)
    loc = 'lines ' + str(blk['start']) + '-' + str(blk['end']) + ' (&STot total tensor block ' + str(idx) + ')'
    src_tensors.append({'filename': 'task/job.property.txt', 'location': loc})
    src_iso.append({'filename': 'task/job.property.txt', 'location': loc})
    src_check.append({'filename': 'task/job.property.txt', 'location': loc})
    tensor_items.append({'nucleus': el, 'index': idx, 'tensor_ppm': t, 'trace_ppm': tr, 'coordinate_system': coord})
    iso_items.append({'nucleus': el, 'index': idx, 'isotropic_ppm': iso, 'trace_ppm': tr, 'coordinate_system': coord})
    check_items.append({'nucleus': el, 'index': idx, 'trace_ppm': tr, 'three_times_isotropic_ppm': three_iso, 'difference_ppm': diff, 'absolute_difference_ppm': abs_diff})

geom_src = {'filename': 'task/hcn.xyz', 'location': 'lines 3-' + str(2 + n_atoms) + ' (ordered elements ' + ','.join(elements) + ')'}
src_tensors.append(geom_src)

limits_common = [
    'Total GIAO shielding tensors from the archived ORCA property output; no reference compound or experimental calibration applied.',
    'PBE0/def2-TZVP single-point calculation on the fixed initial geometry; geometry not optimized and no relativistic correction requested.',
    'Gas phase, neutral singlet, RKS as specified.'
]

doc = {
    'version': 1,
    'observations': [
        {
            'name': 'nmr_shielding_tensor',
            'value': {'coordinate_system': coord, 'tensors': tensor_items},
            'unit': 'ppm',
            'sources': src_tensors,
            'formula': None,
            'artifacts': ['job.property.txt', 'hcn.xyz'],
            'limitations': limits_common + ['Tensors are printed in the ORCA output Cartesian frame, which matches the ordered atom frame in hcn.xyz.']
        },
        {
            'name': 'isotropic_shielding',
            'value': {'coordinate_system': coord, 'nuclei': iso_items},
            'unit': 'ppm',
            'sources': src_iso,
            'formula': 'isotropic shielding = (sigma_xx + sigma_yy + sigma_zz) / 3',
            'artifacts': ['job.property.txt'],
            'limitations': limits_common + ['Isotropic values are derived from the diagonal of the printed total tensor; no separate isotropic printout was used.']
        },
        {
            'name': 'trace_consistency_check',
            'value': {'coordinate_system': coord, 'checks': check_items, 'max_absolute_difference_ppm': max_abs_diff},
            'unit': 'ppm',
            'sources': src_check,
            'formula': 'difference = 3 * isotropic - trace(sigma); consistency means the difference is floating-point round-off',
            'artifacts': ['job.property.txt'],
            'limitations': limits_common + ['Because isotropic is defined as trace/3, the check tests arithmetic/printing consistency only; residual is expected near floating-point zero.']
        }
    ]
}

with open('observations.json', 'w', encoding='utf-8') as fh:
    json.dump(doc, fh, indent=2, ensure_ascii=False)

print(json.dumps({'elements': elements, 'isotropic_ppm': [item['isotropic_ppm'] for item in iso_items], 'max_absolute_difference_ppm': max_abs_diff}, indent=2))
