import json
from pathlib import Path
from orca_task_sdk import run_orca, receipt_source
NL=chr(10)
sel=json.loads(Path('selected.json').read_text())
selected=sel['selected']
results=[]
sources=[]
for s in selected:
    cid=s['candidate_id']
    xyz='sp_%s.xyz' % cid
    inp='sp_%s.inp' % cid
    atom_lines=[]
    for a in s['atoms']:
        atom_lines.append('%s %.10f %.10f %.10f' % (a['element'],a['x'],a['y'],a['z']))
    Path(xyz).write_text(NL.join([str(len(s['atoms'])), cid + ' from MMFF94 selected conformer'] + atom_lines) + NL)
    inp_text=NL.join(['! RKS wB97X-D3 def2-TZVP NoAutoStart','%pal','  nprocs 4','end','%maxcore 192','%scf','  MaxIter 200','  AutoTRAH false','end','* xyzfile 0 1 '+xyz,''])
    Path(inp).write_text(inp_text)
    try:
        res=run_orca(inp, dependencies=[xyz], request_key='sp_%s' % cid, purpose='new_candidate')
        files=res.get('files',{})
        energy=None
        err=None
        if 'stdout.out' in files and Path(files['stdout.out']).exists():
            text=Path(files['stdout.out']).read_text(errors='replace')
            for line in text.splitlines():
                if 'FINAL SINGLE POINT ENERGY' in line:
                    try:
                        energy=float(line.split()[-1])
                    except Exception:
                        err='FINAL SINGLE POINT ENERGY parse failed'
            if energy is None and err is None:
                err='FINAL SINGLE POINT ENERGY not found in stdout.out'
        else:
            err='stdout.out missing from SDK result'
        if energy is not None:
            sources.append(receipt_source(res,'stdout.out',location='FINAL SINGLE POINT ENERGY line'))
        results.append({'candidate_id':cid,'filename':s['filename'],'orca_input':inp,'orca_xyz':xyz,'request_key':res.get('request_key','sp_%s' % cid),'energy_hartree':energy,'error':err,'files':sorted(files.keys())})
    except Exception as e:
        results.append({'candidate_id':cid,'filename':s['filename'],'orca_input':inp,'orca_xyz':xyz,'request_key':'sp_%s' % cid,'energy_hartree':None,'error':repr(e),'files':[]})
    Path('energies_partial.json').write_text(json.dumps({'results':results},indent=2))
ok=[r for r in results if r['energy_hartree'] is not None]
ok.sort(key=lambda r:r['energy_hartree'])
if not ok:
    raise RuntimeError('No successful ORCA single-point energies')
emin=ok[0]['energy_hartree']
for r in results:
    if r['energy_hartree'] is not None:
        r['relative_kj_mol']=(r['energy_hartree']-emin)*2625.4996394799
        r['rank']=1+sum(1 for q in ok if q['energy_hartree']<r['energy_hartree'])
    else:
        r['relative_kj_mol']=None
        r['rank']=None
lowest=ok[0]
low_atoms=None
for s in selected:
    if s['candidate_id']==lowest['candidate_id']:
        low_atoms=s['atoms']
        break
if low_atoms is None:
    raise RuntimeError('Lowest candidate atoms not found')
low_lines=[]
for a in low_atoms:
    low_lines.append('%s %.10f %.10f %.10f' % (a['element'],a['x'],a['y'],a['z']))
Path('lowest_candidate.xyz').write_text(NL.join([str(len(low_atoms)), 'lowest ORCA single-point candidate ' + lowest['candidate_id']] + low_lines) + NL)
Path('energies.json').write_text(json.dumps({'selected':[s['candidate_id'] for s in selected],'results':results,'lowest_candidate':lowest,'successful_count':len(ok),'formula':'delta_E_kJ_mol = 2625.4996394799 * (E_i - min(E))','method':'wB97X-D3','basis':'def2-TZVP','charge':0,'multiplicity':1,'electronic_state':'RKS','environment':'gas_phase'},indent=2))
energy_obs=[{'candidate_id':r['candidate_id'],'filename':r['filename'],'energy_hartree':r['energy_hartree'],'error':r['error']} for r in results]
delta_obs=[{'candidate_id':r['candidate_id'],'rank':r['rank'],'relative_kj_mol':r['relative_kj_mol'],'energy_hartree':r['energy_hartree']} for r in results if r['energy_hartree'] is not None]
obs=[{'name':'energy','value':energy_obs,'unit':'hartree','sources':sources,'formula':None,'artifacts':['energies.json','energies_partial.json','lowest_candidate.xyz']+[r['orca_input'] for r in results]+[r['orca_xyz'] for r in results],'limitations':['Absolute ORCA single-point energies at fixed MMFF94 geometries; no DFT geometry optimization.','Failed ORCA calls remain listed and are not treated as energies.']},{'name':'energy_difference','value':delta_obs,'unit':'kJ/mol','sources':sources+[{'filename':'energies.json','location':'energy ranking table'}],'formula':'delta_E_kJ_mol = 2625.4996394799 * (E_i - min(E))','artifacts':['energies.json','lowest_candidate.xyz'],'limitations':['Ranking is limited to the finite set of successfully computed candidates; not a global minimum or free energy.']},{'name':'candidate_geometry','value':{'candidate_id':lowest['candidate_id'],'filename':'lowest_candidate.xyz','energy_hartree':lowest['energy_hartree']},'unit':None,'sources':sources,'formula':None,'artifacts':['lowest_candidate.xyz','energies.json'],'limitations':['The lowest candidate is the lowest among the finite computed set, not a global minimum.']}]
Path('observations.json').write_text(json.dumps({'version':1,'observations':obs},indent=2))
print('orca_done', len(ok), lowest['candidate_id'], lowest['energy_hartree'])