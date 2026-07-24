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

- **Two planes: fold vs query.** Parameter-free summaries of the whole log (status, frontier,
  freshness, value, elapsed, episodes, stops, demand) are a **fold** — they *are* the `Row`,
  re-derived each tick. A windowed/filtered slice of the raw envelope sequence (the log pane) is a
  **query** parameterized by interactive view state — not a `Row` factor. The fold is the pure core;
  the log query is the reactive shell.

- **One operational law: poll a cheap watermark, apply the delta, never rebuild.** It shows up in
  five places — the stop handshake, the log pane, the fold (re-read only if `last_seq()` moved), the
  resolver→table run-set delta, and the keyed DataTable reconcile. Nothing rebuilds from scratch.

- **The verdict is an open coproduct that wraps runstate's `Outcome` directly.** No hand-maintained
  translation table (that is where logic drifts, and a future `Outcome` member is absorbed for free).
  Every status member is self-describing (a label + a severity), so an unrecognized one renders
  **honestly** via its own label — never a lossy default; a member that genuinely can't render
  crashes verbosely.

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

2. **Status fold** (`RunRef` → a `Row`: verdict, progress, age, value, elapsed, episode count,
   undischarged stops, live demand, issues) — pure over runstate's observables; no UI. **Owns the LRU
   channel pool** (non-negotiable — see Scale). Two shared cells map to one `RunRef`, so a pooled
   channel is folded once and reused.

3. **TUI** (table + drill-down + act) — renders rows; sends `control.stop`; holds only view state
   (cursor, scroll, filter). The table content is a pure function of the latest fold, reconciled by
   run key — never authored in the widget.

Data flow, at 1 Hz:
`resolve → [(RunRef, attrs)] → status fold (pooled) → table (ordered / filtered / sectioned)`.

## What it deliberately is — and isn't

It shows only what runstate **uniquely knows** — the terminal verdict (`peek_terminal`), the step
frontier (`progress`), freshness (`last_activity`), the episode boundary, undischarged stops, live
demand — and does the one thing no metric tracker can: **act** (send `control.stop`, watch it
discharge). Heavy scientific plotting stays wandb / MLflow / TensorBoard's job; keeping the cockpit
out of "another tracking tool" is what lets it avoid runstate's data-plane almost entirely.

That separation is a **stance, not a law.** A *lightweight* in-terminal trend — a value or the
progress rate over steps, or one value against another as a trajectory — is a natural fit, gated only
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
whole-run substrate fault); a **fold-boundary guard** → `corrupt` (a committed, undecodable body — an
atomicity violation, carrying the torn seq); a **per-read guard** → a `malformed` *issue* (a
decodable-but-wrong-shape record, degrading one factor while the real verdict survives). A byte-torn
committed record is a writer/runstate bug, so it is loud (`corrupt`), fail-fast — never a
dismissible ⚠ that a self-heal would retry forever. At table scale a shared-FS hiccup painting
hundreds of identical badges collapses into one super-issue (flood suppression); the drill-down still
enumerates all N.

## Scale constraints (measured on real logs — respect these)

- **~54 µs/run** for a warm status row → a 100-run group ≈ **5 ms/frame**, free at 1 Hz.
- **A `SqliteChannel` holds 3 fds** → a naive viewer EMFILEs at ~340 open runs. **The LRU pool is
  not optional.**
- **No per-frame data-plane refolds.** The O(N) exclusion is `value_series` (~1.9 s at 10⁶
  envelopes). A last-value peek and a zero-replay ring-buffer trend are cheap and *not* replays — a
  full trajectory pays the O(N) cost only on demand, in one run's drill-down (see the value-trend
  roadmap).
- **I/O off the render thread, on a single owner thread** that owns the whole pool (reader == evictor
  → no use-after-close, no lock-order to violate); a **dedicated stop thread** so a data-plane stall
  can't starve `stop`. A wedged open degrades to a cockpit-level `⚠ I/O stalled`, never a frozen
  frame or a dead `stop` key.

## Roadmap — deferred, demand-driven

Each surfaces at a specific first touch; see [`docs/backlog/`](backlog/index.md) for the committed
seams. Highlights:

- **Liveness overlay** (external probes: `os.kill` same-host, `squeue`/`kubectl` cross-host) — the
  core is freshness-only; the overlay seam is committed. Also the home of log-level `conflicted`,
  which needs probe corroboration, not a fold change.
- **`cells` resolver** — the mycooc experiment/cell layout adapter; externally gated on mycooc's
  still-settling layout. Drops onto the shipped resolver/`MultiRunApp` seams.
- **Value trend / trajectory** — the lightweight in-terminal plot discussed above.
- **`run_epoch`, metric-name discovery, animated GIFs** — smaller additive items.

**Shipped since the original design:** read-only open (`open_channel` → `attach_channel` /
`create_channel` split, with stat-before-open collapsed) — the one real upstream ask, now landed;
issue-flood aggregation; relational grouping.
