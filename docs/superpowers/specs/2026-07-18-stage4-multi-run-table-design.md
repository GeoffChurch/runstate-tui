# Stage 4 — the multi-run table: design (2026-07-18)

A **spec-delta** on the core design (`2026-07-17-runstate-tui-core-design.md` §6.4/§9/§10/§13 + its
Post-Stage-3 revisions R2) and the pickup doc (`docs/backlog/stage4-multi-run-table.md`). It records
the decisions taken in the Stage-4 design pass — including a design-review reversal (below) — and is
the source for the implementation plan.

## Goal

Render many runs as a `DataTable`, one row per run — `render = aggregate ∘ map(status_fold) ∘ resolve`
at `|I|>1`, the full functor the cockpit has only ever run at `|I|=1`. **The §11 singleton stays the
acceptance test:** `render_single(r)` equals the table's row for `r`.

## The cadence (chosen: pool + fold-fresh — NOT watermark-gating)

Each tick, a single owner thread reuses a pool of open channels and calls **today's `status_fold`
fresh** for every resolved run. No fold split, no snapshot cache, no watermark. `status_fold` is
unchanged; the table row for `r` **is** `render_single(r)` at the frame's `now`, so the singleton
holds by construction — literally the same function, same argument.

**Per-frame `now`:** the owner thread captures `now = env.clock()` once, then folds every run under
`frame_env = replace(env, clock=lambda: now)` — one consistent `now` across the whole frame (fixes
`table.py`'s current per-row re-sample), and `env.objective`/`stuck_threshold`/`liveness` all carry
through untouched, so a channel-reading `LivenessSignal` overlay keeps working.

### Rejected: watermark-gating (`fold_log`/`derive_clock` + `LogSnapshot`)

The design pass first chose to split `status_fold` into `fold_log` (log-triggered, cached) +
`derive_clock` (clock-triggered), gated on `channel.last_seq()`, so an idle run skips its ~54 µs of
seeks. Three independent plan red-teams (2026-07-18) killed it; the owner's call was to drop it:

- **It buys almost nothing at the stated targets.** The design's own §10 numbers: re-folding *all*
  100 runs every tick is ~5 ms/frame, ~18 ms at the ~340-run EMFILE ceiling — **< 2 % of the 1 Hz
  budget.** The gate optimizes away single-digit milliseconds.
- **The split silently amputates `env.liveness`.** `derive_clock(snap, now, stuck_threshold)` has no
  channel/env, so a channel-reading `LivenessSignal` (a real, tested, exported extension point —
  `resolve_liveness` in `env.py`) becomes inert, undetectably so (the composition self-agrees while
  diverging from `status_fold`).
- **The cache is blind to in-place corruption.** `last_seq()` = `MAX(seq)`; an in-place body rewrite
  (bit rot; the exact class `corrupt_seq` simulates) doesn't move it, so the gate replays a stale
  pre-corruption snapshot forever for idle/terminal runs — the very runs the gate optimizes for. (A
  torn *append* still moves the watermark and is caught; only post-hoc rot of an already-folded row
  is missed — so this is narrower than a keystone break, but it undercuts the "loud `corrupt`"
  promise for idle runs.)

Fold-fresh dissolves all three: full `env.liveness`, corruption caught within one tick, and no
`LogSnapshot`/`derive_clock`/coherence surface to get wrong. The **pool** is the load-bearing,
permanent piece; gating was an optional layer that can be added later, measured, if filer-load or
scale beyond "hundreds of runs / 1 Hz" ever justifies it. **Do not re-propose watermark-gating
without that evidence.**

## The owner thread + LRU pool (§13)

A single thread owns the entire channel pool — all opens, reads, `status_fold`s, evictions, closes
(`reader == evictor`: no use-after-close → no false `unreadable`, no lock-order, uncontended
per-channel lock). The pool is an `OrderedDict[RunRef, Channel]` — **keyed by the full `RunRef`, not
bare `run_id`** (`ref_from_path` derives `run_id = Path.stem`, so `a/run1.db` and `b/run1.db` collide
on `run_id`; a multi-run comparison tool must key by the whole ref). Each tick, on the owner thread:

1. `refs = resolver(now)` — the resolved `IndexSet`.
2. `reconcile`: close + evict any pooled run no longer in `refs`.
3. Per run: stat-before-open → missing/unreadable → a bare integrity `Row` (no channel cached).
   Openable → reuse the pooled channel (open on miss; LRU-evict + close the oldest beyond the cap),
   then `status_fold(channel, frame_env)` fresh. A byte-torn → that run's `Row` is `corrupt`
   (`locate_torn_seq`); a substrate fault → `unreadable`. **On any integrity failure the channel is
   evicted + closed** so the next tick re-opens fresh (self-healing detection; the pool holds only
   healthy handles). **The table survives one bad run** — the per-run boundary contains it.
4. Build the immutable `Table` (an ordered `tuple[(RunRef, Row), ...]`), `post_message(TableReady)`.
5. Self-reschedule the next tick *after* completion (no pile-up).

**Pool cap:** a generous default (128) below the ~340 EMFILE ceiling (§10); LRU-evict + close beyond
it (an evicted-then-reappearing run cold-opens again, ~108 µs). **Cost of fold-fresh:** the truth-
quintet's ~54 µs/run every tick even for idle runs — ~5 ms/frame at 100 runs, free at 1 Hz (§10).

## Main-thread reconcile (§13)

On `TableReady`, inside the **app's** `batch_update()` (`self.batch_update()` — it is an `App` method,
not a `DataTable` method): `remove_row(gone)`; `update_cell(row_key, col_key, …)` for changed cells;
`add_row(*cells, key=row_key)` for new; then `sort()` + `move_cursor` back onto the selected row.
**Columns are created with explicit `(label, key)` tuples** (`add_columns(("run","run"), …)`) — an
unkeyed `add_columns("run", …)` yields an anonymous `ColumnKey(None)` whose per-instance hash makes
every later `update_cell`/`sort` fail deterministically. **The row key is a stable string encoding of
the `RunRef`** (`ref_key(ref)`), not bare `run_id`, so basename-colliding runs stay distinct rows;
the `run` column *displays* `run_id`. **Never `clear()`+repopulate** (resets cursor/scroll). A
vanished run → the detail pane shows `missing`, never a rebind.

