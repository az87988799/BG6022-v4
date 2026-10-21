import os, json, re, math, shutil
from collections import defaultdict
import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem
from orca_task_sdk import run_orca

SEED = 1701
N_CONFS = 32
RMSD_THRESHOLD = 0.15
MAX_SELECT = 6
os.makedirs('candidates', exist_ok=True)

def parse_xyz(path):
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
    n = int(lines[0].strip())
    atoms = []
    for line in lines[2:2+n]:
        p = line.split()
        if len(p) >= 4:
            atoms.append((p[0], float(p[1]), float(p[2]), float(p[3])))
    return atoms

anchor = parse_xyz('anchor.xyz')
anchor_symbols = [a[0] for a in anchor]
if len(anchor) != 20:
    raise RuntimeError(f'anchor.xyz has {len(anchor)} atoms, expected 20 for n-hexane')

mol = Chem.AddHs(Chem.MolFromSmiles('CCCCCC'))
params = AllChem.ETKDGv3()
params.randomSeed = SEED
params.numThreads = 1
conf_ids = list(AllChem.EmbedMultipleConfs(mol, numConfs=N_CONFS, params=params))
if len(conf_ids) < 8:
    raise RuntimeError(f'RDKit generated only {len(conf_ids)} conformers')
symbols = [a.GetSymbol() for a in mol.GetAtoms()]
carbon_indices = [i for i, s in enumerate(symbols) if s == 'C']
if len(carbon_indices) != 6:
    raise RuntimeError('expected 6 carbons')

mmff_props = AllChem.MMFFGetMoleculeProperties(mol, mmffVariant='MMFF94s')
ff_name = 'MMFF94s' if mmff_props is not None else 'UFF'
optimization = []
for cid in conf_ids:
    if mmff_props is not None:
        ret = AllChem.MMFFOptimizeMolecule(mol, mmffVariant='MMFF94s', confId=cid, maxIters=500)
        ff = AllChem.MMFFGetMoleculeForceField(mol, mmff_props, confId=cid)
    else:
        ret = AllChem.UFFOptimizeMolecule(mol, confId=cid, maxIters=500)
        ff = AllChem.UFFGetMoleculeForceField(mol, confId=cid)
    energy = float(ff.CalcEnergy()) if ff is not None else None
    optimization.append({'conf_id': int(cid), 'converged': bool(ret == 0), 'forcefield_energy': energy})

def reorder_to_anchor(sym, coords):
    pools = defaultdict(list)
    for i, s in enumerate(sym):
        pools[s].append(i)
    order = []
    for s in anchor_symbols:
        if not pools[s]:
            return sym, coords, False
        order.append(pools[s].pop(0))
    if len(order) != len(sym):
        return sym, coords, False
    return [sym[i] for i in order], coords[order], True

def write_xyz(path, sym, coords):
    with open(path, 'w', encoding='utf-8') as f:
        print(len(sym), file=f)
        print('n-hexane candidate generated with RDKit ETKDGv3 seed 1701', file=f)
        for s, (x, y, z) in zip(sym, coords):
            print(f'{s} {x:.8f} {y:.8f} {z:.8f}', file=f)

candidates = []
for idx, cid in enumerate(conf_ids):
    conf = mol.GetConformer(cid)
    coords = np.array(conf.GetPositions(), dtype=float)
    rsyms, rcoords, match = reorder_to_anchor(symbols, coords)
    path = f'candidates/cand_{idx:02d}.xyz'
    write_xyz(path, rsyms, rcoords)
    heavy = coords[carbon_indices]
    candidates.append({'id': f'cand_{idx:02d}', 'path': path, 'symbols': rsyms, 'coords': rcoords, 'heavy_coords': heavy, 'forcefield_energy': optimization[idx]['forcefield_energy'], 'converged': optimization[idx]['converged'], 'element_order_matches_anchor': bool(match)})

def kabsch_rmsd(P, Q):
    Pc = P - P.mean(axis=0)
    Qc = Q - Q.mean(axis=0)
    H = Pc.T @ Qc
    U, S, Vt = np.linalg.svd(H)
    d = float(np.sign(np.linalg.det(Vt.T @ U.T)))
    D = np.diag([1.0, 1.0, d])
    R = Vt.T @ D @ U.T
    P_rot = (R @ Pc.T).T
    return float(np.sqrt(np.mean(np.sum((P_rot - Qc) ** 2, axis=1))))

def min_chain_rmsd(A, B):
    return min(kabsch_rmsd(A, B), kabsch_rmsd(A, B[::-1]))

