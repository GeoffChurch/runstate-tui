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
