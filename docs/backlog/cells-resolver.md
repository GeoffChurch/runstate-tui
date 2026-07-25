# `cells` resolver — the mycooc experiment/cell layout adapter

A deferred layout adapter (a `Resolver`) for the mycooc experiment/cell layout — the third on-disk
shape the cockpit observes, after `explicit`/`const` and `glob`. It drops onto the existing
`MultiRunApp` / resolver seams and the grouped-table rendering; no fold or table changes.

## What it is

The `explicit` / `glob` / `cells` trichotomy maps to the three real on-disk layouts runstate
observes. `glob` = flat `runs/*.db` (the translation workload). **`cells` = the mycooc
experiment/cell layout:** an experiment is a set of **cells** (variants), each a thin dir
(`outputs/experiments/<exp>/<cell>/`) holding a **pointer** (`.run_id` → current rid) into a
content-addressed run home (`runs/<rid[:2]>/<rid>/`). The resolver walks an experiment's cell
pointers to each cell's current run. A `runstate.sweep` produces exactly such an experiment of cells.

## Shape of the resolver

- **Enumeration is a manifest cross-product, not a tree walk.** The experiment spec gives
  `scenarios × variants (× seeds)`; the resolver *constructs* each cell path from the spec — it does
  not walk a directory of cells. It needs the spec, not just the tree.
- **Labels and groups come from the resolver, not the path.** A cell's display name (variant) and
  group (scenario) are resolver-supplied `attrs` on the `(RunRef, attrs)` record — never derived by
  `disambiguate` on `RunRef.root`. `root` only has to *open the channel*, so a shared run home yields
  one `RunRef` behind two cell rows. This is the relational-grouping model the table already renders;
  `cells` is its first non-trivial producer.

## Not to be confused with mycooc's `--status`

"Show cells like `--status`" is a *superset* of this resolver, most of which is out of scope.
`--status` is a scenario × variant grid whose substance is a **data-plane metric table** (P@1 /
cos_P@1 / NGMR / NHMR at the best-P@1 step, best-in-column highlighting) plus **facet grouping** (by
scenario) and a **seed-aggregation reduction** (`mean ± std`). The control-plane skeleton — status
counts, ETA, liveness — is already the cockpit's status column + fleet strip; the missing half is the
data plane, deliberately out of scope (runstate's separate visualization project + the deferred
[`metric-discovery`](metric-discovery.md)). So the resolver is small; making the cockpit *look like*
`--status` is the deferred data plane, not this.

## Why it's gated (not merely unbuilt)

runstate deliberately provides **no** cell/enumeration API — the app, not runstate, owns the layout
adapters, and a `list_runs()` capability was refuted upstream. So a `cells` resolver hard-codes
mycooc's pointer layout in the TUI, and that layout is itself still settling (the cell/run split is
under deliberation in runstate's backlog). **Build it on a concrete need to dashboard a live mycooc
sweep, with a real fixture to test against — otherwise defer (YAGNI).**

## Related deferred work

- **Grouping remainders** — the table sections by one attribute today. Still open: multi-attr /
  runtime pivot (group by a tuple, or re-pivot without restart), and a labelled bucket for a run
  *missing* the group attr (it renders under an unnamed `── ──` section for now).
- **Corruption-invisibility full-scan** — an opt-in, marked-expensive integrity scan; a per-run
  drill-down action, not a hot-path fold concern.
- **`conflicted`** — a liveness-overlay feature (see [`liveness-overlay`](liveness-overlay.md)); needs
  `resolve()` / `launcher.terminated` probe corroboration, not a fold change.
- **`--follow-symlinks` / arbitrary `--glob PATTERN`** — additive glob-resolver options (runs inside
  symlinked dirs; power-user patterns), off by default for cycle safety.
