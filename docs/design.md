# runstate-tui — design & philosophy

Why the cockpit is shaped the way it is. This is the *living* distillation; the rigorous,
per-feature derivations live in [`docs/superpowers/specs/`](superpowers/specs/) (the categorical
core is `2026-07-17-runstate-tui-core-design.md`), and the deferred work in
[`docs/backlog/`](backlog/index.md).

## The one rule — do not relax it

> **Use only runstate's public API. Every time you can't, that's a finding, not a workaround.**

No raw `sqlite3`, no `?mode=ro` side-doors, no private `_`-prefixed functions. When the public API
can't answer, **file it against runstate and reduce scope to route around it** — then decide whether
runstate is missing a primitive or the cockpit is asking wrong. A "finding" is a *development-time*
output (a suggestion to improve runstate), never a runtime feature; API gaps never reach the UI.

This is why the cockpit is a **sibling** repo that *depends on* runstate rather than a subdirectory:
it consumes runstate exactly as a third party would, which makes it a standing acceptance test for
runstate's observer surface. The rule has teeth — runstate's first consumer, mycooc, already broke
it (reading channels via raw `sqlite3.connect('file:…?mode=ro')` to dodge the open seam); the
cockpit must not.

## The model

A handful of load-bearing ideas force nearly every concrete decision. Each is stated plainly here;
the formal treatment (limits/colimits, the precedence lattice, the seam table) is in the core spec.

- **One factorization, three units.** The whole cockpit is one composition:

  ```
  render = aggregate ∘ map(status_fold) ∘ resolve
  ```

  `resolve` builds the set of runs, `map(status_fold)` turns each into a row, `aggregate`
  orders/filters/sections them. The three modules *are* those three arrows — the boundaries sit
  where the algebra changes, not where it was convenient to split.

- **Single-run is the base case, not a sibling screen.** The single-run view is the table at the
  singleton resolver (`resolve = const [r]`). Serving one run well de-risks the whole sweep on the
  same fold. "Watch live" and "triage a sweep" are the same table under a different filter/sort;
  "inspect one run" is the drill-down, the *same* fold projected to a finer codomain.

- **Two planes: fold vs query — and the discriminator is cost.** Summaries of the whole log
  (status, frontier, freshness, value, elapsed, episodes, stops, demand) are a **fold** — they *are*
  the `Row`, re-derived each tick from held state. A windowed slice of the raw envelope sequence (the
  log pane) is a **query**, parameterized by interactive view state and not a `Row` factor. The line
  between them is **what a read costs per tick**: O(new records) through a held fold, or an index
  seek, is a fold; anything that walks the log or the lineage per tick is a query, and queries are
  barred from the per-frame path (see Scale). *Parameter-free* is the wrong discriminator — the value
  factor takes an objective and is still a fold — and a read is not a fold merely because it looks
  like a point lookup: see the `latest(VALUE, name=…)` case under Scale, which under 0.3.0 walked a
  topic partition on a miss.

- **One operational law: poll a cheap watermark, apply the delta, never rebuild.** It shows up in
  five places — the stop handshake, the log pane, the fold (runstate's held folds, each reading only
  the records since its last poll), the resolver→table run-set delta, and the keyed DataTable
  reconcile. Nothing rebuilds from scratch.

- **The verdict is an open coproduct that wraps runstate's verdict directly.** A terminal status
  carries runstate's `outcomes` (a set: several death reports may disagree, and runstate chooses
  none) and its `tier` (the viewpoint the verdict rests on: the worker's exit, death reports, or an
  unclaimed launch). No hand-maintained translation table (that is where logic drifts, and a future
  `Outcome` member is absorbed for free). Every status member is self-describing (a label + a
  severity), so an unrecognized one renders **honestly** via its own label — never a lossy default;
  a set renders every member (`preempted/killed`), colored by the worst; a member that genuinely
  can't render crashes verbosely.

- **Faithful representation over lossy reuse — a design law.** Never collapse a distinct condition
  into a near category when the reuse is lossy. It governs `corrupt` ≠ `unreadable`, the stop
  `moot` ≠ `unsafe`, and — one level up — **relational, not hierarchical, grouping** (see below): a
  directory tree serializes a relation into one fixed nesting order, so we carry the relation as data
  instead.

