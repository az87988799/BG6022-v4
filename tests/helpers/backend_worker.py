"""Controlled subprocess fixtures; never imported by production code."""

import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from orca_agent._atomic import atomic_write


def publish_json(path, value):
    atomic_write(path, json.dumps(value).encode("utf-8"))


def record(directory):
    current = psutil.Process()
    data = {"pid": current.pid, "create_time": current.create_time(),
            "affinity": current.cpu_affinity()}
    publish_json(directory / f"node-{current.pid}.json", data)
    return data


def main():
    mode, directory = sys.argv[1], Path(sys.argv[2])
    if mode == "coordinator":
        from orca_agent.backends.local import run_managed

        crash = sys.argv[3]

        def fault(point):
            if point == "after_process_created" and crash == "before_save":
                children = [child.pid for child in psutil.Process().children()]
                publish_json(directory / "created.json", children)
                os._exit(61)

        def save(handle):
            publish_json(directory / "handle.json", handle)

        result = run_managed(
            Path(sys.executable), [str(Path(__file__).resolve()), "tree", str(directory), "2"],
            directory, timeout_s=30, on_started=save, fault=fault,
        )
        publish_json(directory / "result.json", result)
        return
    data = record(directory)
    if mode == "affinity":
        print(json.dumps(data), flush=True)
        print("stderr evidence", file=sys.stderr, flush=True)
        return
    if mode == "environment":
        print(json.dumps({"fixture_value": os.environ.get("ORCA_BACKEND_FIXTURE"),
                          "omp_threads": os.environ.get("OMP_NUM_THREADS")}), flush=True)
        return
    if mode == "tree":
        depth = int(sys.argv[3])
        if depth:
            subprocess.Popen([sys.executable, __file__, "tree", str(directory), str(depth - 1)])
        time.sleep(60)
        return
    if mode == "driver_exits":
        subprocess.Popen([sys.executable, __file__, "tree", str(directory), "0"])
        return
    if mode == "allocate":
        blocks = []
        try:
            while True:
                blocks.append(bytearray(8 * 1024 * 1024))
                time.sleep(0.02)
        except MemoryError:
            print("allocation rejected", flush=True)
            sys.exit(37)
    if mode in ("oversized_allocation", "aggregate_allocation"):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        allocate = kernel.VirtualAlloc
        allocate.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_ulong, ctypes.c_ulong]
        allocate.restype = ctypes.c_void_p
        requested = int(sys.argv[3]) * 1024 * 1024 if len(sys.argv) > 3 else 128 * 1024 * 1024
        pointer = allocate(None, requested, 0x3000, 4)
        if mode == "aggregate_allocation":
            if not pointer:
                sys.exit(40)
            child = subprocess.run([sys.executable, __file__, "oversized_allocation",
                                    str(directory), "64"])
            sys.exit(child.returncode)
        print(json.dumps({"requested_bytes": requested, "allocated": bool(pointer),
                          "error": ctypes.get_last_error(),
                          "private_bytes": psutil.Process().memory_info().private}), flush=True)
        if pointer and len(sys.argv) == 3:
            sys.exit(38)
        return
    if mode == "breakaway":
        try:
            subprocess.Popen([sys.executable, __file__, "tree", str(directory), "0"],
                             creationflags=0x01000000)
        except OSError:
            print("breakaway rejected", flush=True)
            return
        print("breakaway child created; check its job membership", flush=True)
        time.sleep(60)
        return
    raise ValueError(mode)


if __name__ == "__main__":
    main()
