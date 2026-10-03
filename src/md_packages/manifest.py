"""Strict parsing for the small authored JSON manifest surface."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ManifestError
from .models import Artifact, Import, Manifest, Nest, Route
from .paths import normalize_target


def _error(path: Path, message: str) -> ManifestError:
    return ManifestError(f"{path}: {message}")


def _mapping(value: Any, path: Path, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(path, f"{label} must be an object")
    return value


def _string(value: Any, path: Path, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise _error(path, f"{label} must be a non-empty string")
    return value


def load_manifest(path: str | Path) -> Manifest:
    path = Path(path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise _error(path, f"invalid JSON: {exc.msg}") from exc
    raw = _mapping(raw, path, "manifest")
    if raw.get("version", 1) != 1:
        raise _error(path, "only manifest version 1 is supported")

    registry: dict[str, str] = {}
    for name, entry in _mapping(raw.get("registry", {}), path, "registry").items():
        if isinstance(entry, str):
            registry[_string(name, path, "registry name")] = entry
        else:
            registry[_string(name, path, "registry name")] = _string(
                _mapping(entry, path, f"registry.{name}").get("path"), path, f"registry.{name}.path"
            )

    imports: list[Import] = []
    for entry in raw.get("imports", []):
        if isinstance(entry, str):
            imports.append(Import(entry))
            continue
        item = _mapping(entry, path, "import")
        mode = item.get("mode", "flatten")
        if mode not in {"flatten", "nest"}:
            raise _error(path, "import.mode must be flatten or nest")
        imports.append(Import(_string(item.get("package"), path, "import.package"), mode))

    nests: list[Nest] = []
    for entry in raw.get("nest", raw.get("nests", [])):
        item = _mapping(entry, path, "nest")
        nests.append(Nest(normalize_target(_string(item.get("path"), path, "nest.path")), item.get("package")))

    artifacts: list[Artifact] = []
    for entry in raw.get("artifacts", []):
        item = _mapping(entry, path, "artifact")
        kind = _string(item.get("kind"), path, "artifact.kind")
        key = _string(item.get("key"), path, "artifact.key")
        source = _string(item.get("source"), path, "artifact.source")
        target = normalize_target(item.get("target", f"{kind}/{key}"))
        artifacts.append(Artifact(kind, key, target, source))

    routes: list[Route] = []
    raw_routes = raw.get("routing", raw.get("routes", {}))
    if isinstance(raw_routes, list):
        raw_routes = {entry.get("key"): entry for entry in raw_routes if isinstance(entry, dict)}
    for key, entry in _mapping(raw_routes, path, "routing").items():
        item = _mapping(entry, path, f"routing.{key}")
        disabled = item.get("disabled", False)
        if not isinstance(disabled, bool):
            raise _error(path, f"routing.{key}.disabled must be boolean")
        read = item.get("read", [])
        if not isinstance(read, list) or not all(isinstance(v, str) and v for v in read):
            raise _error(path, f"routing.{key}.read must be an array of strings")
        routes.append(Route(_string(key, path, "route key"), item.get("when"), tuple(read), disabled))

    depth = raw.get("maxParentDepth")
    if depth is not None and (not isinstance(depth, int) or isinstance(depth, bool) or depth < 0):
        raise _error(path, "maxParentDepth must be a non-negative integer")
    return Manifest(str(path), raw.get("name"), registry, tuple(imports), tuple(nests), tuple(artifacts), tuple(routes), depth)