## The three units

1. **Resolver** — discovers *which* runs to show and yields a per-row **attribute record**,
   `(RunRef, attrs)` where `attrs: Mapping[str, str]`. It owns the layout adapters — `explicit`,
   `glob` (a live recursive directory walk), `manifest` (a neutral JSON relation), later `cells` /
   `postgres` — and knows nothing about status or UI. A group is a resolver *expression*, re-resolved
   each refresh (a sweep grows; a frozen list is stale on arrival).

   **Labels and groups are formatted from `attrs`, not parsed from paths.** Content-addressed run ids
   are hashes, so the human name and the grouping key are *attributes the resolver supplies*, not
   metadata reverse-engineered out of a filename or a directory tree. Discovery is a plain file/query,
   separate from the per-run observation channels.

2. **Status fold** (`RunRef` → a `Row`: verdict, commits, age, value, elapsed, episode,
   pending stops, live demand, issues) — over runstate's observables and held folds; no UI. **Owns
   the LRU channel pool** (non-negotiable — see Scale), and each pooled channel's folds beside it:
   the folds are state that outlives a tick, so they live with whoever holds the run, and are
   evicted with its channel. Two shared cells map to one `RunRef`, so a pooled channel is folded
   once and reused.

3. **TUI** (table + drill-down + act) — renders rows; sends `control.stop`; holds only view state
   (cursor, scroll, filter). The table content is a pure function of the latest fold, reconciled by
   run key — never authored in the widget.

Data flow, at 1 Hz:
`resolve → [(RunRef, attrs)] → status fold (pooled) → table (ordered / filtered / sectioned)`.

## What it deliberately is — and isn't

It shows only what runstate **uniquely knows** — the terminal verdict and the viewpoint it rests on
(`peek_terminal`), the commit count on the head's lineage (`PrefixLen(Commits())`), freshness
(`last_activity`), the episode boundary, pending stops, live demand — and does the one thing no
metric tracker can: **act** (send `control.stop`, watch it discharge). Heavy scientific plotting stays wandb / MLflow / TensorBoard's job; keeping the cockpit
out of "another tracking tool" is what lets it avoid runstate's data-plane almost entirely.

That separation is a **stance, not a law.** A *lightweight* in-terminal trend — a value or the
progress rate over commits, or one value against another as a trajectory — is a natural fit, gated only
by the scale seam below (cheap sampled trends are free; a full replayed curve is drill-down,
on-demand, one run at a time). Tracked in [`docs/backlog/value-trend.md`](backlog/value-trend.md).

It will **not** replace a workload's own status table and shouldn't try — that is *experiment-aware*
(variants, phases, patience), which are workload opinions the cockpit is forbidden to hold. The
cockpit answers the **run-layer** question across groups and repos; the workload table answers its
**experiment-layer** question inside one. *(The honest risk: if the run layer alone isn't useful
enough to reach for, the cockpit fails — and that failure is itself the finding, that the interesting
state lives in the workload, not the protocol.)*

## Integrity — a bad run is a loud row, never a crash

Every arrow turns its failure modes into a visible, typed issue on the relevant row — never a crash,
never a silent swallow. A physicist won't trust a tool that might quietly show a false state, but
will trust `⚠ log torn at seq 4012`. Surfacing uncertainty *is* the feature.

The defense is three-homed by where a failure can originate: an **open guard** → `unreadable` (a
whole-run substrate fault), or a **format row** when runstate refuses a log in another format
(`format 0.3.0: migrate it (runstate migrate)`, `unversioned`; HIGH, since the cockpit cannot read
the run at all, but an operator action rather than a fault, carrying runstate's own instructions); a **fold-boundary guard** → `corrupt` (a committed, undecodable body — an
atomicity violation, carrying the torn seq); a **per-read guard** → a `malformed` *issue* (a
decodable-but-wrong-shape record, degrading one factor while the real verdict survives). The
records a verdict skipped as unreadable (`RunResult.malformed`) are such an issue too: runstate
reports them rather than raising, so the verdict stands and the signal is flagged. A byte-torn
committed record is a writer/runstate bug, so it is loud (`corrupt`), fail-fast — never a
dismissible ⚠ that a self-heal would retry forever. At table scale a shared-FS hiccup painting
hundreds of identical badges collapses into one super-issue (flood suppression); the drill-down still
enumerates all N.

