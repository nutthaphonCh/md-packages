"""Batched, preconditioned migration of authored Markdown artifacts."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping
import unicodedata

from .errors import ResolutionError
from .materialize import is_materialized, journal_replace, recover_scope, _restore_operations
from .manifest import load_manifest
from .paths import assert_no_target_conflicts, normalize_target
from .promotion import content_hash, _path_hash, _read_json, _json_bytes
from .resolver import Resolver
from .routing import RouteValidationError, validate_routes
from .service import ServiceError, lookup, scope_path
from .transaction import Journal, ScopeLocks, safe_path


class MigrationError(RuntimeError):
    """The requested migration conflicts with authored or managed state."""


PLAN_VERSION = 1


def _component(value: str, label: str) -> str:
    value = unicodedata.normalize("NFC", value)
    if not value or value in {".", ".."} or "/" in value or "\\" in value or value.casefold() in {"md-package.json", ".md"}:
        raise MigrationError(f"invalid {label}: {value!r}")
    return value


def _identity(value: str) -> tuple[str, str]:
    kind, slash, key = value.partition("/")
    if not slash or not kind or not key or "/" in key:
        raise MigrationError("--as must be KIND/KEY")
    return _component(kind, "kind"), _component(key, "key")


def _fingerprint(path: Path) -> dict[str, int] | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise MigrationError(f"symlinked payload is unsafe: {path}")
    rows = {".": path.stat().st_mode & 0o777}
    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if child.is_symlink():
                raise MigrationError(f"symlinked payload is unsafe: {child}")
            rows[child.relative_to(path).as_posix()] = child.stat().st_mode & 0o777
    return rows


def _owner(path: Path) -> Path | None:
    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        if (candidate / "md-package.json").is_file():
            return candidate
        if (candidate / ".git").exists():
            break
    return None


def _manifest(path: Path) -> dict[str, Any]:
    if path.exists():
        load_manifest(path)
    return _read_json(path, {"version": 1})


def _artifact_record(manifest: Mapping[str, Any], kind: str, key: str) -> dict[str, Any] | None:
    matches = [item for item in manifest.get("artifacts", []) if isinstance(item, dict)
               and item.get("kind") == kind and item.get("key") == key]
    if len(matches) > 1:
        raise MigrationError(f"ambiguous authored artifact: {kind}/{key}")
    return matches[0] if matches else None


def _path_record(manifest: Mapping[str, Any], root: Path, path: Path) -> dict[str, Any] | None:
    matches = [item for item in manifest.get("artifacts", []) if isinstance(item, dict)
               and isinstance(item.get("source"), str) and root / item["source"] == path]
    if len(matches) > 1:
        raise MigrationError(f"multiple artifact records own {path}")
    return matches[0] if matches else None


def _route_refs(route: Any, identity: str, target: str) -> bool:
    if not isinstance(route, dict):
        return False
    return any(ref == identity or ref == target or ref.startswith(target + "/")
               for ref in route.get("read", []))


def _route_items(manifest: Mapping[str, Any]) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    field = "routing" if "routing" in manifest else "routes" if "routes" in manifest else "routing"
    raw = manifest.get(field, {})
    if isinstance(raw, dict):
        items = [(str(key), route) for key, route in raw.items() if isinstance(route, dict)]
        if len(items) != len(raw):
            raise MigrationError(f"{field} entries must be objects")
        return field, items
    if isinstance(raw, list):
        items = []
        for route in raw:
            if not isinstance(route, dict) or not isinstance(route.get("key"), str):
                raise MigrationError(f"{field} list entries require a string key")
            items.append((route["key"], {key: value for key, value in route.items() if key != "key"}))
        if len(items) != len({key for key, _ in items}):
            raise MigrationError(f"duplicate route key in {field}")
        return field, items
    raise MigrationError(f"{field} must be an object or array")


def _remove_route(manifest: dict[str, Any], field: str, key: str) -> None:
    raw = manifest.get(field, {})
    if isinstance(raw, dict):
        del raw[key]
    else:
        manifest[field] = [entry for entry in raw if entry.get("key") != key]


def _set_route(manifest: dict[str, Any], key: str, route: Mapping[str, Any]) -> None:
    field, items = _route_items(manifest)
    current = dict(items).get(key)
    if current is not None and current != route:
        raise MigrationError(f"destination route conflict: {key}")
    if current is not None:
        return
    raw = manifest.setdefault(field, {})
    if isinstance(raw, dict):
        raw[key] = copy.deepcopy(dict(route))
    else:
        raw.append({"key": key, **copy.deepcopy(dict(route))})


def _source(operand: str, scope: Path, as_identity: str | None) -> dict[str, Any]:
    candidate = Path(operand).expanduser()
    resolved_identity: dict[str, Any] | None = None
    if not candidate.is_absolute() and not operand.startswith(".") and len(candidate.parts) == 2:
        try:
            resolved_identity = lookup(scope, operand)["record"]
        except ServiceError:
            pass
    if resolved_identity is None and (candidate.exists() or candidate.is_symlink()):
        lexical = Path(os.path.abspath(candidate))
        if lexical.is_symlink():
            raise MigrationError(f"symlinked payload is unsafe: {lexical}")
        lexical_owner = _owner(lexical)
        if lexical_owner:
            current = lexical_owner
            for part in lexical.relative_to(lexical_owner).parts:
                current /= part
                if current.is_symlink():
                    raise MigrationError(f"symlinked payload is unsafe: {current}")
        path = lexical.resolve()
        owner = lexical_owner.resolve() if lexical_owner else _owner(path)
        manifest_path = owner / "md-package.json" if owner else None
        manifest = _manifest(manifest_path) if manifest_path else {}
        record = _path_record(manifest, owner, path) if owner else None
        if record:
            kind, key = record["kind"], record["key"]
            target = normalize_target(record.get("target", f"{kind}/{key}"))
            if as_identity and _identity(as_identity) != (kind, key):
                raise MigrationError("--as conflicts with the source artifact identity")
        elif as_identity:
            kind, key = _identity(as_identity)
            target = f"{kind}/{key}"
        elif path.is_file() and path.suffix.lower() == ".md":
            kind, key = "docs", _component(path.stem, "key")
            target = f"docs/{key}"
        else:
            raise MigrationError("directory or non-Markdown source needs --as KIND/KEY")
    else:
        if as_identity and _identity(as_identity) != _identity(operand):
            raise MigrationError("--as conflicts with the source artifact identity")
        kind, key = _identity(operand)
        record = resolved_identity or lookup(scope, operand)["record"]
        if "source" not in record:
            raise MigrationError("a route is not an artifact source")
        owner = Path(record["source_root"])
        manifest_path = owner / "md-package.json"
        manifest = _manifest(manifest_path)
        authored = _artifact_record(manifest, kind, key)
        if authored is None:
            raise MigrationError(f"source manifest does not own {operand}")
        path = owner / normalize_target(authored["source"])
        record = authored
        target = normalize_target(record.get("target", f"{kind}/{key}"))
    if not path.exists():
        raise MigrationError(f"source does not exist: {path}")
    if path.name == "md-package.json" or (path.is_dir() and any(
            item.name == "md-package.json" for item in path.rglob("md-package.json"))):
        raise MigrationError(f"source includes a package manifest: {path}")
    if owner:
        for entry in manifest.get("artifacts", []):
            if entry is record or not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
                continue
            authored = owner / normalize_target(entry["source"])
            if _overlap(path, authored):
                raise MigrationError(f"source overlaps another authored artifact: {path}")
        safe_path(owner, path)
        if is_materialized(owner, path):
            raise MigrationError(f"generated or materialized source requires explicit capture: {path}")
    else:
        safe_path(path.parent, path)
    if path.is_relative_to(scope) and is_materialized(scope, path):
        raise MigrationError(f"generated or materialized source requires explicit capture: {path}")
    if any(part in {".agents", ".claude", ".md"} for part in path.parts) or path.name in {"ROUTER.md", "router-extension.md", ".md-lock.json"}:
        raise MigrationError(f"generated source is unsafe: {path}")
    digest = content_hash(path)
    return {"operand": str(path) if resolved_identity is None else operand,
            "path": str(path), "hash": digest, "modes": _fingerprint(path),
            "kind": kind, "key": key, "target": target, "owner": str(owner) if owner else None,
            "manifest": str(manifest_path) if manifest_path else None, "record": record}


def _overlap(a: Path, b: Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def plan_migration(sources: list[str] | tuple[str, ...], destination: str | Path, *,
                   scope: str | Path = ".", as_identity: str | None = None,
                   with_routes: tuple[str, ...] = (), registry_manifest: str | Path | None = None,
                   copy_mode: bool = False) -> dict[str, Any]:
    """Compute every payload and manifest edit without writing anything."""
    if not sources:
        raise MigrationError("migrate requires at least one source")
    if as_identity and len(sources) != 1:
        raise MigrationError("--as is valid only with one source")
    raw_destination = Path(destination).expanduser()
    if raw_destination.is_symlink():
        raise MigrationError(f"symlinked destination is unsafe: {raw_destination}")
    root = raw_destination.resolve()
    if not root.is_dir():
        raise MigrationError(f"destination repository/scope does not exist: {root}")
    safe_path(root, ".md/transactions")
    scope_root = scope_path(scope)
    selected = [_source(value, scope_root, as_identity) for value in sources]
    canonical_sources = [item["operand"] for item in selected]
    paths = [Path(item["path"]) for item in selected]
    for index, path in enumerate(paths):
        for other in paths[index + 1:]:
            if _overlap(path, other) or str(path).casefold() == str(other).casefold():
                raise MigrationError(f"duplicate or overlapping sources: {path}, {other}")
    identities = [(item["kind"], item["key"]) for item in selected]
    if len(identities) != len(set((k.casefold(), v.casefold()) for k, v in identities)):
        raise MigrationError("source identities collide")
    dest_manifest_path = root / "md-package.json"
    before: dict[Path, dict[str, Any]] = {}
    after: dict[Path, dict[str, Any]] = {}
    def edit(path: Path) -> dict[str, Any]:
        if path not in after:
            before[path] = _manifest(path)
            after[path] = copy.deepcopy(before[path])
        return after[path]
    dest = edit(dest_manifest_path)
    dest.setdefault("version", 1)
    dest_artifacts = dest.setdefault("artifacts", [])
    if not isinstance(dest_artifacts, list):
        raise MigrationError("destination artifacts must be an array")
    payloads: list[dict[str, Any]] = []
    for item in selected:
        source_path = Path(item["path"])
        kind, key = item["kind"], item["key"]
        suffix = source_path.suffix if source_path.is_file() else ""
        target_path = root / "packages" / kind / (key + suffix)
        safe_path(root, target_path)
        for ancestor in target_path.parents:
            if ancestor == root:
                break
            if ancestor.exists() and not ancestor.is_dir():
                raise MigrationError(f"destination parent is not a directory: {ancestor}")
            if ancestor.parent.exists():
                for sibling in ancestor.parent.iterdir():
                    if sibling.name.casefold() == ancestor.name.casefold() and sibling.name != ancestor.name:
                        raise MigrationError(f"casefold destination conflict: {sibling}")
        if target_path.parent.exists():
            for sibling in target_path.parent.iterdir():
                if sibling.name.casefold() == target_path.name.casefold() and sibling.name != target_path.name:
                    raise MigrationError(f"casefold destination conflict: {sibling}")
        if _overlap(source_path, target_path) and source_path != target_path:
            raise MigrationError("source and destination payload paths overlap")
        relative = target_path.relative_to(root).as_posix()
        proposed = {"kind": kind, "key": key, "source": relative, "target": item["target"]}
        current = _artifact_record(dest, kind, key)
        for entry in dest_artifacts:
            if entry is current or not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
                continue
            owned = root / normalize_target(entry["source"])
            if _overlap(target_path, owned):
                raise MigrationError(f"destination overlaps another authored artifact: {target_path}")
        if any(isinstance(entry, dict) and isinstance(entry.get("kind"), str)
               and isinstance(entry.get("key"), str)
               and entry["kind"].casefold() == kind.casefold()
               and entry["key"].casefold() == key.casefold()
               and entry != current for entry in dest_artifacts):
            raise MigrationError(f"casefold destination identity conflict: {kind}/{key}")
        if current is not None and current != proposed:
            raise MigrationError(f"destination identity conflict: {kind}/{key}")
        old_hash = _path_hash(target_path)
        if old_hash is not None and old_hash != item["hash"]:
            raise MigrationError(f"destination content conflict: {target_path}")
        if old_hash is not None and _fingerprint(target_path) != item["modes"]:
            raise MigrationError(f"destination mode conflict: {target_path}")
        if old_hash is not None and current != proposed:
            raise MigrationError(f"destination payload has no matching ownership: {target_path}")
        if current is None:
            dest_artifacts.append(proposed)
        payloads.append({"source": item["path"], "destination": str(target_path),
                         "sourceHash": item["hash"], "sourceModes": item["modes"],
                         "beforeHash": old_hash, "beforeModes": _fingerprint(target_path),
                         "identity": f"{kind}/{key}", "target": item["target"],
                         "provenance": {"sourceManifest": item["manifest"], "sourceRecord": item["record"]}})
        if not copy_mode and item["manifest"] and source_path != target_path:
            source_manifest = edit(Path(item["manifest"]))
            if item["record"]:
                source_manifest["artifacts"] = [entry for entry in source_manifest.get("artifacts", [])
                                                if entry != item["record"]]
    all_paths = [Path(item["destination"]) for item in payloads]
    for index, path in enumerate(all_paths):
        for other in all_paths[index + 1:]:
            if _overlap(path, other) or str(path).casefold() == str(other).casefold():
                raise MigrationError(f"destination payloads collide: {path}, {other}")
    assert_no_target_conflicts([(item["target"], item["identity"]) for item in payloads])
    assert_no_target_conflicts([
        (normalize_target(entry.get("target", f"{entry['kind']}/{entry['key']}")),
         f"{entry['kind']}/{entry['key']}") for entry in dest_artifacts if isinstance(entry, dict)
         and isinstance(entry.get("kind"), str) and isinstance(entry.get("key"), str)
    ])
    route_keys = list(with_routes)
    if len(route_keys) != len(set(route_keys)):
        raise MigrationError("duplicate --with-route selection")
    found_routes: set[str] = set()
    route_manifests = {Path(item["manifest"]) for item in selected if item["manifest"]}
    if (scope_root / "md-package.json").is_file():
        route_manifests.add(scope_root / "md-package.json")
    for path in route_manifests:
        source_manifest = edit(path)
        source_field, source_routes = _route_items(source_manifest)
        for key, route in source_routes:
            linked = any(_route_refs(route, item["identity"], item["target"]) for item in payloads)
            if key in route_keys:
                if key in found_routes:
                    raise MigrationError(f"ambiguous route ownership: {key}")
                found_routes.add(key)
                _set_route(dest, key, route)
                if not copy_mode and path != dest_manifest_path:
                    _remove_route(source_manifest, source_field, key)
            elif linked and not copy_mode and path != dest_manifest_path:
                raise MigrationError(f"route {key!r} would dangle; select --with-route {key}")
    if found_routes != set(route_keys):
        raise MigrationError(f"route not found in source manifests: {sorted(set(route_keys) - found_routes)}")
    if route_keys:
        try:
            resolved_artifacts = Resolver().resolve(root).artifacts
        except ResolutionError:
            resolved_artifacts = ()
        effective = {item.identity: item.target for item in resolved_artifacts}
        effective.update({item["identity"]: item["target"] for item in payloads})
        _, destination_routes = _route_items(dest)
        selected_routes = {key: route for key, route in destination_routes if key in found_routes}
        try:
            validate_routes(selected_routes, effective)
        except RouteValidationError as exc:
            raise MigrationError(f"selected route is invalid at destination: {exc}") from exc
    if registry_manifest is not None:
        raw_registry = Path(registry_manifest).expanduser()
        if raw_registry.is_symlink():
            raise MigrationError(f"symlinked registry manifest is unsafe: {raw_registry}")
        reg_path = raw_registry.resolve()
        if not reg_path.is_file():
            raise MigrationError(f"registry manifest does not exist: {reg_path}")
        reg = edit(reg_path).setdefault("registry", {})
        if not isinstance(reg, dict):
            raise MigrationError("registry must be an object")
        registry_name = dest.get("name") or root.name
        value = {"path": os.path.relpath(dest_manifest_path, reg_path.parent).replace(os.sep, "/")}
        if registry_name in reg and reg[registry_name] != value:
            raise MigrationError(f"registry conflict: {registry_name}")
        reg[registry_name] = value
    edits = [{"path": str(path), "beforeHash": _path_hash(path),
              "beforeMode": path.stat().st_mode & 0o777 if path.exists() else None,
              "after": after[path]}
             for path in sorted(after) if before[path] != after[path]]
    affected = sorted({str(root), str(scope_root), *(str(path.parent) for path in after),
                       *(str(Path(item["path"]).parent) for item in selected)})
    return {"kind": "migration", "version": PLAN_VERSION,
            "request": {"sources": canonical_sources, "destination": str(root), "scope": str(scope_root),
                        "as": as_identity, "withRoutes": route_keys,
                        "register": str(Path(registry_manifest).expanduser().resolve()) if registry_manifest else None,
                        "copy": copy_mode},
            "mode": "copy" if copy_mode else "move", "payloadOperations": payloads,
            "manifestEdits": edits, "affectedScopes": affected, "pins": "preserved"}


def save_migration_plan(plan: Mapping[str, Any], path: str | Path) -> None:
    Path(path).write_bytes(_json_bytes(dict(plan)))


def load_migration_plan(path: str | Path) -> dict[str, Any]:
    value = _read_json(Path(path), {})
    if value.get("kind") != "migration" or value.get("version") != PLAN_VERSION:
        raise MigrationError("unsupported migration plan")
    return value


def _replan(plan: Mapping[str, Any]) -> dict[str, Any]:
    if plan.get("kind") != "migration" or plan.get("version") != PLAN_VERSION:
        raise MigrationError("unsupported migration plan")
    request = plan.get("request")
    if not isinstance(request, dict) or set(request) != {"sources", "destination", "scope", "as", "withRoutes", "register", "copy"}:
        raise MigrationError("invalid migration plan request")
    if not isinstance(request["sources"], list) or not all(isinstance(s, str) for s in request["sources"]):
        raise MigrationError("invalid migration plan sources")
    if not isinstance(request["withRoutes"], list) or not all(isinstance(s, str) for s in request["withRoutes"]):
        raise MigrationError("invalid migration plan routes")
    if type(request["copy"]) is not bool:
        raise MigrationError("invalid migration plan mode")
    current = plan_migration(request["sources"], request["destination"], scope=request["scope"],
                             as_identity=request["as"], with_routes=tuple(request["withRoutes"]),
                             registry_manifest=request["register"], copy_mode=request["copy"])
    if current != dict(plan):
        raise MigrationError("stale or modified migration plan")
    return current


def _unfinished_recovery_scopes(root: Path) -> set[Path]:
    directory = safe_path(root, ".md/transactions")
    scopes = {root.resolve()}
    if not directory.exists():
        return scopes
    for path in sorted(directory.glob("*/journal.json")):
        journal = Journal.load(path)
        if journal.data.get("state") in {"committed", "rolled_back", "recovered"}:
            continue
        for operation in journal.data.get("operations", []):
            scopes.add(Path(operation.get("scope", journal.scope)).resolve())
    return scopes


def apply_migration(plan: Mapping[str, Any], *,
                    failure_injector: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Apply all changes under ordered locks, with one recovery journal."""
    initial = _replan(plan)
    root = Path(initial["request"]["destination"])
    scopes = {root, *_unfinished_recovery_scopes(root),
              *(Path(edit["path"]).parent for edit in initial["manifestEdits"]),
              *(Path(item["source"]).parent for item in initial["payloadOperations"])}
    with ScopeLocks(scopes):
        locked = {Path(scope).resolve() for scope in scopes}
        if not _unfinished_recovery_scopes(root).issubset(locked):
            raise MigrationError("recovery scopes changed while acquiring locks; retry")
        recover_scope(root)
        checked = _replan(plan)
        journal = Journal(root)
        journal.data["kind"] = "migration"
        journal.save()
        try:
            staged_payloads: list[tuple[dict[str, Any], Path]] = []
            for index, item in enumerate(checked["payloadOperations"]):
                if item["beforeHash"] is not None or item["source"] == item["destination"]:
                    continue
                src = Path(item["source"])
                staged = journal.directory / f"payload-{index}"
                if src.is_dir():
                    shutil.copytree(src, staged, symlinks=True)
                else:
                    shutil.copy2(src, staged)
                if content_hash(staged) != item["sourceHash"] or _fingerprint(staged) != item["sourceModes"]:
                    raise MigrationError(f"source changed during staging: {src}")
                staged_payloads.append((item, staged))
            for item, staged in staged_payloads:
                journal_replace(journal, root, Path(item["destination"]).relative_to(root).as_posix(), staged,
                                failure_injector)
            for index, edit in enumerate(checked["manifestEdits"]):
                path = Path(edit["path"])
                staged = journal.directory / f"manifest-{index}"
                staged.write_bytes(_json_bytes(edit["after"]))
                journal_replace(journal, path.parent, path.name, staged, failure_injector)
            if checked["mode"] == "move":
                for item in checked["payloadOperations"]:
                    src = Path(item["source"])
                    if src != Path(item["destination"]):
                        if content_hash(src) != item["sourceHash"] or _fingerprint(src) != item["sourceModes"]:
                            raise MigrationError(f"source changed before cleanup: {src}")
                        journal_replace(journal, src.parent, src.name, None, failure_injector)
                        backup = Path(journal.data["operations"][-1]["backup"])
                        if content_hash(backup) != item["sourceHash"] or _fingerprint(backup) != item["sourceModes"]:
                            raise MigrationError(f"source changed during cleanup: {src}")
            journal.update(state="committed")
            return {"applied": True, "mode": checked["mode"], "affectedScopes": checked["affectedScopes"]}
        except Exception:
            _restore_operations(journal)
            journal.update(state="rolled_back")
            raise
