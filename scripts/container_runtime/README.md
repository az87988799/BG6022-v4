# Fixed local Linux runtime image

This directory builds the Linux execution image for the step 2 container backend.
It installs only the thin task SDK, three trusted isolation helpers, and the 11
scientific distributions below. It never installs the Agent source tree.

| Component | Frozen version |
| --- | --- |
| Linux CPython | 3.11.2, Debian package `3.11.2-6+deb12u9` |
| ORCA | 6.1.1 Linux x86-64, official shared Open MPI 4.1.8 archive |
| Open MPI | 4.1.8, C bindings; Fortran/C++ bindings disabled in the base build |
| orca-pi | 2.0.0 |
| RDKit | 2025.9.6 |
| NumPy | 2.4.6 |
| Pillow | 12.3.0 |
| platformdirs | 4.12.3 |
| pydantic / pydantic-core | 2.13.5 / 2.46.5 |
| annotated-types | 0.8.0 |
| typing-extensions / typing-inspection | 4.16.0 / 0.4.4 |
| semantic-version | 2.10.0 |
| Build-only pip | 26.2.1; not installed in the final venv |

`requirements-linux-cp311.lock` selects one exact Linux amd64 wheel per runtime
distribution from `uv.lock`. `wheelhouse-manifest.json` records official PyPI
file URLs, sizes, SHA256 values, and the source lock hash. NumPy 2.5.3 in the
project lock belongs to Python >=3.12 and is not used here. Python is distinct
from the earlier Windows runtime, so existing Run snapshots must not be upgraded.

`license-manifest.json` records actual wheel metadata and every included license
or notice file hash. Whole wheel contents preserve the bundled `numpy.libs`,
`rdkit.libs`, `pillow.libs`, distribution metadata, and notices. OPI's GPL-3.0
license does not license the separate ORCA package. ORCA bytes remain in the
user's local licensed image and are not committed to Git or uploaded by these tools.

## Prepare and build explicitly

Run from the repository root with Python >=3.11. Preparation may download the
12 frozen wheel files from the official PyPI file host; it never runs their code.
The download cache, private context and build evidence must be outside Git-tracked
source directories. Each context and evidence directory must be new.

```powershell
.venv\Scripts\python.exe scripts\container_runtime\prepare_context.py `
  --cache data\container-proof-20261018\wheelhouse --download-only
.venv\Scripts\python.exe scripts\container_runtime\prepare_context.py `
  --cache data\container-proof-20261018\wheelhouse --offline `
  --output data\container-proof-20261018\runtime-context-001
.venv\Scripts\python.exe scripts\container_runtime\build_image.py `
  --context data\container-proof-20261018\runtime-context-001 `
  --evidence data\container-proof-20261018\runtime-build-001 `
  --tag orca-agent-runtime:6.1.1-ompi4.1.8-cp311 `
  --docker-context desktop-linux
