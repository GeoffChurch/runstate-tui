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

## The `cells` resolver — the main remaining multi-run feature

`explicit` / `const` / `glob` all shipped; `cells` (a **workload-specific sweep**) is the one resolver
left. The *discovery* is a small `Resolver` (`Time → list[RunRef]`) dropped into the CLI dispatch +
`MultiRunApp` — but two corrections to the old "mechanically trivial, reuses `disambiguate`" framing
(both confirmed 2026-07-23, cross-repo against mycooc's `run_experiment.py` / `channel_read.py`):

- **Enumeration is a manifest cross-product, not a tree walk.** mycooc's `--status` reads the experiment
  YAML and takes `scenarios × variants (× seeds)`, *constructing* each cell path; it does not walk a
  directory of cells. The cells resolver needs the spec, not just the tree.
- **Labels and groups come from the resolver, not the path.** A cell's display name (variant) and group
  (scenario) are resolver-supplied *attributes* (see the relational grouping note below), not derived by
  `disambiguate` on `RunRef.root`. This dissolves an earlier over-asserted construction question (does
  `root` point at the content-addressed home or the cell path?): `root` only has to *open the channel*,
  and a shared run home correctly yields *one* `RunRef` behind *two* cell rows.

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

## Grouping — a per-row *relational* attribute record (the reference-TUI core of `cells`), deferred

This is the reference-TUI-generic slice of the `cells` work above: sectioning + labeling the flat table by
*grouping metadata*, with mycooc's domain specifics (the manifest schema, which axis is a group vs. a
label, the metric/seed data plane) left to mycooc. **Not a parallel feature — the generic core `cells`
consumes.** It has a second, cells-free consumer (glob multi-root / translation's roots), which is what can
*trigger* it independently — not evidence it is separate.

**The model is relational, not hierarchical (2026-07-23, superseding the earlier "single group key /
path-derived" sketch).** The resolver yields a per-row **attribute record** — `(RunRef, attrs:
Mapping[str, str])` — and everything else falls out of it:

- **Group** by any named attribute (or tuple), potentially at runtime like a pivot. The attributes are
  named and all present, so there is no baked-in nesting order and no "group by which depth?" ambiguity —
  that ambiguity was purely an artifact of encoding the relation in a *path*.
- **Label** = format chosen attributes.
- Render = group sections, each headed by a per-group `format_fleet_summary` roll-up; the single-group
  case renders exactly as today (purely additive).
- **Row identity = the attribute record (the cell), not `run_id`.** Two cells sharing one run become two
  rows pointing at one `RunRef` — exactly the run-sharing mycooc's symlink indirection was built for —
  which dissolves the "shared-rid collision" worry, and dissolves the root-construction question with it:
  `root` only has to *open the channel*; display and identity come from `attrs`, never from `disambiguate`
  on the root path.

**Why relational beats the filesystem / symlink-tree approach** — workable as a *mechanism*, wrong as a
*model*:

- **Path-as-metadata is the id-embedding anti-pattern one level up.** A directory path is a string;
  reading scenario/variant off its components is parsing metadata back out of a string — the move already
  rejected for channel ids, just more socially acceptable because everyone encodes meaning in folders. A
  tree serializes a *relation* (run ↔ {scenario, variant, seed, …}) into one fixed nesting order, must be
  hand-edited, and presupposes a hierarchical FS.
- **It conflates two concerns the sharing design already separates.** mycooc's indirection exists so many
  cells can *share one run* — its job is sharing/opening (many views → one content-addressed home), not
  carrying grouping metadata. Which-scenario/which-variant is separate data that merely happens to be
  co-encoded in the same tree. Keep them apart: the tree stays a pure sharing/GC mechanism; the attributes
  travel as data.
- **mycooc's real source is already relational** — the experiment YAML *is* the run → {scenario, variant,
  seed, config} table; the symlink tree is a *derived materialization* of it. A resolver reads the
  relational source, it does not reverse-engineer semantics from the materialization.
- **FS-agnostic.** A relation needs no directories → it also fits `PostgresChannel`, which runstate flagged
  has *no* root/dir/symlink shape at all (`runstate/docs/backlog/third-party-observer.md` item 3). A
  hierarchy would exclude Postgres by construction.
- **Relational subsumes hierarchical, not the reverse.** A path is just one attribute (or its components,
  named). So "point at a directory and go" survives as a **path → attrs adapter** — one *populator* of the
  model, the right home for the glob case — not the model itself. The general model costs nothing; the FS
  convenience is retained as an adapter.

Rejected populators (both smuggle structured data through a string when the transport is rich Python
objects): embedding facet metadata in the **channel id** (mycooc's seed-from-name style), and treating the
**directory tree** as the metadata source rather than a sharing mechanism.

**Gate:** deferred until a *second* grouping consumer creates the pull — the glob multi-root / translation
view feeling flat. `cells` alone doesn't earn it (mycooc owns its own frontend). The FS convenience, if
wanted, is the path → attrs adapter above, not a core default.

## Related deferred findings that surface here (not table features)

- **Corruption-invisibility full-scan** — the deferred opt-in, marked-expensive integrity scan
  (spec §12 / H1) is a natural per-run **drill-down** action, not a hot-path fold concern.
- **`conflicted`** — a liveness-overlay feature (see `liveness-overlay.md`), independent of the
  table: it needs `resolve()`/`launcher.terminated` probe corroboration, not a fold change.
- **`--follow-symlinks` / arbitrary `--glob PATTERN`** — additive glob-resolver options deferred in
  the glob spec (runs inside symlinked dirs; power-user patterns). Off by default keeps cycle safety.
