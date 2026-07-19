# Liveness overlay — external liveness probes

**Status:** deferred (implementation). The **seam is committed now** in the core spec
(`../superpowers/specs/2026-07-17-runstate-tui-core-design.md` §2.1, §14.2), so pulling this in
is additive — no core rewrite. Pull it in at the first same-host run where "is the process
actually alive?" matters beyond freshness, or the first SLURM/k8s group where freshness alone
mislabels a run.

## Why the core defers it

Liveness is a lattice of signals — increasing cost, decreasing generality:

```
freshness (pure fold of the log's timestamps; universal, local or cross-host)
  <  arrival-time (stateful; needs a running Watcher; universal)
  <  external probe (effectful; os.kill same-host, squeue/kubectl cross-host)
  <  heartbeat-semantics (workload-specific)
```

The core commits to **freshness only** — pure, deterministic, topology-invariant, free on the
340-run hot path. Everything richer is effectful and/or deployment-specific and belongs in this
overlay. (This mirrors runstate's own split: the stateless observer plane gives freshness; the
stateful `Watcher` gives arrival-time + handle probe.)

## The probe-backend family

An "external liveness probe" is not one thing — `os.kill` is only the *same-host* instance:

| backend | reach | mechanism | notes |
|---|---|---|---|
| `HandleProbe` | same-host | `os.kill(pid, 0)` on the handle's pid | only meaningful when the worker ran on the cockpit host |
| `SlurmProbe` | cross-host | `squeue`/`sacct` on the run's job id | **batch one query per group per tick**, not per run |
| `K8sProbe` | cross-host | `kubectl get pod` / API on the pod name | same batching discipline |
| `LsfProbe` | cross-host | `bjobs` | " |

The cross-host scheduler probes are the important unlock: real liveness for the common
SLURM/k8s deployments, where `os.kill` from the cockpit host means nothing.

## What it unlocks (additively)

- `presumed_dead` — a probe asserts `DEAD` where the log has no terminal record (the stateless
  core can only say `stale`).
- probe-`conflicted` — a terminal record co-existing with a probe that says the process is alive
  (a genuine contradiction the pure fold cannot see).
- **log-level `conflicted` also belongs here (2026-07-18 red-team verdict).** The §4/§4.1
  triggers — "two live episodes" and "activity strictly after a terminal, no re-start" — look
  like a pure-log seq-ordering check, but a 3-lens adversarial review (verified vs
  `runstate/observables.py`) showed a pure-record check **fires on ordinary crash+relaunch**:
  `live_episode` declares an episode dead via `resolve(handle) is False` with **no**
  `lifecycle.stopped`, so `started → … → started` (no stop between) is the system's *normal*
  recovery shape, and narrowing the read to `lifecycle.*`+`control.*` discards the
  `launcher.terminated` evidence `peek_terminal` already uses. Reliable `conflicted` therefore
  needs this overlay's `resolve()`/`launcher.terminated` corroboration to separate benign
  supersession from genuine split-brain — it is a liveness feature, **not** a fold/seq gap. It
  also needs a prior **product call** on the §4.1 row-3-vs-row-4 tension (does post-terminal
  activity override to `conflicted`, or is a threshold-close straggler benign — a
  `stuck_threshold`-flavored *policy* judgment, not an upstream fact). When built, it is a
  MEDIUM **issue** on the real verdict ("undischarged prior claim — verify"), never a dominating
  status; `StatusKind.CONFLICTED` currently exists at MEDIUM, unused. Judge by **seq, never `t`**.
  Do NOT re-propose the "one seq-aware lifecycle fold" rewrite (the review rejected it: the
  verdict fold is assigned upstream per spec §3.2/§12, `lifecycle.*` includes unbounded
  heartbeats, and §3.1 forbids a single open+all-reads guard).
- These enter the `Status` open coproduct as new self-describing members; the §4.1 precedence
  lattice gains rows **above** the terminal row, reconciled over the abstract `Liveness` value —
  no change to existing rows.

## The seam it plugs into (already in the spec)

- **`env.liveness`** — a composition of `LivenessSignal`s with a defined precedence. The core
  registers only `FreshnessSignal`; this overlay registers the probes.
- **Abstract `Liveness` (LIVE/STALE/DEAD)** — the fold reconciles over this, *never* over
  `os.kill` directly, so a new backend is transparent to the lattice.
