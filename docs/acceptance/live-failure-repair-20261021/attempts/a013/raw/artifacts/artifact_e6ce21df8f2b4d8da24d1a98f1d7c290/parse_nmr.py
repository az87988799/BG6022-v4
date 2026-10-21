import json, re
from pathlib import Path

text = Path('stdout.out').read_text(errors='replace')

# Locate the GIAO shielding tensor block(s)
# ORCA prints: 'CHEMICAL SHIELDING SUMMARY (ppm)' then per-nucleus blocks
# with lines like 'Nucleus   0C :' followed by 3x3 tensor rows.
lines = text.splitlines()

# Find all nucleus tensor blocks
nucleus_blocks = []
i = 0
while i < len(lines):
    m = re.match(r'\s*Nucleus\s+(\d+)\s*([A-Za-z]{1,2})\s*:', lines[i])
    if m:
        idx = int(m.group(1)); elem = m.group(2)
        tensor = []
        j = i + 1
        while j < len(lines) and len(tensor) < 3:
            row = lines[j].strip()
            nums = re.findall(r'-?\d+\.\d+', row)
            if len(nums) >= 3:
                tensor.append([float(x) for x in nums[:3]])
            j += 1
        if len(tensor) == 3:
            nucleus_blocks.append({'index': idx, 'element': elem, 'tensor': tensor})
        i = j
    else:
        i += 1

observations = []
for blk in nucleus_blocks:
    t = blk['tensor']
    iso = (t[0][0] + t[1][1] + t[2][2]) / 3.0
    observations.append({
        'name': 'nmr_shielding',
        'value': {
            'nucleus_index': blk['index'],
            'element': blk['element'],
            'tensor_ppm': t,
            'isotropic_ppm': round(iso, 6),
            'trace_ppm': round(t[0][0] + t[1][1] + t[2][2], 6),
            'coordinate_system': 'cartesian (ORCA GIAO, ppm)'
        },
        'unit': 'ppm',
        'sources': [{'filename': 'stdout.out', 'location': 'CHEMICAL SHIELDING SUMMARY'}],
        'formula': 'isotropic = (sxx+syy+szz)/3',
        'artifacts': [],
        'limitations': ['Parsed from ORCA output; scientific qualification pending.']
    })

out = {'version': 1, 'observations': observations}
Path('observations.json').write_text(json.dumps(out, indent=2))
print(json.dumps(out, indent=2))
