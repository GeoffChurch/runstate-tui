# Stage 3 — the drill-down detail view Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a drill-down detail `Screen` (`enter` opens, `escape` returns) that renders the run's full `Row` — the truth-quintet + episode + full issues + undischarged stops + live demand — plus a **live, incremental raw-envelope log tail** (tail -f).

**Architecture:** The `Row` gains three cheap folded aggregations (episode, undischarged_stops, live_demand). The log tail is NOT a `Row` factor — it's a parameterized query (`read_log_delta`), consumed on-demand by a stateful, incremental log pane (cursor + `last_seq()` watermark + `read(after=cursor)`), off-thread. The drill-down `Screen` is otherwise a pure projection of the `Row`, re-folded each tick for a live header. Filtering/pagination are deferred (the query seam is filter-shaped but unused). See the [event-driven architecture](../../../.claude note) rationale — fold vs query, "poll a watermark, apply the delta, never rebuild."

**Tech Stack:** Python 3.11, runstate (locked), Textual 8.2.8 (`Screen`, `RichLog`, `Pilot`), uv, ruff, mypy --strict, pytest, **pytest-textual-snapshot** (new dev-dep, for SVG snapshot tests).

## Global Constraints

- **Fold vs query:** episode/undischarged_stops/live_demand are cheap parameter-free aggregations → they go in the `Row`. The raw log tail is a parameterized query → `read_log_delta(ref, after, *, limit=None)`, on-demand, NOT a `Row` field.
- **Incremental, off-thread log pane:** the drill-down reads the log delta on a `@work(thread=True, exclusive=True)` worker (never the render thread), touches widgets only via `self.app.call_from_thread(...)`, gates on `last_seq() > cursor`, appends only `read(after=cursor)`, advances the cursor, and **stops its tick when unmounted** (guard on `self.is_mounted`).
- **Crash-on-torn consistency (precursor):** a byte-torn record encountered by the drill-down's fold or log read propagates `json.JSONDecodeError` → crashes (drill-down workers use default `exit_on_error=True`). A missing/unreadable run yields an empty log delta (no phantom-db fabrication; the header shows `missing`/`unreadable`). A mid-read substrate fault (`sqlite3.DatabaseError`/`OSError`) → empty delta.
- **Pure detail rendering:** `format_detail(row)` and `format_envelope(env)` are pure functions (no I/O), unit-tested independently.
- **Public-API-only**; frozen value types; **no back-compat shims**; ruff (E,F,I,UP,B, line 100) + `ruff format` + mypy `--strict` + pytest all green before each commit.

## Verified facts (spiked)

- Observables: `latest_episode(ch)` → `Envelope | None`, handle = `.body.get("handle")`; `undischarged_stops(ch)` / `live_demand(ch)` → `list[Envelope]`. `Envelope` = `(seq, topic, name, request_id, body)`, imported `from runstate.channel import Envelope`.
- Incremental log pane (spiked end-to-end in `run_test`): a `Screen` pushed on `enter`; `escape` pops via the `("escape", "app.pop_screen", …)` binding; a `RichLog` fed via `self.app.call_from_thread(rich_log.write, line)`; `if ch.last_seq() > cursor: for e in ch.read(after=cursor): append; cursor = e.seq`; the worker guards `if not self.is_mounted: return`, so the tick stops after pop. `call_from_thread`/`is_running` are **App** methods — from a `Screen`, use `self.app.call_from_thread` and `self.is_mounted`.
- Headless screenshots: `app.export_screenshot()` (SVG string) / `app.save_screenshot(name, path=…)` work inside `run_test()` with no display. `pytest-textual-snapshot` provides the `snap_compare` fixture.

## File Structure

- **Modify `runstate_tui/types.py`** — `Row` gains `undischarged_stops: tuple[Envelope, ...]`, `live_demand: tuple[Envelope, ...]`.
- **Modify `runstate_tui/fold.py`** — `status_fold` populates `episode`, `undischarged_stops`, `live_demand`.
- **Modify `runstate_tui/table.py`** — `_bare` fills the new fields with `()`; add `read_log_delta`.
- **Modify `runstate_tui/format.py`** — add `format_detail(row)` and `format_envelope(env)`; a compact `⏹{n}` flag in `format_row` when stops are pending.
- **Create `runstate_tui/detail.py`** — `DrillDownScreen`.
- **Modify `runstate_tui/app.py`** — `enter` → push `DrillDownScreen`.
- **Modify `pyproject.toml`** — add `pytest-textual-snapshot` to the dev group.
- **Tests:** `tests/conftest.py` (a `rich_run` fixture), `tests/test_fold.py`, `tests/test_format.py`, `tests/test_table.py`, `tests/test_detail.py` (new — behavioral + snapshot), `tests/test_app.py`.

