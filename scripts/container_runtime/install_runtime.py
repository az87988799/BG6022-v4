"""Trusted image-build installer. All wheel inputs are frozen and local."""

import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

BUILD = Path("/build")
ROOT = Path("/opt/orca-agent-runtime")


def main():
    if sys.version_info[:3] != (3, 11, 2) or sys.platform != "linux":
        raise RuntimeError("this image lock requires Linux CPython 3.11.2")
    manifest = json.loads((BUILD / "wheelhouse-manifest.json").read_bytes())
    for wheel in manifest["wheels"]:
        path = BUILD / "wheelhouse" / wheel["filename"]
        if (
            path.stat().st_size != wheel["bytes"]
            or hashlib.sha256(path.read_bytes()).hexdigest() != wheel["sha256"]
        ):
            raise RuntimeError("frozen wheel hash/size mismatch")
    venv.EnvBuilder(with_pip=False, symlinks=True).create(ROOT / "venv")
    python = ROOT / "venv/bin/python"
    pip = next(wheel for wheel in manifest["wheels"] if wheel["role"] == "build-only")
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "PYTHONPATH": str(BUILD / "wheelhouse" / pip["filename"]),
    }
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--find-links=/build/wheelhouse",
            "--only-binary=:all:",
            "--require-hashes",
            "--no-deps",
            "--no-compile",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "-r",
            "/build/requirements-linux-cp311.lock",
        ],
        env=environment,
        check=True,
        timeout=180,
    )
    # pip is imported from the build-only wheel and is never installed in the venv.
    site = ROOT / "venv/lib/python3.11/site-packages"
    expected = {
        wheel["name"]: wheel["version"]
        for wheel in manifest["wheels"]
        if wheel["role"] == "runtime"
    }
    actual = {
        dist.metadata["Name"].lower().replace("_", "-").replace(".", "-"): dist.version
        for dist in importlib.metadata.distributions(path=[str(site)])
    }
    if actual != expected:
        raise RuntimeError(f"installed distribution closure mismatch: {actual}")
    for path in site.rglob("*.pth"):
        raise RuntimeError(f"unexpected Python startup path hook: {path}")
    (site / "orca_task_sdk.py").write_bytes((BUILD / "orca_task_sdk.py").read_bytes())
    for path in ROOT.rglob("*"):
        if not path.is_symlink():
            os.chmod(path, 0o755 if path.is_dir() else 0o644)


if __name__ == "__main__":
    main()
