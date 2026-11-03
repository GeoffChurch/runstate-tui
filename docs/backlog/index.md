# runstate-tui backlog

Deferred features and their *committed seams*. An entry here means the core design left a place
for the feature to slot in additively — the implementation waits for its first real need. Each entry
describes what is still open, not what already landed; git history carries the provenance.

- [cells-resolver](cells-resolver.md) — the **`cells` resolver**: the mycooc experiment/cell layout
  adapter (the third layout after `explicit` and `glob`), externally gated on mycooc's still-settling
  layout. Drops onto the `MultiRunApp` / resolver seams and the grouped-table rendering. Also carries
  the small grouping remainders (multi-attr pivot, missing-attr bucket) and related deferred findings.
- [cli](cli.md) — the CLI shape-dispatch and its argparse flag layer; deferred **subcommands** if the
  shape-guessing (dir vs `.json` vs `.db`) ever turns ambiguous.
- [value-trend](value-trend.md) — a lightweight in-terminal value/progress trend, and an on-demand
  full trajectory in the drill-down. Control-vs-visualization is a stance, not a law: the feature is
  gated by the **scale seam** (cheap ring-buffer trend per-frame; a one-shot `series` / `aligned`
  lineage read only on demand, one run at a time), not by philosophy.
- [liveness-overlay](liveness-overlay.md) — external liveness probes (`os.kill` same-host;
  `squeue`/`kubectl` cross-host). Seam committed in the core spec §2.1/§14.2; core is
  freshness-only. Under runstate 0.4.0 a death is a record, so an unreported crash reads held and
  ages to `stale`; the probes are what can say `presumed_dead`. Disagreeing death reports are
  runstate's verdict set already; the overlay keeps the probe half of `conflicted`.
- [interactive-objective](interactive-objective.md) — change the value column's metric from inside
  the cockpit instead of only at launch. No longer gated on runstate#19: the value is a held fold,
  so a missing name costs O(new records) a tick; a new name costs one cold fold per run (~17 ms on
  a median mycooc run). Carries the Env-travels-with-the-frame invariant and the verified Textual
  mechanics.
- [per-run-objective](per-run-objective.md) — let the manifest declare each run's metric, as a
  sibling field rather than an attr (attrs are labeling/grouping only, never fold input). Run
  classes that track different metrics are legitimate; the invariant becomes *comparable within a
  section*. Gated on the first real manifest emitter.
- [metric-discovery](metric-discovery.md) — a metric-name *picker*, sourced by runstate's
  `streams()` (the lineage's eager and source names; one-shot, one run). Unblocked; what it cannot
  list (unemitted names, other branches, registers) is recorded there.
- [fold-retention](fold-retention.md) — a group larger than the pool cap re-folds runs cold every
  frame (~54 ms per median mycooc run; 9 mycooc experiments exceed the cap, the largest 904 runs). The
  plan, [#36](https://github.com/GeoffChurch/runstate-tui/issues/36): cache an ended run's row by its
  `last_seq`, and spread the first frame.
- [postgres-resolver](postgres-resolver.md) — discover runs whose logs live on a Postgres server.
  Gated on mycooc's stage 3 ([mycooc#2](https://github.com/GeoffChurch/mycooc/issues/2): live channels on
  `PostgresChannel`): until then no consumer's runs are
  there, and from then on `glob` cannot see a live mycooc run.
- [showcase-gifs](showcase-gifs.md) — animated usage GIFs, generated from the same headless
  fixture-basis machinery that renders the static screenshots.
