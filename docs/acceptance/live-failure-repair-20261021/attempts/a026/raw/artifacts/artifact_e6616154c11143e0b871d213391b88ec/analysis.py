import json
import re
import hashlib
from pathlib import Path

def extract_energy(path):
    text = Path(path).read_text()
    vals = []
    for i, line in enumerate(text.splitlines(), 1):
        if 'FINAL SINGLE POINT ENERGY' in line:
            m = re.search(r'FINAL SINGLE POINT ENERGY\s+(-?\d+\.\d+)', line)
            if m:
                vals.append((i, float(m.group(1)), line.strip()))
    if not vals:
        raise ValueError(f'no final single point energy in {path}')
    return vals[-1]

gas_line, gas_e, gas_line_text = extract_energy('gas_stdout.out')
cpcm_line, cpcm_e, cpcm_line_text = extract_energy('cpcm_stdout.out')

HARTREE_TO_KJMOL = 2625.499638
delta_hartree = cpcm_e - gas_e
delta_kj = delta_hartree * HARTREE_TO_KJMOL

files = [
    ('gas_input.inp', 'gas-phase ORCA input'),
    ('gas_stdout.out', 'gas-phase ORCA output'),
    ('cpcm_input.inp', 'CPCM water ORCA input'),
    ('cpcm_stdout.out', 'CPCM water ORCA output'),
    ('coords.xyz', 'shared XYZ geometry'),
]
file_info = []
for fname, purpose in files:
    p = Path(fname)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    file_info.append({'path': fname, 'sha256': sha, 'purpose': purpose})

diff_obs = {
    'name': 'energy_difference',
    'value': delta_kj,
    'unit': 'kJ/mol',
    'sources': [
        {'filename': 'gas_stdout.out', 'location': f'line {gas_line}: {gas_line_text}'},
        {'filename': 'cpcm_stdout.out', 'location': f'line {cpcm_line}: {cpcm_line_text}'},
    ],
    'formula': 'deltaE = E(CPCM water) - E(gas phase); 1 Eh = 2625.499638 kJ/mol',
    'artifacts': ['gas_stdout.out', 'cpcm_stdout.out'],
    'limitations': [
        'Electronic single-point energy difference only; not a free energy of solvation; no thermal, zero-point, or standard-state corrections.'
    ],
}

delivery_obs = {
    'name': 'input_output_files',
    'value': file_info,
    'unit': None,
    'sources': [
        {'filename': 'gas_input.inp', 'location': 'delivery input'},
        {'filename': 'cpcm_input.inp', 'location': 'delivery input'},
        {'filename': 'coords.xyz', 'location': 'shared geometry'},
    ],
    'formula': None,
    'artifacts': [f['path'] for f in file_info],
    'limitations': [],
}

payload = {'version': 1, 'observations': [diff_obs, delivery_obs]}
Path('observations.json').write_text(json.dumps(payload, indent=2))
print(json.dumps(payload))
