"""Planning and recovery-safe application of generated Markdown outputs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from .routing import render_router_extension
from .transaction import Journal, ScopeLocks, canonical_scope_paths, safe_path, replace_durable, sync_payload, sync_directory


class MaterializationError(RuntimeError):
    pass


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_path(path: Path) -> str:
    if path.is_file():
        return sha256_bytes(path.read_bytes())
    if path.is_dir():
        digest = hashlib.sha256()
        for child in sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.relative_to(path).as_posix()):
            relative = child.relative_to(path).as_posix()
            digest.update(relative.encode("utf-8") + b"\0")
            digest.update(sha256_bytes(child.read_bytes()).encode("ascii") + b"\0")
            digest.update(str(child.stat().st_mode & 0o111).encode("ascii") + b"\0")
        return digest.hexdigest()
    raise FileNotFoundError(path)


def _safe_relative(path: str) -> str:
    pure = PurePosixPath(path)
    if not path or "\\" in path or pure.is_absolute() or ".." in pure.parts or str(pure) == ".":
        raise MaterializationError(f"unsafe generated target: {path!r}")
    if pure.parts[0] in {"md-package.json", ".md-install.lock", ".md"}:
        raise MaterializationError(f"reserved generated target: {path!r}")
    return pure.as_posix()


def _lock_outputs(lock: Mapping[str, Any] | None) -> dict[str, str]:
    if not lock:
        return {}
    raw = lock.get("outputs", lock.get("generated", {}))
    if isinstance(raw, Mapping):
        result = {}
        for path, item in raw.items():
            result[_safe_relative(str(path))] = str(item.get("sha256", item.get("hash"))) if isinstance(item, Mapping) else str(item)
        return result
    result = {}
    for item in raw:
        result[_safe_relative(str(item["path"]))] = str(item.get("sha256", item.get("hash")))
    return result


def read_lock(scope: Path) -> dict[str, Any] | None:
    path = safe_path(scope, ".md-lock.json")
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MaterializationError(f"invalid lock file: {path}") from exc


def is_materialized(scope: Path, source: Path) -> bool:
    """Recognize generated roots, descendants, and directories containing them."""
    source = source.resolve()
    lock = read_lock(scope) or {}
    names = {"ROUTER-EXTENSION.md", "router-extension.md", ".md-lock.json", ".md", ".agents/skills", ".claude/skills"}
    names.update(lock.get("outputs", {}))
    names.update(item["target"] for item in lock.get("resolution", {}).get("artifacts", []))
    return any(source == target or source.is_relative_to(target) or target.is_relative_to(source)
               for target in (scope / name for name in names))


def router_block(router_relative: str = "ROUTER.md") -> str:
    relative = _safe_relative(router_relative)
    return (
        "<!-- md:router:start -->\n"
        f"Read [{relative}]({relative}).\n\n"
        "<!-- md:router:end -->\n"
    )


def patch_agent_entrypoint(content: bytes | str, router_relative: str = "ROUTER.md") -> bytes:
    """Replace only the owned marked block, retaining all user-authored bytes."""
    text = content.decode("utf-8") if isinstance(content, bytes) else content
    start, end = "<!-- md:router:start -->", "<!-- md:router:end -->"
    start_at, end_at = text.find(start), text.find(end)
    if (start_at == -1) != (end_at == -1) or (end_at != -1 and end_at < start_at):
        raise MaterializationError("malformed agent entrypoint router markers")
    block = router_block(router_relative)
    if start_at != -1:
        end_at += len(end)
        # Include one existing final newline in the replacement interval, so a
        # rerun is byte-identical rather than accumulating blank lines.
        if end_at < len(text) and text[end_at] == "\n":
            end_at += 1
        return (text[:start_at] + block + text[end_at:]).encode("utf-8")
    separator = "" if not text or text.endswith("\n") else "\n"
    return (text + separator + ("\n" if text else "") + block).encode("utf-8")


@dataclass(frozen=True)
class PlannedOutput:
    path: str
    content: bytes
    mode: int = 0o644

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.content)


@dataclass(frozen=True)
class MaterializationPlan:
    scope: Path
    outputs: tuple[PlannedOutput, ...]
    removals: tuple[str, ...]
    lock: dict[str, Any]
    previous_lock: dict[str, Any] | None

    @property
    def lock_bytes(self) -> bytes:
        return (json.dumps(self.lock, sort_keys=True, indent=2) + "\n").encode("utf-8")


def plan_materialization(
    scope: Path | str,
    outputs: Mapping[str, bytes | str | PlannedOutput] | None = None,
    *,
    previous_lock: Mapping[str, Any] | None = None,
    effective_routes: Mapping[str, Any] | list[Mapping[str, Any]] | None = None,
    artifacts: Mapping[str, Any] | None = None,
    entrypoints: Mapping[str, str] | Iterable[str] | None = None,
    lock_metadata: Mapping[str, Any] | None = None,
) -> MaterializationPlan:
    """Read and validate everything, then return bytes to write without mutation."""
    root = Path(scope).resolve()
    safe_path(root, ".md/transactions")
    generated: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    for path, content in (outputs or {}).items():
        if isinstance(content, PlannedOutput):
            modes[str(path)] = content.mode
            content = content.content
        generated[_safe_relative(str(path))] = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    if effective_routes is not None:
        generated["ROUTER-EXTENSION.md"] = render_router_extension(effective_routes, artifacts)
    if entrypoints:
        items = entrypoints.items() if isinstance(entrypoints, Mapping) else ((path, "ROUTER.md") for path in entrypoints)
        for path, router_relative in items:
            target = _safe_relative(str(path))
            existing_path = safe_path(root, target)
            existing = existing_path.read_bytes() if existing_path.exists() else b""
            generated[target] = patch_agent_entrypoint(existing, str(router_relative))
    if ".md-lock.json" in generated:
        raise MaterializationError(".md-lock.json is generated by the transaction and written last")

    previous = dict(previous_lock) if previous_lock is not None else read_lock(root)
    managed = _lock_outputs(previous)
    planned = tuple(PlannedOutput(path, generated[path], modes.get(path, 0o644)) for path in sorted(generated))
    managed_aliases: dict[str, str] = {}
    for output in planned:
        if output.path in managed:
            continue
        target = safe_path(root, output.path)
        if not target.exists():
            continue
        aliases = []
        for candidate in managed:
            candidate_path = safe_path(root, candidate)
            if (candidate.casefold() == output.path.casefold() and candidate_path.exists()
                    and os.path.samefile(target, candidate_path)):
                aliases.append(candidate)
        if len(aliases) > 1:
            raise MaterializationError(f"ambiguous managed path casing for {output.path}")
        if aliases:
            managed_aliases[output.path] = aliases[0]

    def managed_key(path: str) -> str:
        return managed_aliases.get(path, path)

    def verify_mode(path: str, target: Path) -> None:
        record = (previous or {}).get("outputs", {}).get(managed_key(path))
        if isinstance(record, Mapping) and "mode" in record and target.stat().st_mode & 0o777 != record["mode"]:
            raise MaterializationError(f"managed path mode was locally modified: {path}")
    for output in planned:
        target = safe_path(root, output.path)
        owner = managed_key(output.path)
        if target.exists() and owner not in managed:
            raise MaterializationError(f"refusing to overwrite unmanaged path: {output.path}")
        if target.exists() and owner in managed and sha256_path(target) != managed[owner]:
            raise MaterializationError(f"managed path was locally modified: {output.path}")
        if target.exists():
            verify_mode(output.path, target)
    removals: list[str] = []
    for path, expected in sorted(managed.items()):
        if path in generated or path in managed_aliases.values():
            continue
        target = safe_path(root, path)
        if target.exists():
            verify_mode(path, target)
            if sha256_path(target) != expected:
                raise MaterializationError(f"refusing to remove locally modified managed path: {path}")
            removals.append(path)
    # A resolver can hand us its complete portable lock payload (provenance,
    # pins, artifacts, routes and renderer/schema versions).  Make a JSON
    # round-trip here, both to reject non-JSON state and to ensure later caller
    # mutation cannot alter an already planned transaction.
    if lock_metadata is None:
        metadata: dict[str, Any] = {}
    elif not isinstance(lock_metadata, Mapping):
        raise MaterializationError("lock_metadata must be a JSON object")
    else:
        try:
            metadata = json.loads(json.dumps(dict(lock_metadata), sort_keys=True))
        except (TypeError, ValueError) as exc:
            raise MaterializationError("lock_metadata must be JSON-compatible") from exc
    calculated_outputs = {output.path: {"sha256": output.sha256, "mode": output.mode} for output in planned}
    if "outputs" in metadata:
        try:
            supplied_outputs = _lock_outputs({"outputs": metadata["outputs"]})
        except (KeyError, TypeError, AttributeError, MaterializationError) as exc:
            raise MaterializationError("lock_metadata.outputs is not a valid managed output table") from exc
        expected_outputs = {path: value["sha256"] for path, value in calculated_outputs.items()}
        if supplied_outputs != expected_outputs:
            raise MaterializationError("lock_metadata.outputs conflicts with calculated managed output hashes")
    metadata["outputs"] = calculated_outputs
    metadata.setdefault("version", 1)
    lock = metadata
    return MaterializationPlan(root, planned, tuple(removals), lock, previous)


def _move_to_backup(root: Path, path: str, backup_root: Path) -> tuple[bool, Path]:
    target, backup = safe_path(root, path), safe_path(backup_root, path)
    if not target.exists():
        return False, backup
    backup.parent.mkdir(parents=True, exist_ok=True)
    os.replace(target, backup)
    return True, backup


def _restore_operations(journal: Journal) -> None:
    root = journal.scope
    for operation in reversed(journal.data.get("operations", [])):
        target = safe_path(Path(operation.get("scope", root)), operation["path"])
        backup = safe_path(journal.directory, operation["backup"])
        # No backup means an intent was recorded but the move did not happen.
        if operation.get("had_original") and not backup.exists():
            continue
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        if operation.get("had_original"):
            if backup.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                # Retain backups until the terminal journal is durable. A crash
                # during recovery can then safely repeat this restoration.
                if backup.is_dir():
                    shutil.copytree(backup, target)
                else:
                    shutil.copy2(backup, target)
                sync_payload(target)
        if target.parent.exists():
            sync_directory(target.parent)


def journal_replace(journal: Journal, scope: Path, path: str, staged: Path | None,
                    failure_injector: Callable[[str], None] | None = None) -> None:
    target = safe_path(scope, path)
    backup = safe_path(journal.directory, f"backup/{len(journal.data['operations'])}")
    operation = {"scope": str(scope), "path": path, "backup": str(backup), "had_original": target.exists()}
    journal.data["operations"].append(operation)
    journal.save()
    if failure_injector:
        failure_injector(f"intent:{path}")
    if operation["had_original"]:
        backup.parent.mkdir(parents=True, exist_ok=True)
        replace_durable(target, backup)
    if failure_injector:
        failure_injector(f"backup:{path}")
    if staged is not None:
        safe_path(journal.directory, staged)
        target.parent.mkdir(parents=True, exist_ok=True)
        sync_payload(staged)
        replace_durable(staged, target)
    if failure_injector:
        failure_injector(path)


def recover_scope(scope: Path | str) -> list[Path]:
    """Roll back unfinished transactions; completed journals are evidence only."""
    root = Path(scope).resolve()
    directory = safe_path(root, ".md/transactions")
    recovered: list[Path] = []
    if not directory.exists():
        return recovered
    for legacy in directory.glob("promote-*.json"):
        state = json.loads(safe_path(root, legacy).read_text()).get("state")
        if state not in {"committed", "rolled-back", "rolled_back", "recovered"}:
            raise MaterializationError(f"legacy unfinished promotion needs manual recovery: {legacy}")
    for path in sorted(directory.glob("*/journal.json")):
        journal = Journal.load(path)
        if journal.data.get("state") in {"committed", "rolled_back", "recovered"}:
            continue
        _restore_operations(journal)
        journal.update(state="recovered")
        recovered.append(path)
    return recovered


def apply_materialization(
    plans: MaterializationPlan | Iterable[MaterializationPlan],
    *,
    failure_injector: Callable[[str], None] | None = None,
) -> None:
    """Apply one or more preplanned scopes.  Atomicity is per replacement path."""
    selected = [plans] if isinstance(plans, MaterializationPlan) else list(plans)
    if not selected:
        return
    roots = canonical_scope_paths(plan.scope for plan in selected)
    by_root = {plan.scope.resolve(): plan for plan in selected}
    if len(by_root) != len(selected):
        raise MaterializationError("only one materialization plan per scope is allowed")
    physical: list[Path] = []
    for plan in selected:
        for relative in [*(item.path for item in plan.outputs), *plan.removals, ".md-lock.json"]:
            target = safe_path(plan.scope, relative)
            for other in physical:
                if target == other or target.is_relative_to(other) or other.is_relative_to(target):
                    raise MaterializationError(f"overlapping physical outputs: {target} and {other}")
            physical.append(target)
    with ScopeLocks(roots):
        for root in roots:
            recover_scope(root)
        # Validate every scope under the complete lock set before creating any
        # journal or replacing an output. A conflict in a later scope must not
        # leave an earlier scope updated.
        for root in roots:
            plan = by_root[root]
            previous_lock = read_lock(root)
            if previous_lock != plan.previous_lock:
                raise MaterializationError(f"plan is stale for scope: {root}")
            current = plan_materialization(
                root,
                {item.path: item for item in plan.outputs},
                previous_lock=previous_lock,
                # Reuse the resolver-provided full payload while recalculating
                # the output table from bytes under the acquired scope locks.
                lock_metadata={key: value for key, value in plan.lock.items() if key != "outputs"},
            )
            if current.removals != plan.removals or current.lock != plan.lock:
                raise MaterializationError(f"plan is stale for scope: {root}")
        for root in roots:
            plan = by_root[root]
            journal = Journal(root)
            journal.data["operations"] = []
            journal.save()
            stage_root = journal.directory / "stage"
            try:
                for output in plan.outputs:
                    staged = stage_root / output.path
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    staged.write_bytes(output.content)
                    staged.chmod(output.mode)
                journal.update(state="staged")
                for output in plan.outputs:
                    journal_replace(journal, root, output.path, stage_root / output.path, failure_injector)
                for path in plan.removals:
                    journal_replace(journal, root, path, None, failure_injector)
                journal.update(state="outputs_applied")
                # The lock is journaled too, and always replaced last.
                temporary_lock = root / ".md" / "transactions" / journal.id / "lock.json"
                temporary_lock.write_bytes(plan.lock_bytes)
                journal_replace(journal, root, ".md-lock.json", temporary_lock, failure_injector)
                journal.update(state="committed")
            except Exception:
                _restore_operations(journal)
                journal.update(state="rolled_back")
                raise
