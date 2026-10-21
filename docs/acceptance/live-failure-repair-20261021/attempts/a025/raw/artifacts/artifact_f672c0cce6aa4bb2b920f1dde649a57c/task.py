import json, re
from pathlib import Path

text = Path('stdout.out').read_text(errors='replace')

isotropic_values = []
tensor_values = []

# Find nucleus blocks
nucleus_pattern = re.compile(r'Nucleus\s+(\d+)\s+([A-Z][a-z]?)', re.I)
for m in nucleus_pattern.finditer(text):
    idx = m.group(1)
    elem = m.group(2)
    start = m.end()
    end = text.find('Nucleus', start)
    if end == -1:
        end = len(text)
    section = text[start:end]
    iso_match = re.search(r'Isotropic\s+(?:shielding\s+)?(?:constant\s*)?[=:]\s*([-+]?\d+\.\d+)', section, re.I)
    if iso_match:
        isotropic_values.append(float(iso_match.group(1)))
    tensor_sec = section.split('Shielding tensor')
    if len(tensor_sec) > 1:
        block = tensor_sec[1]
        lines = [line for line in block.strip().splitlines() if re.match(r'^\s*[-+]?\d+\.\d+\s+[-+]?\d+\.\d+\s+[-+]?\d+\.\d+', line)]
        if len(lines) >= 3:
            tensor = [[float(x) for x in line.split()[:3]] for line in lines[:3]]
            tensor_values.append({'element': elem, 'tensor_ppm': tensor})

observations = {
    'version': 1,
    'observations': [
        {
            'name': 'nmr_shielding',
            'value': {
                'isotropic_shielding_ppm': isotropic_values,
                'tensors': tensor_values
            },
            'unit': 'ppm',
            'sources': [
                {
                    'filename': 'stdout.out',
                    'location': 'NMR shielding sections'
                }
            ],
            'formula': 'GIAO shielding tensor trace/3',
            'artifacts': ['stdout.out'],
            'limitations': ['Parsed from ORCA output; verify tensor completeness; isotropics may be incomplete if not found']
        }
    ]
}
Path('observations.json').write_text(json.dumps(observations, indent=2))
