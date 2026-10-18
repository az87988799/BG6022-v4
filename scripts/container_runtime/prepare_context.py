"""Prepare a private, hash-verified offline Docker context; never runs Docker."""

import argparse
import email
import hashlib
import json
import shutil
import stat
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("ascii")


def fingerprint(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return {"sha256": digest.hexdigest(), "bytes": path.stat().st_size}


def regular(path):
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or path.is_symlink()
        or getattr(info, "st_file_attributes", 0) & 0x400
    ):
        raise ValueError(f"not a plain source file: {path}")


def verify(path, item):
    regular(path)
    actual = fingerprint(path)
    if actual != {"sha256": item["sha256"], "bytes": item["bytes"]}:
        raise ValueError(f"hash/size mismatch: {path}")


def download(cache, item, offline):
    target = cache / item["filename"]
    if target.exists():
        verify(target, item)
        return target
    if offline:
        raise ValueError(f"offline cache missing: {target.name}")
    url = urllib.parse.urlsplit(item["url"])
    if url.scheme != "https" or url.netloc != "files.pythonhosted.org":
        raise ValueError("only the frozen official PyPI file host is allowed")
    partial = target.with_suffix(target.suffix + ".partial")
    started = time.monotonic()
    with urllib.request.urlopen(item["url"], timeout=30) as source, partial.open("xb") as output:
        final_url = urllib.parse.urlsplit(source.url)
        if final_url.scheme != "https" or final_url.netloc != "files.pythonhosted.org":
            raise ValueError("unexpected wheel download redirect")
        count = 0
        while block := source.read(1024 * 1024):
            count += len(block)
            if count > item["bytes"] or time.monotonic() - started > 180:
                raise ValueError("wheel download exceeded frozen size/deadline")
            output.write(block)
    verify(partial, item)
    partial.rename(target)
    return target


def wheel_notices(path, item):
    """Inspect archives as data, without importing or extracting wheel code."""
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        names = [member.filename for member in members]
        if len(names) != len(set(names)) or sum(m.file_size for m in members) > 512 * 1024**2:
            raise ValueError("duplicate or oversized wheel contents")
        for member in members:
            name = PurePosixPath(member.filename)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.filename
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise ValueError("unsafe wheel member")
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ValueError("wheel must contain one distribution metadata file")
        metadata = email.message_from_bytes(archive.read(metadata_names[0]))

        def normalize(value):
            return value.lower().replace("_", "-").replace(".", "-")

        if normalize(metadata["Name"]) != item["name"] or metadata["Version"] != item["version"]:
            raise ValueError("wheel metadata does not match the frozen distribution")
        notices = []
        for member in members:
            if member.is_dir():
                continue
            lower = member.filename.lower()
            if ".dist-info/licenses/" in lower or PurePosixPath(lower).name.startswith(
                ("license", "copying", "notice", "authors")
            ):
                payload = archive.read(member)
                notices.append(
                    {
                        "path": member.filename,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                )
        return {
            "name": item["name"],
            "version": item["version"],
            "role": item["role"],
            "wheel_sha256": item["sha256"],
            "declared_license_expression": str(metadata.get("License-Expression", "")),
            "declared_license": str(metadata.get("License", "")),
            "requires_dist": [str(value) for value in metadata.get_all("Requires-Dist", [])],
            "notice_files": notices,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--download-only", action="store_true")
    args = parser.parse_args()
    if not args.download_only and args.output is None:
        parser.error("--output is required unless --download-only")
    manifest = json.loads((HERE / "wheelhouse-manifest.json").read_bytes())
    if fingerprint(REPO / "uv.lock")["sha256"] != manifest["source_lock"]["sha256"]:
        raise ValueError("uv.lock changed; review and refresh the Linux wheel lock explicitly")
    args.cache.mkdir(parents=True, exist_ok=True)
    wheel_paths, notices = [], []
    for item in manifest["wheels"]:
        path = download(args.cache, item, args.offline)
        wheel_paths.append(path)
        notices.append(wheel_notices(path, item))
        print(f"verified {path.name}", flush=True)
    notice_record = {"schema_version": 1, "wheels": notices}
    (args.cache / "wheel-license-evidence.json").write_bytes(canonical(notice_record))
    recorded_notices = json.loads((HERE / "license-manifest.json").read_bytes())["wheels"]
    if notices != recorded_notices:
        raise ValueError("actual wheel notices differ from the reviewed license manifest")
    if args.download_only:
        return
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    copies = {
        name: HERE / name
        for name in (
            "Dockerfile",
            "requirements-linux-cp311.lock",
            "wheelhouse-manifest.json",
            "license-manifest.json",
            "install_runtime.py",
            "write_runtime_manifest.py",
        )
    }
    copies.update(
        {
            name: REPO / "src/orca_agent/backends/container_runtime" / name
            for name in ("helper.py", "python_task.py", "sandbox.c")
        }
    )
    copies["orca_task_sdk.py"] = REPO / "src/orca_agent/task_sdk.py"
    copies["wheel-license-evidence.json"] = args.cache / "wheel-license-evidence.json"
    copies.update({f"wheelhouse/{path.name}": path for path in wheel_paths})
    for name, source in copies.items():
        regular(source)
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    files = {name: fingerprint(output / name) for name in sorted(copies)}
    (output / "context-manifest.json").write_bytes(canonical({"schema_version": 1, "files": files}))
    print(
        json.dumps(
            {
                "context": str(output),
                "files": len(files),
                "bytes": sum(item["bytes"] for item in files.values()),
            }
        )
    )


if __name__ == "__main__":
    main()
