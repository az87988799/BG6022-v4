import csv, json, math
from pathlib import Path

def row(line):
    p = line.strip().split()
    if len(p) < 4:
        return None
    try:
        i = int(p[0])
        v = [float(x) for x in p[1:4]]
    except Exception:
        return None
    return i, v

lines = Path('job.property.txt').read_text(errors='replace').splitlines()
elements = None
for k, l in enumerate(lines):
    if l.strip().startswith('&Elems'):
        vals = []
        j = k + 1
        while j < len(lines):
            s = lines[j].strip()
            if s.startswith('&'):
                break
            if chr(34) in s:
                for q in s.split(chr(34))[1::2]:
                    if q:
                        vals.append(q)
            elif s and len(s) <= 2 and s.isalpha():
                vals.append(s)
            j += 1
        if vals:
            elements = vals
        break

blocks = []
i = 0
while i < len(lines):
    if '&STot' in lines[i]:
        h = i + 1
        rows = []
        rl = []
        j = i + 1
        while j < len(lines):
            s = lines[j].strip()
            if not s:
                if rows:
                    break
                j += 1
                continue
            if s.startswith('&'):
                break
            r = row(lines[j])
            if r and len(rows) < 3:
                rows.append(r)
                rl.append(j + 1)
                j += 1
                continue
            j += 1
        if len(rows) == 3:
            blocks.append({'h': h, 'rows': rows, 'rl': rl})
            i = j
            continue
    i += 1

if len(blocks) != 3:
    raise RuntimeError('expected 3 &STot blocks, found %d' % len(blocks))

labels = elements[:3] if elements and len(elements) >= 3 else ['C', 'N', 'H']
labels = [str(x) for x in labels]
tensors = []
iso = []
trace = []
for b in blocks:
    rows = sorted(b['rows'], key=lambda x: x[0])
    if [r[0] for r in rows] != [0, 1, 2]:
        raise RuntimeError('unexpected tensor row indices: %r' % ([r[0] for r in rows],))
    t = [r[1] for r in rows]
    for r in t:
        for x in r:
            if not math.isfinite(x):
                raise RuntimeError('non-finite tensor value')
    tensors.append(t)
    tr = sum([t[0][0], t[1][1], t[2][2]])
    isv = tr / 3.0
    iso.append(isv)
    trace.append({'trace_ppm': tr, 'three_times_isotropic_ppm': 3.0 * isv, 'deviation_ppm': isv - tr / 3.0})

tensor_entries = []
iso_entries = []
trace_entries = []
for n, (lab, t) in enumerate(zip(labels, tensors)):
    tensor_entries.append({'nucleus': lab, 'index': n, 'tensor_ppm': t, 'coordinate_system': 'Cartesian x,y,z as written by ORCA property output'})
    iso_entries.append({'nucleus': lab, 'index': n, 'isotropic_ppm': iso[n]})
    trace_entries.append({'nucleus': lab, 'index': n, 'trace_ppm': trace[n]['trace_ppm'], 'three_times_isotropic_ppm': trace[n]['three_times_isotropic_ppm'], 'deviation_ppm': trace[n]['deviation_ppm']})

maxdev = max([abs(x['deviation_ppm']) for x in trace_entries])
src = []
for b in blocks:
    src.append({'filename': 'task/job.property.txt', 'location': 'lines %d-%d' % (b['h'], max(b['rl']))})
lim = ['Parsed from ORCA property output; GIAO total shielding tensors, not reference-calibrated chemical shifts.', 'Nucleus assignment follows parsed &Elems order and ORCA &STot block order.', 'No experimental reference or spectrum used.']
obs = [
    {'name': 'nmr_shielding_tensor', 'value': {'tensors': tensor_entries}, 'unit': 'ppm', 'sources': src, 'formula': None, 'artifacts': ['giao_shielding_tensors.csv'], 'limitations': lim},
    {'name': 'isotropic_shielding', 'value': {'isotropic': iso_entries}, 'unit': 'ppm', 'sources': src, 'formula': 'isotropic_ppm = (sigma_xx + sigma_yy + sigma_zz)/3', 'artifacts': ['giao_shielding_tensors.csv'], 'limitations': lim},
    {'name': 'trace_consistency_check', 'value': {'per_nucleus': trace_entries, 'max_abs_deviation_ppm': maxdev, 'consistent': maxdev < 1e-8}, 'unit': 'ppm', 'sources': src, 'formula': 'deviation_ppm = isotropic_ppm - trace_ppm/3', 'artifacts': ['giao_shielding_tensors.csv'], 'limitations': lim},
]
Path('observations.json').write_text(json.dumps({'version': 1, 'observations': obs}, indent=2, allow_nan=False))
with open('giao_shielding_tensors.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['nucleus', 'index', 'sigma_xx_ppm', 'sigma_xy_ppm', 'sigma_xz_ppm', 'sigma_yx_ppm', 'sigma_yy_ppm', 'sigma_yz_ppm', 'sigma_zx_ppm', 'sigma_zy_ppm', 'sigma_zz_ppm', 'isotropic_ppm', 'trace_ppm', 'deviation_ppm'])
    for n, lab in enumerate(labels):
        t = tensors[n]
        w.writerow([lab, n] + [t[r][c] for r in range(3) for c in range(3)] + [iso[n], trace[n]['trace_ppm'], trace[n]['deviation_ppm']])
