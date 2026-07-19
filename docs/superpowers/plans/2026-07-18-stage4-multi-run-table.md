# Stage 4 — the multi-run table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Render many runs as a `DataTable` (one row per run) — the `render = aggregate ∘ map(status_fold) ∘ resolve` functor at `|I|>1`, computed on a single owner thread over an LRU channel pool and keyed-reconciled into the widget.

**Architecture:** A single owner thread owns an LRU pool of open channels keyed by full `RunRef`. Each tick it captures `now` once, folds **every** resolved run with today's unchanged `status_fold` (fresh, under a per-frame frozen-clock `Env`), builds an immutable `Table`, and `post_message`s it to the main thread, which reconciles a `DataTable` keyed by a stable `ref_key`. No fold split, no snapshot cache, no watermark — so the §11 singleton holds by construction (the table row for `r` *is* `render_single(r)`). A main-thread watchdog raises `⚠ I/O stalled` if the owner thread wedges.

**Tech Stack:** Python 3.11, runstate (locked, public), Textual 8.2.8 (`App`, `DataTable`, `Static`, `Message`, `@work`), uv, ruff, mypy --strict, pytest, pytest-textual-snapshot.

## Global Constraints

Copied from the spec (`docs/superpowers/specs/2026-07-18-stage4-multi-run-table-design.md`).