---

### Task 1: Row enrichment — episode + undischarged_stops + live_demand

**Files:** Modify `runstate_tui/types.py`, `runstate_tui/fold.py`, `runstate_tui/table.py`; Test `tests/test_fold.py`, `tests/conftest.py`.

**Interfaces:**
- Produces: `Row.undischarged_stops: tuple[Envelope, ...]`, `Row.live_demand: tuple[Envelope, ...]`, `Row.episode` now populated by `status_fold` (was always `None`).

- [ ] **Step 1: Add a `rich_run` fixture + the failing fold test**

In `tests/conftest.py`, add (uses the memory backend like `build_log`):

```python
@pytest.fixture
def rich_run(build_log):
    """A live run with an episode, a heartbeat, a value, an undischarged stop, and live demand."""
    def _build():
        return build_log(
            [
                ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
                ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
                ({"value": 0.03, "step": 7, "t": 140.0}, "value", "loss"),
                ({"schedule": {}, "names": ["loss"]}, "control.subscribe", "webui:sub1"),
                ({}, "control.stop", "webui:stop1"),
            ]
        )
    return _build
```

Note: `build_log`'s `_build(records)` takes `(body, topic, name)` triples and sends via `writer.send(body, topic=topic, name=name)`. A `control.subscribe`/`control.stop` needs a `request_id`, not a `name` — check `build_log`: it calls `send(body, topic=topic, name=name)`. **Adjust `build_log`** to also thread a 4th optional `request_id` element, OR (simpler) in `rich_run` pass the request_id as `name` only if that's what the observables key on. VERIFY against `undischarged_stops`/`live_demand`: they match on the `control.stop`/`control.subscribe` envelopes regardless of name; `request_id` is the correlation key. If `build_log` can't set `request_id`, extend its tuple to `(body, topic, name, request_id=None)` and pass `request_id=` to `send`. Do the minimal extension needed so the fixture produces one undischarged stop and one live-demand envelope.

Add to `tests/test_fold.py`:

```python
def test_status_fold_populates_episode_stops_and_demand(rich_run):
    row = status_fold(rich_run(), _env(150.0, objective="loss"))
    assert row.episode == "local://h/1"
    assert len(row.undischarged_stops) == 1
    assert row.undischarged_stops[0].topic == "control.stop"
    assert len(row.live_demand) == 1
    assert row.live_demand[0].topic == "control.subscribe"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_fold.py::test_status_fold_populates_episode_stops_and_demand -q`
Expected: FAIL — `Row` has no `undischarged_stops`; `episode` is `None`.

- [ ] **Step 3: Extend `Row` (`types.py`)**

Add the import `from runstate.channel import Envelope` and two fields to `Row` (place them next to `episode`/before `issues`):

```python
    episode: str | None
    undischarged_stops: tuple[Envelope, ...]
    live_demand: tuple[Envelope, ...]
    issues: tuple[Issue, ...]
```

- [ ] **Step 4: Populate them in `status_fold` (`fold.py`)**

Add the imports `latest_episode, undischarged_stops, live_demand` from `runstate.observables`. In `status_fold`, after the existing `elapsed` read and before building the `Row`:

```python
    episode_env, episode_issue = guarded(latest_episode, channel)
    episode = episode_env.body.get("handle") if episode_env is not None else None
    if episode_issue is not None:
        issues.append(episode_issue)

    stops, stops_issue = guarded(undischarged_stops, channel)
    if stops_issue is not None:
        issues.append(stops_issue)

    demand, demand_issue = guarded(live_demand, channel)
    if demand_issue is not None:
        issues.append(demand_issue)
```

And build the `Row` with `episode=episode`, `undischarged_stops=tuple(stops or ())`, `live_demand=tuple(demand or ())` (drop the old hardcoded `episode=None`).

- [ ] **Step 5: Fix `_bare` (`table.py`)**

`_bare` constructs a `Row` for missing/unreadable; add `undischarged_stops=(), live_demand=()` to its `Row(...)` call (keep `episode=None`).

- [ ] **Step 6: Run tests + full gates**

