"""Publish complete files without importing domain models or tools."""

import os
from pathlib import Path
from uuid import uuid4


def atomic_write(path: Path, data: bytes, *, immutable: bool = False) -> None:
    """Publish a complete file; immutable publication never replaces an existing name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".w-{uuid4().hex[:16]}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
