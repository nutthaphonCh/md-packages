"""Explicit, JSON-serializable data models for authored and resolved state."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Artifact:
    kind: str
    key: str
    target: str
    source: str
    hash: str | None = None
    source_root: str | None = None

    @property
    def identity(self) -> str:
        return f"{self.kind}/{self.key}"


@dataclass(frozen=True)
class Route:
    key: str
    when: str | None = None
    read: tuple[str, ...] = ()
    disabled: bool = False


@dataclass(frozen=True)
class Import:
    package: str
    mode: str = "flatten"


@dataclass(frozen=True)
class Nest:
    path: str
    package: str | None = None


@dataclass(frozen=True)
class Manifest:
    path: str
    name: str | None = None
    registry: dict[str, str] = field(default_factory=dict)
    imports: tuple[Import, ...] = ()
    nests: tuple[Nest, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    routes: tuple[Route, ...] = ()
    max_parent_depth: int | None = None


@dataclass(frozen=True)
class ReplacedRecord:
    identity: str
    previous_scope: str
    previous: dict[str, Any]


@dataclass(frozen=True)
class ResolutionPlan:
    root: str
    scopes: tuple[str, ...]
    artifacts: tuple[Artifact, ...]
    routes: tuple[Route, ...]
    replaced: tuple[ReplacedRecord, ...] = ()
    nested: tuple["ResolutionPlan", ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Stable primitive representation suitable for a lock or JSON output."""
        return asdict(self)
