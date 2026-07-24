# Relational grouping — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement
> this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** group and label the multi-run table by a per-row relational attribute record supplied by the
resolver; add a `manifest_resolver` (the first attribute source) and `--group-by`.

**Architecture:** the resolver contract becomes `Time → list[(RunRef, Attrs)]`; `fold_frame` threads
attrs through to a `(RunRef, Attrs, Row)` table; `MultiRunApp` keys rows on the cell (attrs) and, when
`group_by` is set, renders single-table sections via non-interactive group-header rows, ordered with a
private `_row_locations` reorder. Design of record: `../specs/2026-07-24-relational-grouping-design.md`.

**Tech Stack:** Python 3.11+, Textual 8.2.8, `runstate` (locked git dep), pytest, ruff, mypy --strict.

## Global Constraints

- **Migrate, never accommodate.** The resolver signature change is a hard migration: every resolver and
  every call site moves to `(RunRef, Attrs)`. No dual-path, no "optional attrs tuple", no back-compat
  shim. Empty `attrs = {}` is the uniform shape for the attribute-less resolvers, not a compatibility
  branch.
- **Gate after every task:** `uv run ruff format . && uv run ruff check --fix . && uv run mypy --strict
  runstate_tui && uv run pytest -q` — all clean before the task is done.
- **Verify snapshots by looking**, never blind `--snapshot-update` (the standing discipline).
- `Attrs = Mapping[str, str]`. Attrs are display metadata only (grouping + labeling).
- Crash-on-bad-manifest is intentional (spec §6): `manifest_resolver` does NOT catch parse errors.

---

### Task 1: Resolver contract carries attributes (behavior-preserving migration)

**Files:**
- Modify: `runstate_tui/resolver.py:6-51` (types + `const`/`explicit`/`glob`)
- Modify: `runstate_tui/pool.py:15,94-112` (`Table`, `fold_frame`)
- Modify: `runstate_tui/multirun.py:185-232` (`on_table_ready` iteration)
- Test: `tests/test_resolver.py`, `tests/test_multirun.py`, `tests/test_cli.py` (update resolver shapes)

**Interfaces:**
- Produces: `Attrs = Mapping[str, str]`; `Resolver = Callable[[float], list[tuple[RunRef, Attrs]]]`;
  `Table = tuple[tuple[RunRef, Attrs, Row], ...]`; `fold_frame(pool, items: list[tuple[RunRef, Attrs]],
  env, now) -> Table`.
- Consumes: existing `ref_key`, `disambiguate`, `_fold_error`, `pool.row_for`, `pool.reconcile`.

- [ ] **Step 1: Write the failing test** — resolvers now yield `(ref, {})` pairs.

In `tests/test_resolver.py` add:

```python
def test_resolvers_yield_ref_attrs_pairs():
    from runstate_tui.resolver import const_resolver, explicit_resolver
    ref = ("r", "/root", "sqlite")
    assert const_resolver(ref)(0.0) == [(ref, {})]
    assert explicit_resolver([ref])(0.0) == [(ref, {})]
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/test_resolver.py::test_resolvers_yield_ref_attrs_pairs -q`
Expected: FAIL (resolvers return bare refs).

- [ ] **Step 3: Migrate `resolver.py`**

```python
from collections.abc import Callable, Mapping, Sequence

RunRef = tuple[str, str, str]
Attrs = Mapping[str, str]                                  # display metadata: grouping + labeling
Resolver = Callable[[float], list[tuple[RunRef, Attrs]]]   # Time -> [(ref, attrs)] (re-resolved each frame)


def const_resolver(ref: RunRef) -> Resolver:
    return lambda now: [(ref, {})]


def explicit_resolver(refs: list[RunRef]) -> Resolver:
    snapshot = list(dict.fromkeys(refs))
    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        return [(r, {}) for r in snapshot]
    return resolve


def glob_resolver(root: str) -> Resolver:
    root_path = Path(root)
    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        return [(ref_from_path(str(p)), {}) for p in root_path.rglob("*.db")]
    return resolve
```

(`ref_from_path`, `disambiguate`, `ref_key` are unchanged. Keep their docstrings.)

- [ ] **Step 4: Migrate `pool.py`**

