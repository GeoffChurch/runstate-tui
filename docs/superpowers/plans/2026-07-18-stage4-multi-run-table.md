# Stage 4 — the multi-run table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Render many runs as a `DataTable` (one row per run) — the `render = aggregate ∘ map(status_fold) ∘ resolve` functor at `|I|>1`, watermark-gated on a single owner thread, keyed-reconciled into the widget.

**Architecture:** `status_fold` factorizes into `fold_log` (log-triggered, cacheable) + `derive_clock` (clock-triggered), with `status_fold = derive_clock ∘ fold_log` so the §11 singleton is preserved by construction. A single owner thread owns an LRU channel pool and, each tick, re-runs `fold_log` only for runs whose `last_seq()` moved (else re-derives the clock factors from the cached snapshot); it builds an immutable `Table` and posts it to the main thread, which reconciles a `DataTable` keyed by `run_id`.

**Tech Stack:** Python 3.11, runstate (locked, now public), Textual 8.2.8 (`App`, `DataTable`, `Message`, `@work`), uv, ruff, mypy --strict, pytest, pytest-textual-snapshot.

## Global Constraints

Copied from the spec (`docs/superpowers/specs/2026-07-18-stage4-multi-run-table-design.md`).

- **Singleton invariant (§11):** `render_single(r)` must equal the table's row for `r`. The fold split is behavior-preserving: `status_fold(ch, env) == derive_clock(fold_log(ch), env.clock())`. Existing fold tests must stay green unchanged.
- **Single owner thread owns the pool:** all opens/reads/`fold_log`s/evictions/closes happen on ONE thread (realized as a `@work(thread=True, exclusive=True)` worker touching only `self._pool`); the main thread touches only the `DataTable`. Never assign a reactive from the worker; cross only via `post_message`.
- **Keyed reconcile, never `clear()`+repopulate:** inside `batch_update()`, `remove_row`/`update_cell`/`add_row(key=run_id)` + `sort()` + `move_cursor`.
- **Per-frame `now`:** captured once per tick, threaded to every row.
- **Watermark-gate:** `fold_log` re-runs only when `channel.last_seq() > last_folded_seq`; `derive_clock` runs every tick.
- **Per-run integrity containment:** a byte-torn/missing/unreadable run is a loud `corrupt`/`missing`/`unreadable` ROW; the table never crashes on one bad run (the per-run `open_and_fold` boundary contains it — post-reshape behavior).
- **Deferred (do NOT build):** glob resolver (needs `create=False`), cells resolver, issue-flood aggregation, `⚠ I/O stalled`-after-k-ticks. Explicit resolver only.
- **Gates:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` green before each commit; value types `@dataclass(frozen=True)`; public-API-only; no back-compat shims.

## File Structure

- **Modify `runstate_tui/types.py`** — add `LogSnapshot` (frozen).
- **Modify `runstate_tui/fold.py`** — `fold_log(channel) -> LogSnapshot`, `derive_clock(snap, now) -> Row`, `status_fold = derive_clock ∘ fold_log`.
- **Modify `runstate_tui/resolver.py`** — `explicit_resolver(refs) -> Resolver`.
- **Create `runstate_tui/pool.py`** — `ChannelPool` (LRU, owner-thread-only) + `Table` + `fold_frame(pool, refs, env, now) -> Table`.
- **Create `runstate_tui/multirun.py`** — `MultiRunApp` (owner worker + `TableReady` message + keyed `DataTable` reconcile + `enter`→drill-down).
- **Modify `runstate_tui/__main__.py`** — route ≥2 run args → `MultiRunApp`, 1 arg → `SingleRunApp`.
- **Tests:** `tests/test_fold.py` (split + singleton), `tests/test_pool.py` (new), `tests/test_multirun.py` (new), `tests/scenarios/test_table_plane.py` (new).

---

### Task 1: The fold split — `fold_log` + `derive_clock`

**Files:** Modify `runstate_tui/types.py`, `runstate_tui/fold.py`; Test `tests/test_fold.py`.

**Interfaces:**
- Produces: `LogSnapshot` (frozen); `fold_log(channel: Channel) -> LogSnapshot`; `derive_clock(snap: LogSnapshot, now: float, stuck_threshold: float) -> Row`; `status_fold(channel, env) -> Row` unchanged externally, now `= derive_clock(fold_log(channel), env.clock(), env.stuck_threshold)`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fold.py`:
```python
def test_fold_log_is_now_independent(build_log):
    from runstate_tui.fold import fold_log
    ch = build_log([
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
    ])
    snap = fold_log(ch)
    assert snap.last_activity == 140.0 and snap.started_t == 100.0 and snap.frontier == 7


def test_status_fold_equals_derive_clock_of_fold_log(build_log):
    from runstate_tui.fold import fold_log, derive_clock
    ch = build_log([
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
        ({"value": 0.03, "step": 7, "t": 140.0}, "value", "loss"),
    ])
    env = _env(150.0, objective="loss")
    composed = derive_clock(fold_log(ch), 150.0, env.stuck_threshold)
    assert composed == status_fold(ch, env)  # singleton-preserving composition


def test_derive_clock_resolves_live_stale_from_snapshot(build_log):
    from runstate_tui.fold import fold_log, derive_clock
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 100.0}, "lifecycle.heartbeat", None)])
    snap = fold_log(ch)
    assert derive_clock(snap, 100.0, 60.0).status.kind is StatusKind.LIVE
    assert derive_clock(snap, 1000.0, 60.0).status.kind is StatusKind.STALE  # same snap, later now
```
(These + the ENTIRE existing `test_fold.py` suite are the spec: `status_fold` behavior must be unchanged.)