selected = []
selection_reasons = {}
exclusion_reasons = {}
pairwise_selected = []
for cand in candidates:
    if len(selected) >= MAX_SELECT:
        exclusion_reasons[cand['id']] = f'not selected: reached MAX_SELECT={MAX_SELECT} distinct conformers'
        continue
    rmsds = [min_chain_rmsd(cand['heavy_coords'], s['heavy_coords']) for s in selected]
    if all(r >= RMSD_THRESHOLD for r in rmsds):
        if selected:
            pairwise_selected.append({'a': selected[-1]['id'], 'b': cand['id'], 'min_rmsd_A': float(min(rmsds))})
        selected.append(cand)
        selection_reasons[cand['id']] = f'selected for ORCA SP comparison; min RMSD to prior selected >= {RMSD_THRESHOLD} A'
    else:
        j = int(np.argmin(rmsds))
        sid = selected[j]['id']
        exclusion_reasons[cand['id']] = f'duplicate: min heavy-atom chain-reversal RMSD {rmsds[j]:.4f} A < {RMSD_THRESHOLD} A to {sid}'

if len(selected) < 4:
    raise RuntimeError(f'only {len(selected)} distinct conformers found at {RMSD_THRESHOLD} A threshold')

energy_runs = []
for i, cand in enumerate(selected):
    xyz_name = f'conf_{i}.xyz'
    inp_name = f'conf_{i}.inp'
    write_xyz(xyz_name, cand['symbols'], cand['coords'])
    with open(inp_name, 'w', encoding='utf-8') as f:
        print('! r2SCAN-3c RKS SP NoAutoStart', file=f)
        print('%maxcore 192', file=f)
        print('%scf', file=f)
        print('  MaxIter 256', file=f)
        print('  AutoTRAH false', file=f)
        print('end', file=f)
        print(f'* xyzfile 0 1 {xyz_name}', file=f)
    key = f'sp_conf_{i}'
    rec = {'candidate': cand['id'], 'request_key': key, 'input': inp_name, 'xyz': xyz_name, 'energy_hartree': None, 'status': None, 'error': None, 'stdout_local': None}
    try:
        res = run_orca(inp_name, dependencies=[xyz_name], request_key=key, purpose='new_candidate')
    except Exception as exc:
        rec['status'] = 'sdk_exception'
        rec['error'] = str(exc)
        energy_runs.append(rec)
        continue
    rec['status'] = res.get('operation_status')
    rec['scientific_validation'] = res.get('scientific_validation')
    files = res.get('files', {}) or {}
    stdout_path = files.get('stdout.out')
    if stdout_path and os.path.exists(stdout_path):
        local_stdout = f'conf_{i}.stdout.out'
        shutil.copyfile(stdout_path, local_stdout)
        rec['stdout_local'] = local_stdout
        text = open(stdout_path, 'r', encoding='utf-8', errors='replace').read()
        matches = re.findall(r'FINAL SINGLE POINT ENERGY[ ]+(-?[0-9]+[.][0-9]+)', text)
        if matches:
            rec['energy_hartree'] = float(matches[-1])
    energy_runs.append(rec)

successful = [r for r in energy_runs if r['energy_hartree'] is not None]
if len(successful) < 4:
    raise RuntimeError(f'only {len(successful)} ORCA single-point energies parsed; need at least 4')

min_e = min(r['energy_hartree'] for r in successful)
HARTREE_TO_KJ = 2625.499639
for r in successful:
    r['relative_kj_mol'] = float((r['energy_hartree'] - min_e) * HARTREE_TO_KJ)
successful_sorted = sorted(successful, key=lambda r: r['energy_hartree'])
for rank, r in enumerate(successful_sorted, start=1):
    r['rank'] = rank
lowest = successful_sorted[0]

candidate_value = [{'id': c['id'], 'path': c['path'], 'forcefield_energy': c['forcefield_energy'], 'forcefield_converged': c['converged'], 'selected_for_orca': c['id'] in selection_reasons, 'selection_reason': selection_reasons.get(c['id']) or exclusion_reasons.get(c['id'])} for c in candidates]
energy_value = {'successful': [{'candidate': r['candidate'], 'absolute_energy_hartree': r['energy_hartree'], 'relative_kj_mol': r.get('relative_kj_mol'), 'rank': r.get('rank')} for r in successful_sorted], 'failed': [{'candidate': r['candidate'], 'status': r['status'], 'error': r['error']} for r in energy_runs if r['energy_hartree'] is None], 'lowest_candidate': lowest['candidate'], 'lowest_absolute_energy_hartree': lowest['energy_hartree']}
all_candidate_artifacts = [c['path'] for c in candidates]
selected_artifacts = [c['path'] for c in selected] + [r['input'] for r in energy_runs] + [r['xyz'] for r in energy_runs] + [r['stdout_local'] for r in energy_runs if r['stdout_local']]
concat = []
for x in selected_artifacts:
    if x and x not in concat:
        concat.append(x)

def local_source(path):
    return {'filename': path, 'location': None}

def sdk_source(r):
    return {'internal_request_key': r['request_key'], 'filename': 'stdout.out', 'location': 'FINAL SINGLE POINT ENERGY' if r['energy_hartree'] is not None else None}

pairwise_min = min([p['min_rmsd_A'] for p in pairwise_selected], default=None)
converged_count = sum(1 for c in candidates if c['converged'])
not_converged = [c['id'] for c in candidates if not c['converged']]

