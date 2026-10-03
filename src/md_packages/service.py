"""Application services used by the command line interface."""

from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .errors import MdPackageError
from .hashing import tree_hash
from .manifest import load_manifest
from .materialize import (
    MaterializationPlan,
    PlannedOutput,
    apply_materialization,
    plan_materialization,
    read_lock,
    recover_scope,
    sha256_path,
    is_materialized,
)
from .models import Artifact, ResolutionPlan, Route
from .resolver import Resolver
from .routing import validate_routes


class ServiceError(MdPackageError):
    """A command cannot safely complete with the supplied scope state."""


def scope_path(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path.is_file():
        path = path.parent
    start = path
    for _ in range(6):
        if (path / "md-package.json").is_file():
            return path
        if (path / ".git").exists() or path.parent == path:
            break
        path = path.parent
    return start


def nearest_scope(plan: ResolutionPlan) -> Path:
    return Path(plan.scopes[-1]).parent


def plans_in_tree(plan: ResolutionPlan) -> Iterable[ResolutionPlan]:
    yield plan
    for child in plan.nested:
        yield from plans_in_tree(child)


def _route_dict(route: Route) -> dict[str, Any]:
    return {"when": route.when, "read": list(route.read), "disabled": route.disabled}


def _source_path(artifact: Artifact) -> Path:
    if artifact.source_root is None:
        raise ServiceError(f"{artifact.identity}: resolved artifact has no source root")
    root = Path(artifact.source_root).resolve()
    source = (root / artifact.source).resolve()
    if not source.is_relative_to(root):
        raise ServiceError(f"{artifact.identity}: source escapes its package")
    return source


def _add_output(outputs: dict[str, PlannedOutput], target: str, content: bytes, mode: int = 0o644) -> None:
    previous = outputs.get(target)
    item = PlannedOutput(target, content, mode)
    if previous is not None and previous != item:
        raise ServiceError(f"generated target collision: {target}")
    outputs[target] = item


def _source_files(artifact: Artifact) -> list[tuple[str, bytes, int]]:
    source = _source_path(artifact)
    if tree_hash(source) != artifact.hash:
        raise ServiceError(f"{artifact.identity}: authored source changed during planning")
    if source.is_file():
        return [(artifact.target, source.read_bytes(), source.stat().st_mode & 0o777)]
    files: list[tuple[str, bytes, int]] = []
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ServiceError(f"{artifact.identity}: symlinked source is not distributable: {path}")
        if path.is_file():
            files.append((f"{artifact.target}/{path.relative_to(source).as_posix()}", path.read_bytes(), path.stat().st_mode & 0o777))
    return files


def _outputs(artifacts: tuple[Artifact, ...]) -> dict[str, PlannedOutput]:
    outputs: dict[str, PlannedOutput] = {}
    for artifact in artifacts:
        for target, content, mode in _source_files(artifact):
            _add_output(outputs, target, content, mode)
            if artifact.kind == "skills":
                suffix = target.removeprefix(artifact.target).lstrip("/")
                if suffix:
                    for platform in (".agents", ".claude"):
                        _add_output(outputs, f"{platform}/skills/{artifact.key}/{suffix}", content, mode)
    return outputs


def _resolution_metadata(plan: ResolutionPlan, scope: Path) -> dict[str, Any]:
    sources = []
    for artifact in plan.artifacts:
        raw = asdict(artifact)
        raw.pop("source_root", None)
        raw["sourcePath"] = os.path.relpath(_source_path(artifact), scope).replace(os.sep, "/")
        sources.append(raw)
    manifests = []
    for item in plan.scopes:
        path = Path(item)
        manifests.append({"path": os.path.relpath(path, scope).replace(os.sep, "/"), "sha256": sha256_path(path)})
    return {
        "artifacts": sources,
        "routes": [asdict(item) for item in plan.routes],
        "manifests": manifests,
        "replaced": [dict(asdict(item), previous_scope=os.path.relpath(item.previous_scope, scope).replace(os.sep, "/"))
                     for item in plan.replaced],
    }


def _locked_view(scope: Path) -> tuple[tuple[Artifact, ...], tuple[Route, ...], dict[str, Any]]:
    lock = read_lock(scope)
    if not lock or not isinstance(lock.get("resolution"), dict):
        raise ServiceError(f"{scope}: --locked needs a lock with source metadata; run mdpkg install first")
    metadata = lock["resolution"]
    if not isinstance(metadata.get("artifacts"), list) or not isinstance(metadata.get("routes"), list):
        raise ServiceError(f"{scope}: lock lacks a complete artifact/route snapshot; run mdpkg install")
    artifacts: list[Artifact] = []
    for raw in metadata.get("artifacts", []):
        relative = raw.get("sourcePath")
        if not isinstance(relative, str) or not relative:
            raise ServiceError(f"{scope}: lock artifact lacks sourcePath; cannot replay --locked")
        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise ServiceError(f"{scope}: locked sourcePath must be relative: {relative}")
        unresolved = scope / relative_path
        if any(part.is_symlink() for part in (unresolved, *unresolved.parents)):
            raise ServiceError(f"{scope}: locked source is symlinked: {relative}")
        source_path = unresolved.resolve()
        if is_materialized(scope, source_path):
            raise ServiceError(f"{scope}: locked source is generated content: {relative}")
        if not source_path.exists():
            raise ServiceError(f"{scope}: locked source is unavailable: {relative}")
        item = Artifact(str(raw["kind"]), str(raw["key"]), str(raw["target"]), str(raw["source"]),
                        str(raw["hash"]))
        if tree_hash(source_path) != item.hash:
            raise ServiceError(f"{scope}: locked source hash changed: {relative}")
        artifacts.append(item)
    routes = tuple(Route(str(raw["key"]), raw.get("when"), tuple(raw.get("read", [])), bool(raw.get("disabled", False)))
                   for raw in metadata.get("routes", []))
    for raw in metadata.get("manifests", []):
        path = (scope / raw["path"]).resolve()
        if not path.is_file() or sha256_path(path) != raw["sha256"]:
            raise ServiceError(f"{scope}: locked manifest changed: {raw['path']}")
    return tuple(artifacts), routes, metadata


def _locked_outputs(scope: Path, metadata: dict[str, Any]) -> dict[str, PlannedOutput]:
    outputs: dict[str, PlannedOutput] = {}
    owned = (read_lock(scope) or {}).get("outputs", {})
    for raw in metadata.get("artifacts", []):
        source = (scope / raw["sourcePath"]).resolve()
        target = raw["target"]
        files = [(target, source)] if source.is_file() else [
            (f"{target}/{item.relative_to(source).as_posix()}", item)
            for item in sorted(source.rglob("*")) if item.is_file()
        ]
        for name, path in files:
            if path.is_symlink():
                raise ServiceError(f"locked source contains a symlink: {path}")
            content = path.read_bytes()
            mode = path.stat().st_mode & 0o777
            record = owned.get(name)
            if isinstance(record, dict) and "mode" in record and mode != record["mode"]:
                raise ServiceError(f"locked source mode changed: {name}")
            _add_output(outputs, name, content, mode)
            if raw["kind"] == "skills":
                suffix = name.removeprefix(target).lstrip("/")
                if suffix:
                    for platform in (".agents", ".claude"):
                        _add_output(outputs, f"{platform}/skills/{raw['key']}/{suffix}", content, mode)
    return outputs


def _plan_one(plan: ResolutionPlan, *, entrypoints: tuple[str, ...]) -> MaterializationPlan:
    scope = nearest_scope(plan)
    for artifact in plan.artifacts:
        source = _source_path(artifact)
        overlaps = any(source == target or source.is_relative_to(target) or target.is_relative_to(source)
                       for target in (scope / item.target for item in plan.artifacts))
        if is_materialized(scope, source) or is_materialized(Path(artifact.source_root), source) or overlaps:
            raise ServiceError(f"{artifact.identity}: generated content cannot be an authored source; promote it first")
    artifacts = {item.identity: {"target": item.target} for item in plan.artifacts}
    routes = {item.key: _route_dict(item) for item in plan.routes}
    local = load_manifest(scope / "md-package.json")
    scope_routes = {item.key: _route_dict(item) for item in local.routes}
    validate_routes(routes, artifacts)
    return plan_materialization(scope, _outputs(plan.artifacts), effective_routes=routes,
                                scope_routes=scope_routes, artifacts=artifacts,
                                entrypoints=entrypoints, lock_metadata={"resolution": _resolution_metadata(plan, scope)})


def _plan_locked(scope: Path, *, entrypoints: tuple[str, ...]) -> MaterializationPlan:
    artifacts, routes, metadata = _locked_view(scope)
    owned_outputs = (read_lock(scope) or {}).get("outputs", {})
    remembered_entrypoints = tuple(
        path for path in ("AGENTS.md", "CLAUDE.md")
        if isinstance(owned_outputs, dict) and path in owned_outputs
    )
    replay_entrypoints = tuple(dict.fromkeys((*remembered_entrypoints, *entrypoints)))
    artifacts_map = {item.identity: {"target": item.target} for item in artifacts}
    routes_map = {item.key: _route_dict(item) for item in routes}
    validate_routes(routes_map, artifacts_map)
    local_routes = {item.key: _route_dict(item) for item in load_manifest(scope / "md-package.json").routes}
    return plan_materialization(scope, _locked_outputs(scope, metadata), effective_routes=routes_map,
                                scope_routes=local_routes, artifacts=artifacts_map,
                                entrypoints=replay_entrypoints, lock_metadata={"resolution": metadata})


def _locked_scope_tree(scope: Path) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()

    def visit(current: Path) -> None:
        current = current.resolve()
        if current in seen:
            raise ServiceError(f"nested scope cycle while replaying locks: {current}")
        seen.add(current)
        result.append(current)
        for item in load_manifest(current / "md-package.json").nests:
            visit(current / item.path)

    visit(scope)
    return result


def install(path: str | Path, *, dry_run: bool = False, locked: bool = False, all_scopes: bool = False,
            max_parent_depth: int = 5, entrypoints: tuple[str, ...] = ()) -> dict[str, Any]:
    start = scope_path(path)
    if locked:
        scopes = _locked_scope_tree(start) if all_scopes else [start]
        plans = [_plan_locked(scope, entrypoints=entrypoints) for scope in scopes]
    else:
        resolved = Resolver(max_parent_depth=max_parent_depth).resolve(start)
        plans = [_plan_one(item, entrypoints=entrypoints) for item in (plans_in_tree(resolved) if all_scopes else (resolved,))]
    summary = {"dryRun": dry_run, "locked": locked, "scopes": [
        {"path": str(plan.scope), "outputs": [item.path for item in plan.outputs],
         "removals": list(plan.removals)} for plan in plans]}
    if not dry_run:
        apply_materialization(plans)
    return summary


def resolved_view(path: str | Path, *, max_parent_depth: int = 5) -> dict[str, Any]:
    plan = Resolver(max_parent_depth=max_parent_depth).resolve(scope_path(path))
    return {"scope": str(nearest_scope(plan)), "artifacts": [asdict(item) for item in plan.artifacts],
            "routes": [asdict(item) for item in plan.routes], "replaced": [asdict(item) for item in plan.replaced]}


def lookup(path: str | Path, identity: str) -> dict[str, Any]:
    view = resolved_view(path)
    matches = [item for item in view["artifacts"] if f"{item['kind']}/{item['key']}" == identity]
    matches += [item for item in view["routes"] if item["key"] == identity or f"route/{item['key']}" == identity]
    if not matches:
        raise ServiceError(f"no artifact or route named {identity!r}")
    result = {"identity": identity, "record": matches[0],
              "overrides": [item for item in view["replaced"] if item["identity"] == identity]}
    return result


def doctor(path: str | Path) -> dict[str, Any]:
    scope = scope_path(path)
    findings: list[str] = []
    try:
        view = resolved_view(scope)
        validate_routes({item["key"]: {**item, "read": list(item["read"])} for item in view["routes"]},
                        {f"{item['kind']}/{item['key']}": {"target": item["target"]} for item in view["artifacts"]})
    except (MdPackageError, ValueError, OSError) as exc:
        findings.append(f"manifest/graph: {exc}")
    lock = read_lock(scope)
    if lock is None:
        findings.append("lock missing; run mdpkg install")
    else:
        for target, record in lock.get("outputs", {}).items():
            actual = scope / target
            expected = record.get("sha256") if isinstance(record, dict) else record
            if not actual.exists():
                findings.append(f"managed output missing: {target}")
            elif sha256_path(actual) != expected:
                findings.append(f"managed output modified: {target}")
            elif isinstance(record, dict) and "mode" in record and actual.stat().st_mode & 0o777 != record["mode"]:
                findings.append(f"managed output mode modified: {target}")
        try:
            _locked_view(scope)
        except (MdPackageError, ValueError, KeyError, OSError) as exc:
            findings.append(f"lock source: {exc}")
    return {"scope": str(scope), "ok": not findings, "findings": findings}


def recover(path: str | Path) -> dict[str, Any]:
    scope = scope_path(path)
    return {"scope": str(scope), "recovered": [str(item) for item in recover_scope(scope)]}
