"""Planning and safe application for repository-level Markdown promotions.

Plans are plain JSON. Application shares the installer's journal primitives,
so the CLI can recover both operations without reverse-parsing generated files.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unicodedata
import uuid
from typing import Any, Mapping

from .transaction import Journal, ScopeLocks, safe_path
from .materialize import journal_replace, _restore_operations, recover_scope


class PromotionError(RuntimeError):
    """A promotion is invalid or can no longer be safely applied."""


PLAN_VERSION = 1


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromotionError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"JSON object required: {path}")
    return value


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(path: str | Path) -> str:
    """Hash a file or deterministic directory tree; payload symlinks are illegal."""
    root = Path(path)
    if root.is_symlink():
        raise PromotionError(f"symlinks cannot be promoted: {root}")
    if root.is_file():
        return _sha_bytes(root.read_bytes())
    if not root.is_dir():
        raise PromotionError(f"source does not exist: {root}")
    rows: list[str] = []
    for child in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        relative = child.relative_to(root).as_posix()
        if child.is_symlink():
            raise PromotionError(f"symlinks cannot be promoted: {child}")
        if child.is_dir():
            rows.append(f"d\0{relative}")
        elif child.is_file():
            mode = child.stat().st_mode & 0o777
            rows.append(f"f\0{relative}\0{mode:o}\0{_sha_bytes(child.read_bytes())}")
        else:
            raise PromotionError(f"unsupported payload entry: {child}")
    return _sha_bytes("\n".join(rows).encode())


def _path_hash(path: Path) -> str | None:
    return content_hash(path) if path.exists() or path.is_symlink() else None


def _validate_component(value: str, label: str) -> str:
    value = unicodedata.normalize("NFC", value)
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise PromotionError(f"invalid {label}: {value!r}")
    return value


def _validate_registry_key(value: str) -> str:
    """Registry keys may be scoped names (for example ``@team/frontend``)."""
    value = unicodedata.normalize("NFC", value)
    if not value or value.startswith("/") or "\\" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise PromotionError(f"invalid registry name: {value!r}")
    return value


def _relative(path: Path, base: Path) -> str:
    try:
        return path.relative_to(base).as_posix()
    except ValueError as exc:
        raise PromotionError(f"path escapes repository: {path}") from exc


def _artifact_entry(manifest: Mapping[str, Any], kind: str, name: str) -> Any:
    """Find the authored record consumed by :mod:`md_packages.resolver`."""
    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise PromotionError("manifest artifacts must be an array")
    matches = [item for item in artifacts if isinstance(item, dict) and item.get("kind") == kind and item.get("key") == name]
    if len(matches) > 1:
        raise PromotionError(f"ambiguous destination artifact: {kind}/{name}")
    return matches[0] if matches else None


def _set_artifact(manifest: dict[str, Any], artifact: dict[str, str]) -> None:
    artifacts = manifest.setdefault("artifacts", [])
    if not isinstance(artifacts, list):
        raise PromotionError("manifest artifacts must be an array")
    for index, current in enumerate(artifacts):
        if isinstance(current, dict) and current.get("kind") == artifact["kind"] and current.get("key") == artifact["key"]:
            artifacts[index] = artifact
            return
    artifacts.append(artifact)


def _validate_route(route: Mapping[str, Any]) -> dict[str, Any]:
    """Keep planned route edits within the v1 schema, before any write occurs."""
    allowed = {"when", "read", "disabled"}
    if set(route) - allowed:
        raise PromotionError("route has fields not supported by manifest schema")
    result = dict(route)
    if "when" in result and not isinstance(result["when"], str):
        raise PromotionError("route.when must be a string")
    if "read" in result and (not isinstance(result["read"], list) or not all(isinstance(v, str) and v for v in result["read"])):
        raise PromotionError("route.read must be an array of non-empty strings")
    if "disabled" in result and not isinstance(result["disabled"], bool):
        raise PromotionError("route.disabled must be boolean")
    return result


def _manifest_edit(path: Path, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    if before == after:
        return None
    return {"path": str(path), "beforeHash": _path_hash(path), "after": after}


def _source_provenance(source: Path, materialized: Mapping[str, Any] | None, capture: bool) -> tuple[Path, dict[str, Any] | None, str]:
    actual_hash = content_hash(source)
    if not materialized:
        return source, None, actual_hash
    if materialized.get("captureOnly"):
        if not capture:
            raise PromotionError("generated content requires --from-materialized and --as KIND/NAME")
        return source, {"origin": materialized["origin"], "baseHash": materialized.get("baseHash"),
                        "capturedHash": actual_hash}, actual_hash
    pinned = Path(str(materialized.get("source", ""))).expanduser()
    base_hash = materialized.get("baseHash")
    origin = materialized.get("origin")
    if not pinned or not base_hash or not origin:
        raise PromotionError("materialized provenance needs source, baseHash, and origin")
    if actual_hash == base_hash and not capture:
        if not pinned.exists():
            raise PromotionError(f"pinned authored source is unavailable: {pinned}")
        if content_hash(pinned) != base_hash:
            raise PromotionError("pinned source no longer matches materialized base hash")
        return pinned, None, base_hash
    if actual_hash != base_hash and not capture:
        raise PromotionError("materialized content was edited; use from_materialized=True to capture a fork")
    return source, {"origin": origin, "baseHash": base_hash, "capturedHash": actual_hash}, actual_hash


def plan_promotion(
    source: str | Path,
    *,
    destination_repo: str | Path,
    kind: str,
    name: str | None = None,
    move: bool = False,
    from_materialized: bool = False,
    materialized: Mapping[str, Any] | None = None,
    route_key: str | None = None,
    route: Mapping[str, Any] | None = None,
    registry_manifest: str | Path | None = None,
    registry_name: str | None = None,
    affected_scopes: tuple[str | Path, ...] = (),
) -> dict[str, Any]:
    """Return a JSON-compatible, non-mutating promotion plan.

    ``materialized`` is lock-derived provenance (``source``, ``baseHash``,
    ``origin``). Generated descendants and routers need explicit capture-only
    provenance and the caller's from_materialized opt-in.
    """
    source_path = Path(source).expanduser().resolve()
    repo = Path(destination_repo).expanduser().resolve()
    kind = _validate_component(kind, "kind")
    name = _validate_component(name or source_path.name, "name")
    if not repo.is_dir():
        raise PromotionError(f"destination repository does not exist: {repo}")
    selected, provenance, selected_hash = _source_provenance(source_path, materialized, from_materialized)
    selected = selected.resolve()
    target = repo / "packages" / kind / name
    if selected != target and (target.is_relative_to(selected) or selected.is_relative_to(target)):
        raise PromotionError("source and destination payload paths overlap")
    safe_path(repo, target)
    safe_path(repo, ".md/transactions")
    safe_path(repo, "md-package.json")
    target_rel = _relative(target, repo)
    artifact = {"kind": kind, "key": name, "source": target_rel, "target": f"{kind}/{name}"}
    manifest_path = repo / "md-package.json"
    before_manifest = _read_json(manifest_path, {})
    old_entry = _artifact_entry(before_manifest, kind, name)
    existing_hash = _path_hash(target)
    if existing_hash is not None and existing_hash != selected_hash:
        raise PromotionError(f"destination content conflict: {target}")
    if old_entry is not None and old_entry != artifact:
        raise PromotionError(f"destination artifact conflict: {kind}/{name}")
    after_manifest = json.loads(json.dumps(before_manifest))
    _set_artifact(after_manifest, artifact)
    if route_key is not None:
        route_key = _validate_component(route_key, "route key")
        if route is None or not isinstance(route, Mapping):
            raise PromotionError("route_key requires structured route metadata")
        routing = after_manifest.setdefault("routing", {})
        if not isinstance(routing, dict):
            raise PromotionError("manifest routing must be an object")
        current_route = routing.get(route_key)
        proposed_route = _validate_route(route)
        if current_route is not None and current_route != proposed_route:
            raise PromotionError(f"destination route conflict: {route_key}")
        routing[route_key] = proposed_route
    edits = []
    edit = _manifest_edit(manifest_path, before_manifest, after_manifest)
    if edit:
        edits.append(edit)
    if registry_manifest is not None:
        reg_path = Path(registry_manifest).expanduser().resolve()
        reg_before = _read_json(reg_path, {})
        reg_after = json.loads(json.dumps(reg_before))
        key = _validate_registry_key(registry_name or name)
        registry = reg_after.setdefault("registry", {})
        if not isinstance(registry, dict):
            raise PromotionError("registry must be an object")
        value = {"path": os.path.relpath(manifest_path, reg_path.parent).replace(os.sep, "/")}
        if key in registry and registry[key] != value:
            raise PromotionError(f"registry conflict: {key}")
        registry[key] = value
        reg_edit = _manifest_edit(reg_path, reg_before, reg_after)
        if reg_edit:
            edits.append(reg_edit)
    operation = "noop" if existing_hash == selected_hash and not edits and not move else ("move" if move else "copy")
    return {
        "version": PLAN_VERSION,
        "operation": operation,
        "source": {"path": str(selected), "hash": selected_hash, "materialized": bool(materialized), "capture": bool(provenance), "provenance": provenance},
        "destination": {"repository": str(repo), "path": str(target), "relativePath": target_rel, "kind": kind, "name": name, "beforeHash": existing_hash},
        "manifestEdits": edits,
        "removals": [],
        "affectedScopes": [str(Path(item).expanduser().resolve()) for item in affected_scopes],
        "pins": "preserved",
    }


def save_plan(plan: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_bytes(_json_bytes(dict(plan)))


def load_plan(path: str | Path) -> dict[str, Any]:
    plan = _read_json(Path(path), {})
    if plan.get("version") != PLAN_VERSION:
        raise PromotionError("unsupported promotion plan version")
    return plan


def _replace_from(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix="mdpkg-promote-", dir=destination.parent))
    staged = staging_parent / destination.name
    try:
        if source.is_dir():
            shutil.copytree(source, staged, symlinks=False)
        else:
            shutil.copy2(source, staged)
        if destination.exists():
            if destination.is_dir(): shutil.rmtree(destination)
            else: destination.unlink()
        os.replace(staged, destination)
    finally:
        shutil.rmtree(staging_parent, ignore_errors=True)


def apply_promotion(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a plan with per-file backups and a local recovery journal.

    The shared recovery journal also covers manifest edits and source moves.
    """
    if plan.get("version") != PLAN_VERSION:
        raise PromotionError("unsupported promotion plan version")
    _json_bytes(dict(plan))  # Reject unserializable saved edits before any writes.
    src = Path(str(plan["source"]["path"]))
    dest = Path(str(plan["destination"]["path"]))
    if _path_hash(src) != plan["source"]["hash"]:
        raise PromotionError("stale plan: source changed since planning")
    if _path_hash(dest) != plan["destination"].get("beforeHash"):
        raise PromotionError("stale plan: destination changed since planning")
    for edit in plan.get("manifestEdits", []):
        path = Path(edit["path"])
        if _path_hash(path) != edit.get("beforeHash"):
            raise PromotionError(f"stale plan: touched manifest changed: {path}")
    repo = Path(str(plan["destination"]["repository"]))
    safe_path(repo, dest)
    safe_path(repo, ".md/transactions")
    safe_path(src.parent, src)
    scopes = [repo, *(Path(edit["path"]).parent for edit in plan.get("manifestEdits", []))]
    if plan["operation"] == "move":
        scopes.append(src.parent)
    for edit in plan.get("manifestEdits", []):
        safe_path(Path(edit["path"]).parent, edit["path"])
    with ScopeLocks(scopes):
        recover_scope(repo)
        if _path_hash(src) != plan["source"]["hash"] or _path_hash(dest) != plan["destination"].get("beforeHash"):
            raise PromotionError("stale plan: source or destination changed")
        for edit in plan.get("manifestEdits", []):
            if _path_hash(Path(edit["path"])) != edit.get("beforeHash"):
                raise PromotionError("stale plan: touched manifest changed")
        journal = Journal(repo)
        journal.data["kind"] = "promotion"
        journal.save()
        try:
            if plan["operation"] in {"copy", "move"} and _path_hash(dest) is None:
                staged = journal.directory / "payload"
                if src.is_dir():
                    shutil.copytree(src, staged)
                else:
                    shutil.copy2(src, staged)
                journal_replace(journal, repo, dest.relative_to(repo).as_posix(), staged)
            for index, edit in enumerate(plan.get("manifestEdits", [])):
                path = Path(edit["path"])
                staged = journal.directory / f"manifest-{index}"
                staged.write_bytes(_json_bytes(edit["after"]))
                journal_replace(journal, path.parent, path.name, staged)
            if plan["operation"] == "move" and src != dest:
                journal_replace(journal, src.parent, src.name, None)
            journal.update(state="committed")
            return {"applied": True, "operation": plan["operation"], "affectedScopes": plan.get("affectedScopes", [])}
        except Exception:
            _restore_operations(journal)
            journal.update(state="rolled_back")
            raise
