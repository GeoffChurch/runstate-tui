# Stage 4 — the multi-run table: design (2026-07-18)

A **spec-delta** on the core design (`2026-07-17-runstate-tui-core-design.md` §6.4/§9/§10/§13 + its
Post-Stage-3 revisions R2) and the pickup doc (`docs/backlog/stage4-multi-run-table.md`). It records
the decisions taken in the Stage-4 design pass and is the source for the implementation plan.

## Goal

Render many runs as a `DataTable`, one row per run — `render = aggregate ∘ map(status_fold) ∘ resolve`
at `|I|>1`, the full functor the cockpit has only ever run at `|I|=1`. **The §11 singleton stays the
acceptance test:** `render_single(r)` equals the table's row for `r`.

## The fold factorization (the core change — chosen: watermark-gate from the start)

`status_fold` splits into two composable pieces — the R2 event-driven factorization made concrete:

- **`fold_log(channel) -> LogSnapshot`** — the **log-triggered** part: the `peek_terminal` verdict,
  the `last_activity` and first-`started` *anchors* (raw floats), `progress` (frontier), `read_value`,
  `latest_episode` (episode handle + its seq, for the episode-scope of stops), `undischarged_stops`
  (episode-scoped, R3), `live_demand`, and the accumulated `issues`. Guarded exactly as today
  (byte-torn propagates → `corrupt` at the boundary; malformed → per-factor issue; the `_episode`/
  `_started_t` reads keep alien-body → malformed). **Cacheable** — a pure function of the channel's
  committed log.
- **`derive_clock(snap, now) -> Row`** — the **clock-triggered** part: `freshness = max(0, now −
  last_activity)`, `elapsed = now − started_t` (skew-guarded), the non-finite-anchor guard (#2), and
  the LIVE↔STALE resolution of a non-terminal, has-activity status against `env.stuck_threshold`.
  Cheap; no I/O.
- **`status_fold(channel, env) = derive_clock(fold_log(channel), env.clock())`** — behavior identical
  to today, so **the singleton holds by construction**: the caching is transparent (a current cache
  yields the same `LogSnapshot` as a fresh `fold_log`), so `render_single(r) == render_table(const[r])[0]`
  at any `now`. The single-run app calls the composition; the *gating* is the owner thread's.

`LogSnapshot` is a frozen value type carrying: `terminal: RunResult | None`, the raw anchors
`last_activity: float | None` and `started_t: float | None`, `frontier`, `value`, `episode`,
`undischarged_stops`, `live_demand`, `issues`. `derive_clock` turns those + `now` into the final
`Row` (finishing the status as live/stale/pending/terminal, computing freshness/elapsed). The
whole-run **integrity** statuses (`missing`/`unreadable`/`corrupt`) are decided at the open/boundary
(the `open_and_fold` guards), *before* `fold_log` — they produce a bare `Row` directly and are not
cached as a `LogSnapshot`.

## The owner thread + LRU pool (§13)

A single thread owns the entire channel pool — all opens, reads, `fold_log`s, evictions, closes
(`reader == evictor`: no use-after-close → no false `unreadable`, no lock-order, uncontended
per-channel lock). Per openable run it caches `(channel, last_folded_seq, LogSnapshot)`. Each tick,
on the owner thread:

1. `runs = resolver(now)` — the resolved `IndexSet`.
2. Reconcile the pool to `runs`: open new (stat-before-open; LRU-evict + close the oldest beyond the
   cap); drop gone (close + evict).
3. Capture `now` **once** (per-frame consistency — fixes `table.py`'s current per-row re-sample).
4. Per run: missing/unreadable → a bare integrity `Row` (a cheap per-tick stat/open — not cached).
   Openable → `if channel.last_seq() > last_folded_seq: snap = fold_log(channel); cache`; then
   `row = derive_clock(snap, now)`. A byte-torn in `fold_log` propagates to the per-run boundary →
   that run's `Row` is `corrupt` — **the table survives one corrupt run** (the per-run guard contains
   it; this is exactly the post-reshape behavior — a loud `corrupt` row, not a cockpit crash).
5. Build the immutable `Table` (an ordered `list[(run_id, Row)]`), `post_message(TableReady(table))`.
6. Self-reschedule the next tick *after* completion (no pile-up).

**Watermark payoff:** an idle run costs one O(1) `last_seq()` probe + a nanosecond `derive_clock`,
skipping its ~54 µs of seeks — 5-of-100-active is ~5× cheaper than re-fold-all, degrading toward
hundreds. **Pool cap:** a generous default (128) below the ~340 EMFILE ceiling (§10); LRU-evict+close
beyond it (an evicted-then-reappearing run cold-opens again, ~108 µs).

## Main-thread reconcile (§13)

On `TableReady`, inside `batch_update()`: `remove_row(gone)`; `update_cell(run_id, col, …)` for changed
cells; `add_row(*cells, key=run_id)` for new; then `sort()` + `move_cursor` onto the selected `run_id`.
**Never `clear()`+repopulate** (resets cursor/scroll). Columns mirror `format_row`'s fields —
status / step / age / value / elapsed + a severity-flagged issue indicator. Selection identity rides
the stable `run_id` `RowKey`; a vanished run → the detail pane shows `missing`, never a rebind.

## Drill-down (the R2 query plane)

`enter` on the selected row → the existing `DrillDownScreen` for that run's ref (its own channel +
`read_log_delta`, orthogonal to the pool); `escape` returns to the table. Unchanged from Stage 3, just
parameterized by the selected `run_id`'s ref instead of the CLI ref.

## Resolver

`explicit_resolver(refs: list[RunRef]) -> Resolver` — a fixed list; the CLI accepts multiple
`<run.db>` paths (`ref_from_path` each). **Deferred:** `glob` (blocked on `create=False` — a glob could
open+mutate a foreign valid db, the §8 harm) and `cells` (workload-specific sweep) — both additive.

## Error handling

The integrity taxonomy carries over **per run** (missing/unreadable/corrupt/malformed); one bad run is
a loud row, never a table crash. **Deferred:** `⚠ I/O stalled`-after-k-ticks (a wedged open on the
owner thread — §10 robustness fast-follow) and issue-flood aggregation (collapse N identical badges
from one NFS hiccup into a super-issue — §7).

## Testing

- **The singleton test extends:** drive one owner-thread tick, then assert the table's row for `r`
  equals `render_single(r)` — proving the `fold_log`/`derive_clock` split + gating is transparent.
- **Watermark-gating:** a spy/counter on `fold_log` — an idle run (unchanged `last_seq()`) is NOT
  re-folded across ticks; a run that grows IS.
- **Keyed reconcile:** add / update / remove a run → the correct `DataTable` mutation, cursor/scroll
  preserved (via `log_text`-style content asserts on the table; `snap_compare` for layout).
- **One corrupt run → a `corrupt` row, the table survives** (the per-run boundary contains it).
- **Per-frame `now`:** all rows in one frame share one `now` (freshness consistent).
- Reuse the fixture basis: `held_writer_sqlite_run` (live appends), `advance_tick`, `build_log`,
  `corrupt_seq`, plus a small multi-run harness.

## Deferred (all additive, none load-bearing for the MVP)

glob resolver (`create=False`), cells resolver, issue-flood aggregation, `⚠ I/O stalled` degradation,
watermark-gating of the single-run app (moot at `|I|=1`).