```python
from .resolver import Attrs, RunRef

Table = tuple[tuple[RunRef, Attrs, Row], ...]


def fold_frame(pool: ChannelPool, items: list[tuple[RunRef, Attrs]], env: Env, now: float) -> Table:
    """One owner-thread frame. Fold every DISTINCT run once under a per-frame frozen clock; a run
    named by several items (shared cell -> one RunRef) folds once and its Row is reused, so shared
    runs cost one open channel and one read."""
    frame_env = replace(env, clock=lambda: now)
    refs = [ref for ref, _ in items]
    pool.reconcile(set(refs))
    folded: dict[RunRef, Row] = {}
    out: list[tuple[RunRef, Attrs, Row]] = []
    for ref, attrs in items:
        if ref not in folded:
            try:
                folded[ref] = pool.row_for(ref, frame_env)
            except Exception as exc:  # noqa: BLE001 — internal fold bug -> loud per-run row, table survives
                folded[ref] = _fold_error(exc)
        out.append((ref, attrs, folded[ref]))
    return tuple(out)
```

- [ ] **Step 5: Migrate `on_table_ready` iteration (keying UNCHANGED — still `ref_key`)**

In `runstate_tui/multirun.py`, update the four `msg.table` consumers to the 3-tuple, leaving `ref_key`
as the key (behavior identical this task):

```python
want = {ref_key(ref) for ref, _attrs, _row in msg.table}
labels = disambiguate([ref for ref, _attrs, _row in msg.table])
self._refs_by_key = {ref_key(ref): ref for ref, _attrs, _row in msg.table}
...
for ref, _attrs, row in msg.table:
    key = ref_key(ref)
    cells = _cells(row, labels[key])
    ...
...
summary.update(format_fleet_summary([row for _ref, _attrs, row in msg.table]))
```

- [ ] **Step 6: Migrate the tests that build raw resolvers / assert resolver shape**