- **Singleton invariant (§11):** `render_single(r)` must equal the table's row for `r` at a shared `now`. `status_fold`/`fold.py`/`types.py` are UNCHANGED — the table calls the same fold. Existing fold tests stay green unchanged.
- **Pool + fold-fresh, NOT watermark-gating:** each tick re-folds every resolved run with `status_fold`; the pool caches only open channel *handles*. Do NOT build `fold_log`/`derive_clock`/`LogSnapshot`/`last_seq()` gating (design review rejected it — see spec).
- **Single owner thread owns the pool:** all opens/reads/folds/evictions/closes happen on ONE thread (a `@work(thread=True, exclusive=True)` worker touching only `self._pool`); the main thread touches only the `DataTable`/banner. Never assign a reactive from the worker; cross only via `post_message`. The real serialization guarantee is "only the self-reschedule chain calls `_tick`" — `exclusive=True` does NOT serialize thread workers (it cancels the asyncio wrapper, not the OS thread).
- **Full-`RunRef` keying:** the pool keys by the whole `RunRef` tuple and the `DataTable` rows key by `ref_key(ref)` (a stable string), NOT bare `run_id` — `ref_from_path` sets `run_id = Path.stem`, so `a/run1.db` and `b/run1.db` collide on `run_id`. The `run` column *displays* `run_id`.
- **Keyed reconcile, never `clear()`+repopulate:** inside `self.batch_update()` (an `App` method — `DataTable` has no `batch_update`), `remove_row`/`update_cell`/`add_row(key=ref_key)` + `sort()` + `move_cursor` back onto the selected row. Columns are created with explicit `(label, key)` tuples — an unkeyed `add_columns("run", …)` yields anonymous `ColumnKey(None)` and every later `update_cell`/`sort` fails.
- **Per-frame `now`:** captured once per tick, threaded to every row via `frame_env = replace(env, clock=lambda: now)` (preserves `objective`/`stuck_threshold`/`liveness`).
- **Per-run integrity containment:** a byte-torn/missing/unreadable run is a loud `corrupt`/`missing`/`unreadable` ROW; the table never crashes on one bad run. On an integrity failure the pool evicts + closes that channel (fold-fresh re-detects next tick).
- **`⚠ I/O stalled` (§10, in the MVP):** a main-thread `set_interval` watchdog (independent of the owner thread) shows a banner when the last `TableReady` is older than `k × tick_interval`.
- **Owner-thread teardown:** `async on_unmount` sets `_closing`, `await self.workers.wait_for_complete()`, then `self._pool.close_all()` on the main thread; `_fold_frame` checks `_closing` at its top and its `call_from_thread` marshals are wrapped in `_TEARDOWN_ERRORS` (reused from `detail.py`).
- **Deferred (do NOT build):** glob resolver (needs `create=False`; also the live-discovery path), cells resolver, issue-flood aggregation, per-run I/O recovery beyond the banner. Explicit resolver only.
- **Gates:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` green before each commit; value types `@dataclass(frozen=True)`; public-API-only (no raw sqlite3 in runtime except the exception classes already imported for guards); no back-compat shims.

## File Structure

- **UNCHANGED:** `runstate_tui/fold.py`, `runstate_tui/types.py` — no split, no `LogSnapshot`.
- **Modify `runstate_tui/resolver.py`** — add `explicit_resolver(refs) -> Resolver` and `ref_key(ref) -> str`.
- **Modify `runstate_tui/table.py`** — extract `fold_open_channel(channel, env) -> Row` (the integrity-guarded fold, no close); repoint `open_and_fold` at it (behavior-preserving).
- **Create `runstate_tui/pool.py`** — `ChannelPool` (LRU, owner-thread-only, `RunRef`-keyed) + `Table` + `fold_frame(pool, refs, env, now) -> Table`.
- **Create `runstate_tui/multirun.py`** — `MultiRunApp` (owner worker + `TableReady` message + keyed `DataTable` reconcile + `⚠ I/O stalled` watchdog + `enter`→drill-down) + `_cells`/`_marker` helpers.
- **Modify `runstate_tui/__main__.py`** — route ≥2 run args → `MultiRunApp`, 1 arg → `SingleRunApp`.
- **Tests:** `tests/test_resolver.py`, `tests/test_table.py` (fold_open_channel parity), `tests/test_pool.py` (new), `tests/test_multirun.py` (new), `tests/test_cli.py`, `tests/scenarios/test_table_plane.py` (new).

---

### Task 1: `explicit_resolver` + `ref_key`

**Files:** Modify `runstate_tui/resolver.py`; Test `tests/test_resolver.py`.

**Interfaces:**
- Produces: `explicit_resolver(refs: list[RunRef]) -> Resolver` (a fixed, order-preserving, de-duplicated IndexSet); `ref_key(ref: RunRef) -> str` (a stable, collision-proof string key for the `DataTable` row and reverse lookup).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_resolver.py`:
```python
def test_explicit_resolver_returns_the_fixed_list_regardless_of_now():
    from runstate_tui.resolver import explicit_resolver
    refs = [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    resolve = explicit_resolver(refs)
    assert resolve(0.0) == refs and resolve(9999.0) == refs


def test_explicit_resolver_dedupes_exact_duplicates_preserving_order():
    from runstate_tui.resolver import explicit_resolver
    refs = [("a", "/root", "sqlite"), ("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    assert explicit_resolver(refs)(0.0) == [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]


def test_ref_key_distinguishes_same_basename_across_roots():
    from runstate_tui.resolver import ref_key
    a = ("run1", "/a", "sqlite")
    b = ("run1", "/b", "sqlite")   # same run_id (Path.stem), different root
    assert ref_key(a) != ref_key(b)
    assert ref_key(a) == ref_key(("run1", "/a", "sqlite"))   # stable
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_resolver.py -q` (names undefined).

- [ ] **Step 3: Implement in `runstate_tui/resolver.py`**
```python
def explicit_resolver(refs: list[RunRef]) -> Resolver:
    """A fixed IndexSet — the safe (no create=False) multi-run resolver. Exact
    duplicate refs are dropped (order preserved) so each run is one pooled channel
    and one DataTable row."""
    snapshot = list(dict.fromkeys(refs))

    def resolve(_now: float) -> list[RunRef]:
        return list(snapshot)

    return resolve


def ref_key(ref: RunRef) -> str:
    """A stable, collision-proof string key for a RunRef (run_id alone collides:
    a/run1.db and b/run1.db both have run_id 'run1'). NUL can't appear in a path,
    so it is a safe join separator."""
    return "\x00".join(ref)
```

- [ ] **Step 4: Run tests → pass. Step 5: full gates; commit** `feat(resolver): explicit_resolver + ref_key (full-RunRef keying)`.

---

