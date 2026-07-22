# Stage 4 — the multi-run table

**Status:** deferred (the last major feature). Stages 0–3 (single-run: observe / drill-down /
control) + the integrity taxonomy + the fixture basis are all merged to `master`. This is the
consolidated pickup for the table; the authoritative detail lives in the core spec
(`../superpowers/specs/2026-07-17-runstate-tui-core-design.md`) — this doc points at the right
sections and states the prerequisites in one place so a fresh session can orient fast.

## What it is (spec §6 stage 4, §9)

Index many runs by a **resolver** and render them as a `DataTable`, one row per run:
`render = aggregate ∘ map(status_fold) ∘ resolve`. The single-run `Row` is unchanged — the table
is literally `map(fold)` over a resolved `IndexSet`, so **the acceptance test is still the §11
singleton**: `render_single(r) == render_table(const[r])[0]`. §9 records *why* the table is Stage 4
and not the core: risk-first product judgment — land the smallest complete increment (one run) first,
de-risk the ML case on the same fold, then add resolver + pool complexity.

Scope of the stage: `glob`/`cells`/`explicit` resolvers (`Time → IndexSet`); the LRU channel pool;
selection pinned to a stable `run_id`; issue-flood aggregation (ISA-18.2: hundreds of identical
badges from one NFS hiccup collapse into one super-issue — §3.3/§7). **"The ML researcher's tool."**

## The concurrency model (spec §13 — the load-bearing part)

- **A single owner thread owns the ENTIRE channel pool** — all opens, reads, folds, evictions,
  closes. `reader == evictor`, so a mid-fold channel is never closed under it (no use-after-close →
  no false `unreadable`), there is no lock-order to violate (no `close()`-under-`pool_lock`
  deadlock → EMFILE), and the per-channel lock is uncontended (the cockpit is a pure reader). This
  is the Stage-1b `@work(thread=True)` fold generalized to own a pool.
- **`DataTable` is imperative — there is no reactive "diff a snapshot in."** Loop: the owner thread
  computes the fresh immutable `Table` → `post_message(TableReady)` (thread-safe; **never** assign a
  reactive from a worker thread) → the **main thread** does an explicit `run_id`-keyed reconcile
  inside `batch_update()`: `remove_row(gone)`; `update_cell(run_id, col, …)` for changed cells;
  `add_row(*cells, key=run_id)` for new; then `sort()` + `move_cursor` onto the selected `run_id`.
  **Never `clear()`+repopulate** (it resets cursor/scroll). Self-reschedule the next tick *after*
  completion (not raw `set_interval`) so slow ticks don't pile up. Selection identity falls out of
  the stable `RowKey`: a vanished `run_id` → the detail pane shows `missing`, never a rebind.

## Scale budget (spec §10 — respect these, measured)

- ~54 µs/run warm truth-quintet (indexed `latest()`/`read([started],limit=1)` seeks) → 100 runs ≈
  5 ms/frame, free at 1 Hz. Budget cold-open (~108 µs) + LRU-reopen churn above the pool cap.
- **3 fds/`SqliteChannel`** → EMFILE at ~340 open runs; **the LRU pool is not optional.**
- **No per-frame data-plane refolds** (the O(N) `value_series` exclusion is forever).
- I/O off the render thread; a wedged open (NFS `-shm` D-state) must degrade to a cockpit-level
  **`⚠ I/O stalled`** after *k* ticks — never a frozen frame or a dead `stop` key (§10).
- Capture `now` **once per frame** and thread it to every row for frame-consistent freshness
  (`table.py:52-55` flags this — today `render_table` re-samples `now` per row).

## The event-driven shape it should take (session memory `runstate-tui-event-driven-architecture`)

Stage 4 is where the **"poll a cheap watermark, apply the delta, never rebuild"** principle pays off.
Build it as a delta pipeline, not a rebuild loop: **resolve-delta → watermark-fold → keyed-reconcile.**
- The fold factors into **log-triggered** (cached log-reads, re-read only when `last_seq()` moves)
  and **clock-triggered** (freshness/elapsed/LIVE↔STALE, re-derived every tick from cached anchors).
  An idle run skips its ~54 µs of seeks → a ~20× per-frame I/O cut when ~5 of 100 runs are active.
- The DataTable keyed-reconcile is the *table-shaped* instance of the same delta-application; the
  resolver's run-set delta (runs added/removed) is another. Same principle, three sites.

## Prerequisites & gotchas

- **Safe globbing — SHIPPED (runstate locator split, PR #18 `ba26e50`).** The old hazard
  (`open_channel` ran `executescript(_SCHEMA)` at open, schema-mutating a foreign *valid* db a
  glob matched) is gone: runstate replaced it with **`attach_channel`** — existing-only, raises
  `RunNotFound`, never creates or mutates. The tui already migrated (PR #15): `open_and_fold`,
  `read_log_delta`, and `pool.row_for` open via `attach_channel` + `except RunNotFound` (the
  stat-before-open dance collapsed), so a stale glob match resolving to a missing / empty / foreign
  db reads `missing` and is left byte-identical. **A broad glob is now as safe as an explicit
  run** — build the `glob`/`cells` resolvers directly on that existing open path. (Cockpit item 4
  closed; spec §8.)
- **`max_seq=` (runstate)** — a Stage-4 uniformity nicety only, now filed upstream as
  `GeoffChurch/runstate#15` (the `read()` query layer: `filter=` / `before=` / `max_seq=`). Bound on
  `max_seq` (seq) **only, never `before=`** (t is never an ordering key). Not blocking. Spec §3.2/§8.
- **Resolver glob grammar + zero-match** behavior is an open question (spec §12 known gaps).
- **Terminal-env matrix** (SSH/tmux/`NO_COLOR`/width/UTF-8) should be exercised at the table.
- The fixture basis (`tests/scenarios/`, `tests/helpers.py`) already has `held_writer_sqlite_run`
  and the WAL-visibility scenario — reuse for multi-run harnesses.

## Deferred findings that surface *at* Stage 4 (not blockers, but plan for them)

- **Issue-flood aggregation** (§7) — the |I|=1 core can't flood; the table can. Collapse N identical
  badges into one super-issue.
- **Corruption-invisibility full-scan** — the deferred opt-in, marked-expensive integrity scan
  (spec §12/H1 pattern) is a natural per-run drill-down action here, not a hot-path fold concern.
- **`conflicted`** — a liveness-overlay feature (see `liveness-overlay.md`), not a fold change;
  independent of the table.

## Entry points to read, in order

`build-state` memory → spec §6 (stage 4) → §13 (concurrency) → §10 (scale) → §9 (inversion) →
the `runstate-tui-event-driven-architecture` memory → this doc's prerequisites. Then `table.py`
(`render_table`/`open_and_fold` — the seam Stage 4 grows from) and `app.py` (the `@work` fold worker
Stage 4's owner thread generalizes).
