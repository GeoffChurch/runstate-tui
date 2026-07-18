# runstate-tui backlog

Deferred features and their *committed seams*. An entry here means the core design left a place
for the feature to slot in additively — the implementation waits for its first real need.

State: Stages 0–3 (single-run observe / drill-down / control) + the integrity taxonomy + the
fixture basis are merged to `master`. See the `runstate-tui-build-state` memory for the live status.

- [stage4-multi-run-table](stage4-multi-run-table.md) — **the last major feature.** Index many runs
  by a resolver → a `DataTable` (keyed reconcile + LRU pool + glob/cells resolvers). Consolidated
  pickup for the scattered spec sections (§6/§9/§10/§13) + the event-driven delta pipeline + its
  prerequisites (`create=False`, per-frame `now`, issue-flood aggregation).
- [liveness-overlay](liveness-overlay.md) — external liveness probes (`os.kill` same-host;
  `squeue`/`kubectl` cross-host). Seam committed in the core spec §2.1/§14.2; core is
  freshness-only. **Also the home of log-level `conflicted`** (2026-07-18 red-team: a reliable
  conflict check needs probe corroboration + a row-3-vs-row-4 policy call — not a fold rewrite).
- [metric-discovery](metric-discovery.md) — lazy metric-name discovery (default, labeled
  *partial*) + an explicit expensive full-log scan for completeness; upstream TODO to investigate a
  runstate name-enumeration API, filed only on demand.
- [readme-showcase](readme-showcase.md) — todo: scenario-backed screenshots + GIFs of the screens
  for the README, generated from the fixture basis (deterministic, regenerable) via
  `save_screenshot` → PNG.
