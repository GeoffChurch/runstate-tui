# Multi-run table — remaining deferred work

**Status:** the multi-run table **SHIPPED** (PR #11 `edc40b5`), the **glob resolver SHIPPED**
(PR #16 `e72bbac`), and **issue-flood aggregation SHIPPED** as the fleet summary strip (PR #18
`22fcc68`). The design-of-record lives in the code (`multirun.py`, `pool.py`, `resolver.py`,
`format.py`) plus its specs — `../superpowers/specs/2026-07-18-stage4-multi-run-table-design.md`
(owner-thread pool, keyed reconcile, per-frame frozen clock, `⚠ I/O stalled` watchdog),
`../superpowers/specs/2026-07-21-glob-resolver-design.md` (live directory discovery, minimal-backtrack
labels), and `../superpowers/specs/2026-07-22-flood-summary-strip-design.md` (the always-on legend /
roll-up strip). This doc carries only what is **still deferred** at the table.

Resolved since the old "Stage 4" doc: the **per-frame `now`** concern is closed — `fold_frame` folds
every run under a per-frame frozen clock (`replace(env, clock=lambda: now)`); the legacy
`render_table` per-row resampling is off the live multi-run path. Issue-flood was reframed from
"collapse N badges" into the **additive** summary strip (the table keeps one-row-per-run; the strip
tallies status-partition + non-twin issue tags, worst-first, no threshold).

## The `cells` resolver — the only remaining multi-run feature

`explicit` / `const` / `glob` all shipped; `cells` (a **workload-specific sweep**) is the one resolver
left. The *discovery* is a small `Resolver` (`Time → list[RunRef]`) dropped into the CLI dispatch +
`MultiRunApp` — but two corrections to the old "mechanically trivial, reuses `disambiguate`" framing
(both confirmed 2026-07-23, cross-repo against mycooc's `run_experiment.py` / `channel_read.py`):

- **Enumeration is a manifest cross-product, not a tree walk.** mycooc's `--status` reads the experiment
  YAML and takes `scenarios × variants (× seeds)`, *constructing* each cell path; it does not walk a
  directory of cells. The cells resolver needs the spec, not just the tree.
- **It cannot reuse `disambiguate` for labels.** A cell's `RunRef.root` is the content-addressed run
  home (`runs/<rid[:2]>/<rid>/`, reached by dereferencing the cell's `run` symlink), so `disambiguate`
  would yield hash-garbage labels (`7c/7cfc…`). The resolver must **supply its own label** (the variant)
  — the resolver-declared-provenance seam that the generic-grouping note below also rides.

And "**show cells like mycooc's `--status`**" is a *superset* of this resolver, most of which is **not** a
cells feature: `--status` is a two-level scenario × variant grid whose substance is a **data-plane metric
table** (P@1 / cos_P@1 / NGMR / NHMR at the best-P@1 step, best-in-column highlighting) plus **facet
grouping** (by scenario) and a **seed-aggregation reduction** (`mean ± std`). The control-plane skeleton of
`--status` — status counts, ETA, liveness — is *already* what this cockpit's status column + fleet strip
provide; the missing half is the data plane, deliberately out of the cockpit's scope
(`runstate/docs/backlog/visualization-story.md`'s separate viz project + the deferred `metric-discovery.md`).
So the resolver is small; making the cockpit *look like* `--status` is the deferred data plane, not this.

**What it means** (investigated 2026-07-22, cross-repo — origin: `runstate/docs/backlog/third-party-observer.md`
§3): the `explicit`/`glob`/`cells` trichotomy maps to the three real on-disk layouts runstate observes.
`glob` = flat `runs/*.db` (the *translation* workload); **`cells` = the *mycooc* experiment/cell layout**
— an experiment is a set of **cells** (variants), each a thin dir (`outputs/experiments/<exp>/<cell>/`)
holding a **pointer** (`.run_id` → current-rid) into a content-addressed run home
(`runs/<rid[:2]>/<rid>/`). The resolver walks an experiment's cell-pointers to each cell's current run.
That's the "workload-specific sweep": a `runstate.sweep` produces an experiment of cells.

**Why it's gated** (not merely undefined): runstate deliberately provides **no** cell/enumeration API
— "the app, not runstate, should own the layout adapters"; a `list_runs()` capability was *refuted*
upstream (`runstate/docs/backlog/cockpit.md`). So a `cells` resolver hard-codes mycooc's pointer layout
in the tui, and that layout is itself still settling (Recipe 1 holds for only ~25% of mycooc's cells;
the cell/run split is still being deliberated in runstate's backlog). **Build it only on a concrete
need to dashboard a live mycooc sweep, with a real fixture to test against — otherwise defer (YAGNI).**
See the `runstate-tui-cells-resolver-meaning` session memory.

## Generic grouping (facet sections) — resolver-declared, deferred

A generic **group-by / facet** on the flat table: sort rows by a group key and break them into sections,
each headed by a per-group `format_fleet_summary` roll-up (the strip logic run per-section instead of
once). The single-group case renders exactly as today — purely additive. This is the *only* part of
mycooc's `--status` that generalizes into the cockpit's categories; mycooc domain semantics (seeds,
metric columns) stay out — a seed is a mycooc naming artifact, not a grouping primitive.

**Design decision (2026-07-23): grouping is resolver-declared only — forgo file/path-derived grouping.**
The resolver optionally attaches a structured facet per run; the core never infers a group from the path.
Rationale:

- **The core can canonically derive labels but not groups.** Minimal-backtrack labeling has a *right
  answer* (shortest unique suffix). Grouping is a *coarsening* — "group by which ancestor?" is semantic
  (is the parent the experiment? the host? the date?), with no canonical depth. A core heuristic would be
  guessing, and a guessed grouping is a lossy inference of a structure only the layout-owner knows.
- **No consumer needs core-derived grouping uniquely.** `cells` needs an explicit facet regardless (its
  `root` is the content-addressed home — the `scenario/variant` structure is already gone); `glob` /
  translation are resolvers we own, so emitting the path component as a facet is one line, where the
  layout knowledge already lives.
- **It unifies with a seam `cells` forces anyway.** `cells` must already override the *label* (see the
  resolver section above), so a resolver-declared-provenance seam exists regardless; the group key rides
  it for free — one provenance declaration, two cuts (label = deep suffix, group = shallow prefix).
  File-based grouping would be a second, weaker provenance path bolted alongside.

Rejected: **embedding facet metadata in the channel id** (mycooc's seed-from-name style) and stripping it
transparently — it smuggles structured data through a string when our transport is already rich Python
objects; strictly worse than a structured facet field, and it clutters (or hides state in) the user's ids.

**Gate:** deferred until a *second* grouping consumer creates the pull — the glob multi-root / translation
4-roots view feeling flat. `cells` alone doesn't earn it (mycooc owns its own frontend). Zero-config
folder grouping, if ever wanted, is recoverable as a `--group-by <depth>` opt-in *on the glob resolver*,
not a core default.

## Related deferred findings that surface here (not table features)

- **Corruption-invisibility full-scan** — the deferred opt-in, marked-expensive integrity scan
  (spec §12 / H1) is a natural per-run **drill-down** action, not a hot-path fold concern.
- **`conflicted`** — a liveness-overlay feature (see `liveness-overlay.md`), independent of the
  table: it needs `resolve()`/`launcher.terminated` probe corroboration, not a fold change.
- **`--follow-symlinks` / arbitrary `--glob PATTERN`** — additive glob-resolver options deferred in
  the glob spec (runs inside symlinked dirs; power-user patterns). Off by default keeps cycle safety.