### Task 2: `fold_open_channel` extraction + the LRU `ChannelPool` + `fold_frame`

**Files:** Modify `runstate_tui/table.py`; Create `runstate_tui/pool.py`; Test `tests/test_table.py`, `tests/test_pool.py`.

**Interfaces:**
- Consumes: `status_fold` (fold.py, unchanged), `_bare`/`_corrupt`/`_OPEN_ERRORS`/`locate_torn_seq` (table.py), `RunRef`/`ref_key` (resolver.py), `Env`, `Row`/`Status`/`StatusKind`.
- Produces: `fold_open_channel(channel: Channel, env: Env) -> Row` (table.py); `Table = tuple[tuple[RunRef, Row], ...]`; `ChannelPool(cap: int = 128)` with `__len__`, `reconcile(live)`, `row_for(ref, frame_env)`, `close_all()`; `fold_frame(pool, refs, env, now) -> Table` (pool.py).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_table.py` (parity — the extraction must not change behavior):
```python
def test_fold_open_channel_matches_status_fold_on_a_healthy_run(build_log):
    from runstate_tui.table import fold_open_channel
    ch = build_log([({"handle": "h", "t": 100.0}, "lifecycle.started", None)])
    env = _env(150.0)  # the module's Env helper
    assert fold_open_channel(ch, env) == status_fold(ch, env)


def test_fold_open_channel_maps_byte_torn_to_corrupt(corrupt_seq, tmp_path):
    from runstate import open_channel
    from runstate_tui.table import fold_open_channel
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started"); ch.close()
    corrupt_seq(tmp_path, "r", 1, literal="{not json")
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    assert fold_open_channel(ch, _env(150.0)).status.kind is StatusKind.CORRUPT
    ch.close()
```

Create `tests/test_pool.py`:
```python
import pytest
from runstate import open_channel
from runstate_tui.env import Env
from runstate_tui.pool import ChannelPool, fold_frame
from runstate_tui.table import render_single
from runstate_tui.types import StatusKind


def _seed(tmp_path, run_id, t=100.0):
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": t}, topic="lifecycle.started")
    ch.close()
    return (run_id, str(tmp_path), "sqlite")


def test_fold_frame_row_equals_render_single(tmp_path):
    ref = _seed(tmp_path, "r")
    env = Env(clock=lambda: 150.0)
    pool = ChannelPool(cap=8)
    table = fold_frame(pool, [ref], env, 150.0)
    assert dict(table)[ref] == render_single(ref, env)   # the §11 singleton, through the pool
    pool.close_all()


def test_fold_frame_distinguishes_same_basename_across_roots(tmp_path):
    import os
    a_dir = tmp_path / "a"; b_dir = tmp_path / "b"; os.mkdir(a_dir); os.mkdir(b_dir)
    ra = _seed(a_dir, "run1", t=100.0)
    rb = _seed(b_dir, "run1", t=200.0)
    pool = ChannelPool(cap=8)
    table = fold_frame(pool, [ra, rb], Env(clock=lambda: 300.0), 300.0)
    assert len(table) == 2 and len(pool) == 2          # two distinct runs, two channels
    assert dict(table)[ra].elapsed == 200.0 and dict(table)[rb].elapsed == 100.0
    pool.close_all()


def test_fold_frame_one_corrupt_run_does_not_sink_the_others(tmp_path, corrupt_seq):
    good = _seed(tmp_path, "good")
    _seed(tmp_path, "bad")
    corrupt_seq(tmp_path, "bad", 1, literal="{not json")
    bad = ("bad", str(tmp_path), "sqlite")
    pool = ChannelPool(cap=8)
    table = dict(fold_frame(pool, [good, bad], Env(clock=lambda: 150.0), 150.0))
    assert table[good].status.kind is not StatusKind.CORRUPT
    assert table[bad].status.kind is StatusKind.CORRUPT     # contained
    assert bad not in [r for r in pool._open]               # bad handle evicted; re-detected next tick
    pool.close_all()


