"""Portable target-path validation and conflict detection."""
from __future__ import annotations

import posixpath
import unicodedata
from pathlib import Path

from .errors import ConflictError, PathValidationError

_RESERVED = {".md-lock.json", "md-package.json", "router.md", "router-extension.md", ".md"}


def normalize_target(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise PathValidationError("target must be a non-empty POSIX-relative path")
    value = unicodedata.normalize("NFC", value.replace("\\", "/"))
    if value.startswith("/") or value.startswith("//") or ":" in value.split("/", 1)[0]:
        raise PathValidationError(f"target must be relative: {value!r}")
    normal = posixpath.normpath(value)
    if normal in {".", ".."} or normal.startswith("../"):
        raise PathValidationError(f"target escapes package root: {value!r}")
    parts = normal.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise PathValidationError(f"invalid target: {value!r}")
    if parts[0].casefold() in _RESERVED:
        raise PathValidationError(f"target uses reserved generated path: {value!r}")
    return normal


def target_key(target: str) -> str:
    return normalize_target(target).casefold()


def assert_no_target_conflicts(targets: list[tuple[str, str]]) -> None:
    """Reject equal (case-insensitive) and file/directory prefix claims."""
    normalized = [(target_key(path), owner) for path, owner in targets]
    for index, (path, owner) in enumerate(normalized):
        for other, other_owner in normalized[index + 1 :]:
            if path == other or path.startswith(other + "/") or other.startswith(path + "/"):
                raise ConflictError(f"target conflict: {owner} ({path}) vs {other_owner} ({other})")


def absolute_from(scope: Path, target: str) -> Path:
    return scope / normalize_target(target)