Run: `uv run pytest tests/test_fold.py -q` (the new test passes; the healthy-run test still passes — note it now also asserts nothing about the new fields, which default fine).
Then: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` — all green.

- [ ] **Step 7: Commit**

```bash
git add runstate_tui/types.py runstate_tui/fold.py runstate_tui/table.py tests/test_fold.py tests/conftest.py
git commit -m "feat(fold): enrich Row with episode, undischarged_stops, live_demand"
```

---

### Task 2: `format_detail` + `format_envelope` + `read_log_delta`

**Files:** Modify `runstate_tui/format.py`, `runstate_tui/table.py`; Test `tests/test_format.py`, `tests/test_table.py`.

**Interfaces:**
- Produces: `format_detail(row: Row) -> str` (multi-line, pure); `format_envelope(env: Envelope) -> str` (one line, pure); `read_log_delta(ref: RunRef, after: int, *, limit: int | None = None) -> list[Envelope]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_format.py` (reuse the file's `_row(...)` helper; extend it to pass the new `undischarged_stops=()/live_demand=()` defaults so existing `_row(...)` calls still build a valid Row):

```python
def test_format_envelope_is_a_compact_one_liner():
    from runstate.channel import Envelope
    from runstate_tui.format import format_envelope

    e = Envelope(seq=4, topic="control.stop", name=None, request_id="webui:x", body={})
    line = format_envelope(e)
    assert "4" in line and "control.stop" in line and "webui:x" in line


def test_format_detail_shows_all_factors_and_lists():
    from runstate.channel import Envelope
    from runstate_tui.format import format_detail

    stop = Envelope(seq=5, topic="control.stop", name=None, request_id="webui:s", body={})
    row = _row(
        frontier=7, value=("loss", 0.03, 7), elapsed=50.0, episode="local://h/1",
        undischarged_stops=(stop,),
    )
    text = format_detail(row)
    assert "local://h/1" in text          # episode
    assert "loss" in text                  # value
    assert "webui:s" in text               # the undischarged stop
    assert "undischarged stop" in text.lower()
