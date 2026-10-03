# Design

## Scope tree

Each scope can contain `md-package.json`. `mdpkg install` searches upward no
farther than the configured parent-depth limit, selects the highest discovered
scope, and resolves from parent to child. `flatten` merges a package into the
current scope. `nest` creates a child installation boundary that receives the
parent effective artifacts and routes before applying its own declarations.

Repository locks contain portable explicit inputs only. Ambient personal inputs
are represented by ignored local state and must not leak into a committed lock.

## Authored and generated paths

- Authored: `md-package.json`, `packages/**`, optional local files explicitly
  referenced as read-only routes.
- Generated: `.md-lock.json`, materialized artifact folders, `ROUTER.md`,
  `router-extension.md`, discovery links, transaction journals.
- The lock records ownership and hashes. Existing unmanaged targets or modified
  managed targets block apply.

## Identity and conflicts

Artifact identity is `<kind>/<key>` and route identity is its route key. Targets
are normalized POSIX-relative paths, Unicode-normalized, case-fold checked, and
validated against absolute paths, traversal, reserved outputs, and file/directory
prefix collisions.

- Same identity/target/hash deduplicates.
- Child scopes replace a whole artifact or route record by key and record the
  replaced provenance.
- Same-level ambiguity fails.
- Different identities targeting the same path fail unless an explicit alias
  with identical content is supported by a later schema.
- `disabled: true` is the v1 tombstone; `final` is deferred.

Files use SHA-256 of exact bytes. Directory artifacts use a deterministic tree
hash over sorted relative paths, file hashes, and executable modes. Structured
route metadata has its own hash; equal file bytes do not imply equal routing.

## Installation transaction

Planning has no filesystem mutation. Apply acquires all affected scope locks in
canonical path order, verifies precondition hashes again, stages outputs, writes
a journal, replaces files/directories where safe, writes locks last, and records
enough state for rollback/recovery. Atomicity is scoped to individual filesystem
replacements.

## Routing

The committed agent entrypoint owns only a marked block pointing to `ROUTER.md`
and the bootstrap command. `ROUTER.md` is generated from the effective route set.
`router-extension.md` is generated from the current scope's structured routing
declarations. Neither is an authored source.

## Promotion

`mdpkg promote <artifact> --to-repo <path>` creates repository-authored source
under `packages/<kind>/<name>`, updates exports/routes, optionally updates a
registry, and re-resolves affected installed scopes without upgrading unrelated
pins. Authored inputs copy by default; `--move` is explicit. Materialized inputs
resolve back to their pinned source unless `--from-materialized` explicitly
captures changed bytes as a fork with origin/base-hash provenance.

Promotion supports dry-run and saved plans with precondition hashes. It never
stages, commits, pushes, or publishes Git changes.

## CLI and packaging

Python 3.11+ standard-library runtime, `argparse` CLI, setuptools build. `mdpkg`
is canonical because `md` conflicts with Windows shells. Manifest, lock, router
renderer, and CLI versions evolve independently. `install --locked` never
upgrades dependencies or the CLI.
