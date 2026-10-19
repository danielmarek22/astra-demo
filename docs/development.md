# Development and maintenance

Use the demo's own `.venv`, configuration and state directory. Commands must run
from this repository, or use absolute paths to its entry point. The launcher resolves
source-owned paths relative to this checkout rather than the shell's directory.

The retained Python unit suite protects conversation context, tool authority,
belief evidence/attribution, summaries, canonical storage and vector reconciliation.
Demo integration tests use real SQLite and the real service composition with a fake
model and vector index. They exercise samples, reset, busy operations, stale clients,
manual retry and filesystem boundaries without contacting personal services.

JavaScript tests check stream framing, UTF-8, interrupted connections, the full local
browser import graph, migrated playback/recording behavior, and
new media cancellation guards. Browser and real-model checks are recorded separately
in the validation document.

## Upstream updates

The baseline is recorded in `UPSTREAM.md` in the repository root. Review upstream
fixes even when no new feature is wanted. Import coherent changes including tests,
prerequisites and schema migrations, on a feature branch. Use `cherry-pick -x` when
suitable; record manually adapted source revisions too.

Keep “reviewed through revision X” separate from “contains all changes through X”.
Avoid automatic whole-tree merges that could restore removed product dependencies.
Rebuild fictional examples against the current schema after every relevant port.

Keep increments reviewable. Suggested migration commit:
`feat: migrate Astra speech, screen context and shell into the demo`.

The public repository starts from one fresh demo snapshot. The original Astra commit
is recorded in `UPSTREAM.md`, and source/vendor licenses remain included. The local
`codex/publish-demo` branch tracks `origin/main`; use an explicit branch push. Do not
push the local `codex/bootstrap-demo` archive or use `--all`/`--mirror`, because those
would expose the inherited Astra history. Future ports should copy selected changes
without merging Astra history into the public demo.