- [ ] **Step 2: Run to verify they fail** (`uv run pytest tests/test_fold.py -q` — `fold_log`/`derive_clock` undefined).

- [ ] **Step 3: `types.py` — add `LogSnapshot`**
```python
from runstate import RunResult  # add to imports

@dataclass(frozen=True)
class LogSnapshot:
    terminal: RunResult | None
    last_activity: float | None            # freshness anchor (raw; None if absent or non-finite)
    started_t: float | None                # elapsed anchor (first started.t; None if absent/non-finite)
    frontier: int | None
    value: tuple[str, object, int | None] | None
    episode: str | None
    undischarged_stops: tuple[Envelope, ...]
    live_demand: tuple[Envelope, ...]
    issues: tuple[Issue, ...]              # log-derived issues (malformed, non-finite-la)
    integrity: Status | None = None       # a whole-run override (unused by fold_log; open_and_fold sets bare rows directly)
```

- [ ] **Step 4: `fold.py` — split the fold**

Refactor so `fold_log` gathers every log-derived factor + the anchors (no `now`), and `derive_clock` does the `now`-dependent work. `status_fold` becomes the composition. Concretely:
```python
def fold_log(channel: Channel) -> LogSnapshot:
    issues: list[Issue] = []
    terminal, term_issue = guarded(peek_terminal, channel)
    if term_issue is not None:
        issues.append(term_issue)
    la, la_issue = guarded(last_activity, channel)
    if la_issue is not None:
        issues.append(la_issue)
    if la is not None and not math.isfinite(la):
        issues.append(Issue(kind=IssueKind.MALFORMED, severity=Severity.HIGH,
                            message="activity timestamp is not finite (garbage clock)",
                            detail=f"last_activity={la!r}"))
        la = None
    frontier, frontier_issue = guarded(progress, channel)
    if frontier_issue is not None:
        issues.append(frontier_issue)
    value, value_issue = guarded(lambda ch: read_value(ch, None) if False else read_value(ch, _OBJ.get()), channel)
    # NOTE: value needs env.objective — thread it: fold_log takes `objective: str | None`.
    ...
```
**Adjust the signature:** `fold_log(channel, objective)` needs the objective (a log-read parameter, not clock). Keep `fold_log(channel: Channel, objective: str | None) -> LogSnapshot`; `status_fold` passes `env.objective`. Gather: `terminal`, `la` (freshness anchor, non-finite→None+issue), `frontier`, `value` (via `read_value(ch, objective)` guarded), `episode`+`episode_seq` (the `_episode` guarded read from Stage 3, used to episode-scope stops), `undischarged_stops` (filtered `seq > episode_seq`), `live_demand`, `started_t` (the first-`started.t` via a guarded read, non-finite→None), and `issues`. Return the `LogSnapshot`.

