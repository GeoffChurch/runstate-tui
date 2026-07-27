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
- [metric-discovery](metric-discovery.md) — lazy metric-name discovery (default, labeled
  *partial*) + an explicit expensive full-log scan for completeness; upstream TODO to investigate a
  runstate name-enumeration API, filed only on demand.
- [showcase-gifs](showcase-gifs.md) — animated usage GIFs, generated from the same headless
  fixture-basis machinery that renders the static screenshots.
