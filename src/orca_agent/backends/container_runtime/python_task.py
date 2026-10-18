"""Trusted image bootstrap; the task never executes before the kernel filter."""

import ctypes
import os
import runpy
import sys

if os.getuid() != 10001 or os.getgid() != 10001:
    raise RuntimeError("wrong task identity")
library = ctypes.CDLL("/opt/orca-agent-runtime/sandbox.so", use_errno=True)
library.install_task_filter.restype = ctypes.c_int
result = library.install_task_filter()
if result != 0:
    raise RuntimeError(f"task seccomp installation failed: {result}: {ctypes.get_errno()}")
status = dict(line.split(":", 1) for line in open("/proc/self/status") if ":" in line)
if any(int(status[key].strip(), 16) for key in ("CapEff", "CapPrm", "CapInh", "CapAmb")):
    raise RuntimeError("task retained capabilities")
if status["NoNewPrivs"].strip() != "1" or status["Seccomp"].strip() != "2":
    raise RuntimeError("task kernel boundary missing")
sys.argv = ["/work/sealed/task.py"]
runpy.run_path(sys.argv[0], run_name="__main__")
