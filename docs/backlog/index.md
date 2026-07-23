# runstate-tui backlog

Deferred features and their *committed seams*. An entry here means the core design left a place
for the feature to slot in additively — the implementation waits for its first real need.

State (2026-07-22): merged to `master` — Stages 0–3 (single-run observe / drill-down / control),
the **multi-run table** (PR #11), the drill-down redesign (PR #14), the showcase screenshots (PR
#12/#13), the integrity taxonomy, the fixture basis, the **runstate locator-split migration** (PR
#15: `open_channel` → `attach_channel` / `create_channel`), the **glob resolver** — live recursive
directory discovery (PR #16), and **issue-flood aggregation** — the always-on fleet summary strip (PR
#18). **All the major features have shipped;** what remains below is smaller, additive work. (A
`runstate-tui-build-state` memory, if present, carries finer-grained status, but this doc is the
source of truth.)

- [multi-run-remainders](multi-run-remainders.md) — the table, glob resolver, and issue-flood strip
  all shipped; the deferred multi-run work is the **`cells` resolver** (the *mycooc* experiment/cell
  layout adapter — externally gated on mycooc's still-settling layout) plus its reference-TUI-generic
  core, **grouping via a per-row relational attribute record** (group/label by any named attribute — not
  a path/hierarchy; gated on a second grouping consumer). Both YAGNI-deferred, both drop onto the shipped
  `MultiRunApp` / resolver seams.
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