```python
def derive_clock(snap: LogSnapshot, now: float, stuck_threshold: float) -> Row:
    issues = list(snap.issues)
    # elapsed
    if snap.started_t is None:
        elapsed = None
    elif snap.started_t > now:
        elapsed = 0.0
        issues.append(Issue(kind=IssueKind.SKEW_SUSPECTED, severity=Severity.MEDIUM,
                            message="run epoch is in the future (clock skew)",
                            detail=f"started.t={snap.started_t} > now={now}"))
    else:
        elapsed = now - snap.started_t
    # freshness + status
    la = snap.last_activity
    freshness = None if la is None else max(0.0, now - la)
    if la is not None and la > now:
        issues.append(Issue(kind=IssueKind.SKEW_SUSPECTED, severity=Severity.MEDIUM,
                            message="last activity is in the future (clock skew)"))
    if snap.terminal is not None:
        status = Status.terminal(snap.terminal.outcome, detail=snap.terminal.error)
    elif la is None:
        status = Status.pending()
    else:
        status = Status.live() if freshness <= stuck_threshold else Status.stale()
    return Row(status=status, frontier=snap.frontier, freshness=freshness, value=snap.value,
               elapsed=elapsed, episode=snap.episode, undischarged_stops=snap.undischarged_stops,
               live_demand=snap.live_demand, issues=tuple(issues))


def status_fold(channel: Channel, env: Env) -> Row:
    return derive_clock(fold_log(channel, env.objective), env.clock(), env.stuck_threshold)
```
Preserve every current behavior (freshness clamp, both skew issues, non-finite guards, episode-scope, `Status.detail`). Delete the old inline `reconcile_status`/`read_elapsed`/`status_fold` bodies (or keep `reconcile_status`/`read_elapsed` as thin helpers used by `fold_log`/`derive_clock` if cleaner — implementer's call, but the split must be real and `status_fold` must be the composition). Use `env.stuck_threshold` (confirm it exists on `Env`).

- [ ] **Step 5: Run tests → all `test_fold.py` green** (existing + new). **Step 6: full gates; commit** `refactor(fold): split status_fold into fold_log + derive_clock`.

---

### Task 2: `explicit_resolver`

**Files:** Modify `runstate_tui/resolver.py`; Test `tests/test_resolver.py`.

- [ ] **Step 1: Failing test**
```python
def test_explicit_resolver_returns_the_fixed_list_regardless_of_now():
    from runstate_tui.resolver import explicit_resolver
    refs = [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    resolve = explicit_resolver(refs)
    assert resolve(0.0) == refs and resolve(9999.0) == refs
```
- [ ] **Step 2: fail. Step 3:**
```python
def explicit_resolver(refs: list[RunRef]) -> Resolver:
    """A fixed IndexSet — the safe (no create=False) multi-run resolver."""
    snapshot = list(refs)
    def resolve(_now: float) -> list[RunRef]:
        return list(snapshot)
    return resolve
```
- [ ] **Step 4: pass; gates; commit** `feat(resolver): explicit_resolver (fixed IndexSet)`.

---

### Task 3: The LRU pool + watermark-gated `fold_frame` → `Table`

**Files:** Create `runstate_tui/pool.py`; Test `tests/test_pool.py`.

**Interfaces:**
- Consumes: `fold_log`/`derive_clock` (Task 1), `open_and_fold`/`_bare`/`_corrupt` guards (table.py), `RunRef`, `Env`, `Row`.
- Produces: `Table = tuple[tuple[str, Row], ...]` (ordered `(run_id, Row)`); `ChannelPool(cap=128)` with `.close_all()`; `fold_frame(pool, refs, env, now) -> Table`.

- [ ] **Step 1: Failing tests**
```python
def test_fold_frame_watermark_gates_idle_runs(tmp_path, monkeypatch):
    # an idle run's fold_log is NOT re-run across ticks; a grown run IS.
    from runstate import open_channel
    from runstate_tui.pool import ChannelPool, fold_frame
    import runstate_tui.pool as poolmod
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    ref = ("r", str(tmp_path), "sqlite")
    calls = {"n": 0}
    real = poolmod.fold_log
    monkeypatch.setattr(poolmod, "fold_log", lambda c, o: (calls.__setitem__("n", calls["n"] + 1), real(c, o))[1])
    pool = ChannelPool(cap=8)
    env = Env(clock=lambda: 150.0)
    fold_frame(pool, [ref], env, 150.0); n1 = calls["n"]
    fold_frame(pool, [ref], env, 151.0); assert calls["n"] == n1   # idle -> not re-folded
    w = open_channel("r", root=tmp_path, backend="sqlite"); w.send({"step": 1, "consumed_seq": 0, "t": 152.0}, topic="lifecycle.heartbeat"); w.close()
    fold_frame(pool, [ref], env, 153.0); assert calls["n"] == n1 + 1  # grew -> re-folded
    pool.close_all()


def test_fold_frame_one_corrupt_run_does_not_sink_the_others(tmp_path):
    from runstate import open_channel
    from runstate_tui.pool import ChannelPool, fold_frame
    from runstate_tui.helpers import corrupt_seq  # or inline the raw UPDATE
    good = open_channel("good", root=tmp_path, backend="sqlite"); good.send({"handle":"h","t":100.0}, topic="lifecycle.started"); good.close()
    bad = open_channel("bad", root=tmp_path, backend="sqlite"); bad.send({"handle":"h","t":100.0}, topic="lifecycle.started"); bad.close()
    corrupt_seq(tmp_path, "bad", 1, literal="{not json")
    pool = ChannelPool(cap=8)
    table = fold_frame(pool, [("good", str(tmp_path), "sqlite"), ("bad", str(tmp_path), "sqlite")], Env(clock=lambda: 150.0), 150.0)
    rows = dict(table)
    assert rows["good"].status.kind is not StatusKind.CORRUPT
    assert rows["bad"].status.kind is StatusKind.CORRUPT   # contained, table survived
    pool.close_all()


def test_pool_lru_evicts_beyond_cap(tmp_path):
    # cap=2, resolve 3 runs -> at most 2 channels open; the LRU one is closed.
    ... (build 3 sqlite runs; ChannelPool(cap=2); fold_frame over all 3; assert pool holds <= 2 open channels)
```
- [ ] **Step 2: fail. Step 3: implement `pool.py`.**

`ChannelPool`: an `OrderedDict[run_id -> (channel, last_folded_seq, LogSnapshot)]` (owner-thread-only). Methods: `snapshot_for(ref, env, now) -> Row` that (a) stat-before-open + open via the pooled channel (open on miss; LRU-evict+close the oldest beyond `cap`), (b) `if channel.last_seq() > last_folded_seq: snap = fold_log(channel, env.objective)` else reuse cached snap, (c) `derive_clock(snap, now, env.stuck_threshold)`. Integrity (missing/unreadable/corrupt) → a bare `Row` via the same guards `open_and_fold` uses (reuse `_bare`/`_corrupt`; a byte-torn in `fold_log` is caught here → `_corrupt(locate_torn_seq(channel))`, NOT propagated). `close_all()` closes every channel.

`fold_frame(pool, refs, env, now) -> Table`: `pool.reconcile(set(run_ids))` (evict/close runs no longer resolved); then `tuple((r[0], pool.snapshot_for(r, env, now)) for r in refs)`. Capture `now` is the caller's (per-frame).

- [ ] **Step 4: pass (incl. the gating counter, corrupt containment, LRU). Step 5: gates; commit** `feat(pool): LRU channel pool + watermark-gated fold_frame`.

---

### Task 4: `MultiRunApp` — owner worker + keyed `DataTable` reconcile

**Files:** Create `runstate_tui/multirun.py`; Test `tests/test_multirun.py`.

**Interfaces:**
- Consumes: `fold_frame`/`ChannelPool`/`Table` (Task 3), `Resolver` (Task 2), `Env`, `format_row`'s field logic (for cells).
- Produces: `MultiRunApp(App[None])` — `__init__(self, resolver, env, tick_interval=1.0, pool_cap=128)`.

- [ ] **Step 1: Failing tests** (via `run_test`): the table shows N runs (one row per run_id); appending activity to a run updates its row (cursor preserved); a resolver whose set shrinks removes the row. Assert cell content via the `DataTable` API and `run_id` row keys. Snapshot the layout (`snap_compare`). (Use `held_writer_sqlite_run` / seeded sqlite runs + `advance_tick`-style manual ticks; build the app at `tick_interval=999` and drive `_tick` manually.)

- [ ] **Step 2: fail. Step 3: implement `multirun.py`.**
```python
class TableReady(Message):
    def __init__(self, table: Table) -> None:
        self.table = table
        super().__init__()

class MultiRunApp(App[None]):
    BINDINGS = [("enter", "detail", "Detail")]
    def __init__(self, resolver, env, tick_interval=1.0, pool_cap=128):
        super().__init__(); self._resolver = resolver; self._env = env
        self._tick_interval = tick_interval; self._pool = ChannelPool(cap=pool_cap)
    def compose(self): yield DataTable(id="runs")
    def on_mount(self):
        t = self.query_one("#runs", DataTable)
        t.add_columns("run", "status", "step", "age", "value", "elapsed", "!")
        t.cursor_type = "row"; self._tick()
    def _tick(self): self._fold_frame()
    @work(thread=True, exclusive=True)
    def _fold_frame(self):                      # the single owner thread
        now = self._env.clock()
        table = fold_frame(self._pool, self._resolver(now), self._env, now)
        self.call_from_thread(self.post_message, TableReady(table))
        self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
    def on_table_ready(self, msg: TableReady):  # main thread: keyed reconcile
        t = self.query_one("#runs", DataTable)
        want = {rid for rid, _ in msg.table}
        with t.batch_update():
            for key in [k.value for k in list(t.rows.keys())]:
                if key not in want: t.remove_row(key)
            for rid, row in msg.table:
                cells = _cells(rid, row)
                if rid in t.rows: 
                    for col, val in zip(_COLS, cells): t.update_cell(rid, col, val)
                else: t.add_row(*cells, key=rid)
            t.sort("run")
    def on_unmount(self): self._pool.close_all()
```
Provide `_cells(rid, row)` (mirror `format_row`'s fields: run_id, status label, step, age, value, elapsed, an issue marker like `⚠{maxsev}` or "") and `_COLS` (the column keys). Verify the exact `DataTable` API for row-key iteration / `update_cell(row_key, column_key, value)` / `rows.keys()` against Textual 8.2.8 (adapt if the accessor differs). `format_row`'s field-formatting helpers should be reused, not reinvented — extract them from `format.py` if needed.

- [ ] **Step 4: pass (3× for the threaded ticks). Step 5: gates; commit** `feat(multirun): MultiRunApp — owner-thread pool + keyed DataTable reconcile`.

---

### Task 5: `enter` → drill-down for the selected run

**Files:** Modify `runstate_tui/multirun.py`; Test `tests/test_multirun.py`.

- [ ] **Step 1: Failing test** — `enter` on the selected row pushes a `DrillDownScreen` for that run's ref; `escape` returns. (Map the cursor's `run_id` back to its `RunRef` — keep a `{run_id: RunRef}` map updated each frame, or reconstruct from the resolver.)
- [ ] **Step 2: fail. Step 3:**
```python
    def action_detail(self):
        t = self.query_one("#runs", DataTable)
        if t.row_count == 0: return
        run_id = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        ref = self._ref_by_id.get(run_id)
        if ref is not None:
            self.push_screen(DrillDownScreen(ref, self._env, self._tick_interval))
```
Maintain `self._ref_by_id` (updated in `_fold_frame` or from the resolver). Import `DrillDownScreen`.
- [ ] **Step 4: pass (3×). Step 5: gates; commit** `feat(multirun): enter opens the drill-down for the selected run`.

---

### Task 6: CLI wiring

**Files:** Modify `runstate_tui/__main__.py`; Test `tests/test_cli.py`.

- [ ] **Step 1: Failing test** — `main(["a.db", "b.db"])` constructs a `MultiRunApp` (monkeypatch its `.run`); `main(["a.db"])` still constructs `SingleRunApp`; `main([])` → usage → 2.
- [ ] **Step 2: fail. Step 3:** in `main`, `if len(runs) >= 2: MultiRunApp(explicit_resolver([ref_from_path(p) for p in runs]), Env(clock=time.time)).run()` else the existing single-run path. Update usage text to accept multiple paths.
- [ ] **Step 4: pass; gates; commit** `feat(cli): multiple run paths -> MultiRunApp`.

---

## Self-Review

- **Spec coverage:** fold split + singleton (T1), explicit resolver (T2), LRU pool + watermark-gate + corrupt-containment + per-frame now (T3), owner-thread + keyed reconcile + columns (T4), drill-down for selected (T5), CLI (T6). Deferred set (glob/cells/issue-flood/I-O-stalled) intentionally absent.
- **Singleton:** T1's `test_status_fold_equals_derive_clock_of_fold_log` + the whole unchanged `test_fold.py` guard the composition; add an explicit table-vs-single-run singleton test in T4 (drive a tick, assert the table's row for `r` equals `render_single(r)`).
- **Placeholders:** T1/T3/T4 note two "verify the exact API" points (`Env.stuck_threshold`, the Textual 8.2.8 `DataTable` row-key/`update_cell` accessors) — the implementer confirms empirically, not guesses; all other code is complete.
- **Type consistency:** `LogSnapshot`, `fold_log(channel, objective)`, `derive_clock(snap, now, stuck_threshold)`, `Table`, `ChannelPool`, `fold_frame`, `explicit_resolver`, `MultiRunApp`, `TableReady` names match across tasks.
