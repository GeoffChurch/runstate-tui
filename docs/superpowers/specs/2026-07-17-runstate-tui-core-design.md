# runstate-tui — core design: scope & sequencing

*Design doc. 2026-07-17. Supersedes the framing in `README.md` where they differ (see
§9 "Inversion").*

## 0. Purpose

A **control-plane cockpit** for [runstate](https://github.com/GeoffChurch/runstate) runs:
a terminal UI that answers *"what is happening / what happened"* across groups of runs and
lets you act on one. **No scientific data-plane plots/curves** (the ban is scoped to trajectory
reconstruction, *not* to a run's current scalar value — see §10). It works on a cold log — no
daemon, no server, no instrumentation — because the log already holds everything it renders.

This doc settles the two questions the README left open: **what is the unassailable core, and
in what order is it built.** The core is the *initial object* of the design (§1). The build
order puts the fold first by data-flow necessity; single-run-before-table is then a risk-first
product choice, not a theorem (§6, §9).

### The one rule (unchanged, but reclassified)

> **Use only runstate's public API. Every time you can't, that's a finding.**

A "finding" is a **development-time, dogfooding output** — a suggestion to improve runstate —
**not** a runtime feature of the cockpit. When the public API can't answer cleanly, we file it
against runstate and reduce cockpit scope to route around it; the running cockpit renders only
what the public API cleanly gives (API gaps never reach the UI — see §5). This rule has teeth:
runstate's first consumer, mycooc, already **broke** it — it reads channels via raw
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
  the drill-down factors and the issue channel:
  `Row = status × frontier × freshness × value × elapsed × episode × undischarged_stops × live_demand × issues`.
  `status_fold` is the unique mediating map `Snapshot × Clock → Row` (**one** consistent per-row
  read drives every sub-fold — see §3, snapshot consistency). **This is the atom.**
- **Across runs**, the table is the **colimit** (disjoint union) of rows over an index set the
  resolver builds, re-taken each tick: `Table(g) = ∐_{i ∈ resolve(g)} Row_i`, then
  ordered/filtered.
- **The single-run view is `Table` at the singleton explicit resolver** — `resolve = const [r]`.
  It is the *base case* of the table, not a sibling of it.
- **Drill-down** is `Row` with a **finer codomain** — expose more factors (episodes, demand, the
  raw envelope tail) of the *same* fold. No separate data path.
- **Stop** is an effectful arrow attached to the run *object* — orthogonal to observation.

**Initiality claim.** `status_fold` of one run is the object both extensions attach to — the
table by colimit over the resolver, the drill-down by codomain refinement — and evaluating at
the singleton diagram recovers it. It cannot be factored away.

**What initiality does and does not force.** It forces **Stage 0 (the fold) first** in the
data-flow, and forces table/drill-down to sit *after* it. It is **neutral** between shipping the
single-run view first and the table first — those are siblings on the same fold. Single-run-first
is justified in §9 as a product/risk choice, not as a theorem. View/selection state (which run
is focused, scroll, filter) is **not** a factor of `Row` and **not** a fifth algebraic seam — it
lives in the cockpit (§13).

**Consequence for users.** The physicist's one big run is *literally the base case* of the ML
researcher's sweep. You cannot build the sweep view without the atom, and serving the physicist
de-risks the ML case for free.

## 2. Seams on the algebraic fault lines

Module boundaries are drawn where the ambient category changes — that is where correctness
lives and where the abstraction would otherwise leak.

| region | algebra | module |
|---|---|---|
| observation | pure functor (`Snapshot×Clock → Row`), cached | status fold |
| control | effectful (Kleisli); safety is a predicate we don't own | `stop` arrow |
| time | ambient environment (injected clock), **defensive** | clock seam |
| resolution | diagram over time (`Time → IndexSet`) | resolver |

Forcing control or time *into* the fold would hide these seams, not remove them — and they'd
resurface as bugs in the one place that matters most (was a stop *served* or *armed for the next
episode?*). So: **hard uniformity law on the pure half; honest, explicitly-typed arrows across
the seams.**

The **clock seam is defensive, not merely injected**: freshness is computed against the
emitter's *self-stamped* `t` (runstate's observer clock is emitter-side; skew-immune
arrival-time lives only in the stateful `Watcher`, a boundary the stateless core deliberately
does not cross). So the fold caps age at ≥ 0 and downgrades `stale` to a *hint* (§4).

## 3. The keystone: surface every possible issue

**Every arrow turns its failure modes into a visible, typed issue on the relevant row — never a
crash, never a silent swallow.** This is what makes the tool *reach-for-able*: a physicist won't
trust a tool that might quietly show a stale or reconciled-away-false state, but will trust one
that says `⚠ log torn at seq 4012`. Surfacing uncertainty *is* the feature.

**The defense boundary is the channel open+read, not the observable.** The canonical torn case
does **not** raise `MalformedRecordError` — that type only wraps `cls(**body)` violations on an
*already-decoded* body (`observables.py`); the real `json.loads` is a layer below, in
`channel.read()/latest()`, and `open_channel` runs `PRAGMA` + `executescript(_SCHEMA)` at open.
So the real failure modes are:

- byte/DB tear → `json.JSONDecodeError` / `sqlite3.DatabaseError` from inside the channel;
- corrupt/foreign/interrupted `.db` → `sqlite3.DatabaseError` at *open* (below every sub-fold);
- a foreign *valid* sqlite db → silently schema-mutated by `executescript`, then rendered empty.

Therefore each row's read is wrapped **once, at the open+read boundary**, catching
`(sqlite3.DatabaseError, sqlite3.OperationalError, PermissionError, OSError,
json.JSONDecodeError, MalformedRecordError)`. A per-observable `try` alone is insufficient.

- `Row = ∏ observables × issues:[Issue]`, and each row carries a **severity spanning both
  channels** (status *and* issues), used to sort/color the table: **high** — `UnsafeStop`,
  `unreadable` (control/substrate); **medium** — `conflicted`, `Torn`, `SkewSuspected`;
  **informational** — `pending`, `missing`. Encode preattentively — hue capped at ≤4
  discriminable levels **plus a redundant non-color channel** (glyph) for colorblind
  accessibility. Ranking is orthogonal to "never swallow" — nothing is hidden, only ordered.
- The **table** shows the max-severity badge; the **drill-down** enumerates all issues.

**The integrity set is bounded but not closed.** It is the failure modes of {open, observe,
resolve, control} over runstate's real surface — a finite set, but **treat it as open to new
members** as real substrate errors surface; do not assume the list is exhaustive. It splits
across **two channels**:

*Surfaced as the `status` verdict itself* (no clean lifecycle verdict is possible):

- `pending` — an existing run with **zero interpretable records**
  (`peek_terminal/progress/last_activity` all `None`): the normal first-moments-after-open
  state, and the foreign-empty-db case. Guards the freshness subtraction so `now − None` can
  never crash or mislabel a just-born run as `stuck`.
- `missing` — resolver globbed a pointer whose run is gone/GC'd; nothing to open.
- `unreadable` — the file exists but open/read raised a substrate error (corrupt/foreign db); no
  verdict is derivable. Carries the caught error's detail.
- `conflicted` — observables contradict with **no defined precedence**. Where a precedence
  exists (the observer clock settles live-vs-dead) the fold reconciles silently; only a genuine
  no-precedence contradiction surfaces as `conflicted` — and only over a *consistent snapshot*
  (below), so it means a real contradiction, not a torn read.

*Surfaced in the `issues` list, co-existing with a real verdict* (a row can be `live` **and**
carry these):

- `Torn` — a byte/DB/schema-level bad record while the run is otherwise readable
  (`⚠ log torn at seq N`).
- `SkewSuspected` — any dated `t` exceeds observer-now (forward clock skew); freshness is then
  unverifiable and `stale` must not read as `live` (§4).
- `UnsafeStop` — a stop was sent but the API can't confirm served-vs-armed (§6, §8).

**Snapshot consistency.** If each observable reads the *live* channel independently, a writer
advancing mid-fold can produce a `Row` mixing pre-/post-event states (a false `conflicted`). The
fold takes **one point-in-time read per row per frame** and drives every sub-fold from that
immutable slice — restoring the §2 "pure functor over a fixed snapshot" property. A naive full
`read()` snapshot reintroduces an O(N) refold (§10), so this needs a small upstream ask: a
`max_seq=`/`before=` read bound (mirror the existing `after=`); until then, bracket with a
`last_seq()` consistency check. Filed in §8.

**Alarm flood is a Stage-4 concern.** At table scale a correlated failure (a shared-FS hiccup
across 300+ runs) paints hundreds of identical badges — an alarm flood that desensitizes the
operator and defeats the trust this section builds. Collapsing N same-type issues in one tick
into a single super-issue (`⚠ 340 runs torn — likely systemic`) is a **named Stage-4 acceptance
criterion** (§6, §11), following ISA-18.2 flood suppression. "Never swallow" is preserved:
drill-down still enumerates all N. The Stage-0–2 core (|I|=1) cannot flood, so this does not
block the core.

## 4. Status codomain

The reconciled verdict is a single enum, rich enough to name the integrity states:

```
Status ∈ { pending, live, done, errored, killed, stale, missing, unreadable, conflicted }
```

- `live / done / errored / killed` — reconciled from `peek_terminal` + liveness. *(The full
  precedence lattice — terminal verdict vs. a still-live handle, killed-vs-errored ordering — is
  the most under-specified part of the core; enumerate it before Stage 1. §12.)*
- `stale` — **a freshness hint, not a hard liveness verdict.** Freshness = `now − last_activity`
  over the emitter's self-stamped `t`; forward skew can make a hung worker look fresh. The fold
  **caps age at ≥ 0** (a negative age never reads `live`) and raises `SkewSuspected` when a dated
  `t` exceeds observer-now. Skew-immune liveness would require the stateful Watcher's
  arrival-time — out of the stateless core's scope by design.
- `pending`, `missing`, `unreadable`, `conflicted` — see §3.

`issues:[Issue]` (severity-ranked) rides *alongside* status.

## 5. In the runtime framework vs out

- **In (runtime):** observation/data-integrity issues (§3) — including substrate open/read
  errors (`Unreadable`, `Torn`). Properties of the *observed data*; they belong in the UI.
- **Out (dev-time dogfooding):** API gaps (`no run_epoch primitive`, `open_channel manufactures
  a phantom`, `no create=False open`). Filed against runstate (§8), never rendered.

## 6. The sequence

**Stage 0 (the fold) is forced first by the data-flow** (§1). Single-run-before-table is then a
risk-first product choice (§9), not a mathematical necessity — so the ordering below past Stage 0
is a *decision*, not a theorem.

- **0 · Defended fold → `Row`** (no UI). Sub-folds for the **truth-quintet** — verdict
  (`peek_terminal`), frontier (`progress`), freshness (`last_activity` + injected clock, capped
  ≥ 0), **value** (`channel.latest(Topic.VALUE)`, O(1)), **elapsed** (age since the first
  `lifecycle.started.t`, one indexed read) — plus severity-ranked `issues`, all over **one
  consistent snapshot**. Defended at the open+read boundary. Pure, cached, tested against a
  hand-built log **including a byte-torn record *and* a corrupt/foreign db** (§11). **The initial
  object.**
- **1 · Single-run view = render `Row` through the singleton-table path.**
  `runstate-tui <run>`: stat-before-open (missing ⇒ `missing`; open/read error ⇒ `unreadable`),
  fold **in a worker thread** (I/O never on the render thread — §13), render, 1 Hz. **The
  physicist's complete tool.** Acceptance = the **singleton test**: it literally *is*
  `table(const[run])`, no bespoke screen.
- **2 · Attach `stop`** — the one effectful arrow, with a **confirm-before-stop gate** (modal /
  type-the-run-id; stopping a live scientific worker is irreversible). `request_id =
  "webui:<unique>"`, confirm via `await_consumed` **off the render thread** (the handshake
  blocks, default `timeout=None`); unprovable safety → an `UnsafeStop` issue (the first real
  dogfooding pressure on runstate). The physicist can now act, not just watch.
- **3 · Drill-down = codomain refinement.** Same `Row`, more factors: episodes, undischarged
  stops, live demand, full issue list, raw envelope tail. No new data path.
- **4 · Index by resolver → the table.** `resolve` goes non-trivial (`glob`/`cells`/`explicit`,
  `Time → IndexSet`, re-resolved per tick); `map(fold)`; the **LRU channel pool** (EMFILE at ~340
  open runs — §10); **selection pinned to `run_id`** across re-resolution (a GC'd selected run
  shows `missing`, never rebinds the detail pane); **issue-flood aggregation** (§3). **Safe
  globbing depends on the `create=False` upstream fix** (§8) — until it ships, an explicit glob
  over untrusted dirs can hit the foreign-db mutation; the Stage-1 explicit-run core is lower
  risk because the user names a real run. **The ML researcher's tool** — Stage 1's view at
  `|I|>1`, no new rendering model.
- **5+ · Deferred, demand-driven** — `run_epoch`, postgres resolver, more control actions,
  progress-rate micro-trend (§10). Pulled in *when first touched*.

## 7. Scope of the core

**Core = Stages 0–2.** Concretely:

- **Observables (v0), the truth-quintet:** verdict, frontier, freshness, **value**, **elapsed**.
  Verdict/frontier/freshness answer "alive / progressing / stuck / done"; value shows
  `loss=0.0034 @ step 4012` (the scientific payload, O(1), no plot); elapsed answers "how long
  has it run" — the one column *every* fleet tool and tracker has.
- **Issues from day one (severity-ranked):** `Torn`, `Unreadable`, `Conflicted`, `SkewSuspected`,
  `UnsafeStop`; `pending`/`missing` as statuses.
- **One action:** stop, with a confirm gate.
- **One view:** single run, rendered through the singleton-table path.
- **Interactive commitments (part of the core):** Textual (§13); fold + stop handshake off the
  render thread; a headless TUI test harness.

Explicitly **out of the core:** the table (Stage 4), drill-down richness (Stage 3), multiple
resolvers, scientific data-plane curves (forever), issue-flood aggregation (Stage 4), API-gap
surfacing (dev-only).

## 8. Layering & upstreaming

Decision rule:

- **Upstream into runstate** iff a *policy-free fact about a run* that any observer needs and the
  public API currently can't give — i.e. a **finding**.
- **Sibling library** iff *general observer machinery* encoding consumer-side policy runstate
  deliberately excludes.
- **Stays in the cockpit** iff *presentation/product opinion*.

**Findings to file upstream (do not hoard workarounds):**

- **`create=False` / read-only open — HIGH; gates safe Stage-4 globbing.** `open_channel` runs
  `executescript(_SCHEMA)` at open, so it (a) crashes the frame on a corrupt/foreign db via
  `sqlite3.DatabaseError` below every sub-fold, and (b) **silently schema-mutates a foreign valid
  db** and renders it empty. Only `create=False` prevents (b) with the public API — a defended
  open catches the crash but cannot un-mutate. That this is real, not hypothetical: mycooc
  bypasses `open_channel` with raw `?mode=ro` (`run_experiment.py:2394`) for exactly this reason.
- **`max_seq=`/`before=` read bound** — for a bounded consistent-cut snapshot (§3) without an
  O(N) full read. Mirrors the existing `after=`.
- **Safe-stop predicate** — "served now, or armed for next episode?" is policy-free and needed by
  any orchestrator; the cockpit is the forcing second consumer. (`await_consumed` answers *was my
  stop consumed*, not *will a stop now be served or armed* — the predicate is still missing.)
- **`run_epoch`** — policy-free twin of `last_activity`. Elapsed is *derivable today* (first
  `lifecycle.started.t`), but `run_epoch` would remove the per-episode summation the derivation
  can't cleanly do.
- **Total observation** — a total `Log → Value + Issue` observer surface, widened beyond
  `MalformedRecordError` to the substrate byte/DB errors (`JSONDecodeError`,
  `sqlite3.DatabaseError`) that escape below the observable. (Caveat: runstate may prefer raising
  and call presentation the observer's job — file and discuss.)

**The observer core does NOT go upstream** — principled: if runstate absorbed the
fold+pool+resolver it would be testing *itself*, destroying the acceptance-test property the
cockpit exists to provide.

**Sibling library — later, on a real second consumer.** The headless observer core (resolver,
defended status fold → `Row`, LRU pool, issue aggregation) is general to any UI (TUI, web, Slack
bot, a CI gate, a `--status` replacement) but is consumer-side policy. runstate's own record
already shows the demand: mycooc hand-rolled `channel_read.py` as its "single read seam," and
runstate's post-migration audit flags F5–F8 as *"every consumer reimplements this"* primitive
gaps. The `request_id="webui:<…>"` convention and the `/remote-control` context point at a web
UI as the second consumer. Therefore: **design the boundary now, split the package later.** Keep
runstate-tui one repo with three cleanly importable units (`resolver | fold+pool | TUI`) — such
that fold+pool+resolver *could* be `pip install`-ed alone — but don't pay the
versioning/release/docs cost until the web UI consumes it. Adopting Textual (§13) is a live test
of this boundary: the core must never import it.

**Coordinate with runstate's `cli-status` backlog** — a per-run `runstate status` CLI is already
a runstate backlog item; it overlaps our Stage-1 single-run view. Decide whether the cockpit's
fold *is* that CLI's implementation (the fold as the shared seam) rather than a parallel one.

**Stays in the cockpit:** verdict labels and the "stuck" threshold, sort/filter, drill-down
layout, keybindings, stop UX (incl. the confirm gate), 1 Hz cadence, selection/view state, the
value metric-selection & formatting policy — and the human **labels** (a name is a *layout*
artifact, not runstate's concern).

## 9. Inversion (vs the README)

The README leads with the **table** as hero and treats a single run as a drill-down. This design
**demotes the table to Stage 4** and makes the **single-run atom the core**. The algebra is
*neutral* on this ordering (§1); the justification is **risk-first product judgment**, not a
theorem:

- land an unassailable core first — the smallest *complete* increment is one run rendered well;
- the physicist is a first-class user whose complete tool that increment already is;
- serving the physicist de-risks the ML case (same fold) before adding resolver + pool complexity.

The unassailable core (Stages 0–2) is *complete for the physicist before the ML researcher's
table exists*.

## 10. Scale constraints (measured; respect these)

- **~54 µs/run** is the **warm-channel** cost of the indexed truth-quintet (`peek_terminal`,
  `progress`, `last_activity` via `latest()` seeks; `value` via `latest(VALUE)`; `elapsed` via
  one indexed `started` read) → a 100-run group ≈ **5 ms/frame**, free at 1 Hz. Budget the
  **cold-open (~108 µs) + LRU-reopen churn** separately for groups larger than the pool cap.
- **`live_demand` is cheap** — it reads only its 4 routed topics (index-served on every backend),
  so it can sit in the per-frame Row or in drill-down freely.
- **A `SqliteChannel` holds 3 fds** (db + `-wal` + `-shm`) → a naive viewer EMFILEs at ~340 open
  runs. **The LRU pool is not optional** (Stage 4, owned by the fold+pool unit). The pool cap
  must sit below the fd ceiling, so groups above the cap reopen evicted channels each frame —
  hence the cold-open budget above.
- **No per-frame refolds of the data plane.** The O(N) exclusion is `value_series` (a
  10⁶-envelope replay is ~1.9 s) — that stays out forever. This is a *narrower* claim than "no
  plots": a last-value peek and a control-plane progress-rate micro-trend (a ring buffer over the
  already-sampled frontier, zero replay) are both cheap and are **not** data-plane replays. The
  rate micro-trend is a Stage-3 candidate, not a core item.
- **I/O off the render thread.** `SqliteChannel` opens block (no-timeout connect,
  `busy_timeout=5000`, a 20× WAL-birth retry, and NFS `-shm` can wedge in D-state), and the
  `await_consumed` stop handshake blocks (`timeout=None`). A single slow open must never freeze
  the 1 Hz frame or the `stop` key (§13).

## 11. Acceptance criteria

- **Singleton test:** the single-run view is `table(const[run])` through one code path — no
  bespoke single-run screen. (Initiality, made falsifiable.)
- **Never crash / never swallow:** every failure mode of {open, observe, resolve, control}
  renders as a typed issue/status on the relevant row. The fixture must include a **byte-torn
  record (`JSONDecodeError`)** *and* a **corrupt/foreign db (`sqlite3.DatabaseError`)** — a
  `MalformedRecordError`-only fixture would miss the real crash.
- **No phantom, no mutation:** resolving a missing pointer yields `missing`; an unreadable one
  yields `unreadable`; the core never fabricates or schema-mutates a `.db` (the residual
  foreign-valid-db mutation is gated on the `create=False` upstream fix, §8).
- **Public-API-only:** no raw `sqlite3`, no `?mode=ro` side-doors (unlike mycooc's workaround),
  no `_`-prefixed calls. Each place the public API can't answer is filed upstream (§8).
- **Freshness never lies bright:** a negative age never renders `live`; forward skew raises
  `SkewSuspected`.
- **Interactive:** a keypress→stop dispatches with the right `request_id`; a torn-log fixture
  renders its severity badge in a **real frame** without crashing; resize reflows; a tick with an
  artificially slow open keeps input and the `stop` key responsive; the confirm-before-stop gate
  fires.
- **Textual invariant:** no widget holds derived data — a `Row` is always a fresh fold from the
  log; only view/selection state lives in widgets (§13).
- **Selection identity:** drilling into a run then GC-ing it shows `missing` in the detail pane,
  never another run's tail.
- **The real bar:** you reach for it, unprompted, instead of a workload's `--status`.

## 12. Open questions / deferred (decide at first touch)

- **Verdict precedence lattice** — enumerate the full reconciliation order (terminal vs.
  still-live handle, killed vs. errored) before Stage 1. Most under-specified part of the core.
- **Safe stop** — the served-vs-armed predicate; until it ships, surface `UnsafeStop`.
- **`create=False` open** — defended open + `unreadable` now; gates safe Stage-4 globbing (§8).
- **`max_seq=`/`before=` read bound** — for the consistent snapshot (§3, §8).
- **`run_epoch`** — elapsed derivable today; promote for clean per-episode runtime.
- **Total observation** — file the widened `Log → Value + Issue` finding; keep the in-tree
  defended open+read until/unless runstate makes observation total.
- **Value metric-selection policy** — a nameless `latest(VALUE)` returns whichever metric wrote
  last; the cockpit needs an objective-selection + formatting choice (default: latest-written,
  configurable).

### Known gaps (resolve before/at the relevant stage)

- **Stop authorization/ownership** — anyone who can run the TUI can stop any resolved run; no auth
  model. (Before Stage 2 ships beyond a trusted single operator.)
- **Cockpit cold-start** — if freshness ever goes stateful (arrival-time), restart loses it;
  define the restart semantics.
- **Resolver glob grammar & zero-match** — the pattern language, run-root discovery, and
  zero-match vs. mis-scoped-directory behavior. (Stage 4.)
- **Terminal-environment matrix** — SSH/tmux/mosh, `NO_COLOR`/limited-color, narrow widths,
  non-UTF-8 badge glyphs. (Stage 1.)
- **Injected-clock source & timezone** — monotonic vs. wall, display tz, host NTP steps.
- **Integration test vs a real concurrent writer** — the only way to catch read-skew /
  lock-contention regressions (§3 snapshot).
- **Cockpit self-observability** — logging/diagnosis of the cockpit's *own* systematic failures
  (a per-row `unreadable` is not a story for cockpit-wide malfunction).

## 13. The TUI unit (framework & concurrency)

The interactive layer is genuinely net-new (mycooc's two monitors are non-interactive one-shot
prints), so it is committed here rather than left implicit.

**Architecture: a pure-projection core + a thin MVU/reactive shell.** The data core (resolver →
fold → pool → `Row`/`Table`) is *not* MVU — it is a pure projection re-derived from the cold log
each tick (spiritually immediate-mode). The interactive shell *is* MVU-flavored:
`(ViewState, keypress) → ViewState`, with `stop` as an effectful `Cmd` that feeds back a
`StopConfirmed`/`UnsafeStop` message. The two meet at one data boundary — the immutable `Table`
snapshot.

**Framework: Textual.** It is reactive/virtual-DOM (not Elm/TEA), but it is the only candidate
(vs. Rich = renderer only, prompt_toolkit = REPL-input-shaped, urwid = manual/dated) that
supplies the three things this design needs: **workers** (`@work`, threaded/async — run the
blocking opens and the `await_consumed` handshake off the render thread), automatic **input +
resize**, and a real **headless test harness** (`run_test()`/Pilot + `pytest-textual-snapshot`).

**The marriage (and the boundary that keeps it clean):**

- each 1 Hz tick (`set_interval`) → a **worker** computes the fresh immutable `Table` → assign to
  a `reactive` attribute → a `watch_` handler diffs it into a `DataTable`;
- **selection/view state lives in Textual, keyed by `run_id`**, reconciled against each new
  snapshot (a vanished run → `missing`, never a rebind);
- **the core never imports Textual** (§8 boundary); Textual is one renderer of the same `Table`
  the future web UI will render;
- **invariant:** derived data is always a fresh fold — never mutated in a widget; only view state
  is retained. This preserves "the cold log holds everything it renders" and the
  singleton/never-crash guarantees.

## Public observables consumed

`peek_terminal`, `progress`, `last_activity`, `live_episode`, `latest_episode`, `live_demand`,
`undischarged_stops`, `channel.latest(Topic.VALUE)` (the current scalar), and the first
`lifecycle.started.t` (elapsed) — plus `open_channel`, `Watcher`, `await_consumed`.
`value_series` is deliberately **unused** (no data-plane replay).
