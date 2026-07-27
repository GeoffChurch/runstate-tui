# CLI dispatch — deferred hardening

The CLI is a **shape-dispatch**: it branches on what the positional target *is* — `is_dir` → glob,
`.json` → manifest, ≥2 paths → explicit, one `.db` → single. The flag layer is **argparse**:
`--group-by` (incl. `=`-form), unknown-flag rejection, and a frozen `CliArgs` dataclass for typed
field access + one validation site. The shape-dispatch stays custom because no flag parser does
type-of-positional dispatch.

## Deferred: explicit subcommands

If the shape-guessing ever becomes a problem — an ambiguous target (a directory literally named
`x.json`; `.db`-vs-`.json` intent), or a flag that needs to be scoped per target kind — promote to
argparse **subcommands**: `runstate-tui watch <dir>` / `manifest <file>` / `single <run.db>` / `runs
<db> <db> …`. That makes intent explicit, eliminates the `is_dir`/suffix guessing, and lets argparse
scope each flag to the subcommand where it applies (so the current "`--group-by` is meaningless for a
single run" post-dispatch check falls out for free).

**Cost:** a verb-first UX instead of today's terse `runstate-tui <thing>`. **Not built now** — the
shape-guessing hasn't actually bitten; do it on the first real ambiguity, not speculatively.
