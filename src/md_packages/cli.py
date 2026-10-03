"""The public mdpkg command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from . import __version__
from .errors import MdPackageError
from .manifest import load_manifest
from .materialize import MaterializationError, read_lock, is_materialized
from .hashing import tree_hash
from .promotion import PromotionError, apply_promotion, content_hash, load_plan, plan_promotion, save_plan
from .routing import RouteValidationError
from .service import ServiceError, doctor, install, lookup, recover, resolved_view, scope_path
from .transaction import ScopeLockError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mdpkg", description="Recursive Markdown package manager")
    parser.add_argument("--version", action="version", version=f"mdpkg {__version__}")
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    install_cmd = commands.add_parser("install", help="resolve and materialize a scope")
    install_cmd.add_argument("path", nargs="?", default=".")
    install_cmd.add_argument("--dry-run", action="store_true")
    install_cmd.add_argument("--locked", action="store_true")
    install_cmd.add_argument("--all", action="store_true", help="include nested scopes")
    install_cmd.add_argument("--max-parent-depth", type=int, default=5, metavar="N")
    install_cmd.add_argument("--patch-entrypoint", action="append", choices=["AGENTS.md", "CLAUDE.md"],
                             default=[], help="explicitly patch only the marked router block")
    for command in ("doctor", "list", "recover"):
        sub = commands.add_parser(command)
        sub.add_argument("path", nargs="?", default=".")
    for command in ("which", "explain"):
        sub = commands.add_parser(command)
        sub.add_argument("identity", help="artifact kind/key or route key")
        sub.add_argument("path", nargs="?", default=".")

    promote = commands.add_parser("promote", help="promote an authored artifact into a repository package")
    promote.add_argument("source", help="artifact identity or local source path")
    promote.add_argument("--to-repo", required=True, metavar="PATH")
    promote.add_argument("--as", dest="as_identity", metavar="KIND/NAME")
    promote.add_argument("--route", metavar="KEY", help="also promote this structured route declaration")
    promote.add_argument("--register", metavar="MANIFEST", help="add an entry to the given registry manifest")
    promote.add_argument("--move", action="store_true")
    promote.add_argument("--from-materialized", action="store_true")
    promote.add_argument("--dry-run", action="store_true")
    promote.add_argument("--plan", metavar="FILE", help="save a preconditioned plan for later apply")
    promote.add_argument("--scope", default=".", metavar="PATH", help="scope used to resolve identities")
    apply = commands.add_parser("apply", help="apply a saved promotion plan")
    apply.add_argument("plan", metavar="FILE")
    return parser


def _parse_identity(value: str) -> tuple[str, str]:
    kind, separator, name = value.partition("/")
    if not separator or not kind or not name or "/" in name:
        raise ServiceError("--as must be KIND/NAME")
    return kind, name


def _route_data(scope: Path, key: str) -> dict[str, Any]:
    manifest = load_manifest(scope / "md-package.json")
    matches = [route for route in manifest.routes if route.key == key]
    if not matches:
        raise ServiceError(f"route {key!r} is not authored in {scope / 'md-package.json'}")
    item = matches[0]
    return {"when": item.when, "read": list(item.read), "disabled": item.disabled}


def _materialized_provenance(scope: Path, source: Path) -> dict[str, Any] | None:
    lock = read_lock(scope)
    if not is_materialized(scope, source):
        return None
    for item in (lock or {}).get("resolution", {}).get("artifacts", []):
        materialized = (scope / item["target"]).resolve()
        if source == materialized:
            authored = (scope / item["sourcePath"]).resolve()
            if not authored.exists() or tree_hash(authored) != item["hash"]:
                raise ServiceError(f"pinned authored source is unavailable or changed: {authored}")
            return {"source": str(authored), "baseHash": content_hash(authored),
                    "origin": f"{item['kind']}/{item['key']}"}
    return {"captureOnly": True, "origin": source.relative_to(scope).as_posix(),
            "baseHash": None}


def _promote(args: argparse.Namespace) -> dict[str, Any]:
    scope = scope_path(args.scope)
    candidate = Path(args.source).expanduser()
    match: dict[str, Any] | None = None
    if not candidate.exists():
        match = lookup(scope, args.source)["record"]
    if match and "source" in match:
        source = Path(match["source_root"]) / match["source"]
        kind, name = match["kind"], match["key"]
    elif match:
        raise ServiceError("a route needs an artifact source; pass an artifact and --route KEY")
    else:
        source = candidate.resolve()
        if not args.as_identity:
            raise ServiceError("a source path needs --as KIND/NAME")
        kind, name = _parse_identity(args.as_identity)
    if args.as_identity:
        kind, name = _parse_identity(args.as_identity)
    materialized = _materialized_provenance(scope, source)
    if args.from_materialized and materialized is None:
        raise ServiceError("--from-materialized requires a managed materialized artifact path")
    if materialized and not args.from_materialized:
        # The promotion engine checks whether the materialized bytes still
        # match their pinned authored source and adopts that source if so.
        pass
    if args.dry_run and args.plan:
        raise ServiceError("--dry-run and --plan cannot be combined because --plan writes a file")
    plan = plan_promotion(
        source, destination_repo=args.to_repo, kind=kind, name=name, move=args.move,
        from_materialized=args.from_materialized, materialized=materialized,
        route_key=args.route, route=_route_data(scope, args.route) if args.route else None,
        registry_manifest=args.register,
        affected_scopes=tuple(dict.fromkeys((scope, Path(args.to_repo).expanduser().resolve(),
                              *((Path(args.register).expanduser().resolve().parent,) if args.register else ())))),
    )
    if args.plan:
        save_plan(plan, args.plan)
        return {"savedPlan": str(Path(args.plan).resolve()), "plan": plan}
    if args.dry_run:
        return {"dryRun": True, "plan": plan}
    return {"plan": plan, "result": apply_promotion(plan)}


def _run(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "install":
        if args.max_parent_depth < 0:
            raise ServiceError("--max-parent-depth must be non-negative")
        return install(args.path, dry_run=args.dry_run, locked=args.locked, all_scopes=args.all,
                       max_parent_depth=args.max_parent_depth, entrypoints=tuple(args.patch_entrypoint))
    if args.command == "doctor":
        return doctor(args.path)
    if args.command == "list":
        return resolved_view(args.path)
    if args.command in {"which", "explain"}:
        result = lookup(args.path, args.identity)
        if args.command == "which":
            return {"identity": args.identity, "record": result["record"]}
        return result
    if args.command == "recover":
        return recover(args.path)
    if args.command == "promote":
        return _promote(args)
    if args.command == "apply":
        return apply_promotion(load_plan(args.plan))
    raise ServiceError(f"unsupported command: {args.command}")


def _print_text(command: str, result: dict[str, Any]) -> None:
    if command == "install":
        mode = "Would install" if result["dryRun"] else "Installed"
        for scope in result["scopes"]:
            print(f"{mode} {scope['path']}: {len(scope['outputs'])} outputs, {len(scope['removals'])} removals")
    elif command == "doctor":
        print("OK" if result["ok"] else "Problems found")
        for finding in result["findings"]:
            print(f"- {finding}")
    elif command == "recover":
        print(f"Recovered {len(result['recovered'])} transaction(s) in {result['scope']}")
    else:
        print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = _run(args)
        if args.json:
            print(json.dumps(result, sort_keys=True, indent=2, ensure_ascii=False))
        else:
            _print_text(args.command, result)
        return 1 if args.command == "doctor" and not result["ok"] else 0
    except KeyboardInterrupt:
        print("mdpkg: interrupted", file=sys.stderr)
        return 130
    except (MdPackageError, MaterializationError, PromotionError, RouteValidationError,
            ScopeLockError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"mdpkg: {exc}", file=sys.stderr)
        return 2
