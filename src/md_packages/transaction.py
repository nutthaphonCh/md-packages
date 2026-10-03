"""Small recovery-oriented primitives for materialization transactions."""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Iterable


class ScopeLockError(RuntimeError):
    pass


def safe_path(scope: Path, path: Path | str) -> Path:
    """Validate lexical containment and every existing ancestor before I/O."""
    root = Path(os.path.abspath(scope))
    target = Path(path) if Path(path).is_absolute() else root / path
    target = Path(os.path.abspath(target))
    if not target.is_relative_to(root):
        raise ValueError(f"path escapes scope: {target}")
    for part in (target, *target.parents):
        if part.is_symlink():
            raise ValueError(f"symlinked path is unsafe: {part}")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"resolved path escapes scope: {target}")
    return target


def canonical_scope_paths(scopes: Iterable[Path | str]) -> list[Path]:
    """Deduplicate and order scopes before taking any exclusive lock."""
    return sorted({Path(scope).resolve() for scope in scopes}, key=lambda path: os.path.normcase(str(path)))


class ScopeLocks(AbstractContextManager["ScopeLocks"]):
    """Exclusive per-scope locks acquired in canonical path order."""

    def __init__(self, scopes: Iterable[Path | str]) -> None:
        self.scopes = canonical_scope_paths(scopes)
        self._fds: list[tuple[Path, int]] = []

    def acquire(self) -> "ScopeLocks":
        try:
            for scope in self.scopes:
                safe_path(scope, ".md-install.lock")
                scope.mkdir(parents=True, exist_ok=True)
                path = scope / ".md-install.lock"
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError as exc:
                    raise ScopeLockError(f"scope is already locked: {scope}") from exc
                os.write(fd, f"pid={os.getpid()}\ntime={time.time()}\n".encode())
                self._fds.append((path, fd))
        except Exception:
            self.release()
            raise
        return self

    def release(self) -> None:
        for path, fd in reversed(self._fds):
            try:
                os.close(fd)
            finally:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
        self._fds.clear()

    def __enter__(self) -> "ScopeLocks":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def transaction_dir(scope: Path, transaction_id: str) -> Path:
    return scope / ".md" / "transactions" / transaction_id


def sync_directory(path: Path) -> None:
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def sync_payload(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            sync_payload(child)
        sync_directory(path)
    else:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())


def replace_durable(source: Path, target: Path) -> None:
    os.replace(source, target)
    sync_directory(target.parent)
    if source.parent != target.parent:
        sync_directory(source.parent)


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    safe_path(path.parent, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    replace_durable(temporary, path)


class Journal:
    """A durable JSON journal.  It is deliberately retained after completion."""

    def __init__(self, scope: Path, transaction_id: str | None = None) -> None:
        self.scope = scope.resolve()
        self.id = transaction_id or uuid.uuid4().hex
        self.directory = transaction_dir(self.scope, self.id)
        self.path = self.directory / "journal.json"
        self.data: dict[str, Any] = {
            "version": 1,
            "id": self.id,
            "scope": str(self.scope),
            "state": "prepared",
            "operations": [],
        }

    def save(self) -> None:
        safe_path(self.scope, self.path)
        write_json_atomic(self.path, self.data)

    def update(self, **changes: Any) -> None:
        self.data.update(changes)
        self.save()

    @classmethod
    def load(cls, path: Path) -> "Journal":
        data = json.loads(path.read_text(encoding="utf-8"))
        journal = cls(Path(data["scope"]), str(data["id"]))
        if journal.path != path:
            raise ValueError(f"journal location does not match its scope: {path}")
        safe_path(journal.scope, path)
        journal.directory = path.parent
        journal.path = path
        journal.data = data
        return journal
