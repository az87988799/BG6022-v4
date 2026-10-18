"""Hash actual image contents at build time; do not execute scientific programs."""

import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

ROOT = Path("/opt/orca-agent-runtime")


def file_record(path):
    path = Path(path)
    resolved = path.resolve(strict=True)
    if not resolved.is_file() or not any(
        resolved.is_relative_to(root) for root in (Path("/opt"), Path("/usr"))
    ):
        raise RuntimeError(f"runtime file resolved outside trusted image roots: {path}")
    digest = hashlib.sha256()
    with resolved.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "sha256": digest.hexdigest(),
        "bytes": resolved.stat().st_size,
        "resolved_path": str(resolved),
    }


def main():
    lock = json.loads(Path("/build/wheelhouse-manifest.json").read_bytes())
    site = ROOT / "venv/lib/python3.11/site-packages"
    files = [ROOT / name for name in ("helper.py", "python_task.py", "sandbox.so")]
    python = ROOT / "venv/bin/python"
    sdk = site / "orca_task_sdk.py"
    orca = Path("/opt/orca/orca")
    mpi = Path("/opt/openmpi/bin/mpirun")
    files += [python, sdk, orca, mpi, Path("/usr/bin/python3.11")]
    # The complete image ID freezes the remaining ORCA modules and library bytes.
    files += [
        Path("/opt/orca") / name
        for name in (
            "orca_startup",
            "orca_startup_mpi",
            "orca_leanscf",
            "orca_leanscf_mpi",
            "orca_scfgrad",
            "orca_scfgrad_mpi",
            "orca_prop",
            "orca_prop_mpi",
        )
    ]
    files += [
        Path("/opt/orca/lib") / name
        for name in ("liborca_tools_6_1_1.so.6", "liborca_tools_6_1_1_mpi.so.6")
    ]
    files += [
        Path("/opt/openmpi") / name
        for name in (
            "bin/orted",
            "bin/ompi_info",
            "lib/libmpi.so.40",
            "lib/libopen-rte.so.40",
            "lib/libopen-pal.so.40",
        )
    ]
    files += list((ROOT / "provenance").glob("*"))
    records = {str(path): file_record(path) for path in files}
    distributions = {
        dist.metadata["Name"].lower().replace("_", "-").replace(".", "-"): dist.version
        for dist in importlib.metadata.distributions(path=[str(site)])
    }
    expected = {
        wheel["name"]: wheel["version"] for wheel in lock["wheels"] if wheel["role"] == "runtime"
    }
    if distributions != expected or sys.version.split()[0] != lock["python_version"]:
        raise RuntimeError("actual Python/distributions differ from frozen selection")
    value = {
        "schema_version": 1,
        "version": "local-linux-runtime-1",
        "platform": "linux/amd64",
        "isolation_policy": "linux-container-v1",
        "sandbox_source_sha256": hashlib.sha256(Path("/build/sandbox.c").read_bytes()).hexdigest(),
        "python": {
            "version": sys.version.split()[0],
            "build": sys.version,
            "path": str(python),
            "sha256": records[str(python)]["sha256"],
        },
        "orca": {
            "version": "6.1.1",
            "path": str(orca),
            "sha256": records[str(orca)]["sha256"],
            "source_package_sha256": lock["orca_package"]["sha256"],
        },
        "mpi": {"version": "4.1.8", "path": str(mpi), "sha256": records[str(mpi)]["sha256"]},
        "sdk": {"path": str(sdk), "sha256": records[str(sdk)]["sha256"]},
        "distributions": distributions,
        "files": records,
        "wheels": [
            {key: wheel[key] for key in ("name", "version", "filename", "sha256", "role")}
            for wheel in lock["wheels"]
        ],
        "base_image": lock["base_image"],
        "compiler_image": lock["compiler_image"],
        "source_lock": lock["source_lock"],
        "openmpi_source": lock["openmpi_source"],
        "limitations": [
            "ORCA and MPI version strings come from the frozen base image provenance; "
            "manifest creation does not start ORCA or MPI.",
            "Selected key files are listed; the image ID freezes all other image bytes.",
        ],
    }
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")
    (ROOT / "runtime-manifest.json").write_bytes(encoded)


if __name__ == "__main__":
    main()