`grep -rn "lambda now" tests/` and the resolver-output assertions. Every raw resolver lambda must yield
`(ref, {})` pairs; e.g. `tests/test_multirun.py`'s `lambda now: list(live["refs"])` becomes
`lambda now: [(r, {}) for r in live["refs"]]`, `lambda now: [a, a]` becomes `lambda now: [(a, {}), (a, {})]`,
and the M1 resolver returns `[(a, {})]`. In `tests/test_cli.py`, the two `made["refs"] = self._resolver(0.0)`
assertions become `[(ref_from_path(a), {}), (ref_from_path(b), {})]` and the glob set becomes a set of
`(ref, {})`... **use a dict, not a set** (dicts aren't hashable): compare
`sorted(made["refs"]) == sorted([(ref_from_path(...), {}), ...])` or assert on
`{r for r, _ in made["refs"]}`.

- [ ] **Step 7: Run the full suite, verify green (behavior-preserving)**

Run the gate. Expected: all pre-existing behavior tests pass unchanged; the new pair-shape test passes.

- [ ] **Step 8: Commit**

```bash
git add -A && git commit -m "refactor: resolver contract carries attrs (RunRef, Attrs); behavior-preserving"
```

---

### Task 2: `manifest_resolver` — the first attribute source

**Files:**
- Modify: `runstate_tui/resolver.py` (add `manifest_resolver`)
- Test: `tests/test_resolver.py`

**Interfaces:**
- Produces: `manifest_resolver(path: str) -> Resolver`. Reads a JSON array of
  `{"run_id","root","backend","attrs":{...}}` each call; returns `[((run_id, root, backend), attrs), ...]`.
  Missing `attrs` → `{}`. A missing file, malformed JSON, or a missing required key **raises** (spec §6).

- [ ] **Step 1: Write the failing tests**

```python
import json
import pytest
from runstate_tui.resolver import manifest_resolver


def _write(tmp_path, obj):
    p = tmp_path / "m.json"
    p.write_text(json.dumps(obj))
    return str(p)


def test_manifest_resolver_reads_pairs(tmp_path):
    path = _write(tmp_path, [
        {"run_id": "r1", "root": "/x", "backend": "sqlite", "attrs": {"scenario": "s", "variant": "v"}},
    ])
    assert manifest_resolver(path)(0.0) == [(("r1", "/x", "sqlite"), {"scenario": "s", "variant": "v"})]


def test_manifest_empty_list_is_empty(tmp_path):
    assert manifest_resolver(_write(tmp_path, []))(0.0) == []


def test_manifest_missing_attrs_defaults_empty(tmp_path):
    path = _write(tmp_path, [{"run_id": "r1", "root": "/x", "backend": "sqlite"}])
    assert manifest_resolver(path)(0.0) == [(("r1", "/x", "sqlite"), {})]


def test_manifest_malformed_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(json.JSONDecodeError):
        manifest_resolver(str(p))(0.0)


def test_manifest_missing_required_key_raises(tmp_path):
    path = _write(tmp_path, [{"root": "/x", "backend": "sqlite"}])  # no run_id
    with pytest.raises(KeyError):
        manifest_resolver(path)(0.0)
```

- [ ] **Step 2: Run, verify fail** (`manifest_resolver` not defined).

- [ ] **Step 3: Implement**

```python
import json


def manifest_resolver(path: str) -> Resolver:
    """A LIVE resolver over a neutral JSON manifest (the discovery INDEX; the per-run channels are the
    contents). Each frame it re-reads `path` and returns one (RunRef, attrs) per entry. It deliberately
    does NOT tolerate a bad read: a malformed manifest raises (crashes the cockpit via the owner-thread
    catastrophic path) -- correct because the emitter's atomic-write contract (spec §2) guarantees a
    read never sees a torn file, so a parse failure is a real emitter bug. See spec §6."""
    manifest_path = Path(path)

    def resolve(_now: float) -> list[tuple[RunRef, Attrs]]:
        entries = json.loads(manifest_path.read_text())
        return [
            ((e["run_id"], e["root"], e["backend"]), dict(e.get("attrs", {})))
            for e in entries
        ]

    return resolve
```

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Commit** — `git commit -m "feat: manifest_resolver reads a neutral (RunRef, attrs) JSON relation"`.

---

### Task 3: Reconcile key = the cell; label from attrs (flat, no sections yet)

**Files:**
- Modify: `runstate_tui/multirun.py` (add `row_key`, `_label`, `group_by` param; use in `on_table_ready`)
- Test: `tests/test_multirun.py`

**Interfaces:**
- Produces: `MultiRunApp(..., group_by: str | None = None)`; `row_key(ref: RunRef, attrs: Attrs) -> str`;
  `_label(ref, attrs, group_by, disambig) -> str`.
- Consumes: `Attrs`, `ref_key`, `disambiguate` from resolver.

- [ ] **Step 1: Write the failing tests**

```python
def test_cell_is_keyed_and_labeled_by_attrs(tmp_path):
    asyncio.run(_cell_keyed_and_labeled(tmp_path))

async def _cell_keyed_and_labeled(tmp_path):
    from runstate_tui.multirun import row_key
    ref = _seed(tmp_path, "r1")
    attrs = {"scenario": "s", "variant": "v"}
    app = MultiRunApp(lambda now: [(ref, attrs)], Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        key = row_key(ref, attrs)
        assert {k.value for k in t.rows.keys()} == {key}
        assert t.get_cell(key, "run") == "s v"           # non-group attrs joined


def test_two_cells_sharing_one_run_are_two_rows_one_channel(tmp_path):
    asyncio.run(_shared_run_two_rows(tmp_path))

async def _shared_run_two_rows(tmp_path):
    ref = _seed(tmp_path, "r1")
    items = [(ref, {"cell": "A"}), (ref, {"cell": "B"})]
    app = MultiRunApp(lambda now: items, Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.row_count == 2                            # two distinct cell rows
        assert len(app._pool) == 1                         # ...over ONE pooled channel
```

- [ ] **Step 2: Run, verify fail** (rows keyed by `ref_key`, label is the stem).

- [ ] **Step 3: Add `group_by` param + helpers**

In `MultiRunApp.__init__`, add `group_by: str | None = None` → `self._group_by = group_by`.

```python
def row_key(ref: RunRef, attrs: Attrs) -> str:
    """A row's identity is the CELL (its attrs), not the run: two cells sharing a run stay two rows.
    Falls back to ref_key when attrs is empty (the attribute-less resolvers -> today's behavior).
    Invariant: attrs are row-unique (spec §2); a producer that repeats an attrs record collapses to
    one row (last-wins), a documented producer bug, not a crash."""
    if attrs:
        return "\x00".join(f"{k}={v}" for k, v in sorted(attrs.items()))
    return ref_key(ref)


def _label(ref: RunRef, attrs: Attrs, group_by: str | None, disambig: dict[str, str]) -> str:
    if attrs:
        shown = " ".join(v for k, v in sorted(attrs.items()) if k != group_by)
        return shown or disambig[ref_key(ref)]            # all attrs were the group key -> fall back
    return disambig[ref_key(ref)]
```

- [ ] **Step 4: Use them in `on_table_ready`** (keys + labels; sort still `t.sort("run")`)

```python
want = {row_key(ref, attrs) for ref, attrs, _row in msg.table}
labels = disambiguate([ref for ref, _attrs, _row in msg.table])
self._refs_by_key = {row_key(ref, attrs): ref for ref, attrs, _row in msg.table}
...
for ref, attrs, row in msg.table:
    key = row_key(ref, attrs)
    cells = _cells(row, _label(ref, attrs, self._group_by, labels))
    ...   # add_row(*cells, key=key) / update_cell(key, ...) exactly as before
t.sort("run")
```

- [ ] **Step 5: Run the gate, verify green** (existing empty-attrs tests still pass — `row_key` falls
  back to `ref_key`, so all prior keying/labeling is unchanged).

- [ ] **Step 6: Commit** — `git commit -m "feat: key rows on the cell (attrs) and label from attrs"`.

---

### Task 4: Grouped sections — group-header rows + `_row_locations` reorder

**Files:**
- Modify: `runstate_tui/multirun.py` (grouped branch in `on_table_ready`; `_reorder` helper)
- Test: `tests/test_multirun.py`

**Interfaces:**
- Consumes: `self._group_by`, `row_key`, `_label`.
- Produces: single-table sections — one non-interactive header row per group value, rows ordered
  `(group, header-before-data, label)`. Header row key = `"\x00\x00GRP\x00" + value` (won't collide with
  data keys); `enter` on a header is a no-op (no `_refs_by_key` entry).

- [ ] **Step 1: Write the failing tests**

```python
def test_grouping_renders_sections_in_order(tmp_path):
    asyncio.run(_grouping_sections(tmp_path))

async def _grouping_sections(tmp_path):
    r1, r2, r3 = _seed(tmp_path, "r1"), _seed(tmp_path, "r2"), _seed(tmp_path, "r3")
    items = [
        (r2, {"scenario": "beta", "variant": "v1"}),
        (r1, {"scenario": "alpha", "variant": "v2"}),
        (r3, {"scenario": "alpha", "variant": "v1"}),
    ]
    app = MultiRunApp(lambda now: items, Env(clock=lambda: 150.0), tick_interval=999, group_by="scenario")
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        order = [t.get_row_at(i)[1] for i in range(t.row_count)]   # the 'run' column, top to bottom
        assert order == ["── alpha ──", "v1", "v2", "── beta ──", "v1"]  # groups asc; header first; label asc


def test_enter_on_header_row_is_a_noop(tmp_path):
    asyncio.run(_enter_header_noop(tmp_path))

async def _enter_header_noop(tmp_path):
    r1 = _seed(tmp_path, "r1")
    app = MultiRunApp(lambda now: [(r1, {"scenario": "a", "variant": "v"})],
                      Env(clock=lambda: 150.0), tick_interval=999, group_by="scenario")
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        t.move_cursor(row=0)                     # the "── a ──" header row
        await pilot.press("enter")
        await pilot.pause()
        from runstate_tui.detail import DrillDownScreen
        assert not isinstance(app.screen, DrillDownScreen)   # header opens nothing
```

- [ ] **Step 2: Run, verify fail** (no header rows; flat order).

- [ ] **Step 3: Add the `_reorder` helper** (mirrors `DataTable.sort`'s own body — the only way to order
  by a Python-computed sequence when labels repeat across groups so cell-value sort can't tell rows apart;
  verified against Textual 8.2.8)

```python
from textual._two_way_dict import TwoWayDict  # private: reorder the table to an arbitrary key sequence

def _reorder(table: DataTable, ordered_keys: list[str]) -> None:
    """Set the table's display order to `ordered_keys` exactly (mirrors DataTable.sort's internals).
    Needed because sort(key=...) only sees cell values, and grouped labels repeat across groups, so no
    cell tuple identifies a row -- only its str key does. Pinned to Textual 8.2.8."""
    by_val = {k.value: k for k in table.rows.keys()}
    table._row_locations = TwoWayDict({by_val[v]: i for i, v in enumerate(ordered_keys)})
    table._update_count += 1
    table.refresh()
```

- [ ] **Step 4: Branch `on_table_ready` on `self._group_by`**

Factor the flat reconcile so the grouped branch adds header rows and reorders. Sketch (inside
`batch_update`, replacing the single `t.sort("run")`):

```python
GRP = "\x00\x00GRP\x00"
if self._group_by is None:
    # ...existing flat reconcile + t.sort("run")...
else:
    # bucket data rows by group value, build the wanted key set INCLUDING header keys
    buckets: dict[str, list[tuple[str, str]]] = {}   # group -> [(row_key, label)]
    for ref, attrs, row in msg.table:
        g = attrs.get(self._group_by, "")
        buckets.setdefault(g, []).append((row_key(ref, attrs), _label(ref, attrs, self._group_by, labels)))
    header_keys = {GRP + g for g in buckets}
    want = {k for rows in buckets.values() for k, _ in rows} | header_keys
    # remove gone rows; add/update data rows (as in the flat path); add/update header rows:
    for g in buckets:
        hkey = GRP + g
        hcells = _header_cells(g)                     # ("", f"── {g} ──", "", "", "", "", "", Text(""))
        if hkey in present: ...update_cell each column...
        else: t.add_row(*hcells, key=hkey); present.add(hkey)
    # ORDER: groups asc; within group header first, then rows by label asc
    ordered: list[str] = []
    for g in sorted(buckets):
        ordered.append(GRP + g)
        ordered.extend(k for k, _ in sorted(buckets[g], key=lambda kl: kl[1]))
    _reorder(t, ordered)
```

Add `_header_cells(group: str)` returning the 8-tuple with `Text(f"── {group} ──", style="dim")` in the
`run` slot and `Text("")`/`""` elsewhere. `self._refs_by_key` must include ONLY data rows (headers absent)
so `action_detail` on a header finds no ref and returns — that already holds since Task 3 builds
`_refs_by_key` from `msg.table`, which has no header rows.

- [ ] **Step 5: Run the gate, verify green** (both new grouped tests + all flat tests).

- [ ] **Step 6: Commit** — `git commit -m "feat: grouped single-table sections via header rows + reorder"`.

---

### Task 5: CLI dispatch — `.json` manifest + `--group-by`

**Files:**
- Modify: `runstate_tui/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `manifest_resolver`, `MultiRunApp(group_by=...)`.
- Produces: `runstate-tui <manifest>.json [--group-by <attr>]` dispatch.

- [ ] **Step 1: Write the failing tests**

```python
def test_json_manifest_constructs_multirun_with_group_by(monkeypatch, tmp_path):
    import json
    import runstate_tui.__main__ as m
    made = {}
    def fake_run(self):
        made["multi"] = self
        made["items"] = self._resolver(0.0)
    monkeypatch.setattr(m.MultiRunApp, "run", fake_run)
    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([
        {"run_id": "r1", "root": "/x", "backend": "sqlite", "attrs": {"scenario": "s", "variant": "v"}},
    ]))
    m.main([str(manifest), "--group-by", "scenario"])
    assert made["multi"]._group_by == "scenario"
    assert made["items"] == [(("r1", "/x", "sqlite"), {"scenario": "s", "variant": "v"})]
    assert made["multi"]._empty_hint is not None


