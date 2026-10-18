"""Fixed developer smoke; records one ORCA launch, never a production runner."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path


def resources():
    root = Path('/sys/fs/cgroup')
    names = ('memory.max', 'memory.swap.max', 'memory.current', 'memory.peak',
             'memory.events', 'cpu.max', 'cpu.stat', 'cpuset.cpus.effective', 'pids.max')
    return {name: (root / name).read_text().strip() for name in names}


def main():
    before = resources()
    if (os.geteuid() != 10001 or before['memory.max'] != '1073741824'
            or before['memory.swap.max'] != '0' or before['cpu.max'] != '400000 100000'
            or before['cpuset.cpus.effective'] != '0-3'):
        raise RuntimeError('required restricted execution envelope missing')
    task = Path('/tmp/task')
    for name in ('water.inp', 'geometry.xyz'):
        shutil.copyfile(Path('/opt/orca-probe-input') / name, task / name)
    start = time.monotonic()
    processes = {}
    with (task / 'stdout.out').open('xb') as out, (task / 'stderr.txt').open('xb') as err:
        process = subprocess.Popen(['/opt/orca/orca', 'water.inp'], cwd=task,
                                   stdout=out, stderr=err)
        while process.poll() is None:
            for item in Path('/proc').iterdir():
                if not item.name.isdigit():
                    continue
                try:
                    command = (item / 'cmdline').read_bytes()[:8192].split(b'\0')
                    command = [part.decode(errors='replace') for part in command if part]
                    if command and (command[0].startswith('/opt/orca/')
                                    or command[0].endswith('mpirun')):
                        key = item.name
                        if key not in processes:
                            processes[key] = {'cmd': command,
                                              'stat': (item / 'stat').read_text(),
                                              'status': (item / 'status').read_text(),
                                              'cgroup': (item / 'cgroup').read_text(),
                                              'observed_seconds': time.monotonic() - start}
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    pass
            if time.monotonic() - start > 110:
                # The outer fixed timeout bounds and kills the whole container.
                raise TimeoutError('ORCA smoke exceeded its inner observation deadline')
            time.sleep(0.02)
    result = {'exit_code': process.returncode, 'seconds': time.monotonic() - start,
              'before': before, 'after': resources(), 'processes': processes,
              'scientific_validation': 'unverified'}
    (task / 'probe-record.json').write_text(json.dumps(result, indent=2))
    print(json.dumps({key: value for key, value in result.items() if key != 'processes'}))
    return process.returncode


if __name__ == '__main__':
    raise SystemExit(main())
