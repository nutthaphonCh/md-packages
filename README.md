# md-packages

`mdpkg` is a recursive Markdown package manager for agent skills, wiki pages,
ADRs, runbooks, and arbitrary documentation. It resolves package scopes from
parent to child, materializes an effective document set at each scope, and
generates deterministic routers with provenance and content hashes.

The canonical executable is `mdpkg`. A convenience `md` alias is also installed,
but `mdpkg` is recommended in scripts because `md` conflicts with directory
creation commands in some Windows shells.

## Status

This repository currently contains an MVP. The file formats are versioned, but
compatibility is not promised until the first stable release.

Implemented:

- recursive parent scopes with bounded discovery;
- `flatten` imports and nested installation boundaries;
- skills, wiki, ADRs, docs, and arbitrary artifact kinds;
- whole-record child overrides and tombstones;
- key, case-folded path, prefix-path, ownership, and hash conflict checks;
- deterministic `.md-lock.json`, `ROUTER.md`, and `router-extension.md`;
- copied Codex/Claude skill discovery for portable MVP installations;
- journaled materialization, recovery, locked replay, and dry-run;
- authored and explicit materialized-fork promotion to repository packages.

Current safety-oriented limitations:

- package payload symlinks are rejected;
- an existing unmanaged `AGENTS.md` or `CLAUDE.md` is not adopted automatically;
- promotion reports the selected source, destination, and optional registry scopes; it does not discover other
  dependents outside those scopes. Reinstall affected consumers explicitly;
- cross-repository operations are journaled and recoverable, not globally atomic;
- release publishing and self-update are intentionally deferred.

## Install for development

Python 3.11 or newer is required.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
mdpkg --help
```

For an isolated user installation after a release artifact exists:

```sh
pipx install md-packages
# or
uv tool install md-packages
```

## Package layout

Authored package content and generated installation content are deliberately
separate:

```text
workspace/
├─ md-package.json             # authored declarations
├─ packages/                   # authored sources
│  ├─ skills/
│  ├─ wiki/
│  └─ adr/
├─ .md-lock.json               # generated resolution
├─ skills/                     # generated effective artifacts
├─ wiki/                       # generated effective artifacts
├─ adr/                        # generated effective artifacts
├─ ROUTER.md                   # generated effective routing
└─ router-extension.md         # generated current-scope routing
```

Example `md-package.json`:

```json
{
  "version": 1,
  "name": "example-workspace",
  "artifacts": [
    {
      "kind": "skills",
      "key": "frontend",
      "source": "packages/skills/frontend",
      "target": "skills/frontend"
    },
    {
      "kind": "wiki",
      "key": "architecture",
      "source": "packages/wiki/architecture.md",
      "target": "wiki/architecture.md"
    }
  ],
  "routing": {
    "frontend-work": {
      "when": "Working on frontend behavior",
      "read": ["skills/frontend", "wiki/architecture"]
    }
  }
}
```

## Commands

```sh
mdpkg install
mdpkg install --dry-run
mdpkg install --all
mdpkg install --locked

mdpkg --json list
mdpkg --json which skills/frontend
mdpkg --json explain frontend-work
mdpkg --json doctor
```

Promote authored local content into a repository-owned package:

```sh
mdpkg promote ./notes/recovery.md \
  --as docs/recovery \
  --to-repo . \
  --dry-run

mdpkg promote ./notes/recovery.md \
  --as docs/recovery \
  --to-repo .
```

Changed generated content requires explicit capture:

```sh
mdpkg promote skills/frontend \
  --as skills/frontend \
  --from-materialized \
  --to-repo .
```

`promote` never stages, commits, pushes, or publishes Git changes.
Generated descendants, routers, and discovery copies also require
`--from-materialized --as KIND/NAME`. Only clean exact artifact roots can resolve
back to their pinned authored source automatically. `mdpkg recover` rolls back
unfinished installation and promotion journals; repeated recovery is safe.
Promotion moves across filesystems are not supported and fail before removing
the source. Recovery journals retain backups locally under `.md/transactions`.

## Development

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Build artifacts when the `build` package is available:

```sh
python3 -m build
```

The CLI's own build and test process does not depend on running `mdpkg install`.

## License

MIT
