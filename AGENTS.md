# md-packages contributor guide

This repository owns the standalone recursive Markdown package manager.

## Product invariants

- `md-package.json` is authored input. Generated outputs must never be treated as
  authored sources without an explicit promote/capture operation.
- `.md-lock.json` describes one resolution profile. Repository locks must be
  portable; ambient personal overlays use ignored local state.
- Parent-to-child inheritance uses nearest-scope replacement. Ambiguous
  same-level collisions fail.
- Package payloads may not contain symlinks. Installer-created discovery links
  are controlled output and are validated separately.
- Never overwrite an unmanaged path or a managed path whose current hash differs
  from its previous lock record.
- Multi-scope writes use preflight validation and a recovery journal. Do not
  claim cross-filesystem atomicity.
- The CLI must build and test without first running itself.

## Engineering conventions

- Python 3.11+ and the standard library are the default runtime surface.
- Keep filesystem effects behind explicit planning/apply boundaries.
- Tests assert resulting files, locks, routes, conflicts, idempotency, dry-run,
  and failure safety rather than internal call counts.
- Do not commit, push, publish, or cut a release unless the user asks.