def test_pool_lru_evicts_beyond_cap(tmp_path):
    refs = [_seed(tmp_path, f"r{i}") for i in range(3)]
    env = Env(clock=lambda: 150.0)
    pool = ChannelPool(cap=2)
    fold_frame(pool, refs, env, 150.0)
    assert len(pool) <= 2                                   # LRU kept the pool bounded
    pool.close_all()


def test_reconcile_closes_runs_that_left_the_resolver(tmp_path):
    a = _seed(tmp_path, "a"); b = _seed(tmp_path, "b")
    env = Env(clock=lambda: 150.0)
    pool = ChannelPool(cap=8)
    fold_frame(pool, [a, b], env, 150.0); assert len(pool) == 2
    fold_frame(pool, [a], env, 151.0); assert len(pool) == 1   # b dropped + closed
    pool.close_all()
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_pool.py tests/test_table.py -q`.

- [ ] **Step 3: Extract `fold_open_channel` in `table.py`**

Add (import `Channel`: change `from runstate.channel import Envelope` to `from runstate.channel import Channel, Envelope`):
```python
def fold_open_channel(channel: Channel, env: Env) -> Row:
    """Fold an ALREADY-OPEN channel with the integrity guards, WITHOUT closing it.
    A byte-torn (json.JSONDecodeError) -> loud `corrupt` carrying the located seq; a
    substrate fault mid-read -> `unreadable`. open_and_fold closes in its own finally;
    the pool keeps the handle and re-uses it next tick (folding fresh)."""
    try:
        return status_fold(channel, env)
    except json.JSONDecodeError:
        return _corrupt(locate_torn_seq(channel))
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return _bare(Status.unreadable())
```
Repoint `open_and_fold`'s fold body at it (leave the stat + open + `finally: channel.close()` exactly as-is):
```python
    try:
        return fold_open_channel(channel, env)
    finally:
        channel.close()
```
(Delete the now-duplicated `except json.JSONDecodeError` / `except (sqlite3.DatabaseError, …)` block from `open_and_fold` — `fold_open_channel` owns it.) The whole existing `tests/test_table.py` suite must stay green (pure refactor).

- [ ] **Step 4: Implement `runstate_tui/pool.py`**
```python
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from runstate import open_channel
from runstate.channel import Channel

from .env import Env
from .resolver import RunRef
from .table import _OPEN_ERRORS, _bare, fold_open_channel
from .types import Row, Status, StatusKind

Table = tuple[tuple[RunRef, Row], ...]

# integrity verdicts that mean the pooled handle is no good — evict + close so the
# next tick cold-opens fresh (self-healing detection; the pool holds only healthy handles).
_EVICT_KINDS = (StatusKind.CORRUPT, StatusKind.UNREADABLE)


class ChannelPool:
    """Owner-thread-ONLY LRU pool of open channels, keyed by the full RunRef.
    reader == evictor: a mid-fold channel is never closed under it. NOT thread-safe
    by design — one owner thread touches it (opens, reads, evicts, closes)."""

    def __init__(self, cap: int = 128) -> None:
        self._cap = cap
        self._open: OrderedDict[RunRef, Channel] = OrderedDict()

    def __len__(self) -> int:
        return len(self._open)

    def _evict(self, ref: RunRef) -> None:
        ch = self._open.pop(ref, None)
        if ch is not None:
            ch.close()

    def _evict_oldest(self) -> None:
        _ref, ch = self._open.popitem(last=False)
        ch.close()

    def reconcile(self, live: set[RunRef]) -> None:
        """Close + drop any pooled run no longer resolved this frame."""
        for ref in [r for r in self._open if r not in live]:
            self._evict(ref)

    def row_for(self, ref: RunRef, frame_env: Env) -> Row:
        run_id, root, backend = ref
        # stat-before-open EVERY tick (never fabricate a phantom db; catch a run whose
        # file vanished mid-session -> honest `missing`, matching open_and_fold).
        if backend == "sqlite":
            try:
                (Path(root) / f"{run_id}.db").stat()
            except FileNotFoundError:
                self._evict(ref)
                return _bare(Status.missing())
            except OSError:
                self._evict(ref)
                return _bare(Status.unreadable())
        ch = self._open.get(ref)
        if ch is None:
            try:
                ch = open_channel(run_id, root=root, backend=backend)
            except _OPEN_ERRORS:
                return _bare(Status.unreadable())   # not cached — retried next tick
            if len(self._open) >= self._cap:
                self._evict_oldest()
            self._open[ref] = ch
        self._open.move_to_end(ref)                 # LRU: most-recently-used last
        row = fold_open_channel(ch, frame_env)
        if row.status.kind in _EVICT_KINDS:
            self._evict(ref)
        return row

    def close_all(self) -> None:
        for ch in self._open.values():
            ch.close()
        self._open.clear()


