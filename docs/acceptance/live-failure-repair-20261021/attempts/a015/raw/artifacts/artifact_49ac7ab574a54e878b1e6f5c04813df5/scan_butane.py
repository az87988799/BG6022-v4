import json, math, os
from orca_task_sdk import run_orca, checkpoint, receipt_source

ANGLES = [-180, -120, -60, 0, 60, 120, 180]

with open('initial.xyz') as f:
    lines = f.read().splitlines()
natoms = int(lines[0].strip())
atom_lines = lines[2:2+natoms]

# identify the four backbone carbons C1-C2-C3-C4 (first four C atoms in order)
carbon_idx = [i for i, l in enumerate(atom_lines) if l.split()[0].upper() == 'C']
assert len(carbon_idx) >= 4, carbon_idx
c1, c2, c3, c4 = carbon_idx[0], carbon_idx[1], carbon_idx[2], carbon_idx[3]

def write_xyz(path, coords):
    with open(path, 'w') as f:
        f.write(f'{len(coords)}\n\n')
        for el, (x, y, z) in coords:
            f.write(f'{el} {x:.8f} {y:.8f} {z:.8f}\n')

base_coords = []
for l in atom_lines:
    p = l.split()
    base_coords.append((p[0], (float(p[1]), float(p[2]), float(p[3]))))

results = []
for ang in ANGLES:
    key = f'scan_{ang}'
    xyz_name = f'start_{ang}.xyz'
    write_xyz(xyz_name, base_coords)
    inp = f'''! B3LYP def2-SVP Opt
%maxcore 192
%scf MaxIter 256
  AutoTRAH false
end
%geom MaxIter 128
  Constraints
    {{ B {c2+1} {c1+1} {c3+1} {c4+1} {ang} C }}
  end
end
* xyzfile 0 1 {xyz_name}
'''
    inp_name = f'job_{ang}.inp'
    with open(inp_name, 'w') as f:
        f.write(inp)
    try:
        rec = run_orca(inp_name, dependencies=[xyz_name], request_key=key, purpose='new_candidate')
    except Exception as e:
        results.append({'angle_requested': ang, 'request_key': key, 'error': str(e), 'converged': False})
        continue
    files = rec.get('files', {})
    entry = {'angle_requested': ang, 'request_key': key, 'operation_status': rec.get('operation_status'),
             'scientific_validation': rec.get('scientific_validation'), 'files': list(files.keys())}
    # parse final energy and convergence from stdout
    energy = None
    converged = False
    actual_angle = None
    if 'stdout.out' in files:
        with open(files['stdout.out']) as f:
            txt = f.read()
        for line in txt.splitlines():
            if 'FINAL SINGLE POINT ENERGY' in line:
                try:
                    energy = float(line.split()[-1])
                except Exception:
                    pass
            if 'THE OPTIMIZATION HAS CONVERGED' in line:
                converged = True
            if 'HURRAY' in line:
                converged = True
    # final geometry
    final_xyz = None
    if 'job.xyz' in files:
        final_xyz = files['job.xyz']
        with open(final_xyz) as f:
            fl = f.read().splitlines()
        try:
            n = int(fl[0].strip())
            coords = []
            for l in fl[2:2+n]:
                p = l.split()
                coords.append((float(p[1]), float(p[2]), float(p[3])))
            def vec(a, b):
                return (b[0]-a[0], b[1]-a[1], b[2]-a[2])
            def cross(u, v):
                return (u[1]*v[2]-u[2]*v[1], u[2]*v[0]-u[0]*v[2], u[0]*v[1]-u[1]*v[0])
            def dot(u, v):
                return u[0]*v[0]+u[1]*v[1]+u[2]*v[2]
            def norm(u):
                return math.sqrt(dot(u, u))
            b1 = vec(coords[c1], coords[c2])
            b2 = vec(coords[c2], coords[c3])
            b3 = vec(coords[c3], coords[c4])
            n1 = cross(b1, b2)
            n2 = cross(b2, b3)
            m = cross(n1, (b2[0]/norm(b2), b2[1]/norm(b2), b2[2]/norm(b2)))
            x = dot(n1, n2)
            y = dot(m, n2)
            actual_angle = math.degrees(math.atan2(y, x))
        except Exception as e:
            entry['angle_parse_error'] = str(e)
    entry['energy'] = energy
    entry['converged'] = converged
    entry['actual_angle'] = actual_angle
    entry['final_xyz'] = final_xyz
    results.append(entry)

# relative energies
valid = [r for r in results if r.get('energy') is not None]
if valid:
    emin = min(r['energy'] for r in valid)
    for r in results:
        if r.get('energy') is not None:
            r['relative_energy_kcal'] = (r['energy'] - emin) * 627.509474

with open('scan_results.json', 'w') as f:
    json.dump(results, f, indent=2)

checkpoint({'results': results}, request_key='coarse_scan_done', requires_decision=True,
           reason='Coarse scan complete; decide on refinement per pre-stated criterion')
