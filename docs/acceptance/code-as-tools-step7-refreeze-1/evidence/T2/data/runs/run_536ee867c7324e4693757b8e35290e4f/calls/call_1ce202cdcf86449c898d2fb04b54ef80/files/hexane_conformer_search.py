import json, math, os, shutil
from rdkit import Chem
from rdkit.Chem import AllChem, rdMolAlign

SEED = 1701
THRESH = 0.15
N_EMBED = 60
N_SELECT = 5
HARTREE_TO_KJ = 2625.499639

def xyz_text(m, cid, comment):
    conf = m.GetConformer(cid)
    lines = [str(m.GetNumAtoms()), comment]
    for i, a in enumerate(m.GetAtoms()):
        p = conf.GetAtomPosition(i)
        lines.append('%s %.6f %.6f %.6f' % (a.GetSymbol(), p.x, p.y, p.z))
    return '\n'.join(lines) + '\n'

mol = Chem.AddHs(Chem.MolFromSmiles('CCCCCC'))
mh = Chem.RemoveHs(Chem.Mol(mol))
ps = AllChem.ETKDGv3()
ps.randomSeed = SEED
ps.numThreads = 1
ps.pruneRmsThresh = -1.0
cids = list(AllChem.EmbedMultipleConfs(mol, numConfs=N_EMBED, params=ps))
ff = AllChem.MMFFOptimizeMoleculeConfs(mol, maxIters=2000, numThreads=1)

recs = []
for i, cid in enumerate(cids):
    nconv, e = ff[i]
    fname = 'cand_%02d.xyz' % (i + 1)
    with open(fname, 'w') as fh:
        fh.write(xyz_text(mol, cid, 'n-hexane candidate RDKit ETKDGv3 seed=1701 MMFF94'))
    recs.append({'cid': cid, 'file': fname, 'mmff': float(e), 'conv': bool(nconv == 0)})

n_conv = sum(1 for r in recs if r['conv'])

def rmsd(a, b):
    return float(rdMolAlign.GetBestRMS(mh, mh, prbId=a, refId=b))

order = sorted(recs, key=lambda r: r['mmff'])
accepted = []
for r in order:
    best = None
    for a in accepted:
        v = rmsd(r['cid'], a['cid'])
        if best is None or v < best[0]:
            best = (v, a)
    if best is None:
        r['rmsd_min'] = 'reference (lowest MMFF94, no lower-energy unique conformer)'
        r['excluded'] = ''
        accepted.append(r)
    elif best[0] < THRESH:
        r['rmsd_min'] = round(best[0], 6)
        r['excluded'] = 'duplicate of %s (heavy-atom best-fit RMSD %.4f A < 0.15 A)' % (best[1]['file'], best[0])
    else:
        r['rmsd_min'] = round(best[0], 6)
        r['excluded'] = ''
        accepted.append(r)

selected = accepted[:N_SELECT]
for idx, r in enumerate(selected, 1):
    r['sel'] = True
    r['sel_index'] = idx
for r in accepted:
    if 'sel' not in r:
        r['sel'] = False

from orca_task_sdk import run_orca