def test_json_manifest_without_group_by_is_flat(monkeypatch, tmp_path):
    import json
    import runstate_tui.__main__ as m
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: None)
    manifest = tmp_path / "exp.json"
    manifest.write_text(json.dumps([]))
    # capture the constructed app
    created = {}
    orig_init = m.MultiRunApp.__init__
    def spy_init(self, *a, **k):
        orig_init(self, *a, **k); created["app"] = self
    monkeypatch.setattr(m.MultiRunApp, "__init__", spy_init)
    m.main([str(manifest)])
    assert created["app"]._group_by is None
```

- [ ] **Step 2: Run, verify fail** (`.json` routes nowhere useful; no `--group-by`).

- [ ] **Step 3: Implement dispatch** (minimal manual flag parse, matching the file's existing style)

```python
from .resolver import explicit_resolver, glob_resolver, manifest_resolver, ref_from_path


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    group_by: str | None = None
    if "--group-by" in args:
        i = args.index("--group-by")
        try:
            group_by = args[i + 1]
        except IndexError:
            print("usage: --group-by <attr>", file=sys.stderr)
            return 2
        del args[i : i + 2]
    if len(args) < 1:
        print("usage: runstate-tui <run.db> | <dir> | <manifest.json> | <run.db> [<run.db> ...]", file=sys.stderr)
        return 2
    if len(args) == 1 and Path(args[0]).is_dir():
        root = args[0]
        MultiRunApp(glob_resolver(root), Env(clock=time.time),
                    group_by=group_by, empty_hint=f"watching {root}/**/*.db — no runs yet").run()
        return 0
    if len(args) == 1 and Path(args[0]).is_file() and Path(args[0]).suffix == ".json":
        path = args[0]
        MultiRunApp(manifest_resolver(path), Env(clock=time.time),
                    group_by=group_by, empty_hint=f"watching {path} — no runs yet").run()
        return 0
    if len(args) >= 2:
        MultiRunApp(explicit_resolver([ref_from_path(p) for p in args]),
                    Env(clock=time.time), group_by=group_by).run()
        return 0
    SingleRunApp(ref_from_path(args[0]), Env(clock=time.time)).run()
    return 0
