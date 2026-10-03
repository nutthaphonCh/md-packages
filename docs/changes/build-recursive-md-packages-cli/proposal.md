# Build recursive Markdown packages CLI

## Why

Agent knowledge is currently copied, linked, or snapshotted differently in each
workspace. That causes version drift, path coupling, ambiguous same-name skills,
and routers that cannot explain provenance. A standalone tool should distribute
all Markdown artifacts through recursive scopes while keeping authored sources,
resolved installations, and portable package definitions distinct.

## What

- Create the standalone `mdpkg` CLI with an optional `md` alias.
- Resolve `md-package.json` scopes upward within a bounded depth and materialize
  recursive `flatten` and `nest` package graphs downward.
- Generate `.md-lock.json`, artifact folders, agent discovery links,
  `ROUTER.md`, and `router-extension.md` with hashes and provenance.
- Reject key, normalized target-path, prefix-path, and content ownership
  conflicts before mutation.
- Add recovery-safe transaction journals and exclusive scope locks.
- Add `mdpkg promote` to adopt or promote local authored artifacts and explicit
  materialized forks into a repository-owned package.
- Add `mdpkg migrate <sources...> <destination>` for recovery-safe batch moves
  or copies between authored package scopes, including explicit route and
  registry transfer.
- Package the tool as a Python wheel/source distribution. Standalone native
  bundles and publishing are deferred.

## Non-goals

- Publishing releases, pushing repositories, or self-updating the CLI.
- Supporting symlinks inside distributable payloads.
- Editing arbitrary local documents unless explicitly adopted or promoted.
- Promising one atomic filesystem transaction across multiple repositories.