## Scale constraints (measured on real logs — respect these)

- **A warm row costs ~0.35 ms, whatever the log's size**, plus ~0.36 ms for the pool's re-attach check
  (measured 2026-11-02 on 40 mycooc runs). Each tick polls runstate's held folds, which read only the
  records since the last tick, so a 100-run group is ~70 ms/frame, fine at 1 Hz.
- **A cold row reads the log once.** A run's first poll costs ~54 ms median, ~100 ms p90, for mycooc's
  logs (median 3.6k records; 6,125 logs), ~70% of it the objective's `LastElement`. So a group's first
  frame is O(log) per run, and a group larger than the pool re-folds evicted runs cold every frame:
  9 mycooc experiments exceed the cap, the largest with 904 runs (see
  [`fold-retention`](backlog/fold-retention.md), and the ended-row cache that answers it,
  [#36](https://github.com/GeoffChurch/runstate-tui/issues/36)).
- **Held folds hold ~0.7 MB for a median mycooc run**: `PrefixLen` and `LastElement` keep a per-node
  map, and `LastElement` every value's name. The pool bounds it as it bounds fds.
- **A `SqliteChannel` holds 3 fds** → a naive viewer EMFILEs at ~340 open runs. **The LRU pool is
  not optional.**
- **No per-frame one-shot lineage reads.** `series`, `aligned`, `streams` and the pure
  `prefix_len`/`last_element` walk the lineage; a polled path holds the folds instead (runstate's own
  rule: a polled read's cost is independent of lineage length). A full trajectory pays the walk only
  on demand, in one run's drill-down (see the value-trend roadmap).
- **The value column is a fold, not a peek.** Under 0.3.0 it was `latest(VALUE, name=objective)`,
  which cost ~85 ms per run per tick at 500k value records when the name was absent (runstate#19,
  since index-served upstream). It is now `LastElement(objective)` on the head's lineage: an absent
  name costs O(new records) a tick like any other, and a *new* name costs one cold fold per run.
- **I/O off the render thread, on a single owner thread** that owns the whole pool (reader == evictor
  → no use-after-close, no lock-order to violate); a **dedicated stop thread** so a data-plane stall
  can't starve `stop`. A wedged open degrades to a cockpit-level `⚠ I/O stalled`, never a frozen
  frame or a dead `stop` key.

## Liveness under log format 0.4.0

A claim ends by its own exit or by a death report naming it (`supervisor.died`), and no read probes
a process. So a crash nobody reported reads as **held**: no verdict, only a freshness that ages it to
`stale` past `stuck_threshold`. The cockpit shows exactly that, and writes no death report itself:
it is an observer, and runstate's rule is that a read never writes. A death report, when one lands,
is the verdict, with its `evidence` as the detail (`killed: slurm:TIMEOUT`), and is never dated as
activity, so a reaped run's age is its last record's. Telling "slow" from "dead" before anyone
reports is the [liveness overlay](backlog/liveness-overlay.md)'s job.

A stop is a request about the process, pending until an exit's `honored`, a withdrawal or a refusal
names it. One sent to a claim that then crashed is still pending, and the next claim's first drain
honors it, so the cockpit shows it rather than filtering it as a prior episode's.

## Roadmap — deferred, demand-driven

Each surfaces at a specific first touch; see [`docs/backlog/`](backlog/index.md) for the committed
seams. Highlights:

- **Liveness overlay** (external probes: `os.kill` same-host, `squeue`/`kubectl` cross-host) — the
  core is freshness-only; the overlay seam is committed. Disagreeing death reports are already
  runstate's verdict set; the probe half of `conflicted` (a stranded claim whose process lives on)
  stays here.
- **`cells` resolver** — the mycooc experiment/cell layout adapter; externally gated on mycooc's
  still-settling layout. Drops onto the existing resolver/`MultiRunApp` seams.
- **Value trend / trajectory** — the lightweight in-terminal plot discussed above.
- **Fold retention** — groups larger than the pool re-fold cold every frame under 0.4.0.
- **`run_epoch`, metric-name discovery, animated GIFs** — smaller additive items.
