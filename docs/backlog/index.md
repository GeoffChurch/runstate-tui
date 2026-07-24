# runstate-tui backlog

Deferred features and their *committed seams*. An entry here means the core design left a place
for the feature to slot in additively — the implementation waits for its first real need.

State (2026-07-22): merged to `master` — Stages 0–3 (single-run observe / drill-down / control),
the **multi-run table** (PR #11), the drill-down redesign (PR #14), the showcase screenshots (PR
#12/#13), the integrity taxonomy, the fixture basis, the **runstate locator-split migration** (PR
#15: `open_channel` → `attach_channel` / `create_channel`), the **glob resolver** — live recursive
directory discovery (PR #16), **issue-flood aggregation** — the always-on fleet summary strip (PR
#18), and **relational grouping** — sectioning the table by a manifest-supplied attribute record (PR
#22). **All the major features have shipped;** what remains below is smaller, additive work. (A
`runstate-tui-build-state` memory, if present, carries finer-grained status, but this doc is the
source of truth.)

- [multi-run-remainders](multi-run-remainders.md) — the table, glob resolver, issue-flood strip, and
  **relational grouping** (PR #22 — section/label the table by a per-row attribute record from a
  `manifest_resolver`) all shipped; the one deferred multi-run feature left is the **`cells` resolver**
  (the *mycooc* experiment/cell layout adapter — externally gated on mycooc's still-settling layout).
  Drops onto the shipped `MultiRunApp` / resolver seams.
- [cli](cli.md) — the CLI shape-dispatch + the argparse flag layer (`--group-by`, `CliArgs`); deferred
  **subcommands** if the shape-guessing (dir vs `.json` vs `.db`) ever turns ambiguous.
- [value-trend](value-trend.md) — a lightweight in-terminal value/progress trend, and an on-demand
  full trajectory in the drill-down. The control-vs-visualization separation was relaxed from a law to
  a stance (owner, 2026-07-24); the feature is gated by the **scale seam** (cheap ring-buffer trend
  per-frame; O(N) `value_series` replay only on demand, one run at a time), not by philosophy.
- [liveness-overlay](liveness-overlay.md) — external liveness probes (`os.kill` same-host;
  `squeue`/`kubectl` cross-host). Seam committed in the core spec §2.1/§14.2; core is
  freshness-only. **Also the home of log-level `conflicted`** (2026-07-18 red-team: a reliable
  conflict check needs probe corroboration + a row-3-vs-row-4 policy call — not a fold rewrite).
- [metric-discovery](metric-discovery.md) — lazy metric-name discovery (default, labeled
  *partial*) + an explicit expensive full-log scan for completeness; upstream TODO to investigate a
  runstate name-enumeration API, filed only on demand.
- [readme-showcase](readme-showcase.md) — static scenario-backed screenshots **shipped** (PR #12: 5
  scenes in `docs/img/` + README `## Screens` + a CI smoke test). Deferred: **GIFs** (animated usage),
  generated from the same fixture-basis machinery.
