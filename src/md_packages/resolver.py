"""Pure(read-only) recursive resolver for md-package scopes."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Iterable

from .errors import ConflictError, ManifestError, ResolutionError, SourceSymlinkError
from .hashing import tree_hash
from .manifest import load_manifest
from .models import Artifact, Manifest, ReplacedRecord, ResolutionPlan, Route
from .paths import assert_no_target_conflicts, normalize_target

DEFAULT_MAX_PARENT_DEPTH = 5
DEFAULT_MAX_NEST_DEPTH = 8
DEFAULT_MAX_GRAPH_NODES = 128


def discover_ancestors(start: str | Path, *, max_depth: int = DEFAULT_MAX_PARENT_DEPTH,
                       root_markers: Iterable[str] = (".git",)) -> tuple[Path, ...]:
    """Find scope manifests from highest ancestor to nearest, with a hard bound."""
    directory = Path(start).resolve()
    if directory.is_file():
        directory = directory.parent
    found: list[Path] = []
    for depth in range(max_depth + 1):
        manifest = directory / "md-package.json"
        if manifest.is_file():
            found.append(manifest)
        if depth == max_depth or any((directory / marker).exists() for marker in root_markers):
            break
        if directory.parent == directory:
            break
        directory = directory.parent
    return tuple(reversed(found))


class Resolver:
    def __init__(self, *, max_parent_depth: int = DEFAULT_MAX_PARENT_DEPTH,
                 max_nest_depth: int = DEFAULT_MAX_NEST_DEPTH,
                 max_graph_nodes: int = DEFAULT_MAX_GRAPH_NODES) -> None:
        self.max_parent_depth = max_parent_depth
        self.max_nest_depth = max_nest_depth
        self.max_graph_nodes = max_graph_nodes
        self._nodes = 0

    def resolve(self, start: str | Path) -> ResolutionPlan:
        manifests = discover_ancestors(start, max_depth=self.max_parent_depth)
        if not manifests:
            raise ResolutionError(f"no md-package.json found above {Path(start).resolve()}")
        loaded = [load_manifest(path) for path in manifests]
        max_from_scope = next((m.max_parent_depth for m in reversed(loaded) if m.max_parent_depth is not None), None)
        if max_from_scope is not None:
            loaded = [load_manifest(path) for path in discover_ancestors(start, max_depth=min(self.max_parent_depth, max_from_scope))]
        self._nodes = 0
        artifacts: dict[str, tuple[Artifact, str]] = {}
        routes: dict[str, tuple[Route, str]] = {}
        replaced: list[ReplacedRecord] = []
        for manifest in loaded:
            level_artifacts, level_routes = self._expand_manifest(manifest, stack=())
            self._apply_level(artifacts, routes, level_artifacts, level_routes, manifest.path, replaced)
        plans = self._resolve_nests(loaded[-1], artifacts, routes, tuple(replaced), depth=0, stack=())
        return ResolutionPlan(
            root=str(Path(loaded[0].path).parent),
            scopes=tuple(m.path for m in loaded),
            artifacts=tuple(value[0] for _, value in sorted(artifacts.items())),
            routes=tuple(value[0] for _, value in sorted(routes.items())),
            replaced=tuple(replaced), nested=plans,
        )

    def _count_node(self) -> None:
        self._nodes += 1
        if self._nodes > self.max_graph_nodes:
            raise ResolutionError(f"package graph exceeds maxGraphNodes={self.max_graph_nodes}")

    def _package_path(self, manifest: Manifest, name: str) -> Path:
        entry = manifest.registry.get(name)
        if entry is None:
            raise ResolutionError(f"{manifest.path}: package {name!r} is not registered")
        candidate = (Path(manifest.path).parent / entry).resolve()
        if candidate.is_dir():
            candidate /= "md-package.json"
        return candidate

    def _expand_manifest(self, manifest: Manifest, *, stack: tuple[Path, ...]) -> tuple[list[Artifact], list[Route]]:
        path = Path(manifest.path).resolve()
        if path in stack:
            chain = " -> ".join(str(item) for item in (*stack, path))
            raise ResolutionError(f"package import cycle: {chain}")
        self._count_node()
        artifacts: list[Artifact] = []
        routes: list[Route] = []
        for imported in manifest.imports:
            # Registry membership alone is deliberately not a graph edge.
            imported_manifest = load_manifest(self._package_path(manifest, imported.package))
            imported_artifacts, imported_routes = self._expand_manifest(imported_manifest, stack=(*stack, path))
            if imported.mode == "flatten":
                artifacts.extend(imported_artifacts)
                routes.extend(imported_routes)
            # `nest` imports are represented by explicit nest declarations; they
            # don't become ambient artifacts in their importing scope.
        scope = Path(manifest.path).parent
        for artifact in manifest.artifacts:
            source = normalize_target(artifact.source)
            source_path = self._source_path(scope, source)
            if not source_path.exists():
                raise ManifestError(f"{manifest.path}: artifact source does not exist: {source}")
            digest = tree_hash(source_path)
            artifacts.append(replace(artifact, source=source, hash=digest, source_root=str(scope)))
        routes.extend(manifest.routes)
        self._assert_level_unambiguous(artifacts, routes, manifest.path)
        return artifacts, routes

    @staticmethod
    def _source_path(scope: Path, source: str) -> Path:
        """Build a source path while rejecting symlinks in every component."""
        current = scope
        for part in source.split("/"):
            current /= part
            if current.is_symlink():
                raise SourceSymlinkError(f"symlinked source is not distributable: {current}")
        return current

    @staticmethod
    def _assert_level_unambiguous(artifacts: list[Artifact], routes: list[Route], label: str) -> None:
        by_identity: dict[str, Artifact] = {}
        for item in artifacts:
            old = by_identity.get(item.identity)
            same_content = old and old.kind == item.kind and old.key == item.key and old.target == item.target and old.hash == item.hash
            if old and not same_content:
                raise ConflictError(f"same-level artifact ambiguity in {label}: {item.identity}")
            by_identity[item.identity] = item
        by_route: dict[str, Route] = {}
        for item in routes:
            old = by_route.get(item.key)
            if old and old != item:
                raise ConflictError(f"same-level route ambiguity in {label}: {item.key}")
            by_route[item.key] = item
        assert_no_target_conflicts([(item.target, item.identity) for item in by_identity.values()])

    def _apply_level(self, artifacts: dict[str, tuple[Artifact, str]], routes: dict[str, tuple[Route, str]],
                     level_artifacts: list[Artifact], level_routes: list[Route], scope: str,
                     replaced: list[ReplacedRecord]) -> None:
        for item in level_artifacts:
            old = artifacts.get(item.identity)
            if old and old[0] != item:
                replaced.append(ReplacedRecord(item.identity, old[1], {"kind": old[0].kind, "key": old[0].key,
                    "target": old[0].target, "source": old[0].source, "hash": old[0].hash}))
            artifacts[item.identity] = (item, scope)
        for item in level_routes:
            old = routes.get(item.key)
            if old and old[0] != item:
                replaced.append(ReplacedRecord(f"route/{item.key}", old[1], {"key": old[0].key,
                    "when": old[0].when, "read": list(old[0].read), "disabled": old[0].disabled}))
            if item.disabled:
                routes.pop(item.key, None)
            else:
                routes[item.key] = (item, scope)
        assert_no_target_conflicts([(value[0].target, identity) for identity, value in artifacts.items()])

    def _resolve_nests(self, manifest: Manifest, artifacts: dict[str, tuple[Artifact, str]],
                       routes: dict[str, tuple[Route, str]], replaced: tuple[ReplacedRecord, ...], *, depth: int,
                       stack: tuple[Path, ...]) -> tuple[ResolutionPlan, ...]:
        if depth >= self.max_nest_depth and manifest.nests:
            raise ResolutionError(f"nested scopes exceed maxNestedDepth={self.max_nest_depth}")
        result: list[ResolutionPlan] = []
        for nest in manifest.nests:
            child_root = Path(manifest.path).parent / nest.path
            child_path = child_root / "md-package.json"
            child = load_manifest(child_path)
            if Path(child.path).resolve() in stack:
                raise ResolutionError(f"nested scope cycle at {child.path}")
            inherited_artifacts = dict(artifacts)
            inherited_routes = dict(routes)
            child_replaced = list(replaced)
            if nest.package:
                package = load_manifest(self._package_path(manifest, nest.package))
                package_artifacts, package_routes = self._expand_manifest(package, stack=stack)
                self._apply_level(inherited_artifacts, inherited_routes, package_artifacts, package_routes,
                                  package.path, child_replaced)
            child_artifacts, child_routes = self._expand_manifest(child, stack=stack)
            self._apply_level(inherited_artifacts, inherited_routes, child_artifacts, child_routes, child.path, child_replaced)
            nested = self._resolve_nests(child, inherited_artifacts, inherited_routes, tuple(child_replaced), depth=depth + 1,
                                        stack=(*stack, Path(child.path).resolve()))
            result.append(ResolutionPlan(str(child_root), (child.path,),
                tuple(value[0] for _, value in sorted(inherited_artifacts.items())),
                tuple(value[0] for _, value in sorted(inherited_routes.items())), tuple(child_replaced), nested))
        return tuple(result)


def resolve(start: str | Path, **limits: int) -> ResolutionPlan:
    """Convenience API for callers that only need an immutable resolution plan."""
    return Resolver(**limits).resolve(start)
