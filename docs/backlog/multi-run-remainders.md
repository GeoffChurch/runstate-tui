# Multi-run table — remaining deferred work

**Status:** the multi-run table **SHIPPED** (PR #11 `edc40b5`) and the **glob resolver SHIPPED**
(PR #16 `e72bbac`). The table's design-of-record now lives in the code (`multirun.py`, `pool.py`,
`resolver.py`) plus its specs — `../superpowers/specs/2026-07-18-stage4-multi-run-table-design.md`
(owner-thread pool, keyed reconcile, per-frame frozen clock, `⚠ I/O stalled` watchdog) and
`../superpowers/specs/2026-07-21-glob-resolver-design.md` (live directory discovery, minimal-backtrack
labels). This doc carries only what is **still deferred** at the table.

Resolved since the old "Stage 4" doc: the **per-frame `now`** concern is closed — `fold_frame` folds
every run under a per-frame frozen clock (`replace(env, clock=lambda: now)`); the legacy
`render_table` per-row resampling is off the live multi-run path.

## 1. The `cells` resolver — the last deferred resolver

`explicit` / `const` / `glob` all shipped; `cells` (a **workload-specific sweep**) is the one
resolver left. It is just another `Resolver` (`Time → list[RunRef]`) dropped into the CLI dispatch +
`MultiRunApp` — it reuses the shipped discovery + `disambiguate` machinery in `resolver.py`, so the
seam is trivial. **Open question before building:** what a "cell" concretely is — a runstate
namespace/grouping? a parameter-sweep grid config? It was never pinned down (a bare word in core
spec §6). Pin the meaning against a real workload first, or drop it (YAGNI) if it never crystallizes.

## 2. Issue-flood aggregation (spec §3.3 / §7, ISA-18.2)

The `|I|=1` core can't flood; the table can. A shared-FS hiccup can paint **hundreds of identical
badges** across rows. Collapse N identical badges into **one super-issue** at the table level, while
the drill-down still enumerates all N. **Open design questions:** the equivalence key (same
`IssueKind`? kind + message? a kind seen across ≥K distinct runs), the flood threshold K, and the
render locus (a table-wide banner — "17 runs unreadable — shared-FS?" — vs. per-row treatment). Not
built; the `_marker` axis in `multirun.py` is where it would hook.

## Related deferred findings that surface here (not table features)

- **Corruption-invisibility full-scan** — the deferred opt-in, marked-expensive integrity scan
  (spec §12 / H1) is a natural per-run **drill-down** action, not a hot-path fold concern.
- **`conflicted`** — a liveness-overlay feature (see `liveness-overlay.md`), independent of the
  table: it needs `resolve()`/`launcher.terminated` probe corroboration, not a fold change.
- **`--follow-symlinks` / arbitrary `--glob PATTERN`** — additive glob-resolver options deferred in
  the glob spec (runs inside symlinked dirs; power-user patterns). Off by default keeps cycle safety.
