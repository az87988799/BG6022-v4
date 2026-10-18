"""Explicit offline image build and exact manifest-label sealing; never starts workloads."""

import argparse
import hashlib
import json
import re
import subprocess
import time
import uuid
from pathlib import Path

from prepare_context import canonical, fingerprint, regular


def validate_context(context):
    record = json.loads((context / "context-manifest.json").read_bytes())
    actual = set()
    for path in context.rglob("*"):
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise ValueError("private context contains a link/reparse point")
        if path.is_dir() and not path.is_symlink():
            continue
        regular(path)
        relative = path.relative_to(context).as_posix()
        if relative != "context-manifest.json":
            actual.add(relative)
            if fingerprint(path) != record["files"].get(relative):
                raise ValueError(f"private context changed: {relative}")
    if actual != set(record["files"]):
        raise ValueError("private context missing or unexpected files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--docker", default="docker")
    parser.add_argument("--docker-context", default="desktop-linux")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9_.-]+", args.tag):
        raise ValueError("an explicit local image tag is required")
    context = args.context.resolve()
    validate_context(context)
    args.evidence.mkdir(parents=True, exist_ok=False)
    evidence = args.evidence.resolve()
    docker = [args.docker, "--context", args.docker_context]
    counter = 0

    def call(arguments, timeout=60):
        nonlocal counter
        counter += 1
        stem = evidence / f"{counter:02d}-{arguments[0]}"
        started = time.monotonic()
        with (
            stem.with_suffix(".stdout").open("wb") as out,
            stem.with_suffix(".stderr").open("wb") as err,
        ):
            try:
                result = subprocess.run(
                    docker + arguments, stdout=out, stderr=err, timeout=timeout, check=False
                )
            except subprocess.TimeoutExpired:
                stem.with_suffix(".json").write_bytes(
                    canonical(
                        {
                            "arguments": arguments,
                            "elapsed_seconds": time.monotonic() - started,
                            "status": "timeout_unknown_daemon_state",
                            "deadline_seconds": timeout,
                        }
                    )
                )
                raise RuntimeError(
                    "Docker deadline exceeded; reconcile daemon state before retrying"
                ) from None
        stem.with_suffix(".json").write_bytes(
            canonical(
                {
                    "arguments": arguments,
                    "elapsed_seconds": time.monotonic() - started,
                    "returncode": result.returncode,
                }
            )
        )
        if result.returncode:
            raise RuntimeError(f"Docker command failed; see {stem}")
        return stem.with_suffix(".stdout").read_bytes()

    def inspect(reference):
        return json.loads(call(["image", "inspect", reference]))[0]

    info = json.loads(call(["info", "--format", "{{json .}}"]))
    if info.get("OSType") != "linux" or info.get("Architecture") not in {"x86_64", "amd64"}:
        raise RuntimeError("requires a local Linux amd64 Docker engine")
    lock = json.loads((context / "wheelhouse-manifest.json").read_bytes())
    nonce = uuid.uuid4().hex
    private_tags = {}
    for key in ("base_image", "compiler_image"):
        expected = lock[key]
        if inspect(expected["tag"])["Id"] != expected["image_id"]:
            raise RuntimeError(f"frozen local {key} has changed")
        alias = f"orca-runtime-build-{nonce}:{key}"
        call(["image", "tag", expected["image_id"], alias])
        private_tags[key] = alias
    temporary = f"orca-runtime-build-{nonce}:unlabeled"
    call(
        [
            "build",
            "--pull=false",
            "--network=none",
            "--progress=plain",
            "--build-arg",
            f"BASE_IMAGE={private_tags['base_image']}",
            "--build-arg",
            f"COMPILER_IMAGE={private_tags['compiler_image']}",
            "--tag",
            temporary,
            "--iidfile",
            str(evidence / "unlabeled-image-id.txt"),
            str(context),
        ],
        timeout=1800,
    )
    validate_context(context)
    image_id = (evidence / "unlabeled-image-id.txt").read_text().strip()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise RuntimeError("unexpected built image identity")
    container_id = (
        call(
            [
                "create",
                "--network=none",
                "--read-only",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges",
                "--entrypoint=/bin/true",
                image_id,
            ]
        )
        .decode()
        .strip()
    )
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise RuntimeError("unexpected temporary container identity; inspect evidence")
    try:
        call(
            [
                "cp",
                f"{container_id}:/opt/orca-agent-runtime/runtime-manifest.json",
                str(evidence / "runtime-manifest.json"),
            ]
        )
        call(
            [
                "cp",
                f"{container_id}:/opt/orca-agent-runtime/provenance/debian-packages.txt",
                str(evidence / "debian-packages.txt"),
            ]
        )
    finally:
        call(["rm", container_id])  # This script created it and never started it.
    raw = (evidence / "runtime-manifest.json").read_bytes()
    manifest = json.loads(raw)
    if raw != canonical(manifest) or len(raw) > 48 * 1024:
        raise RuntimeError("runtime manifest must be bounded canonical JSON without a newline")
    if (
        manifest["base_image"] != lock["base_image"]
        or manifest["compiler_image"] != lock["compiler_image"]
    ):
        raise RuntimeError("manifest image provenance differs from the frozen inputs")
    digest = hashlib.sha256(raw).hexdigest()
    label_context = evidence / "label-context"
    label_context.mkdir()
    # JSON string quoting is Dockerfile LABEL quoting. Never pass this JSON on a Windows CLI.
    (label_context / "Dockerfile").write_text(
        f"FROM {temporary}\n"
        f"LABEL org.orca-agent.runtime-manifest={json.dumps(raw.decode('ascii'))}\n"
        f"LABEL org.orca-agent.manifest-sha256={json.dumps(digest)}\n",
        encoding="ascii",
    )
    call(
        [
            "build",
            "--pull=false",
            "--network=none",
            "--progress=plain",
            "--tag",
            args.tag,
            "--iidfile",
            str(evidence / "final-image-id.txt"),
            str(label_context),
        ],
        timeout=120,
    )
    final = inspect(args.tag)
    labels = final.get("Config", {}).get("Labels", {})
    if (
        labels.get("org.orca-agent.runtime-manifest", "").encode("ascii") != raw
        or labels.get("org.orca-agent.manifest-sha256") != digest
    ):
        raise RuntimeError("final manifest label does not equal the actual image file")
    for key, alias in private_tags.items():
        if inspect(alias)["Id"] != lock[key]["image_id"]:
            raise RuntimeError("a private source-image tag changed during build")
    result = {
        "image_id": final["Id"],
        "tag": args.tag,
        "manifest_sha256": digest,
        "manifest_bytes": len(raw),
        "workload_starts": 0,
        "context_manifest_sha256": fingerprint(context / "context-manifest.json")["sha256"],
        "retained_build_tags": [*private_tags.values(), temporary],
    }
    (evidence / "result.json").write_bytes(canonical(result))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