observations = [
    {'name': 'conformer_search', 'value': {'method': 'RDKit ETKDGv3 + ' + ff_name + ' pre-optimization', 'num_generated': len(candidates), 'num_saved': len(candidates), 'num_selected_for_orca': len(selected), 'random_seed': SEED, 'forcefield': ff_name, 'forcefield_converged': converged_count, 'forcefield_not_converged': len(candidates) - converged_count}, 'unit': None, 'sources': [local_source('anchor.xyz')] + [local_source(c['path']) for c in candidates], 'formula': None, 'artifacts': all_candidate_artifacts, 'limitations': ['Finite conformer search, not exhaustive or global.', 'Force-field geometries only for unselected candidates.']},
    {'name': 'candidate_structures', 'value': {'count': len(candidates), 'candidates': candidate_value}, 'unit': None, 'sources': [local_source(c['path']) for c in candidates], 'formula': None, 'artifacts': all_candidate_artifacts, 'limitations': ['All saved candidates are force-field generated/optimized; not ORCA energies.']},
    {'name': 'selected_conformers', 'value': {'count': len(selected), 'selected': [{'id': c['id'], 'path': c['path'], 'selection_reason': selection_reasons[c['id']]} for c in selected]}, 'unit': None, 'sources': [local_source(c['path']) for c in selected], 'formula': None, 'artifacts': [c['path'] for c in selected], 'limitations': [f'Selected from finite {len(candidates)}-candidate set; not global minimum.']},
    {'name': 'rmsd_duplicate_check', 'value': {'threshold_A': RMSD_THRESHOLD, 'heavy_atoms': 'six carbons', 'chain_reversal_considered': True, 'selected_pairwise_min_rmsd_A': pairwise_min, 'selected_pairs': pairwise_selected}, 'unit': 'angstrom', 'sources': [local_source(c['path']) for c in candidates], 'formula': 'min over direct/reversed carbon-chain mappings of Kabsch heavy-atom RMSD', 'artifacts': all_candidate_artifacts, 'limitations': ['Only heavy atoms (carbons) considered; hydrogens excluded from RMSD.']},
    {'name': 'energy', 'value': energy_value, 'unit': 'hartree; relative kJ/mol', 'sources': [sdk_source(r) for r in energy_runs], 'formula': 'relative_kj_mol = (E_i - E_min) * 2625.499639', 'artifacts': concat, 'limitations': ['ORCA single-point energies at r2SCAN-3c/RKS/gas phase on selected force-field geometries.', 'Lowest is lowest among ORCA-evaluated selected conformers, not global or free-energy minimum.']},
    {'name': 'delivery_report', 'value': {'all_candidates': candidate_value, 'selected': [c['id'] for c in selected], 'exclusion_reasons': exclusion_reasons, 'energy_ranking': energy_value['successful'], 'lowest_candidate': lowest['candidate'], 'lowest_candidate_path': next(c['path'] for c in selected if c['id'] == lowest['candidate']), 'method_basis_reason': 'r2SCAN-3c is a self-contained composite method with built-in basis, giving uniform method/basis/SP comparison for all selected conformers.'}, 'unit': None, 'sources': [sdk_source(r) for r in energy_runs], 'formula': 'relative_kj_mol = (E_i - E_min) * 2625.499639', 'artifacts': all_candidate_artifacts + concat, 'limitations': ['Finite search; lowest is not global or free-energy minimum.', 'All unselected candidates lack ORCA energies.']},
    {'name': 'search_scope_report', 'value': {'search_scope': f'RDKit ETKDGv3 generated {len(candidates)} conformers of n-hexane from SMILES CCCCCC, followed by {ff_name} optimization.', 'geometry_source': 'Prepared initial XYZ anchor.xyz; candidate conformers generated by RDKit from n-hexane SMILES and optimized with force field.', 'forcefield': ff_name, 'forcefield_converged': converged_count, 'forcefield_not_converged': len(candidates) - converged_count, 'not_converged_ids': not_converged, 'random_seed': SEED}, 'unit': None, 'sources': [local_source('anchor.xyz')] + [local_source(c['path']) for c in candidates], 'formula': None, 'artifacts': all_candidate_artifacts, 'limitations': ['Force-field convergence does not establish ORCA scientific validity.', 'Finite search scope.']},
    {'name': 'random_seed_record', 'value': {'seed': SEED, 'rdkit_embed_randomSeed': SEED, 'forcefield': ff_name, 'forcefield_deterministic': True, 'orca_randomness': 'not applicable to single-point energy'}, 'unit': None, 'sources': [], 'formula': None, 'artifacts': [], 'limitations': ['RDKit supports fixed seed; ORCA SP does not use random numbers here.']}
]

with open('observations.json', 'w', encoding='utf-8') as f:
    json.dump({'version': 1, 'observations': observations}, f, indent=2, allow_nan=False)
