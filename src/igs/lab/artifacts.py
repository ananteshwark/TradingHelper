"""Local immutable-by-convention bundles with verified file content hashes.

Atomic directory publication prevents incomplete bundles being consumed. Existing
bundles cannot be overwritten by this API. Hashes detect accidental changes; these
are not signatures or a replacement for filesystem permissions/off-host backups.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, default=str, allow_nan=False) + "\n",
        encoding="utf-8",
    )


@contextmanager
def bundle(destination: Path):
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL also serializes two publishers targeting the same path.
    lock = destination.with_name(destination.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    temporary = Path(tempfile.mkdtemp(prefix=".lab-", dir=destination.parent))
    try:
        if destination.exists():
            raise FileExistsError(f"Bundle already exists: {destination}")
        yield temporary
        os.rename(temporary, destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
        os.close(fd)
        lock.unlink()


def seal(path: Path, metadata: dict) -> dict:
    manifest = {
        **metadata,
        "files": {
            f.name: digest(f)
            for f in sorted(path.iterdir())
            if f.is_file() and f.name != "manifest.json"
        },
    }
    write_json(path / "manifest.json", manifest)
    return manifest


def verify(path: Path) -> dict:
    manifest = json.loads((path / "manifest.json").read_text())
    if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise ValueError("Empty or invalid artifact manifest")
    for name, expected in manifest["files"].items():
        if (
            Path(name).name != name
            or not (path / name).is_file()
            or digest(path / name) != expected
        ):
            raise ValueError(f"Artifact checksum mismatch: {name}")
    return manifest
