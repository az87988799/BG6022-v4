from pathlib import Path
import ctypes as c
import hashlib
import json
import os
import shutil
import uuid
import psutil

from orca_agent.backends.isolation import AppContainerSession, clean_environment
from orca_agent.backends.local import run_managed

root = Path('E:/orca-agent-isolation-probes') / ('mpi-' + uuid.uuid4().hex)
root.mkdir()
runtime = root / 'runtime'
shutil.copytree(Path('C:/WINDOW~1/orca-private-python-8ly3qb74/runtime'), runtime)
mpi_source = Path('C:/Program Files/Microsoft MPI/Bin')
files = {}
for name in ('mpiexec.exe', 'smpd.exe'):
    source = mpi_source / name
    shutil.copyfile(source, runtime / name)
    files[name] = {'source': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}
for name in ('msmpi.dll', 'msmpires.dll'):
    source = Path('C:/Windows/System32') / name
    shutil.copyfile(source, runtime / name)
    files[name] = {'source': str(source), 'sha256': hashlib.sha256(source.read_bytes()).hexdigest()}

worker = '''import ctypes as c, json, os, time
from ctypes import wintypes as w
k=c.WinDLL("kernel32", use_last_error=True); a=c.WinDLL("advapi32", use_last_error=True)
k.GetCurrentProcess.restype=w.HANDLE
a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes=[w.HANDLE,c.c_int,w.LPVOID,w.DWORD,c.POINTER(w.DWORD)]
k.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,c.POINTER(w.BOOL)]
token=w.HANDLE(); a.OpenProcessToken(k.GetCurrentProcess(),8,c.byref(token))
size=w.DWORD(); app=w.DWORD(); a.GetTokenInformation(token,29,c.byref(app),c.sizeof(app),c.byref(size))
inside=w.BOOL(); k.IsProcessInJob(k.GetCurrentProcess(),None,c.byref(inside))
print(json.dumps({"pid":os.getpid(),"ppid":os.getppid(),"appcontainer":bool(app.value),"inside_job":bool(inside.value)}),flush=True)
time.sleep(0.4)
'''

def mpi_processes():
    result=[]
    for p in psutil.process_iter(['name','exe','create_time','ppid']):
        if (p.info['name'] or '').lower() in {'mpiexec.exe','smpd.exe','msmpilaunchsvc.exe'}:
            result.append({'pid':p.pid, **p.info})
    return result

results=[]
for index, prefix in enumerate((['-n','2'],['-localonly','-n','2']),1):
    work=root/f'work-{index}';work.mkdir();(work/'worker.py').write_text(worker)
    before=mpi_processes()
    with AppContainerSession(root) as boundary:
        boundary.grant_read(runtime);boundary.grant_write(work)
        env=clean_environment(runtime,work)
        outcome=run_managed(runtime/'mpiexec.exe', [*prefix,'../runtime/python.exe','-I','worker.py'],
            work,cores=2,total_memory_mb=256,timeout_s=20,isolation=boundary.policy(allow_children=True),
            environment=env,argv0='../runtime/mpiexec.exe')
    record={'invocation':index,'args':prefix,'outcome':outcome,'before_mpi_processes':before,
            'after_mpi_processes':mpi_processes(),
            'stdout':(work/'stdout.out').read_text(errors='replace') if (work/'stdout.out').exists() else None,
            'stderr':(work/'stderr.txt').read_text(errors='replace') if (work/'stderr.txt').exists() else None}
    results.append(record)
    (root/'results.json').write_text(json.dumps({'runtime_files':files,'results':results},indent=2),encoding='utf-8')
    print(json.dumps(record,indent=2),flush=True)
    if record['after_mpi_processes']:
        print('MPI processes remain; no second invocation',flush=True)
        break
print('EVIDENCE',root)