## The `⚠ I/O stalled` watchdog (§10 — in the MVP)

A wedged open (NFS `-shm` D-state) on the single owner thread makes `fold_frame` never return →
`TableReady` never fires → **every** row freezes and ticking stops, silently — §10 forbids exactly
this ("never a frozen frame"), and the single-owner-thread model turns one wedged run into an N-run
blackout. So the MVP carries a **main-thread** staleness watchdog, independent of the owner thread: a
`set_interval` checks `now − last_TableReady_age`; beyond `k × tick_interval` it shows a `⚠ I/O
stalled` banner. It makes the hang *loud*; per-run recovery (cancellable opens) stays deferred.

## Owner-thread teardown (§13 — the use-after-close boundary)

`Worker.cancel()`/`exclusive=True` cancel the asyncio wrapper around `run_in_executor`, **not** the
running OS thread — so `on_unmount` must not touch the pool from the main thread while a fold is in
flight (that is the exact use-after-close race the owner-thread model exists to prevent). Teardown:
`async def on_unmount` sets a `_closing` flag, `await self.workers.wait_for_complete()` (drains the
in-flight frame), then `self._pool.close_all()` on the main thread with the owner thread guaranteed
idle. `_fold_frame` checks `_closing` at its top and bails without touching the pool, and its
`call_from_thread` marshals are wrapped in the `_TEARDOWN_ERRORS` guard `detail.py` already uses.
Note the real serialization guarantee is **"only the self-reschedule chain calls `_tick`"** — not
`exclusive=True`, which does not serialize thread workers.

## Drill-down (the R2 query plane)

`enter` on the selected row → the existing `DrillDownScreen` for that run's ref (its own channel +
`read_log_delta`, orthogonal to the pool); `escape` returns to the table. Unchanged from Stage 3, just
parameterized by the selected row's `RunRef` — reconstructed synchronously in `action_detail` from
`resolver(now)` (pure, no I/O), not mutated in from the worker thread.

## Resolver

`explicit_resolver(refs: list[RunRef]) -> Resolver` — a fixed list; the CLI accepts multiple
`<run.db>` paths (`ref_from_path` each). **Deferred:** `glob` (blocked on `create=False` — a glob
could open+mutate a foreign valid db, the §8 harm; and it is the resolver that would give *live*
discovery of runs not-yet-existing at launch, which the fixed list cannot) and `cells`
(workload-specific sweep) — both additive.

## Error handling

The integrity taxonomy carries over **per run** (missing/unreadable/corrupt/malformed); one bad run is
a loud row, never a table crash. **Deferred:** issue-flood aggregation (collapse N identical badges
from one NFS hiccup into a super-issue — §7); per-run I/O recovery (cancellable opens) beyond the
main-thread `⚠ I/O stalled` banner.

## Testing

- **The singleton test extends:** drive one owner-thread tick under a fixed clock, then assert the
  table's row for `r` equals `render_single(r)` — proving the pooled fold-fresh path is the functor.
- **Ref-key distinctness:** two refs with the same `run_id` but different roots → two distinct rows /
  two pooled channels (the 2b collision guard).
- **Keyed reconcile:** add / update / remove a run → the correct `DataTable` mutation, cursor/scroll
  preserved (via `log_text`-style content asserts on the table; `snap_compare` for layout).
- **One corrupt run → a `corrupt` row, the table survives** (the per-run boundary contains it), and
  the bad channel is evicted (fold-fresh re-detects next tick).
- **LRU eviction:** cap=2 over 3 runs → ≤ 2 channels open, the LRU one closed.
- **Per-frame `now`:** all rows in one frame share one `now` (freshness consistent).
- **`⚠ I/O stalled`:** advancing the (fake) clock past `k × tick_interval` with no new `TableReady`
  raises the banner; a fresh `TableReady` clears it.
- Reuse the fixture basis: `held_writer_sqlite_run` (live appends), `advance_tick`, `build_log`,
  `corrupt_seq`, plus a small multi-run harness.

## Deferred (all additive, none load-bearing for the MVP)

glob resolver (`create=False`; also the live-discovery path), cells resolver, issue-flood
aggregation, per-run I/O recovery (cancellable opens), and — explicitly rejected above, not merely
deferred — watermark-gating / the `fold_log`+`derive_clock` split.
