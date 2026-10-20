import json, math, re, shutil
from pathlib import Path
from orca_task_sdk import run_orca, checkpoint

ANGLES = [-180, -120, -60, 0, 60, 120, 180]
GEOM = 'butane_initial.xyz'
HARTREE_TO_KJ = 2625.4996394799
REFINEMENT_CRITERION = (
    'Predeclared refinement rule: after the seven coarse constrained optimizations, sort points by actual dihedral. '
    'Refine only if (i) adjacent relative energies show a slope sign change bracketing an interior extremum, or '
    '(ii) an adjacent pair differs by >2.0 kJ/mol while the lower-energy point is not at a requested angle and a midpoint could lower the energy by >0.5 kJ/mol; add at most two midpoints. '
    'Do not refine if the coarse curve is monotonic, all adjacent differences <0.5 kJ/mol, or convergence failures prevent reliable comparison. '
    'This is a numerical sampling criterion, not transition-state validation.'
)

def read_lines(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        return f.readlines()

def parse_xyz(path):
    lines = read_lines(path)
    n = int(lines[0].split()[0])
    atoms = []
    for line in lines[2:2+n]:
        p = line.split()
        atoms.append((p[0], float(p[1]), float(p[2]), float(p[3])))
    return atoms

def vsub(a,b):
    return (a[0]-b[0], a[1]-b[1], a[2]-b[2])

def vdot(a,b):
    return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]

def vcross(a,b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])

def vscale(a,s):
    return (a[0]*s, a[1]*s, a[2]*s)

def vnorm(a):
    return math.sqrt(vdot(a,a))

def dist(a,b):
    return math.sqrt(sum((a[i]-b[i])**2 for i in range(1,4)))

def carbon_indices(atoms):
    carbons = [i for i,a in enumerate(atoms) if a[0].upper() == 'C']
    if len(carbons) != 4:
        raise ValueError('expected 4 carbons in butane, got %d' % len(carbons))
    adj = {i: [] for i in carbons}
    for i in carbons:
        for j in carbons:
            if i < j and dist(atoms[i], atoms[j]) < 1.8:
                adj[i].append(j); adj[j].append(i)
    ends = [i for i in carbons if len(adj[i]) == 1]
    if len(ends) != 2:
        raise ValueError('could not find butane carbon chain endpoints: %s' % str(ends))
    start, target = ends
    stack = [(start, [start])]
    while stack:
        node, path = stack.pop()
        if node == target and len(path) == 4:
            return path
        for nb in adj[node]:
            if nb not in path:
                stack.append((nb, path + [nb]))
    raise ValueError('could not order four-carbon chain')

def dihedral_deg(atoms, idx):
    p0 = atoms[idx[0]][1:]
    p1 = atoms[idx[1]][1:]
    p2 = atoms[idx[2]][1:]
    p3 = atoms[idx[3]][1:]
    b0 = vsub(p0,p1)
    b1 = vsub(p2,p1)
    b2 = vsub(p3,p2)
    b1n = vscale(b1, 1.0/vnorm(b1))
    v = vsub(b0, vscale(b1n, vdot(b0,b1n)))
    w = vsub(b2, vscale(b1n, vdot(b2,b1n)))
    x = vdot(v,w)
    y = vdot(vcross(b1n,v),w)
    return math.degrees(math.atan2(y,x))

def read_text_if_exists(path):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception:
        return ''

def find_text(files):
    for key in ['stdout.out','job.out','output.out']:
        if key in files:
            return key, files[key]
    for key,path in files.items():
        if key.lower().endswith('.out'):
            return key, path
    return None, None

def last_energy(text):
    ms = re.findall(r'FINAL SINGLE POINT ENERGY\s+(-?\d+(?:\.\d+)?)', text)
    return float(ms[-1]) if ms else None

