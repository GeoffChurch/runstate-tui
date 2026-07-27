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
  gated by the **scale seam** (cheap ring-buffer trend per-frame; O(N) `value_series` replay only on
  demand, one run at a time), not by philosophy.
- [liveness-overlay](liveness-overlay.md) — external liveness probes (`os.kill` same-host;
  `squeue`/`kubectl` cross-host). Seam committed in the core spec §2.1/§14.2; core is
  freshness-only. **Also the home of log-level `conflicted`**, which needs probe corroboration plus a
  row-3-vs-row-4 policy call — not a fold rewrite.
- [interactive-objective](interactive-objective.md) — change the value column's metric from inside
  the cockpit instead of only at launch. **Gated on runstate#19**: `latest(VALUE, name=…)` is an
  index seek only on a hit, and typing names makes the ~85 ms miss routine. Carries the
  Env-travels-with-the-frame invariant and the verified Textual mechanics.
- [per-run-objective](per-run-objective.md) — let the manifest declare each run's metric, as a
  sibling field rather than an attr (attrs are labeling/grouping only, never fold input). Run
  classes that track different metrics are legitimate; the invariant becomes *comparable within a
  section*. Gated on the first real manifest emitter.
- [metric-discovery](metric-discovery.md) — a metric-name *picker*. Blocked on the same missing
  name-aware index (runstate#19), which would make the complete list an index scan and retire the
  lazy/partial design before it is built.
- [showcase-gifs](showcase-gifs.md) — animated usage GIFs, generated from the same headless
  fixture-basis machinery that renders the static screenshots.
