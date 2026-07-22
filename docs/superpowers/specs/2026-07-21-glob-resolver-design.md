# Glob resolver: design (2026-07-21)

Add **live directory discovery** to the shipped Stage-4 multi-run table. Today the cockpit indexes a
**fixed** set of runs (`explicit_resolver` over CLI `<run.db>` paths). This adds a `glob_resolver`
that points the table at a **directory** and re-discovers its runs every frame — new runs appear,
finished/deleted runs drop, without a relaunch. "Point the cockpit at my runs folder and watch."

This is the first of the two deferred Stage-4 resolvers (`glob`, then `cells`), unblocked by the
runstate locator split: every open already goes through `attach_channel` (existing-only, `RunNotFound`,
never creates or mutates — PR #15), so a glob that matches a stale / foreign / half-written `.db`
reads `missing`/`unreadable` and leaves the file byte-identical. A broad glob is now as safe as an
explicit run.

## Goal

Turn the table from "the runs I named on the command line" into a **live dashboard over a runs
directory**, with three properties:

1. **Live** — runs appearing/vanishing on disk are reflected within one tick (no relaunch).
2. **Safe** — a foreign or half-written `.db` under the tree is never fabricated or mutated; it reads
   as an honest `missing`/`unreadable` row.
3. **Legible** — recursively-discovered runs whose `run_id` stems collide (sweeps reuse run names
   across subdirs) are shown with the **shortest disambiguating path**, not indistinguishable dupes.

Non-goals (v1): arbitrary glob patterns, following symlinked directories, and any cap on tree size —
all designed-for and deferred below.

## Invocation — a directory positional

`runstate-tui runs/` — a **directory** argument means "discover and watch every run under here."

CLI dispatch (`runstate_tui/__main__.py`), one new branch:

| args | dispatch | mode |
|---|---|---|
| `runstate-tui <dir>` | `Path.is_dir()` → `glob_resolver(dir)` → `MultiRunApp` | **new: live glob** |
| `runstate-tui <run.db>` | `SingleRunApp` | unchanged |
| `runstate-tui <run.db> <run.db> …` | `explicit_resolver` | unchanged |

The directory form is chosen over a `--glob 'pattern'` flag deliberately: a directory **cannot be
shell-expanded**, so it dodges the footgun where `runstate-tui runs/*.db` is expanded by the *shell*
into a fixed list before the process sees it — silently giving the non-live explicit resolver. A
directory reaches us intact and stays a live pattern. (`--glob` remains a clean additive power-user
option later — see Deferred.) Usage string updates to
`usage: runstate-tui <run.db> | <dir> | <run.db> [<run.db> …]`.

A directory containing exactly one run still routes through `MultiRunApp` (the table at |I|=1 — the
§11 singleton — is the single-run view; `enter` opens the full drill-down). No special-casing.

## The glob resolver — `Path.rglob`, always recursive

```python
def glob_resolver(root: str) -> Resolver:
    root_path = Path(root)
    def resolve(_now: float) -> list[RunRef]:
        refs = [ref_from_path(str(p)) for p in root_path.rglob("*.db")]
        return list(dict.fromkeys(refs))   # dedup; order is irrelevant (the table sorts)
    return resolve
```

- **Recursive** — `rglob("*.db")` walks the whole tree (`runs/*.db` *and* `runs/exp1/trial.db`),
  matching how sweeps lay out on disk. Backend is sqlite-only — glob is on-disk `.db` files; a fact,
  not a config knob (`ref_from_path` sets `"sqlite"`).
- **Re-evaluated every frame** by the owner thread inside `fold_frame` (`self._resolver(now)`,
  `multirun.py:154`) — off the render thread, already. This *is* the live discovery; the run-set
  delta falls out of the existing keyed reconcile (`add_row` for new, `remove_row` for gone).
- **Safe by the `attach_channel` guarantee** — a match that is missing / empty / foreign / mid-write
  reads `missing` or `unreadable` and is left byte-identical (the migration collapsed stat-before-open
  into `except RunNotFound`). No pre-filtering to "is this really a runstate db"; the fold classifies
  it honestly (faithful-representation over a lossy pre-filter).

### Symlinks are a non-issue — `pathlib` already does the right thing (verified)

We deliberately use `pathlib.Path.rglob`, **not** `glob.glob(recursive=True)`. Measured on this
environment's runtime (Python 3.11) and 3.12:

| expander | symlinked **dir** (`linked/ → ext/`) | a **cycle** (`sub/loop → runs`) | symlinked **file** (`latest.db → …`) |
|---|---|---|---|
| **`Path.rglob('*.db')`** | not recursed | **invisible** — no hang, no explosion | **matched** ✓ |
| `glob.glob('**/*.db', recursive=True)` | recursed | **explodes** into `sub/loop/sub/loop/…` dupes | matched |

So `pathlib` refuses to recurse into symlinked *directories* (a cycle is physically never entered —
no hang to engineer around, no "whose fault is a cycle" to adjudicate) but still includes symlinked
*files* matched at a real level. That is exactly the desired behavior: the common legit pattern
`latest.db → …/run.db` **works**; a cyclic/deep symlinked directory tree is **avoided by
construction**. Bonus: `rglob`'s `**` also skips hidden dirs (`.git`, `.venv`). The `recurse_symlinks`
kwarg is 3.13+, so we cannot (and need not) tune it. The one residual gap — a run living *inside* a
symlinked-in directory — **fails safe** (a missing row, never a hung cockpit); an opt-in
`--follow-symlinks` is a clean additive follow-up.

## Legibility — the minimal-backtrack disambiguation label (load-bearing)

Recursive discovery makes `run_id`-stem collision the *common* case: a sweep laid out as
`runs/exp1/trial.db`, `runs/exp2/trial.db` yields two runs that are **distinct rows** (keyed by full
`RunRef`, so the reconcile keeps them correct and separate) but whose `run` column both shows the bare
stem `trial` — visually identical, and they sort ambiguously. Fix: show the **shortest trailing path
that makes each run unique**.

**Algorithm** (`disambiguate(refs) -> {ref_key: label}`, a pure function over each run's path parts,
`Path(root, run_id).parts` — available from the `RunRef` alone):

- Start every run at depth 1 (the bare stem).
- Group by current label; **any group that still collides grows one more parent level** (depth += 1,
  capped at the run's own path length).
- Repeat until no group collides.

Traced on `runs/g1/run000.db … runs/g1/run099.db` + `runs/g2/run000.db`:

- Round 0: `run000` collides (g1 vs g2); `run001…run099` are already unique.
- Round 1: **only the colliding group** grows → `g1/run000`, `g2/run000`; the other 99 stay bare.
- Done.

Labels are **minimal and ragged** — each run shows the least path needed and no more. This beats the
uniform-depth alternative (where one deep collision would force long labels on *everyone*). Distinct
refs have distinct full part-tuples, so the loop always terminates and fully disambiguates (worst case
= the full path).

**The property that makes this free:** when every stem is already unique — explicit mode, single-run,
every showcase scene, every existing test — the algorithm is a **no-op** (every label is the bare
stem), so the `run` column renders **byte-identical to today**. Therefore it is applied **globally**,
one code path, with **zero** screenshot/test churn; the label only ever grows in the presence of a
real collision, which only recursive glob produces.

**Where it lives (display-only):** computed once per frame on the **main thread** in `on_table_ready`
from that frame's refs (`[ref for ref, _ in msg.table]`) and threaded into `_cells` (which grows a
`label` argument, replacing its bare `ref[0]` for the `run` cell). The table's existing
`t.sort("run")` now sorts on the disambiguated label — a nice side effect: sweep families group
together. **`fold_frame`, the `ChannelPool`, `fold.py`, and `types.py` are untouched** — the label is
never part of the fold, the pool key, or the `attach_channel` inputs.

## Zero-match — a quiet placeholder

Point the cockpit at a folder before any run has started and the resolver returns `[]`; the reconcile
empties the table to bare headers with no explanation (the `⚠ I/O stalled` banner fires only on a
*wedged* owner thread, not on empty results). For a live-discovery tool this is a legitimate transient
state, so `MultiRunApp` shows a quiet placeholder — e.g. `watching runs/**/*.db — no runs yet` — a
display-toggled `Static` (mirroring the stall banner) shown when `row_count == 0`, swapped for the
table the instant a run appears. `MultiRunApp` gains an optional `empty_hint: str | None` the CLI fills
with the watched pattern (explicit mode leaves it `None`; an explicit list is never empty).

## What does NOT change (the seam this grows from)

Everything load-bearing is already shipped and is reused verbatim:

- **`MultiRunApp`** — owner-thread pool, `TableReady` marshal, keyed `DataTable` reconcile in
  `batch_update()`, `move_cursor`-after-`sort`, the `⚠ I/O stalled` watchdog, bounded teardown drain,
  `enter` → drill-down. A `Resolver` is a `Resolver`; the glob one is a drop-in. **No concurrency,
  reconcile, or teardown change.**
- **`fold_frame` / `ChannelPool`** — per-frame frozen clock, LRU eviction, per-run `_fold_error`
  containment. Untouched.
- **The fold** (`fold.py`, `types.py`, `status_fold`, the integrity taxonomy). Untouched.
- **Drill-down reconstruction** (`multirun.py:240`, `by_key` from the resolver) uses the `RunRef`, not
  the label — unaffected. (Optionally the drill-down header may show the full relative path; a nicety,
  not required.)

## Components / files

- **`runstate_tui/resolver.py`** — `glob_resolver(root) -> Resolver` (the `rglob` re-scan);
  `disambiguate(refs) -> dict[str, str]` (the pure minimal-backtrack labeler). Both sit beside the
  existing `const_resolver`/`explicit_resolver`/`ref_from_path`/`ref_key`.
- **`runstate_tui/multirun.py`** — `_cells` gains a `label` argument (the `run` cell); `on_table_ready`
  builds the per-frame label map and threads it in; the zero-match placeholder `Static` + `empty_hint`.
- **`runstate_tui/__main__.py`** — the `Path.is_dir()` dispatch branch + usage string.

## Model vs view (daemon / multi-frontend readiness)

- **Model (frontend-agnostic, daemon-servable):** the resolver contract `Time → list[RunRef]` and the
  run-set delta. A future headless daemon serves *which runs exist* to N frontends; `glob_resolver` is
  one implementation of that discovery.
- **View (cockpit-only):** the disambiguation label, the `run` column, the zero-match placeholder, the
  `is_dir` CLI ergonomics. An Emacs frontend would discover the same run-set and render its own labels.
- The `RunRef` stays the semantic boundary; the label is pure display derived from it. Discovery goes
  toward the daemon direction (upstream `#16` change-notification could later push run-set deltas
  instead of a per-frame re-scan) without touching this contract.

## Testing

- **Resolver / discovery:** `rglob` over a tmp tree finds nested `*.db`, dedups; a run added between
  two `resolve()` calls appears on the second (live); a run removed drops. Reuse the fixture basis
  (`held_writer_sqlite_run`, `foreign_db`).
- **Symlink safety (regression-pin the empirical guarantee):** a tree with a **cyclic** dir symlink
  → `resolve()` returns promptly and does **not** explode (no `loop/loop/…` entries); a symlinked
  **file** *is* found; a run inside a symlinked **dir** is *not* (documents the fail-safe gap).
- **`disambiguate` (pure unit tests):** unique stems → bare stems (**no-op**, the churn-free property);
  a colliding pair → `parent/stem`; ragged output (99 unique + 1 colliding pair → only the pair grows);
  deeper nesting needs deeper suffixes; the suffix-overlap edge (`(x,trial)` vs `(y,x,trial)`)
  terminates and disambiguates.
- **Dispatch:** `main(argv=[dir])` → `MultiRunApp` + glob resolver; `main(argv=[file])` → `SingleRunApp`;
  `main(argv=[f1, f2])` → explicit. (Assert the resolver/app wiring, not a full run.)
- **Zero-match:** an empty dir → the placeholder is shown and the table hidden; adding a run swaps to
  the table.
- **Safe foreign match:** a foreign / empty `.db` under the tree → a `missing`/`unreadable` row and the
  file is byte-identical afterward (reuse `foreign_db`).
- **No-churn:** the existing suite + showcase scenes stay green unchanged — the global label is a
  no-op on their unique stems.
- No `@pytest.mark.asyncio` (use the `asyncio.run` wrapper); reuse the fixture/showcase seed style.

## Deferred (designed-for, not built)

- **`--glob 'PATTERN'` flag** — arbitrary patterns for power users (with the shell-quoting caveat).
  The directory form is the ergonomic default; this is purely additive.
- **`--follow-symlinks`** — opt into runs that live inside symlinked-in directories (the one fail-safe
  gap above). Additive; off by default keeps the cycle safety.
- **Tree-size cap + truncation banner** — for a pathological tree the LRU pool already bounds fds and
  §10 targets ~100 runs; a hard cap with a "showing N of M" banner is a later robustness layer, not
  v1.
- **`cells` resolver** (workload-specific sweep) — the natural next resolver built on this same
  discovery + disambiguation machinery.
- **Uniform-depth labeling** — explicitly rejected in favor of ragged-minimal (one deep collision must
  not lengthen every label).