def is_converged(text):
    return ('HURRAY! THE OPTIMIZATION HAS CONVERGED' in text) or ('THE OPTIMIZATION HAS CONVERGED' in text)

atoms = parse_xyz(GEOM)
cidx = carbon_indices(atoms)
orca_idx = [i+1 for i in cidx]
Path('refinement_criterion.txt').write_text(REFINEMENT_CRITERION + '\n', encoding='utf-8')

records = []
sources_by_name = {name: [] for name in ['sampling','energy','optimized_geometry','member_table','energy_difference','sampling_check','random_seed']}

for angle in ANGLES:
    tag = str(angle).replace('-', 'm')
    key = 'coarse_dihedral_' + tag
    inp = 'scan_' + tag + '.inp'
    input_text = '! r2SCAN-3c RKS NoAutoStart\n%maxcore 192\n%scf\n  MaxIter 256\n  AutoTRAH false\nend\n%geom\n  MaxIter 128\n  Constraints\n    {D %d %d %d %d %.1f C}\n  end\nend\n* xyzfile 0 1 %s\n' % (orca_idx[0], orca_idx[1], orca_idx[2], orca_idx[3], float(angle), GEOM)
    Path(inp).write_text(input_text, encoding='utf-8')
    rec = {
        'requested_angle_deg': angle,
        'actual_angle_deg': None,
        'energy_hartree': None,
        'converged': False,
        'structure_file': None,
        'stdout_file': None,
        'stderr_file': None,
        'request_key': key,
        'operation_status': None,
        'error': None,
        'files_keys': []
    }
    try:
        res = run_orca(inp, dependencies=(GEOM,), request_key=key, purpose='new_candidate')
        rec['operation_status'] = res.get('operation_status')
        files = res.get('files') or {}
        rec['files_keys'] = sorted(files.keys())
        xyz_keys = [k for k in files.keys() if k.lower().endswith('.xyz') and k != GEOM]
        geom_key = 'job.xyz' if 'job.xyz' in files else (xyz_keys[0] if xyz_keys else None)
        if geom_key:
            out_xyz = 'member_' + tag + '.xyz'
            shutil.copyfile(files[geom_key], out_xyz)
            rec['structure_file'] = out_xyz
            final_atoms = parse_xyz(out_xyz)
            rec['actual_angle_deg'] = dihedral_deg(final_atoms, cidx)
            sources_by_name['optimized_geometry'].append({'internal_request_key': key, 'filename': geom_key, 'location': 'final optimized geometry'})
            sources_by_name['member_table'].append({'internal_request_key': key, 'filename': geom_key, 'location': 'final optimized geometry'})
        log_key, log_path = find_text(files)
        if log_key:
            text = read_text_if_exists(log_path)
            rec['energy_hartree'] = last_energy(text)
            rec['converged'] = is_converged(text)
            out_log = 'member_' + tag + '.out'
            Path(out_log).write_text(text, encoding='utf-8')
            rec['stdout_file'] = out_log
            loc_e = 'FINAL SINGLE POINT ENERGY' if rec['energy_hartree'] is not None else 'optimization/convergence log'
            sources_by_name['energy'].append({'internal_request_key': key, 'filename': log_key, 'location': loc_e})
            sources_by_name['member_table'].append({'internal_request_key': key, 'filename': log_key, 'location': 'energy and convergence status'})
            sources_by_name['sampling'].append({'internal_request_key': key, 'filename': log_key, 'location': 'requested dihedral %s deg' % angle})
        if 'stderr.txt' in files:
            out_err = 'member_' + tag + '.err.txt'
            shutil.copyfile(files['stderr.txt'], out_err)
            rec['stderr_file'] = out_err
    except Exception as e:
        rec['error'] = str(e)
        rec['operation_status'] = 'sdk_exception'
        rec['input_file'] = inp
    records.append(rec)