def fold_frame(pool: ChannelPool, refs: list[RunRef], env: Env, now: float) -> Table:
    """One owner-thread frame. Reconcile the pool to `refs`, then fold EVERY run fresh
    under a single per-frame `now` (via a frozen-clock Env so objective/threshold/
    liveness carry through). The row for `r` == render_single(r) at this `now`."""
    frame_env = replace(env, clock=lambda: now)
    pool.reconcile(set(refs))
    return tuple((ref, pool.row_for(ref, frame_env)) for ref in refs)
```

- [ ] **Step 5: Run tests → pass** (`uv run pytest tests/test_pool.py tests/test_table.py -q`). **Step 6: full gates; commit** `feat(pool): LRU RunRef-keyed channel pool + fold_frame (fold-fresh)`.

---

### Task 3: `MultiRunApp` — owner worker + keyed reconcile + `⚠ I/O stalled` watchdog

**Files:** Create `runstate_tui/multirun.py`; Test `tests/test_multirun.py`, `tests/scenarios/test_table_plane.py`.

**Interfaces:**
- Consumes: `fold_frame`/`ChannelPool`/`Table` (Task 2), `explicit_resolver`/`ref_key`/`RunRef` (Task 1), `Env`, `Row`/`Severity`, `_TEARDOWN_ERRORS` (detail.py).
- Produces: `MultiRunApp(App[None])` — `__init__(self, resolver, env, *, tick_interval=1.0, pool_cap=128, stall_ticks=3)`; `TableReady(Message)`.

- [ ] **Step 1: Write the failing tests**

Follow the async `run_test()` harness already used in `tests/test_app.py` (build the app at a large `tick_interval` so it doesn't self-tick during the test; `await pilot.pause()` twice to let the mount tick's worker run and its `TableReady` reconcile). Create `tests/test_multirun.py`:
```python
import pytest
from runstate import open_channel
from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import explicit_resolver, ref_key
from textual.widgets import DataTable, Static


def _seed(tmp_path, run_id, t=100.0):
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": t}, topic="lifecycle.started"); ch.close()
    return (run_id, str(tmp_path), "sqlite")