- **Deployment context from the resolver** — a SLURM-resolved group carries its `SlurmProbe`
  down through `env`; the run→job-id mapping is parsed from the handle/launcher vocabulary
  (which per runstate's layering lives in `vocabulary/`, not the observable fold).

## Caveats that keep it an overlay, not core

- Effectful (subprocess / API call) — not a pure fold; must not run on the render thread.
- Deployment-specific — a probe only applies where its scheduler/host assumption holds; each
  declares a precondition and returns `None` ("no signal here") otherwise.
- Cost — a `squeue`-per-run-per-frame is unacceptable; **batch one scheduler query per group per
  tick** and index the result by job id.
- Fits §8 "stays in the cockpit" (deployment policy) and runstate's bring-your-own-launcher
  stance (a SLURM launcher example already ships in runstate `examples/submitit/`).

## Representation, severity & residual decisions (settled 2026-07-19)

The 2026-07-18 verdict above left `conflicted`'s *shape* open. Settled now:

- **Don't collapse — render the disagreement, don't fold it into a verdict.** Keep the log-fold
  verdict (the existing `status`) and the probe verdict as two *separate* factors on the `Row`;
  `conflicted` is the derived observation `log_verdict ≠ probe_verdict`, computed at render, never
  a stored third status. This is the R2 fold-vs-query split applied literally (log-fold =
  aggregation, probe = query/overlay), and it is the **least invasive** option: `status`/
  `status_fold` is untouched, no new dominating `StatusKind`, purely additive (one probe-verdict
  field on `Row`).
  - **This dissolves the hard half of the row-3-vs-row-4 call** — there is no "which verdict wins
    the row" to adjudicate; you show both. For a *probed* run the probe is ground truth, so the old
    pure-log "activity-after-terminal: override or benign-straggler (a `stuck_threshold`-flavored
    policy)" heuristic is *superseded*, not needed. The threshold question survives only for
    *un-probed* runs, and that folds into the cadence/scope decision below.
- **Severity = MEDIUM (derived, not chosen).** HIGH = "can't read/trust the substrate at all"
  (unreadable/corrupt/internal-error); MEDIUM = "verdict stands, a signal is flagged" (malformed,
  skew). Conflict is *two valid readings in tension* — nothing failed to read — so it is the
  structural twin of `SKEW_SUSPECTED` (also two valid signals disagreeing), which is MEDIUM.
  Clincher: severity drives `row.severity = max(...)` → the marker glyph, so HIGH would render
  conflict *identically* (`⚠⚠`) to "I literally cannot read this run," conflating a recoverable,
  fully-visible tension with an opaque failure. MEDIUM keeps them honestly distinct (`⚠` vs `⚠⚠`),
  and matches the existing `_STATUS_SEVERITY[CONFLICTED] = MEDIUM`.
- **Direction (zombie vs ghost) rides the badge message, never the severity.** Uniform MEDIUM for
  both; the `Issue.message`/`detail` names which way the contradiction runs — "log terminal, worker
  alive" (zombie) vs "log active, worker gone" (ghost → `presumed_dead`). Severity is the tier; the
  message is the cause (faithful-representation: don't smuggle the direction into the severity
  channel).
- **Structural gotcha — the probe must stay OUT of the status fold.** The probe is a
  channel-reading `LivenessSignal` and slots into the existing `env.liveness` seam — BUT it must
  **not** flow through `resolve_liveness → status` the way `FreshnessSignal` does, or it is folded
  back into the single verdict and "don't collapse" is silently undone. The probe is a separate
  factor *compared against* the status, not another vote *inside* it. (Freshness is a log-clock
  inference → correctly inside status; the probe is an external check → a distinct factor; opposite
  sides of the fold-vs-query line.)

**Deferred to build-time (cost-driven — decide against measurements, not now):**

1. **Probe cadence/scope within §10** — the crux. A live `resolve(handle)` per run per tick is I/O;
   N of them can blow the frame budget or wedge (D-state, like a wedged open). Likely cheapest first
   cut: **drill-down-only** (probe the one selected run, on demand) — sidesteps N-per-tick entirely.
   Alternatives: throttle, or probe only *contradictable* runs (live/stale, or freshly-terminal).
   Cross-host probes still follow the "one scheduler query per group per tick" batching rule above.
2. **Three-valued probe** — when the probe can't decide (measurement ambiguity), read it as "no
   disagreement (trust the log)" or as a distinct "unverified" marker?
3. **Cosmetic** — surface the world-verdict as a standing column always, or only as a badge when it
   disagrees?