success = [r for r in records if r.get('energy_hartree') is not None and not r.get('error')]
min_e = min([r['energy_hartree'] for r in success]) if success else None
energy_diff = []
for r in success:
    rel = (r['energy_hartree'] - min_e) * HARTREE_TO_KJ
    energy_diff.append({
        'requested_angle_deg': r['requested_angle_deg'],
        'actual_angle_deg': r['actual_angle_deg'],
        'energy_hartree': r['energy_hartree'],
        'relative_energy_kj_mol': rel
    })
    sources_by_name['energy_difference'].append({'internal_request_key': r['request_key'], 'filename': r.get('stdout_file') or '', 'location': 'FINAL SINGLE POINT ENERGY'})

obs = []
def add(name, value, unit, sources, formula=None, artifacts=None, limitations=None):
    obs.append({
        'name': name,
        'value': value,
        'unit': unit,
        'sources': sources,
        'formula': formula,
        'artifacts': artifacts or [],
        'limitations': limitations or []
    })

add('sampling', {'requested_angles_deg': ANGLES, 'actual_angles_deg': [r['actual_angle_deg'] for r in records], 'method': 'r2SCAN-3c', 'basis': 'built-in', 'job': 'constrained geometry optimization at fixed central C-C-C-C dihedral'}, 'deg', sources_by_name['sampling'], artifacts=[r.get('structure_file') for r in records if r.get('structure_file')], limitations=['Constrained optimizations fix the dihedral; they do not validate transition states.'])
add('energy', [{'requested_angle_deg': r['requested_angle_deg'], 'actual_angle_deg': r['actual_angle_deg'], 'energy_hartree': r['energy_hartree'], 'converged': r['converged']} for r in records], 'hartree', sources_by_name['energy'], artifacts=[r.get('stdout_file') for r in records if r.get('stdout_file')], limitations=['Only parsed output-evidenced FINAL SINGLE POINT ENERGY values are included; failed/nonconverged points remain in member_table.'])
add('optimized_geometry', [{'requested_angle_deg': r['requested_angle_deg'], 'actual_angle_deg': r['actual_angle_deg'], 'structure_file': r.get('structure_file'), 'converged': r['converged']} for r in records], 'deg', sources_by_name['optimized_geometry'], artifacts=[r.get('structure_file') for r in records if r.get('structure_file')], limitations=['Structures are final geometries from each constrained optimization; nonconverged jobs are not treated as successful points.'])
add('member_table', records, None, sources_by_name['member_table'], artifacts=[a for r in records for a in [r.get('structure_file'), r.get('stdout_file'), r.get('stderr_file'), r.get('input_file')] if a], limitations=['Failed members and exclusion reasons are retained; no failed member is counted as a successful point.'])
add('energy_difference', energy_diff, 'kJ/mol', sources_by_name['energy_difference'], formula='relative_energy_kj_mol = (E_i - min(E_success)) * 2625.4996394799', artifacts=[r.get('stdout_file') for r in success if r.get('stdout_file')], limitations=['Relative energies are for the discrete constrained optimized points only; this is not a transition-state or global-optimum claim.'])
add('sampling_check', REFINEMENT_CRITERION, None, [], limitations=['The refinement decision is not made in this execution; it is returned to the main decision loop after checkpoint.'])
add('random_seed', 1701, None, [], artifacts=[], limitations=['No random number generator is used by these deterministic constrained optimizations; the requested seed is recorded.'])

with open('observations.json', 'w', encoding='utf-8') as f:
    json.dump({'version': 1, 'observations': obs}, f, indent=2)

state = {
    'observations_file': 'observations.json',
    'refinement_criterion_file': 'refinement_criterion.txt',
    'angles': ANGLES,
    'members': records,
    'energy_difference': energy_diff,
    'completed_request_keys': [r['request_key'] for r in records]
}
checkpoint(state, request_key='coarse_scan_done', requires_decision=True, reason='Assess the coarse butane dihedral scan and apply the predeclared refinement criterion.')