@pytest.mark.asyncio
async def test_table_shows_one_keyed_row_per_run(tmp_path):
    refs = [_seed(tmp_path, "a"), _seed(tmp_path, "b")]
    app = MultiRunApp(explicit_resolver(refs), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert {k.value for k in t.rows.keys()} == {ref_key(r) for r in refs}


@pytest.mark.asyncio
async def test_row_updates_and_preserves_cursor(tmp_path):
    ref = _seed(tmp_path, "a")
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        before = t.cursor_coordinate
        w = open_channel("a", root=ref[1], backend="sqlite")
        w.send({"step": 5, "consumed_seq": 0, "t": 150.0}, topic="lifecycle.heartbeat"); w.close()
        app._tick()
        await pilot.pause(); await pilot.pause()
        assert t.cursor_coordinate == before                    # keyed reconcile, cursor kept
        assert t.get_row_index(ref_key(ref)) == 0               # still present, re-sorted


@pytest.mark.asyncio
async def test_shrinking_resolver_removes_the_row(tmp_path):
    a = _seed(tmp_path, "a"); b = _seed(tmp_path, "b")
    live = {"refs": [a, b]}
    app = MultiRunApp(lambda now: list(live["refs"]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.row_count == 2
        live["refs"] = [a]
        app._tick(); await pilot.pause(); await pilot.pause()
        assert {k.value for k in t.rows.keys()} == {ref_key(a)}


def test_io_stalled_watchdog_raises_and_clears():
    # Unit-test the watchdog directly (no threads): a stale last_ready under the fake
    # clock raises the banner; a fresh ready clears it.
    from runstate_tui.multirun import MultiRunApp
    clock = {"t": 100.0}
    app = MultiRunApp(explicit_resolver([]), Env(clock=lambda: clock["t"]),
                      tick_interval=1.0, stall_ticks=3)
    banner = Static("", id="stall")
    app._last_ready = 100.0
    clock["t"] = 104.0                                  # 4s > 3 * 1s
    assert app._is_stalled()                            # banner condition true
    app._last_ready = 104.0
    assert not app._is_stalled()                        # a fresh ready cleared it
```
Add a `snap_compare` layout test in `tests/scenarios/test_table_plane.py` (two seeded runs, assert the rendered table snapshot) following the existing snapshot-test convention.

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_multirun.py -q`.

- [ ] **Step 3: Implement `runstate_tui/multirun.py`**
```python
from __future__ import annotations

from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import DataTable, Static
from textual.worker import work

from .detail import DrillDownScreen, _TEARDOWN_ERRORS
from .env import Env
from .pool import ChannelPool, Table, fold_frame
from .resolver import Resolver, RunRef, ref_key
from .types import Row, Severity

_COLUMNS = ("run", "status", "step", "age", "value", "elapsed", "!")


def _marker(row: Row) -> str:
    """A compact per-row severity glyph (keeps the table below the ISA-18.2 flood line).
    row.severity already folds status + issues (CORRUPT/UNREADABLE -> HIGH)."""
    stops = f"⏹{len(row.undischarged_stops)}" if row.undischarged_stops else ""
    if row.severity >= Severity.HIGH:
        return f"⚠⚠{stops}"
    if row.severity >= Severity.MEDIUM:
        return f"⚠{stops}"
    return stops


def _cells(ref: RunRef, row: Row) -> tuple[str, str, str, str, str, str, str]:
    """The 7 column cells — same field semantics as format_row, one field per column."""
    run_id = ref[0]
    status = row.status.label + (f": {row.status.detail}" if row.status.detail else "")
    step = "" if row.frontier is None else str(row.frontier)
    age = "" if row.freshness is None else f"{row.freshness:.0f}s"
    if row.value is None:
        value = ""
    else:
        name, val, vstep = row.value
        value = f"{name}={val}" + (f"@{vstep}" if vstep is not None else "")
    elapsed = "" if row.elapsed is None else f"{row.elapsed:.0f}s"
    return (run_id, status, step, age, value, elapsed, _marker(row))


class TableReady(Message):
    def __init__(self, table: Table) -> None:
        self.table = table
        super().__init__()


class MultiRunApp(App[None]):
    CSS = "#stall { color: $warning; height: auto; }"
    BINDINGS = [("enter", "detail", "Detail")]

    def __init__(self, resolver: Resolver, env: Env, *, tick_interval: float = 1.0,
                 pool_cap: int = 128, stall_ticks: int = 3) -> None:
        super().__init__()
        self._resolver = resolver
        self._env = env
        self._tick_interval = tick_interval
        self._pool = ChannelPool(cap=pool_cap)
        self._stall_after = stall_ticks * tick_interval
        self._last_ready: float | None = None
        self._closing = False

    def compose(self) -> ComposeResult:
        yield Static("", id="stall")     # the watchdog banner (empty text == hidden)
        yield DataTable(id="runs")

    def on_mount(self) -> None:
        t = self.query_one("#runs", DataTable)
        t.add_columns(*[(c, c) for c in _COLUMNS])   # explicit (label, key); anonymous keys break update_cell/sort
        t.cursor_type = "row"
        self.set_interval(self._tick_interval, self._on_watchdog)   # MAIN-thread, independent of the owner thread
        self._tick()

    def _tick(self) -> None:
        # ONLY on_mount and _fold_frame's own tail may call this. That self-reschedule
        # chain is the real serialization — exclusive=True does NOT serialize thread workers.
        if not self._closing:
            self._fold_frame()

    @work(thread=True, exclusive=True)
    def _fold_frame(self) -> None:                     # the single owner thread — owns the whole pool
        if self._closing:
            return
        now = self._env.clock()
        table = fold_frame(self._pool, self._resolver(now), self._env, now)
        try:
            self.post_message(TableReady(table))                    # post_message is thread-safe on its own
            self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
        except _TEARDOWN_ERRORS:
            pass                                       # a quit landed mid-frame; drop the marshal

    def on_table_ready(self, msg: TableReady) -> None:  # MAIN thread: keyed reconcile
        self._last_ready = self._env.clock()
        t = self.query_one("#runs", DataTable)
        want = {ref_key(ref) for ref, _ in msg.table}
        sel = None
        if t.row_count:
            sel = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        with self.batch_update():                       # App.batch_update — DataTable has none
            present = {k.value for k in list(t.rows.keys())}
            for key in present:
                if key not in want:
                    t.remove_row(key)
            present &= want
            for ref, row in msg.table:
                key = ref_key(ref)
                cells = _cells(ref, row)
                if key in present:
                    for col, val in zip(_COLUMNS, cells):
                        t.update_cell(key, col, val)
                else:
                    t.add_row(*cells, key=key)
            t.sort("run")
            if sel is not None and sel in want:
                t.move_cursor(row=t.get_row_index(sel))  # sort() doesn't track the key; restore selection

    def _is_stalled(self) -> bool:
        return self._last_ready is not None and self._env.clock() - self._last_ready > self._stall_after

    def _on_watchdog(self) -> None:
        self.query_one("#stall", Static).update("⚠ I/O stalled" if self._is_stalled() else "")

    def action_detail(self) -> None:
        t = self.query_one("#runs", DataTable)
        if t.row_count == 0:
            return
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        by_key = {ref_key(r): r for r in self._resolver(self._env.clock())}   # reconstruct, no worker mutation
        ref = by_key.get(key)
        if ref is not None:
            self.push_screen(DrillDownScreen(ref, self._env, self._tick_interval))

    async def on_unmount(self) -> None:
        self._closing = True
        await self.workers.wait_for_complete()          # drain the in-flight fold (cancel can't stop the OS thread)
        self._pool.close_all()                          # now safe: owner thread idle
```
Notes for the implementer: (a) `action_detail`/`DrillDownScreen` wiring is exercised in Task 4 — it is present here so the binding resolves; keep it. (b) If `self.workers.wait_for_complete()` is not awaitable in Textual 8.2.8, confirm the correct drain call against the installed `textual/worker_manager.py` and use it — the requirement is "no main-thread pool touch until the owner thread is idle." (c) Confirm `Static.update("")` hides the banner (empty renderable) in 8.2.8; if it leaves a blank line, toggle `display` instead.

- [ ] **Step 4: Run tests → pass** (the async tests may need `await pilot.pause()` counts adjusted for the threaded worker + message round-trip — match `tests/test_app.py`). **Step 5: full gates; commit** `feat(multirun): MultiRunApp — owner-thread pool, keyed reconcile, I/O-stalled watchdog`.

---

### Task 4: `enter` → drill-down for the selected run

**Files:** Modify `runstate_tui/multirun.py`; Test `tests/test_multirun.py`.

(The `action_detail` code shipped in Task 3; this task pins its behavior with a test and verifies the escape-return path.)

- [ ] **Step 1: Write the failing test**
```python
@pytest.mark.asyncio
async def test_enter_opens_drilldown_for_selected_run_and_escape_returns(tmp_path):
    from runstate_tui.detail import DrillDownScreen
    a = _seed(tmp_path, "a"); b = _seed(tmp_path, "b")
    app = MultiRunApp(explicit_resolver([a, b]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DrillDownScreen)
        assert app.screen._ref in (a, b)            # the SELECTED run's ref (adapt attr name to DrillDownScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DrillDownScreen)
```
Adapt `app.screen._ref` to `DrillDownScreen`'s actual ref attribute (read `detail.py`).

- [ ] **Step 2: Run to verify it fails/passes; adjust `action_detail` only if the test reveals a gap** (e.g. the cursor-key→ref mapping). **Step 3:** no new code expected beyond Task 3's `action_detail`; if the ref attribute differs, fix the assertion or the lookup.

- [ ] **Step 4: pass. Step 5: gates; commit** `test(multirun): enter opens the drill-down for the selected run`.

---

### Task 5: CLI wiring

**Files:** Modify `runstate_tui/__main__.py`; Test `tests/test_cli.py`.

- [ ] **Step 1: Write the failing tests**
```python
def test_two_paths_construct_multirun(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m
    made = {}
    monkeypatch.setattr(m.MultiRunApp, "run", lambda self: made.setdefault("multi", self))
    m.main([str(tmp_path / "a.db"), str(tmp_path / "b.db")])
    assert "multi" in made


def test_one_path_still_constructs_single(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m
    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    m.main([str(tmp_path / "a.db")])
    assert "single" in made


def test_no_args_is_usage_error():
    import runstate_tui.__main__ as m
    assert m.main([]) == 2
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_cli.py -q`.

- [ ] **Step 3: Implement in `runstate_tui/__main__.py`**

Import `MultiRunApp` and `explicit_resolver`; branch on the number of run paths:
```python
    runs = [a for a in argv if ...]  # keep the existing arg parsing
    if len(runs) >= 2:
        from .multirun import MultiRunApp
        from .resolver import explicit_resolver, ref_from_path
        MultiRunApp(explicit_resolver([ref_from_path(p) for p in runs]), Env(clock=time.time)).run()
        return 0
    # else: the existing single-run path (SingleRunApp) unchanged
```
Update the usage string to accept one-or-more `<run.db>` paths.

- [ ] **Step 4: Run tests → pass. Step 5: full gates; commit** `feat(cli): multiple run paths -> MultiRunApp`.

---

## Self-Review

- **Spec coverage:** explicit resolver + ref_key (T1); `fold_open_channel` extraction + LRU RunRef-keyed pool + fold-fresh `fold_frame` + corrupt-containment + per-frame `now` + LRU + reconcile (T2); owner-thread worker + keyed reconcile (C's `batch_update`/column-key/`move_cursor` fixes) + `⚠ I/O stalled` watchdog + async teardown drain (T3); drill-down for the selected run (T4); CLI (T5). Rejected watermark-gating and deferred glob/cells/issue-flood/I-O-recovery intentionally absent. **fold.py/types.py unchanged** (no split).
- **Singleton:** T2's `test_fold_frame_row_equals_render_single` proves the pooled path equals `render_single` at a shared `now`; T2's parity test proves `fold_open_channel` didn't shift behavior.
- **Red-team fixes folded in:** C-crit-1 (`self.batch_update()`, `(label,key)` columns, `move_cursor`), C-crit-2 + C-imp-4 (async `on_unmount` drain + `_TEARDOWN_ERRORS` guard + `_closing`), C-imp-3 (serialization comment), C-minor (`action_detail` reconstructs refs, direct `post_message`); B-2b (full-RunRef pool key + `ref_key` row key + distinctness test); B-1a/2a dissolved by fold-fresh; A-crit (`⚠ I/O stalled` watchdog in the MVP).
- **Placeholders:** T3 flags three "confirm against Textual 8.2.8" points (`wait_for_complete` awaitability, `Static.update("")` hiding, `pilot.pause()` counts) — the implementer verifies empirically, not guesses; all logic is complete. T4 flags one (`DrillDownScreen`'s ref attribute name) — read `detail.py`.
- **Type consistency:** `explicit_resolver`, `ref_key`, `fold_open_channel(channel, env)`, `Table`, `ChannelPool(cap=…)`/`row_for`/`reconcile`/`close_all`, `fold_frame(pool, refs, env, now)`, `MultiRunApp(resolver, env, *, tick_interval, pool_cap, stall_ticks)`, `TableReady`, `_cells`/`_marker` names match across tasks.
