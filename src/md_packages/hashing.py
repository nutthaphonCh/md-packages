"""Content hashes which deliberately reject symlinked package payloads."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .errors import SourceSymlinkError


def file_hash(path: Path) -> str:
    if path.is_symlink():
        raise SourceSymlinkError(f"symlinked source is not distributable: {path}")
    if not path.is_file():
        raise ValueError(f"expected regular file: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_hash(path: Path) -> str:
    if path.is_symlink():
        raise SourceSymlinkError(f"symlinked source is not distributable: {path}")
    if path.is_file():
        return file_hash(path)
    if not path.is_dir():
        raise ValueError(f"expected regular file or directory: {path}")
    records: list[bytes] = []
    for current, directories, files in os.walk(path, followlinks=False):
        current_path = Path(current)
        if current_path.is_symlink() or any((current_path / name).is_symlink() for name in directories + files):
            raise SourceSymlinkError(f"symlinked source is not distributable: {current_path}")
        directories.sort()
        files.sort()
        for name in files:
            item = current_path / name
            relative = item.relative_to(path).as_posix()
            mode = item.stat().st_mode & 0o777
            records.append(f"{relative}\0{mode:o}\0{file_hash(item)}\n".encode())
    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(record)
    return digest.hexdigest()
