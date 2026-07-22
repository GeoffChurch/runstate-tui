# Multi-run table — remaining deferred work

**Status:** the multi-run table **SHIPPED** (PR #11 `edc40b5`), the **glob resolver SHIPPED**
(PR #16 `e72bbac`), and **issue-flood aggregation SHIPPED** as the fleet summary strip (PR #18
`22fcc68`). The design-of-record lives in the code (`multirun.py`, `pool.py`, `resolver.py`,
`format.py`) plus its specs — `../superpowers/specs/2026-07-18-stage4-multi-run-table-design.md`
(owner-thread pool, keyed reconcile, per-frame frozen clock, `⚠ I/O stalled` watchdog),
`../superpowers/specs/2026-07-21-glob-resolver-design.md` (live directory discovery, minimal-backtrack
labels), and `../superpowers/specs/2026-07-22-flood-summary-strip-design.md` (the always-on legend /
roll-up strip). This doc carries only what is **still deferred** at the table.

Resolved since the old "Stage 4" doc: the **per-frame `now`** concern is closed — `fold_frame` folds
every run under a per-frame frozen clock (`replace(env, clock=lambda: now)`); the legacy
`render_table` per-row resampling is off the live multi-run path. Issue-flood was reframed from
"collapse N badges" into the **additive** summary strip (the table keeps one-row-per-run; the strip
tallies status-partition + non-twin issue tags, worst-first, no threshold).

## The `cells` resolver — the only remaining multi-run feature

`explicit` / `const` / `glob` all shipped; `cells` (a **workload-specific sweep**) is the one resolver
left. Mechanically trivial: it's just another `Resolver` (`Time → list[RunRef]`) dropped into the CLI
dispatch + `MultiRunApp`, reusing the shipped discovery + `disambiguate` machinery in `resolver.py`.

**What it means** (investigated 2026-07-22, cross-repo — origin: `runstate/docs/backlog/third-party-observer.md`
§3): the `explicit`/`glob`/`cells` trichotomy maps to the three real on-disk layouts runstate observes.
`glob` = flat `runs/*.db` (the *translation* workload); **`cells` = the *mycooc* experiment/cell layout**
— an experiment is a set of **cells** (variants), each a thin dir (`outputs/experiments/<exp>/<cell>/`)
holding a **pointer** (`.run_id` → current-rid) into a content-addressed run home
(`runs/<rid[:2]>/<rid>/`). The resolver walks an experiment's cell-pointers to each cell's current run.
That's the "workload-specific sweep": a `runstate.sweep` produces an experiment of cells.

**Why it's gated** (not merely undefined): runstate deliberately provides **no** cell/enumeration API
— "the app, not runstate, should own the layout adapters"; a `list_runs()` capability was *refuted*
upstream (`runstate/docs/backlog/cockpit.md`). So a `cells` resolver hard-codes mycooc's pointer layout
in the tui, and that layout is itself still settling (Recipe 1 holds for only ~25% of mycooc's cells;
the cell/run split is still being deliberated in runstate's backlog). **Build it only on a concrete
need to dashboard a live mycooc sweep, with a real fixture to test against — otherwise defer (YAGNI).**
See the `runstate-tui-cells-resolver-meaning` session memory.

## Related deferred findings that surface here (not table features)

- **Corruption-invisibility full-scan** — the deferred opt-in, marked-expensive integrity scan
  (spec §12 / H1) is a natural per-run **drill-down** action, not a hot-path fold concern.
- **`conflicted`** — a liveness-overlay feature (see `liveness-overlay.md`), independent of the
  table: it needs `resolve()`/`launcher.terminated` probe corroboration, not a fold change.
- **`--follow-symlinks` / arbitrary `--glob PATTERN`** — additive glob-resolver options deferred in
  the glob spec (runs inside symlinked dirs; power-user patterns). Off by default keeps cycle safety.
