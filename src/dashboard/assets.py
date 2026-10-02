"""Stable cache keys for the dashboard's locally served assets."""

from hashlib import sha256
from pathlib import Path


def asset_version(directory: Path) -> str:
    digest = sha256()
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix in {".css", ".js"}:
            digest.update(path.relative_to(directory).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()[:16]
