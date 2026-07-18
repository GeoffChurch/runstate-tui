# runstate-tui — core design: scope & sequencing

*Design doc. 2026-07-17. Supersedes the framing in `README.md` where they differ (see
§9 "Inversion").*

## 0. Purpose

A **control-plane cockpit** for [runstate](https://github.com/GeoffChurch/runstate) runs:
a terminal UI that answers *"what is happening / what happened"* across groups of runs and
lets you act on one. **No scientific data-plane plots/curves** (the ban is scoped to trajectory
reconstruction, *not* to a run's current scalar value — see §10). It works on a cold log — no
daemon, no server, no instrumentation — because the log already holds everything it renders.

This doc settles what the unassailable core is and the order it is built. The core is the
*initial object* of the design (§1); the build order puts the fold first by data-flow
necessity, and single-run-before-table is a risk-first product choice (§6, §9).

### The one rule (unchanged, but reclassified)

> **Use only runstate's public API. Every time you can't, that's a finding.**

A "finding" is a **development-time, dogfooding output** — a suggestion to improve runstate —
**not** a runtime feature of the cockpit. When the public API can't answer cleanly, we file it
against runstate and reduce cockpit scope to route around it; the running cockpit renders only
what the public API cleanly gives (API gaps never reach the UI — §5). This rule has teeth:
runstate's first consumer, mycooc, already **broke** it — reading channels via raw
`sqlite3.connect('file:…?mode=ro')` (`run_experiment.py:2394`) to dodge the open-seam (§3, §8).
The cockpit must not; that seam is a filed upstream finding instead.

## 1. The factorization (the core is an initial object)

Read the three units as one construction:

```
render = aggregate ∘ map(status_fold) ∘ resolve
```

- **Within a run**, the observables are independent projections, so a run's row is their
  **product** (a limit). The per-frame core projection is the **truth-quintet** —
  `status × frontier × freshness × value × elapsed` — and the full `Row` additionally carries
  the drill-down factors and the issue channel (§14 pins the types). `status_fold` is the
  mediating map, conceptually `Snapshot × Env → Row` (concretely `Channel × Env`, §14). **This
  is the atom.**
- **Across runs**, the table is the **colimit** over an index set the resolver builds, re-taken
  each tick: `Table(g) = ∐_{i ∈ resolve(g)} Row_i`, then ordered/filtered.
- **The single-run view is `Table` at the singleton explicit resolver** — `resolve = const [r]`.
  The *base case* of the table, not a sibling.
- **Drill-down** is `Row` with a **finer codomain** — more factors of the *same* fold.
- **Stop** is an effectful arrow on the run *object* — orthogonal to observation.

**Initiality.** `status_fold` of one run is the object both extensions attach to; evaluating at
the singleton diagram recovers it. It forces **Stage 0 (the fold) first** and forces
table/drill-down to sit after it; it is **neutral** between shipping the single-run view first
and the table first (those are siblings — single-run-first is the §9 product choice).
View/selection state is **not** a factor of `Row` and **not** an algebraic seam — it lives in
the cockpit (§13).

**Consequence for users.** The physicist's one big run is the base case of the ML researcher's
sweep; serving the physicist de-risks the ML case for free.

## 2. Seams on the algebraic fault lines

Module boundaries sit where the ambient category changes.

| region | algebra | module |
|---|---|---|
| observation | pure functor (`Snapshot×Env → Row`), cached | status fold |
| control | effectful (Kleisli); safety a predicate we don't own | `stop` arrow |
| time / policy | ambient environment (injected `Env`) | the `Env` seam |
| resolution | diagram over time (`Time → IndexSet`) | resolver |

Forcing control or time into the fold hides these seams rather than removing them — they
resurface as bugs where it matters most (was a stop *served* or *armed for the next episode?*).
So: **hard uniformity law on the pure half; honest, explicitly-typed arrows across the seams.**

### 2.1 Liveness is a lattice of signals — the core takes only the pure one

Liveness is not one thing; it is a lattice of increasing cost and decreasing generality:

```
freshness (pure fold of the log's timestamps, universal)
  <  arrival-time (stateful, needs a running watcher, universal)
  <  external probe (effectful: os.kill same-host, squeue/kubectl cross-host)
  <  heartbeat-semantics (workload-specific)
```

The **core commits to freshness only** — a pure fold of the log against the injected clock,
identical local or cross-host. `os.kill` is *only meaningful same-host* and would make the fold
impure, non-deterministic, and hot-path-costly at 340 runs; scheduler probes (`squeue`/`kubectl`)
are cross-host but effectful and deployment-specific. All richer signals are an **overlay arrow**
composed on top, mirroring `stop`. This aligns the core with runstate's own stateless observer
plane (freshness) vs. the stateful `Watcher`. The overlay is **deferred**
(`docs/backlog/liveness-overlay.md`); the **seam it plugs into is committed now** (§14.2), so
the deferral forecloses nothing.

## 3. The keystone: surface every possible issue

**Every arrow turns its failure modes into a visible, typed issue on the relevant row — never a
crash, never a silent swallow.** A physicist won't trust a tool that might quietly show a false
state but will trust `⚠ log torn at seq 4012`. Surfacing uncertainty *is* the feature.

### 3.1 Two-tier defense (not one)

The torn case does **not** raise `MalformedRecordError` — that type only wraps `cls(**body)`
violations on an already-decoded body (`observables.py`); the real `json.loads` is a layer
below in `channel.read()/latest()`, and `open_channel` runs `PRAGMA` + `executescript(_SCHEMA)`
at open. **A single guard around open+all-reads is wrong**: a malformed `lifecycle.stopped`
would collapse the *whole* row to `unreadable` even though the run is alive — voiding this
section's own "a row can be `live` **and** carry a `Torn`" promise. Instead:

1. **Guard the open once** → `unreadable` (a corrupt/foreign/interrupted db raises
   `sqlite3.DatabaseError` before any sub-fold; no verdict is derivable).
2. **Guard each observable read individually** → on `JSONDecodeError`/`sqlite3.DatabaseError`/
   `MalformedRecordError` that *one factor* degrades to `None` + a `Torn` issue; `status` folds
   from the survivors. So a torn `value` never hides a live verdict.

A per-observable guard is **necessary but not sufficient** — pair it with the outer open guard.
(`Torn` is sqlite-only; `MemoryChannel` stores decoded bodies and can't raise on read.)

### 3.2 Consistency, correctly scoped

Only the **verdict/liveness reconciliation** can manufacture a false verdict, and it reads only
`{started, stopped, terminated}` — low-cardinality. So it takes **one atomic
`read(topics=[…])`** (a single locked `fetchall` = a consistent cut, zero upstream dependency).
`frontier`/`value`/`freshness`/`elapsed` stay independent guarded `latest()` seeks whose tears
are cosmetic (`loss @ 4012` vs `4013`) and provably never cross a precedence boundary. The
`last_seq()` whole-fold bracket is retired (real, but livelocks under a hot writer); the
`max_seq=`/`before=` upstream ask is a **Stage-4 uniformity nicety**, not on the Stage-0 path.
Caveat (H3): `peek_terminal` is itself ~4 separate seeks today, so Stage 0 *calls* it and accepts
the rare cosmetic verdict tear; making the atomic read *the* verdict source would need a pure
`[Envelope] → RunResult` fold upstream (§8/§12), not an in-tree re-derivation of the `Outcome`
mapping (which would be the F7 drift §4 forbids).

### 3.3 Issues

`Row = ∏ observables × issues:[Issue]`. Each row has a **severity spanning status *and* issues**,
driving **color/badge** (hue capped at ≤4 levels + a redundant glyph for colorblind
accessibility) and an **optional** sort — but the **default table order is a stable user-owned
key** (spawn order / `run_id`), so live-view rows don't jitter as transient issues flap.
Severity: **high** = `unreadable`, `UnsafeStop`; **medium** = `conflicted`, `Torn`,
`SkewSuspected`; **informational** = `pending`, `missing`.

The `⚠ torn at seq N` precision needs the seq, which a bare `JSONDecodeError` does not carry — but
the cockpit **recovers it in-tree**: on a decode failure it locates the torn seq via a bounded
`read(after=k, limit=1)` walk (append-only contiguity), on the rare torn path only. A seq-carrying
substrate error upstream is therefore an *optional* ergonomic nicety, **not** a prerequisite (§8).

The integrity set is bounded (failure modes of {open, observe, resolve, control}) but **treat it
as open** — do not assume the list is exhaustive. **Alarm flood** at table scale (a shared-FS
hiccup painting hundreds of identical badges) is collapsed into one super-issue at Stage 4
(ISA-18.2 flood suppression); drill-down still enumerates all N. The |I|=1 core cannot flood.

## 4. Status codomain & the verdict-precedence lattice

`Status` is an **open coproduct** of three tiers; the terminal tier **wraps runstate's `Outcome`
directly** (no hand-maintained translation table — that is where logic drifts, cf. runstate
audit F7 — and a future `Outcome` member is absorbed for free):

```
Status =
    Pending | Live | Stale                         # non-terminal, freshness-derived
  | Terminal(Outcome)                               # Outcome ∈ {completed, preempted, errored, killed}
  | Missing | Unreadable | Conflicted              # resolution / substrate / semantic integrity
```

Display labels are cockpit policy (`completed` renders as `done` if preferred); the point is the
*representation* wraps `Outcome`, not the string. Every member is **self-describing** (a label +
a severity), so an unrecognized member renders **honestly** via its own label/severity — never a
lossy default; a code path that genuinely can't render a member **crashes verbosely**.

- `conflicted` is **narrowed** to *semantic* self-inconsistency — parseable records that violate
  lifecycle ordering (two live episodes; activity strictly after a terminal with no re-start).
  Distinct from `Torn` (*syntactic* — a record won't parse). Its former sources are gone: torn-
  read false positives (fixed by §3.2), and terminal-vs-probe contradiction (moves to the
  liveness overlay).
- `stale` is a **freshness hint**, not a hard verdict: freshness = `max(0, now − last_activity)`
  over the emitter's self-stamped `t`; forward skew raises `SkewSuspected` and a negative age
  never reads `live`.
- `presumed_dead` and probe-derived `conflicted` are **not** in the core — they arrive with the
  liveness overlay (§2.1), since the stateless fold cannot probe a handle.

### 4.1 The precedence lattice (freshness-only core; top row wins)

Inputs from one fold: `T = peek_terminal` (None | `Outcome`, never `presumed_dead` — that is
Watcher-only); `A = last_activity`, `age = max(0, now − A)`, `stale = age > env.stuck_threshold`;
`E = latest_episode` present?; `skew` → a `SkewSuspected` issue (never changes status).

| # | condition | `Status` |
|---|---|---|
| 1 | open/read raised a substrate error | `unreadable` (+detail) |
| 2 | resolver pointer, no file | `missing` |
| 3 | verdict records violate lifecycle ordering | `conflicted` (+detail) |
| 4 | `T` terminal | `Terminal(T)` — terminal wins over a later stray heartbeat |
| 5 | `T None`, `E` present, `not stale` | `live` |
| 6 | `T None`, `E` present, `stale` | `stale` |
| 7 | `T None`, `E None`, some dated/step activity (orphan/hand-run) | `live`/`stale` by freshness |
| 8 | `T None`, and `peek_terminal`/`progress`/`last_activity` all `None` | `pending` |
| — | overlay on any status | + `Torn` / `SkewSuspected` / `UnsafeStop` |

The liveness overlay adds rows (probe-`dead` → `presumed_dead`; terminal + probe-alive →
`conflicted`) **above** row 4, over the same abstract `Liveness` input (§14.2) — so it slots in
without rewriting the lattice.

## 5. In the runtime framework vs out

- **In (runtime):** observation/data-integrity issues (§3), including substrate open/read errors.
- **Out (dev-time dogfooding):** API gaps (`no run_epoch`, `open_channel` phantom, `no
  create=False`). Filed against runstate (§8), never rendered.

## 6. The sequence

**Stage 0 (the fold) is forced first**; single-run-before-table is the §9 product choice.

- **0 · Defended fold → `Row`** (no UI). Truth-quintet — verdict (`peek_terminal`), frontier
  (`progress`), freshness (`last_activity` + injected clock, capped ≥ 0), **value**
  (`latest(VALUE, name=env.objective)`, O(1)), **elapsed** (`now − first `started`.t`, via
  `read([started], limit=1)`, capped ≥ 0, `None` if no `started`) — plus severity-ranked
  `issues`. Two-tier defense (§3.1); atomic verdict read (§3.2). Pure over `(channel, env)`.
  Tested against a hand-built log **including a byte-torn record and a corrupt/foreign db**, and
  a torn record on one topic must yield the real verdict + `Torn`, **not** `unreadable`. **The
  initial object.**
- **1 · Single-run view = render `Row` through the singleton-table path.** `runstate-tui <run>`:
  stat-before-open (missing ⇒ `missing`; open/read error ⇒ `unreadable`), fold **on the single
  I/O owner thread** (§13), render, 1 Hz. **The physicist's complete tool.** Acceptance = the
  **singleton test** (`render_single(r) == render_table(const[r])`, one path).
- **2 · Attach `stop`** — one effectful arrow, **confirm-before-stop** gate, on a **dedicated
  stop thread** with a **bounded** `await_consumed` → `UnsafeStop` on timeout (never
  `timeout=None`). `request_id="webui:<unique>"`.
- **3 · Drill-down = codomain refinement** — episodes, undischarged stops, live demand, full
  issues, raw envelope tail. No new data path.
- **4 · Index by resolver → the table** — `glob`/`cells`/`explicit`, `Time → IndexSet`;
  `map(fold)`; the **LRU pool** owned by the single I/O thread (§13); selection pinned to
  `run_id`; issue-flood aggregation. **Safe globbing depends on `create=False`** (§8) — Stage-1
  explicit-run is lower risk. **The ML researcher's tool.**
- **5+ · Deferred** — liveness overlay (§2.1), `run_epoch`, postgres resolver, more control
  actions, progress-rate micro-trend (§10).

## 7. Scope of the core

**Core = Stages 0–2.** Truth-quintet (verdict, frontier, freshness, **value** via a group-level
objective, **elapsed** as wall-age); issues `Torn`/`Unreadable`/`Conflicted`/`SkewSuspected`/
`UnsafeStop`; `pending`/`missing` statuses; one action (stop, with a confirm gate); one view.
**Interactive commitments:** Textual (§13); a **single I/O owner thread** + a **dedicated stop
thread**, never Textual's default executor; a headless TUI test harness. Out of core: the table
(Stage 4), drill-down (Stage 3), data-plane curves (forever), the liveness overlay, flood
aggregation (Stage 4), API-gap surfacing (dev-only).

## 8. Layering & upstreaming

**Decision rule:** upstream iff a *policy-free fact any observer needs* that the API can't give
(a finding); sibling library iff *general observer machinery* that is consumer-side policy;
cockpit iff *presentation/product opinion*.

**Upstream findings — disposition (the prerequisite red-team collapsed five asks to one).** Only
`create=False` is a genuine filing; the rest dissolve in-tree, derive today, or self-defer. All are
additive (no wire/schema bump).
- **`create=False` / read-only open — FILE (the one real ask).** `executescript(_SCHEMA)` at open
  **silently schema-mutates a foreign *valid* db** (adds a `log` table to a file we don't own),
  rendering it empty. `stat-before-open` handles the missing-pointer phantom and our outer guard
  catches the corrupt-db crash → `unreadable`, but **neither catches the mutation** (the file
  exists; the mutation is *at open*), and no in-tree probe can (magic bytes are identical; a
  read-only sqlite probe is the banned `?mode=ro`). Already runstate's open item 4; filed with the
  mutation harm (runstate PR #14). Gates safe Stage-4 globbing; Stage-1 explicit-run is lower risk.
- **Total observation — do NOT file as a prerequisite; dissolve in-tree.** The `⚠ torn at seq N`
  badge is not blocked — the cockpit locates the torn seq via a bounded `read(after=k, limit=1)`
  walk (§3.3). *Optionally* file a low-priority seq-carrying `TornRecordError` at the substrate
  decode boundary — narrow ("give the decode error its seq"), **never** "make the observables
  total" (that changes return types and blasts the Watcher, which deliberately propagates
  `MalformedRecordError`).
- **Safe-stop — do NOT file "served-vs-armed"; report the finding.** The "armed for the next
  episode" half depends on a *future relaunch*, which run-episodes makes **caller policy**
  ("idle-may-relaunch and finished are identical on the log") — runstate can't answer it. The
  actionable gate (a live episode to serve it *now*?) dissolves in-tree: `live_episode` + freshness
  + a bounded `await_consumed` → `UnsafeStop`. runstate's item 6 suspects the shipped observer clock
  already dissolves it — so we report that. Optional residual: an atomic *send-with-live-witness* to
  close a TOCTOU.
- **`run_epoch` — not a cockpit prerequisite.** Wall-age `elapsed` derives today
  (`read([started], limit=1).t`). runstate independently plans `observables.run_epoch` and names the
  cockpit its second-consumer trigger — a light deferred +1. It's a *birth anchor* (= wall-age),
  **not** "accumulated runtime" (a separate cockpit-side fold over started/stopped pairs).
- **`max_seq=` — not a prerequisite; Stage-4.** If ever filed: the `max_seq` (seq) bound **only,
  never `before=`** (t is never an ordering key), framed as a bound on the `last_seq` coordinate the
  viewer already asserts — riding the principle that admitted `last_seq`, **never** the generic
  `read_range` runstate already rejected.

**The observer core does NOT go upstream** (it would make runstate test itself, killing the
acceptance-test property). **Sibling library — later, on a real second consumer.** runstate's own
record shows the demand: mycooc hand-rolled `channel_read.py`, and its audit flags F5–F8 as
*"every consumer reimplements this."* The `webui:` convention points at a web UI as that
consumer. Design the boundary now (`resolver | fold+pool | TUI`, cleanly importable), split the
package later. Adopting Textual is a live test of the boundary: the core never imports it.

**Coordinate with runstate's `cli-status` backlog** — a per-run `runstate status` CLI overlaps
our Stage-1 view; decide whether our fold *is* that CLI's implementation.

**Stays in the cockpit:** verdict *labels* + the stuck threshold, sort/filter, drill-down layout,
keybindings, stop UX + confirm gate, 1 Hz cadence, selection/view state, the **value objective &
formatting policy**, the **liveness overlay & its probe backends** (deployment policy), and the
human **labels**.

## 9. Inversion (vs the README)

The README leads with the table; this design makes the **single-run atom the core** and demotes
the table to Stage 4. The algebra is *neutral* on the ordering (§1); the justification is
**risk-first product judgment** — land the smallest *complete* increment (one run rendered well,
already the physicist's complete tool), de-risking the ML case on the same fold before adding
resolver + pool complexity.

## 10. Scale constraints (measured; respect these)

- **~54 µs/run** warm truth-quintet (indexed `latest()`/`read([started],limit=1)` seeks) → 100
  runs ≈ 5 ms/frame, free at 1 Hz. Budget cold-open (~108 µs) + LRU-reopen churn above the pool
  cap separately.
- **`live_demand` is cheap** — 4 index-served topics (not a full-log scan).
- **3 fds/`SqliteChannel`** → EMFILE at ~340 open runs; the **LRU pool is not optional** (Stage 4).
- **No per-frame data-plane refolds.** The O(N) exclusion is `value_series` (~1.9 s at 10⁶) —
  out forever. A last-value peek and a control-plane progress-rate micro-trend (a ring buffer
  over the sampled frontier, zero replay) are cheap and **not** data-plane replays (the trend is
  a Stage-3 candidate).
- **I/O off the render thread, on a single owner thread** (§13). A single wedged open (NFS `-shm`
  D-state, 5 s `busy_timeout`, 20× WAL retry) must degrade to a cockpit-level **`⚠ I/O stalled`**
  after *k* ticks — never a frozen frame or a dead `stop` key.

## 11. Acceptance criteria

- **Singleton test:** the single-run view is `table(const[run])` through one code path.
- **Granular degradation:** a torn record on **one** topic yields the run's **real verdict +
  a `Torn` issue**, not `unreadable`. (A `MalformedRecordError`-only fixture would miss this;
  the fixture includes a byte-torn record *and* a corrupt/foreign db.)
- **No phantom, no *runstate*-db mutation:** `missing` for an absent pointer, `unreadable` for a
  substrate error; the residual foreign-**valid**-db mutation is a known gap until `create=False`
  (§8) — `pending` must be read as "no records yet, *possibly a mis-resolved foreign db*."
- **Public-API-only:** no raw `sqlite3`, no `?mode=ro`, no `_`-prefixed calls.
- **Freshness never lies bright:** negative age never `live`; forward skew → `SkewSuspected`.
- **Interactive:** keypress→stop dispatches the right `request_id`; a torn fixture renders its
  badge in a real frame without crashing; resize reflows; an artificially slow open keeps input
  and the `stop` key responsive (proves the owner-thread/stop-thread split); the confirm gate
  fires; a GC'd selected run shows `missing` in the detail pane, never another run's tail.
- **Honest unknowns:** an unrecognized `Status` member renders via its own label/severity;
  an unrenderable one crashes verbosely — never a silent default.
- **The real bar:** you reach for it, unprompted, instead of a workload's `--status`.

## 12. Open questions / deferred (decide at first touch)

- **Liveness overlay** — `docs/backlog/liveness-overlay.md`; the seam is committed (§14.2).
- **`create=False` open** (the one real upstream ask) — filed, runstate PR #14; `unreadable` +
  stat-before-open now; gates safe Stage-4 globbing (§8).
- **Safe stop** — derive the served gate in-tree, surface `UnsafeStop`; the literal served-vs-armed
  is partly caller policy, not filed (§8).
- **Total observation** — not filed; seq recovered in-tree via a bounded walk; optional
  `TornRecordError` nicety only (§3.3, §8).
- **`run_epoch`** — not needed (wall-age derives today); a deferred +1 to runstate's own plan (§8).
- **`max_seq=`** — Stage-4 only; `max_seq`, never `before=` (§3.2, §8).
- **Metric-name discovery (H1)** — the value objective is hand-configured (injected) in the core. A
  later metric-picker discovers names **lazily**, but the lazy set is only a *lower bound* (metrics
  logged before attach / not yet seen are absent), so the picker **labels it "seen so far
  (partial)"** and offers an **explicit, opt-in, marked-expensive full-log scan** for the complete
  set (off the hot path — distinct from the banned per-frame `value_series` replay). Upstream
  investigation backlogged (`docs/backlog/metric-discovery.md`): a runstate name-enumeration API
  would make the complete list cheap — file only on demand.
- **Pure verdict fold (H3)** — Stage 0 calls `peek_terminal` (accept the rare cosmetic tear); a pure
  `[Envelope] → RunResult` fold is a small upstream nicety for the atomic verdict read (§3.2).

### Known gaps (resolve before/at the relevant stage)

Stop authorization/ownership (before Stage 2 beyond a trusted operator); cockpit cold-start if
freshness ever goes stateful; resolver glob grammar + zero-match (Stage 4); terminal-env matrix
(SSH/tmux/`NO_COLOR`/width/UTF-8, Stage 1); injected-clock source & tz; integration test vs a
real concurrent writer; cockpit self-observability (beyond a per-row `unreadable`).

## 13. The TUI unit (framework & concurrency)

Net-new (mycooc's monitors are non-interactive one-shot prints), so committed here.

**Architecture: a pure-projection core + a thin MVU/reactive shell.** The core is *not* MVU — a
pure projection re-derived from the cold log each tick. The shell *is* MVU-flavored:
`(ViewState, keypress) → ViewState`, `stop` an effectful `Cmd`. They meet at one boundary — the
immutable `Table` snapshot.

**Framework: Textual** — the only candidate (vs. Rich = renderer, prompt_toolkit = REPL-input,
urwid = manual/dated) with `@work` workers, auto input+resize, and a headless harness
(`run_test()`/Pilot + `pytest-textual-snapshot`).

**Concurrency (dolphie-proven; §13's v2 loop was wrong):**
- **A single owner thread owns the entire channel pool** — all opens, reads, folds, evictions,
  closes. `reader == evictor`, so a mid-fold channel is never closed under it (no use-after-close
  → no false `unreadable`), there is no lock-order to violate (no `close()`-under-`pool_lock`
  deadlock → EMFILE), and the per-channel lock is uncontended (the cockpit is a pure reader).
  *(This removes cockpit-internal races; the external writer's tear is still handled by §3.2's
  atomic verdict read.)*
- **A separate dedicated thread for the stop handshake** (bounded `await_consumed`) — so a
  data-plane stall can't starve `stop`. **Never Textual's default (`None`) executor** for either
  (its shared 32-thread pool couples fold I/O + stop I/O and lets one NFS wedge kill the stop key).
- **DataTable is imperative — there is no reactive "diff a snapshot in".** The loop: the owner
  thread computes the fresh immutable `Table` → `post_message(TableReady)` (thread-safe; **never**
  assign a reactive from a worker thread) → the **main thread** does an explicit `run_id`-keyed
  reconcile inside `batch_update()`: `remove_row(gone)`; `update_cell(run_id, col, …)` for changed
  cells; `add_row(*cells, key=run_id)` for new; then `sort()` + `move_cursor` onto the selected
  `run_id`. **Never `clear()`+repopulate** (it resets cursor/scroll). Self-reschedule the next
  tick *after* completion (not raw `set_interval`) so slow ticks don't pile up.
- **Selection identity** falls out of the stable `RowKey`: a vanished `run_id` → the detail pane
  shows `missing`, never a rebind.
- **Invariant (corrected):** DataTable content is a *pure function of the latest `Table`,
  reconciled by `run_id`*; only cursor/scroll/filter are authored in the widget. (Not "no widget
  holds derived data" — it necessarily does; the point is it is derived, never authored.)

## 14. Concrete types & signatures (plan-readiness)

```python
Severity = IntEnum("Severity", "INFO MEDIUM HIGH")          # int-valued: row badge = max(...)
IssueKind = Enum(... "Torn", "SkewSuspected", "UnsafeStop")
@dataclass(frozen=True)
class Issue: kind: IssueKind; severity: Severity; message: str; seq: int | None = None; detail: str | None = None

# Status is the open coproduct of §4; the terminal arm wraps runstate.Outcome directly.
# Non-terminal: PENDING/LIVE/STALE; integrity: MISSING/UNREADABLE/CONFLICTED. Every member
# is self-describing: (label: str, severity: Severity). Unknown → render honestly; unrenderable → raise.

@dataclass(frozen=True)                                     # frozen + value-equality: the singleton test's ==
class Row:
    status: Status
    frontier: int | None                                    # progress()
    freshness: float | None                                 # age = max(0, now - last_activity)
    value: tuple[str, object, int | None] | None            # (name, scalar, step) via latest(VALUE, name=objective)
    elapsed: float | None                                   # now - first started.t; None if no started (render "—")
    episode: str | None                                     # latest_episode handle (PURE; NOT live_episode -> os.kill, §2.1)
    undischarged_stops: list[Envelope]                      # drill-down
    live_demand: list[Envelope]                             # drill-down (cheap)
    issues: list[Issue]

RunRef = tuple[str, str, str]                               # (run_id, root, backend) — open_channel needs all three
Resolver = Callable[[float], list[RunRef]]                  # Time -> IndexSet; const([r]) is the singleton
```

### 14.1 The fold signature and the `Env` seam

`status_fold(channel: Channel, env: Env) -> Row`, where `now` is captured **once per frame** and
threaded to every freshness/elapsed sub-fold. `Env` is the injected presentation policy —
`clock`, `objective` (the value metric, group-level default), `stuck_threshold`, and `liveness`
(§14.2). The "consistent snapshot" is realized *inside* the fold as the atomic verdict read
(§3.2), not a materialized object — so §1/§2's "pure functor over a fixed snapshot" is
bracket-approximated for the verdict subset and cosmetically tolerant elsewhere, no upstream
dependency.

### 14.2 The committed liveness seam (implementation deferred, §2.1)

```python
class LivenessSignal(Protocol):
    def liveness(self, channel: Channel, env: Env) -> Liveness | None: ...   # None = "I have no signal here"
Liveness = Enum(... "LIVE", "STALE", "DEAD")               # an ordered authority; probes may assert DEAD
```
`env.liveness` is a *composition* of signals with a defined precedence; the **core registers only
`FreshnessSignal`** (LIVE/STALE from the log). The §4.1 lattice reconciles over the abstract
`Liveness`, never over `os.kill` — so an overlay signal (`HandleProbe` same-host; `SlurmProbe`/
`K8sProbe` cross-host, batched per group, keyed off the deployment context the **resolver**
supplies via the handle/launcher vocabulary) slots in without touching the fold. `Status`'s open
coproduct absorbs the overlay's new members (`presumed_dead`, probe-`conflicted`) additively.

## Public observables consumed

`peek_terminal`, `progress`, `last_activity`, `live_episode`, `latest_episode`, `live_demand`,
`undischarged_stops`, `latest(Topic.VALUE, name=objective)`, and the first `lifecycle.started.t`
(elapsed) — plus `open_channel`, `Watcher`, `await_consumed`. `value_series` is deliberately
**unused** (no data-plane replay).