sp = []
failures = []
for idx, r in enumerate(selected, 1):
    base = 'sp_%d' % idx
    inp = base + '.inp'
    with open(inp, 'w') as fh:
        fh.write('! B3LYP def2-SVP RKS NoAutoStart\n')
        fh.write('%maxcore 192\n')
        fh.write('%scf\n  MaxIter 256\n  AutoTRAH false\nend\n')
        fh.write('* xyzfile 0 1 %s\n' % r['file'])
    rk = 'hexane_sp_%d' % idx
    try:
        res = run_orca(inp, dependencies=[r['file']], request_key=rk, purpose='new_candidate')
    except Exception as exc:
        failures.append({'file': r['file'], 'input': inp, 'request_key': rk, 'error': str(exc)})
        continue
    files = res.get('files', {}) or {}
    energy = None
    src_key = None
    src_line = None
    for key in sorted(files.keys()):
        p = files[key]
        if not isinstance(p, str) or not os.path.exists(p):
            continue
        try:
            txt = open(p, 'r', errors='replace').read()
        except Exception:
            continue
        for ln_no, ln in enumerate(txt.splitlines(), 1):
            s = ln.strip()
            if s.startswith('FINAL SINGLE POINT ENERGY'):
                try:
                    energy = float(s.split()[-1])
                    src_key = key
                    src_line = ln_no
                except Exception:
                    energy = None
                break
        if energy is not None:
            break
    outfile = ''
    if energy is not None and src_key is not None and os.path.exists(files[src_key]):
        outfile = 'orca_%s_stdout.out' % base
        try:
            shutil.copyfile(files[src_key], outfile)
        except Exception:
            outfile = ''
    if energy is None:
        failures.append({'file': r['file'], 'input': inp, 'request_key': rk, 'error': 'no FINAL SINGLE POINT ENERGY in returned files'})
    sp.append({'file': r['file'], 'input': inp, 'request_key': rk, 'energy_hartree': energy, 'output_key': src_key if src_key else '', 'output_line': src_line if src_line else 0, 'output_copy': outfile, 'operation_status': str(res.get('operation_status'))})

valid = [s for s in sp if s['energy_hartree'] is not None]
valid.sort(key=lambda s: s['energy_hartree'])
for s in valid:
    s['rel'] = (s['energy_hartree'] - valid[0]['energy_hartree']) * HARTREE_TO_KJ

cand_entries = []
for r in recs:
    if r.get('sel'):
        status = 'selected_for_orca_comparison'
    elif r['excluded'] == '':
        status = 'unique_but_not_selected'
    else:
        status = 'excluded_duplicate'
    cand_entries.append({'file': r['file'], 'mmff94_energy_kcal_per_mol': round(r['mmff'], 6), 'mmff_converged': r['conv'], 'min_heavy_atom_rmsd_to_lower_energy_unique': r['rmsd_min'], 'status': status, 'exclusion_reason': r['excluded']})

src_local = [{'filename': r['file'], 'location': None} for r in recs]
sp_sources = [{'filename': s['input'], 'location': None} for s in sp]
for s in sp:
    if s['energy_hartree'] is not None and s['output_key']:
        sp_sources.append({'internal_request_key': s['request_key'], 'filename': s['output_key'], 'location': 'line %d' % s['output_line']})