```

Add to `tests/test_table.py`:

```python
def test_read_log_delta_is_incremental(tmp_path):
    from runstate_tui.table import read_log_delta

    ch = open_channel("d", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")     # seq 1
    ch.send({"step": 1, "consumed_seq": 0, "t": 2.0}, topic="lifecycle.heartbeat")  # seq 2
    ch.close()
    ref = ("d", str(tmp_path), "sqlite")

    all_ = read_log_delta(ref, after=0)
    assert [e.seq for e in all_] == [1, 2]
    assert read_log_delta(ref, after=1)[0].seq == 2   # only the delta
    assert read_log_delta(ref, after=2) == []         # nothing new


def test_read_log_delta_missing_run_is_empty(tmp_path):
    from runstate_tui.table import read_log_delta

    assert read_log_delta(("ghost", str(tmp_path), "sqlite"), after=0) == []
    assert not (tmp_path / "ghost.db").exists()        # no phantom fabricated
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_format.py tests/test_table.py -q`
Expected: FAIL — the functions don't exist yet; `_row` may need the new defaults.

- [ ] **Step 3: Implement in `format.py`**

Add `from runstate.channel import Envelope` and:

```python
def format_envelope(env: Envelope) -> str:
    """One compact line for the raw log tail: seq, topic, request_id?, body."""
    rid = f"  {env.request_id}" if env.request_id else ""
    return f"{env.seq:>5}  {env.topic:<20}{rid}  {env.body}"


def format_detail(row: Row) -> str:
    """The drill-down header: every Row factor + full issues + stops + demand. Pure."""
    lines = [format_row(row)]  # the one-line summary at the top
    lines.append(f"episode: {row.episode}" if row.episode else "episode: —")
    if row.undischarged_stops:
        lines.append(f"undischarged stops ({len(row.undischarged_stops)}):")
        lines += [f"  {format_envelope(e)}" for e in row.undischarged_stops]
    if row.live_demand:
        lines.append(f"live demand ({len(row.live_demand)}):")
        lines += [f"  {format_envelope(e)}" for e in row.live_demand]
    if row.issues:
        lines.append("issues:")
        lines += [f"  ⚠ {i.message}" for i in row.issues]
    return "\n".join(lines)
```

Also add a compact undischarged-stop flag to `format_row` — append `f"  ⏹{len(row.undischarged_stops)}"` when `row.undischarged_stops` is non-empty (a pending stop is status-relevant). Keep it minimal; add a `test_format_row_flags_undischarged_stops` if the file's style expects one.

- [ ] **Step 4: Implement `read_log_delta` in `table.py`**

Add (mirrors `open_and_fold`'s protections but returns raw envelopes; byte-torn crashes):

```python
def read_log_delta(ref: RunRef, after: int, *, limit: int | None = None) -> list[Envelope]:
    """The raw log tail as a query: envelopes with seq > `after`. Filter-shaped for
    later (topics/name/request_ids). Missing/unreadable/substrate-fault -> [] (the
    header carries the run's status); a byte-torn record -> json.JSONDecodeError -> crash."""
    run_id, root, backend = ref
    if backend == "sqlite":
        try:
            (Path(root) / f"{run_id}.db").stat()
        except OSError:
            return []  # missing pointer / unreadable dir — never fabricate a phantom db
    try:
        channel = open_channel(run_id, root=root, backend=backend)
    except _OPEN_ERRORS:
        return []
    try:
        return channel.read(after=after, limit=limit)
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return []  # substrate fault mid-read (byte-torn's JSONDecodeError is NOT caught -> crash)
    finally:
        channel.close()
```

Add `from runstate.channel import Envelope` to `table.py`.

- [ ] **Step 5: Run tests + full gates + commit**

Run: `uv run pytest tests/test_format.py tests/test_table.py -q` then the full gate quartet — all green.

```bash
git add runstate_tui/format.py runstate_tui/table.py tests/test_format.py tests/test_table.py
git commit -m "feat(detail): format_detail/format_envelope + read_log_delta query seam"
```

---

### Task 3: `DrillDownScreen` — the live detail view

**Files:** Create `runstate_tui/detail.py`; Modify `pyproject.toml`; Test `tests/test_detail.py`.

**Interfaces:**
- Consumes: `render_single`, `read_log_delta`, `format_detail`, `format_envelope`, `Env`, `RunRef`.
- Produces: `DrillDownScreen(Screen[None])` — `__init__(self, ref, env, tick_interval=1.0, log_cap=500)`; `escape` pops.

- [ ] **Step 1: Add the snapshot dev-dep**

Run: `uv add --dev pytest-textual-snapshot` (updates `pyproject.toml` + `uv.lock`).

- [ ] **Step 2: Write the failing tests**

Create `tests/test_detail.py` (behavioral via Pilot; a scenario built on a real sqlite run so `read_log_delta` works by ref):

```python
import asyncio

from runstate import open_channel
from textual.widgets import RichLog, Static

from runstate_tui.detail import DrillDownScreen
from runstate_tui.env import Env


def _sqlite_rich(tmp_path):
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    ch.send({"step": 7, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
    ch.send({}, topic="control.stop", request_id="webui:stop1")
    ch.close()
    return ("r", str(tmp_path), "sqlite")


def test_drilldown_renders_header_and_streams_the_log(tmp_path):
    asyncio.run(_renders(tmp_path))


async def _renders(tmp_path):
    from textual.app import App, ComposeResult
    from textual.widgets import Static as S

    ref = _sqlite_rich(tmp_path)

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield S("host")

    app = Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 150.0), tick_interval=0.05)
        await app.push_screen(screen)
        for _ in range(60):
            await pilot.pause(0.02)
            head = str(app.query_one("#detail-head", Static).content)
            log_lines = app.query_one("#detail-log", RichLog).lines
            if "local://h/1" in head and len(log_lines) >= 3:
                break
        assert "local://h/1" in str(app.query_one("#detail-head", Static).content)  # episode in header
        assert "webui:stop1" in str(app.query_one("#detail-head", Static).content)  # the stop
        assert len(app.query_one("#detail-log", RichLog).lines) >= 3                # log streamed


def test_drilldown_snapshot(snap_compare, tmp_path):
    # SVG snapshot of the drill-down layout (headless). First run writes the baseline;
    # subsequent runs diff against it. Run `uv run pytest --snapshot-update` to refresh
    # after an intentional layout change.
    ref = _sqlite_rich(tmp_path)

    from textual.app import App, ComposeResult
    from textual.widgets import Static as S

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield S("host")

        def on_mount(self) -> None:
            self.push_screen(DrillDownScreen(ref, Env(clock=lambda: 150.0), tick_interval=0.05))

    assert snap_compare(Host(), press=[])
```

(If `snap_compare`'s app-instance signature differs in the installed version, adapt to its documented form — an app instance or a path; keep the single assertion.)

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_detail.py -q`
Expected: FAIL — `runstate_tui.detail` does not exist.

- [ ] **Step 4: Implement `detail.py`** (the spike-verified pattern)

```python
from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import RichLog, Static

from .env import Env
from .format import format_detail, format_envelope
from .resolver import RunRef
from .table import read_log_delta, render_single


class DrillDownScreen(Screen[None]):
    """The drill-down detail view: a live header (the Row, re-folded each tick, a pure
    projection) + a live incremental raw-envelope log tail (cursor + last_seq() watermark
    + read(after=cursor), off-thread). `escape` returns. The log pane is the reactive
    shell; the fold is the pure core (event-driven architecture)."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(
        self, ref: RunRef, env: Env, tick_interval: float = 1.0, log_cap: int = 500
    ) -> None:
        super().__init__()
        self._ref = ref
        self._env = env
        self._tick_interval = tick_interval
        self._cursor = 0
        self._log_cap = log_cap

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="detail-head")
        yield RichLog(id="detail-log", max_lines=self._log_cap)

    def on_mount(self) -> None:
        self._tick()

    def _tick(self) -> None:
        self._refresh()

    def _show_head(self, text: str) -> None:
        self.query_one("#detail-head", Static).update(text)

    def _append_log(self, line: str) -> None:
        self.query_one("#detail-log", RichLog).write(line)

    @work(thread=True, exclusive=True)
    def _refresh(self) -> None:
        if not self.is_mounted:  # popped -> stop the loop
            return
        # header: the Row, re-folded off-thread (byte-torn -> crash, per the precursor)
        row = render_single(self._ref, self._env)
        self.app.call_from_thread(self._show_head, format_detail(row))
        # log tail: incremental delta only, watermark-gated inside read_log_delta's read
        for e in read_log_delta(self._ref, after=self._cursor):
            self.app.call_from_thread(self._append_log, format_envelope(e))
            self._cursor = e.seq
        self.app.call_from_thread(self.set_timer, self._tick_interval, self._tick)
```

- [ ] **Step 5: Run tests (write the snapshot baseline) + gates**

Run: `uv run pytest tests/test_detail.py -q` (the first snapshot run creates the baseline under `tests/__snapshots__/`; commit it). Rerun 3× — the behavioral test drives threads/timers, confirm non-flaky.
Then: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` — all green.

- [ ] **Step 6: Commit** (include the snapshot baseline + the lockfile)

```bash
git add runstate_tui/detail.py tests/test_detail.py pyproject.toml uv.lock tests/__snapshots__
git commit -m "feat(detail): DrillDownScreen — live header + incremental log tail"
```

---

### Task 4: Wire the `enter` binding

**Files:** Modify `runstate_tui/app.py`; Test `tests/test_app.py`.

**Interfaces:** `SingleRunApp` gains an `enter` binding → `push_screen(DrillDownScreen(self._ref, self._env, self._tick_interval))`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_app.py`:

```python
from runstate_tui.detail import DrillDownScreen


def test_enter_opens_the_drilldown(tmp_path):
    asyncio.run(_opens_drilldown(tmp_path))


async def _opens_drilldown(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DrillDownScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DrillDownScreen)  # returns to the main view
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_app.py::test_enter_opens_the_drilldown -q`
Expected: FAIL — no `enter` binding.

- [ ] **Step 3: Wire it in `app.py`**

Add `from .detail import DrillDownScreen` and extend `BINDINGS`:

```python
    BINDINGS = [("s", "stop", "Stop run"), ("enter", "detail", "Detail")]
```

Add the action:

```python
    def action_detail(self) -> None:
        self.push_screen(DrillDownScreen(self._ref, self._env, self._tick_interval))
```

- [ ] **Step 4: Run tests + gates + commit**

Run: `uv run pytest tests/test_app.py -q` (rerun 3× — confirm stable), then the full gate quartet — all green.

```bash
git add runstate_tui/app.py tests/test_app.py
git commit -m "feat(app): enter opens the drill-down detail view"
```

---

## Self-Review

- **Coverage:** episode/stops/demand (Task 1), the detail renderers + query seam (Task 2), the live incremental screen + snapshot (Task 3), the binding (Task 4). The incremental+watermark+off-thread+stops-on-unmount pattern is exactly the verified spike.
- **Placeholder scan:** none — every code/test step is complete. (Two adapt-if-needed notes: `build_log`'s request_id threading in Task 1 Step 1, and `snap_compare`'s exact signature in Task 3 — both call for the smallest change that satisfies the stated assertion, not a rewrite.)
- **Type consistency:** `Envelope` imported the same way everywhere; `read_log_delta`/`format_detail`/`format_envelope`/`DrillDownScreen` signatures match across tasks.
- **Scope:** deferred (seam present, unused) — filter UI, backward scrollback (`max_seq=`), postgres push, the Stage-4 watermark-gated fold factorization.
```
