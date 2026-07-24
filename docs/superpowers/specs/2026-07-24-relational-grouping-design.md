# Relational grouping for the multi-run table — design

**Status:** design (2026-07-24). Un-deferred from `docs/backlog/multi-run-remainders.md` (which
recorded the shape across PRs #20/#21) to dogfood through mycooc. Supersedes the "single group
key / path-derived / deferred" framing there.

**Goal:** let the multi-run table group and label rows by a per-row **relational attribute
record** supplied by the resolver — never parsed from a path. First consumer: mycooc, which emits
a neutral manifest describing its cells.

## Non-goals (explicitly out)

- **No FS/path-derived grouping.** Nothing parses attributes out of a directory path. "Grouping
  should be possible" ≠ "grouping from the filesystem." `const`/`explicit`/`glob` keep returning
  empty attrs → today's flat table.
- **No data-plane metric columns / seed aggregation.** Multi-metric best-step tables and `mean±std`
  are mycooc's `--status` domain / the separate viz project, not this.
- **No TUI-side mycooc-schema adapter.** The reference TUI never reads mycooc's YAML or symlink
  tree; mycooc emits a neutral relation instead.
- **No runtime pivot in v1.** The group attribute is fixed at launch; cycling it with a keypress is
  a named fast-follow.

## Two data paths (the load-bearing separation)

| | question | carried by | runstate? |
|---|---|---|---|
| **Discovery** | which runs exist, with what attrs? | the manifest file, read by the resolver | no |
| **Observation** | what is *this* run doing? | each run's Channel (Values, lifecycle, …) | yes |

Discovery is a level above the substrate — runstate is per-run and deliberately has no enumeration
surface (the refused `list_runs()`). The manifest is the *index*; the channels are the *contents*.
That is why the manifest is a plain file, not a message on the substrate.

## Architecture

### 1. Resolver signature carries attributes

```
Attrs    = Mapping[str, str]
Resolver = Callable[[float], list[tuple[RunRef, Attrs]]]
```

`const`/`explicit`/`glob` return `attrs = {}` for every run → backward-neutral (empty attrs render
exactly as today; no compat shim, just an extra empty field). Attrs are display metadata — strings,
for grouping and labeling only.

### 2. The manifest resolver (the generic attribute source)

`manifest_resolver(path) -> Resolver`. Each tick it reads one JSON file:

```json
[
  {"run_id": "7cfc…", "root": "outputs/runs/7c/7cfc…", "backend": "sqlite",
   "attrs": {"scenario": "en-ru-16M", "variant": "fb_5k", "seed": "43"}}
]
```

and returns `[(RunRef(run_id, root, backend), attrs), …]`. Re-read per frame (cheap) so cells
appearing/disappearing track live. Any workload can emit this file; mycooc is the first producer; a
hand-written fixture manifest exercises the whole feature with no mycooc present. (`root` is
backend-specific — a path for sqlite, absent/ignored for a rootless backend like postgres.)

**Invariant:** attrs are **row-unique** — they are the cell identity (see the reconcile key). The
producer owns this; mycooc includes `seed`, so its cells are naturally unique.

### 3. Grouping + labeling in the table

- **Group** by one chosen attribute (v1): the table renders one section per distinct value of that
  attribute, each section headed by a per-group `format_fleet_summary` roll-up (the shipped strip,
  computed per-section instead of once globally). Runs with empty attrs, or when no `--group-by` is
  given, form a single implicit group = today's flat table.
- **Label** = the non-group attrs joined for display (e.g. `fb_5k seed43`). When attrs are empty the
  label falls back to today's disambiguated `RunRef` label — unchanged behavior.

### 4. Reconcile key = the cell, not the run

Row identity becomes the attrs record when non-empty, else the `RunRef` (today's key). Consequences:

- Two cells sharing one run (same `RunRef`, different attrs) become **two distinct rows**. The LRU
  channel pool already keys on `RunRef`, so both rows fold **one** shared open channel — no extra
  I/O or fds.
- The keyed `batch_update` reconcile in `multirun.py` swaps its key from `RunRef` to a
  `row_key(ref, attrs)` function. **This is the only non-additive change**; fold, pool, and the
  summary strip are untouched.
- Drill-down (`enter`) maps the selected row → its `RunRef` (cached from the last frame, per the
  existing M1 pattern), so a shared run opens correctly from either row.
- A manifest entry whose run is gone (stale rid) → `attach_channel` raises `RunNotFound` → the
  existing fold yields a `missing`/`unreadable` row. No new handling; no crash.

### 5. CLI / dispatch

`runstate-tui <manifest>.json [--group-by <attr>]`:

- Dispatch by argument shape, same as the existing `.db`-file / directory branches: a `.json` file →
  `manifest_resolver`.
- `--group-by <attr>` selects the grouping attribute. Omitted → flat table with attr-labels.
- Missing/empty manifest at launch → the existing zero-match placeholder (`empty_hint`, e.g.
  "watching &lt;manifest&gt; — no runs yet").

### 6. Error handling (discovery path)

Discovery is UI/control-flavored, not the fail-fast data plane:

- **Malformed / transiently-unreadable manifest on a tick** → keep the last good list, surface a
  small indicator (reuse the `⚠ I/O stalled`-style main-thread marker); do not crash, do not flicker
  to empty.
- **Per-run open failures** → already handled by the fold (see §4), rendered as `missing` /
  `unreadable`.

## mycooc side (separate repo change)

mycooc already computes `(run → {scenario, variant, seed})` for `--status`. Add a small emit step
(`--emit-manifest <path>`, or fold it into `--status`) that writes the JSON in §2 and rewrites it
when the cell set changes. No TUI coupling; mycooc owns its schema→manifest mapping. This is the
dogfood on the producer side, tracked in mycooc's own repo — out of scope for this plan except as
the format contract in §2.

## Testing

- **manifest_resolver**: fixture manifests → returns the expected `[(RunRef, attrs)]`; tolerates a
  missing file (empty list) and a malformed file (last-good / empty, no raise).
- **grouping render**: a 2-group manifest → 2 sections, each with its own roll-up header; an
  empty-attrs manifest → flat table (regression-neutral vs today).
- **shared run**: two entries with the same `RunRef`, different attrs → two rows, one pooled channel.
- **snapshot**: a grouped-table scene added to the showcase, verified by rendering + looking (the
  standing discipline).

## Seams left open (not built now)

- **Multi-attr / tuple grouping and runtime pivot** — the signature already carries all attrs; v1
  just fixes one group attribute at launch.
- **attrs from other populators** — e.g. a future `attrs-from-log` fold of a run's config record;
  the resolver signature already accommodates it.
- **Daemon push discovery** — replacing per-tick file re-reads with pushed updates; same discovery
  role, different transport (relates to upstream runstate #16).