obs = []
obs.append({'name': 'candidate_conformer_search', 'value': {'generator': 'RDKit ETKDGv3 embedding (randomSeed=1701) followed by MMFF94 optimization (maxIters=2000)', 'rmsd_definition': 'RDKit rdMolAlign.GetBestRMS on the six carbon atoms only; graph automorphisms are considered, so reversed carbon-chain labelling is treated as equivalent', 'rmsd_duplicate_threshold_angstrom': 0.15, 'num_conformers_embedded': len(cids), 'num_structures_saved': len(recs), 'num_unique_after_rmsd_dedup': len(accepted), 'num_selected_for_orca': len(selected), 'selection_rule': 'lowest MMFF94 energy first, accept only if heavy-atom RMSD >= 0.15 A to every already accepted unique conformer', 'candidates': cand_entries}, 'unit': None, 'sources': src_local, 'formula': 'heavy-atom best-fit RMSD = sqrt(mean squared C-atom displacement) after optimal superposition, minimised over graph automorphisms including chain reversal', 'artifacts': [r['file'] for r in recs], 'limitations': ['Finite stochastic conformer search of 60 ETKDGv3 embeddings with seed 1701; not an exhaustive conformer enumeration.', 'Duplicate determination uses MMFF94-optimized geometries at a 0.15 A heavy-atom RMSD threshold.', 'Force-field geometries were not re-optimized at the DFT level.']})
obs.append({'name': 'orca_single_point_energy_comparison', 'value': {'program': 'ORCA 6.1 managed native run (raw evidence, unqualified)', 'method': 'B3LYP', 'basis': 'def2-SVP', 'job_type': 'single point energy (SP)', 'scf_type': 'RKS', 'charge': 0, 'multiplicity': 1, 'environment': 'gas phase', 'energies': [{'file': s['file'], 'energy_hartree': s['energy_hartree'], 'request_key': s['request_key'], 'output_key': s['output_key']} for s in sp if s['energy_hartree'] is not None], 'relative_energies_kj_per_mol': [{'file': s['file'], 'relative_kj_per_mol': round(s['rel'], 6)} for s in valid], 'failed_members': failures}, 'unit': None, 'sources': sp_sources, 'formula': 'relative energy (kJ/mol) = (E_hartree - E_min_hartree) * 2625.499639', 'artifacts': [s['input'] for s in sp] + [s['output_copy'] for s in sp if s['output_copy']], 'limitations': ['Energies are B3LYP/def2-SVP SCF single-point energies at MMFF94 geometries without zero-point or thermal corrections.', 'Only the selected distinct conformers were evaluated with ORCA; unselected candidates have no ORCA energy.']})
ranking = [{'rank': i, 'file': s['file'], 'absolute_energy_hartree': s['energy_hartree'], 'relative_to_lowest_kj_per_mol': round(s['rel'], 6)} for i, s in enumerate(valid, 1)]
obs.append({'name': 'candidate_delivery_and_energy_ranking', 'value': {'search_scope': 'n-hexane (SMILES CCCCCC, all 20 atoms with explicit hydrogens); 60 ETKDGv3 conformers with seed 1701; MMFF94 optimization; heavy-atom RMSD dedup at 0.15 A', 'geometry_source': 'RDKit ETKDGv3 embeddings optimized with MMFF94; no DFT geometry optimization', 'force_field': 'MMFF94', 'force_field_converged_count': n_conv, 'force_field_total_count': len(recs), 'lowest_candidate_file': valid[0]['file'] if valid else '', 'all_candidates': cand_entries, 'energy_ranking': ranking}, 'unit': None, 'sources': src_local + sp_sources, 'formula': 'relative energy (kJ/mol) = (E_hartree - E_min_hartree) * 2625.499639', 'artifacts': [r['file'] for r in recs] + [s['input'] for s in sp] + [s['output_copy'] for s in sp if s['output_copy']], 'limitations': ['All generated candidates are reported; ORCA energies exist only for the selected distinct conformers.', 'The lowest member is the lowest of this finite search, not a global or free-energy minimum.']})
obs.append({'name': 'random_seed_record', 'value': {'seed': 1701, 'used_by': ['RDKit ETKDGv3 conformer embedding (randomSeed)'], 'no_random_numbers': ['MMFF94 optimization is deterministic given the starting geometry', 'ORCA SCF single-point calculations use no random numbers'], 'unsupported_fixed_seed': []}, 'unit': None, 'sources': src_local, 'formula': None, 'artifacts': [r['file'] for r in recs], 'limitations': ['Only the RDKit embedding step consumes random numbers; the seed 1701 was applied there explicitly.']})
obs.append({'name': 'search_limitations_disclosure', 'value': {'search_scope': '60 ETKDGv3 conformers of n-hexane at seed 1701, MMFF94 optimized, deduplicated by heavy-atom best-fit RMSD (chain-reversal aware) at 0.15 A', 'force_field_prescreening_only': True, 'uncomputed_candidates_have_no_orca_energy': True, 'global_minimum_claim': False, 'free_energy_claim': False, 'notes': 'MMFF94 is used only as a pre-screen; absolute energies reported are ORCA B3LYP/def2-SVP single points, and the finite-search lowest member is not asserted to be the global or free-energy minimum.'}, 'unit': None, 'sources': src_local, 'formula': None, 'artifacts': [r['file'] for r in recs], 'limitations': ['Finite conformer search; coverage of conformational space is not exhaustive.', 'Energy differences omit vibrational, thermal and solvation contributions.']})

with open('observations.json', 'w') as fh:
    json.dump({'version': 1, 'observations': obs}, fh, indent=1)

print('candidates', len(recs), 'unique', len(accepted), 'selected', len(selected), 'orca_valid', len(valid), 'failures', len(failures))