```

The source and compiler images must already exist locally at the exact IDs in
`wheelhouse-manifest.json`. The build script verifies their IDs, creates private
temporary tags for those IDs, verifies every context file, and builds with
`--network=none --pull=false`. No apt or package resolution occurs in this build.
Only the fixed compiler-stage sandbox library and the offline venv enter the
final image. The compiler image is not a final-image ancestor.

The manifest is generated from the actual image files. The script creates a
temporary container without starting it, copies the manifest and Debian package
inventory, then removes that container. A separate tiny Dockerfile adds the
canonical JSON as `org.orca-agent.runtime-manifest` and its SHA256 as
`org.orca-agent.manifest-sha256`; this avoids Windows command-line length limits.
The final label bytes must match the file bytes. `inspect_environment` can then
read image labels without starting containers. Build logs, exact image IDs,
package inventory and manifest stay in the evidence directory. Temporary build
tags are recorded and retained for inspection; the script never prunes Docker.

## Runtime boundary and verification limits

The image fixes `/opt/orca-agent-runtime/venv/bin/python`, `/opt/orca/orca` and
`/opt/openmpi/bin/mpirun`. Manifest hashes cover all trusted helpers and the SDK,
the interpreter, and selected key ORCA/MPI executables/libraries. The exact image
ID covers remaining image bytes. This is not a complete software bill of materials.

The keeper uses UID 10003, Python task UID 10001, and native workload UID 10002.
Only the top `/opt/orca` directory changes to root:10002 mode 0750; its existing
17 GB subtree is not rewritten. `/work/gates`, `sealed`, and `responses` are
root-owned. The backend supplies a private named volume, no network, read-only
root filesystem, dropped capabilities, no-new-privileges, bounded tmpfs, CPU,
aggregate memory/swap, PID and deadline limits. The image alone does not enforce
those invocation settings. Python's fixed bootstrap installs the additional
seccomp filter before loading task code.

The base image's earlier four-rank ORCA compatibility result does not by itself
validate this derived image or its isolation helpers. Imports, SDK IPC, true
four-rank ORCA, resource limits, cancellation and adversarial boundary tests must
be run against the final image ID and reported separately. Building or inspecting
the image never counts as a scientific execution or a scientific check pass.

The Debian base and compiler are fixed by existing local image identities. Their
earlier apt resolution is recorded in package inventories; rebuilding those
foundation images byte-for-byte from remote apt repositories is not promised.
The runtime wheel selection and offline build inputs are reproducible from the
recorded hashes and the retained foundation images.

## Select the backend for new Runs

Use `config.container.example.toml` as the local configuration template and set
the two identity values to the build receipt. The image is private and local;
execution never pulls a tag or downloads missing dependencies.

```powershell
.venv\Scripts\orca-agent.exe --config config.local.toml doctor
```

New Run configuration version 2 freezes this selection. Historical version 1
Runs retain Windows execution; select their original configuration when working
with them. Changing the host configuration does not migrate existing Runs.

Explicit verification uses `ORCA_AGENT_CONTAINER_CONFIG` pointing to the local
TOML file and `pytest --live-container`; scientific tests also require
`--live-orca`. Use a fresh `--basetemp` directory for each evidence batch. These
tests are developer-authored and do not enable a model or subsequent plan steps.

The host records the gated launcher before opening its gate. An ORCA start is
counted only after the fixed helper observes the same PID/start ticks executing
the installed ORCA inode. A lost or too-short observation remains unconfirmed,
retains its reservation and cannot establish a completed scientific operation.
CPU accounting is a lower bound from the last cgroup observation; collector
CPU and end-to-end costs are incomplete. Artifact byte budgets do not impose
a hard disk quota on the persistent evidence volume.

## Export completed acceptance batches

The exporter only reads local evidence; it never contacts Docker or starts a
workload. Give it completed, immutable `--basetemp` batch directory names. It also
includes sibling `<batch>.log` and `<batch>.xml` reports when present.

```powershell
.venv\Scripts\python.exe scripts\container_runtime\export_acceptance.py `
  live-final-001 live-security-001 live-security-002
.venv\Scripts\python.exe scripts\container_runtime\export_acceptance.py `
  live-final-001 live-security-001 live-security-002 --verify-only
```

The default destination is
`docs/acceptance/code-as-tools-step2-linux/production`. Each immutable
`batches/<batch>/index.json` maps original relative/local paths and SHA256/size to
deduplicated `objects/<prefix>/<sha256>.txt` files. The `.txt` suffix does not
change the original bytes; the index retains the original filename and format.
Binary files, large text files and duplicate test wrappers remain local with
their hashes and omission reasons in the index. Unique lifecycle/reconciliation
facts are retained as explicit JSON projections. Docker inspection projections
exclude the full environment and host mount source paths. This policy assumes
the reviewed developer test baseline contains no real credentials; it is not a
general-purpose secret detector.

The exporter rejects changing sources, a publication larger than 10 MiB, and any
attempt to overwrite an existing batch/object with different bytes. Repeating
the same export is idempotent; append a new batch for later attempts. Verification
requires retained local originals and checks both original and published hashes.
Run state, Result checks, raw ORCA text and pytest outcomes remain distinct. CPU
summaries sum each Run's persisted aggregate usage once and exclude additional
SDK internal accounting; incomplete measurements remain lower bounds.