```

- [ ] **Step 4: Run, verify pass** (+ existing CLI tests still green — dir/db/two-path branches intact).

- [ ] **Step 5: Commit** — `git commit -m "feat: CLI dispatch for .json manifest + --group-by"`.

---

### Task 6: Grouped-table snapshot scene

**Files:**
- Create: `tests/scenarios/test_grouped_table.py` (mirror `tests/scenarios/test_table_plane.py`)
- Snapshot: `tests/scenarios/__snapshots__/test_grouped_table/...svg` (generated + inspected)

**Interfaces:**
- Consumes: `MultiRunApp(manifest_resolver_or_lambda, group_by="scenario")`, `snap_compare`.

- [ ] **Step 1: Read `tests/scenarios/test_table_plane.py`** to match its `snap_compare` harness (seeded
  logs under a fixed clock, `run_test` driver).

- [ ] **Step 2: Write the snapshot test** — a 2-scenario manifest driven under a fixed clock, grouped by
  `scenario`, asserting `snap_compare(app)`.

- [ ] **Step 3: Generate the snapshot** with `--snapshot-update`, then **render the SVG to PNG and LOOK**
  (`cairosvg`): confirm two `── <scenario> ──` header rows, rows grouped and label-sorted beneath each,
  columns aligned across the whole table, the global `#summary` strip intact. Do NOT accept the snapshot
  without looking.

- [ ] **Step 4: Run the gate** (snapshot now matches).

- [ ] **Step 5: Commit** — `git commit -m "test: grouped-table snapshot scene (verified by looking)"`.

---

## Self-Review

- **Spec coverage:** §1 resolver signature → Task 1; §2 manifest + format → Task 2; §3 grouping/label →
  Tasks 3–4; §4 reconcile key = cell (shared-run one channel, drill-down) → Tasks 3–4; §5 CLI/`--group-by`
  → Task 5; §6 crash-on-bad-manifest → Task 2 (no catch, tested to raise); Testing/snapshot → Task 6. The
  mycooc emitter + atomic-write contract are out of scope (separate repo, spec §"mycooc side").
- **Type consistency:** `Attrs`, `Resolver`, `Table`, `row_key`, `_label`, `_reorder`, `group_by` are
  used with the same signatures across tasks.
- **Placeholder scan:** every code step carries real code; the one sketch (Task 4 Step 4) is an explicit
  reconcile skeleton, not a TODO — the surrounding flat reconcile it factors from is `multirun.py:194-218`.
