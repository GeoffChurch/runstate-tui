# Drill-down redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Redesign `DrillDownScreen` from a flat text blob into a two-region tabular detail view — a compact summary card over a colored, zebra-striped, newest-at-top log `DataTable` with `y`-yank (OSC 52), `enter`-expand, and a `/`-filter + topic toggles over a bounded in-memory window.

**Architecture:** The log pane holds a bounded in-memory window (deque of the last *N* raw envelopes), fed by the existing incremental `read_log_delta` cursor read. Rendering applies the current filter predicate and paints the `DataTable` newest-first. The filter *seam* is `read_log_delta(filter=)` (present for upstream `runstate#15`); v1 evaluates the predicate over the in-memory window. Model (windowed/filtered query) stays frontend-agnostic; view (widgets/colors/keys/OSC 52) stays in the screen.

**Tech Stack:** Python 3.11, runstate, Textual 8.2.8 (`Screen`, `ModalScreen`, `DataTable`, `Input`, `Static`, `@work`, `copy_to_clipboard`), Rich (`Text`), uv, ruff, mypy --strict, pytest.

## Global Constraints

Copied from the spec (`docs/superpowers/specs/2026-07-19-drilldown-redesign-design.md`).

- **Two regions:** a compact summary **card** (top) + a **log panel** (filter bar + `DataTable` + topic-toggle chips), plus a footer. Never a flat text blob.
- **Newest-at-top** (reverse-chron): the log renders seq **descending**; new events appear at the top. The read stays incremental (cursor delta); the bounded window re-renders — **never rebuild from seq 0**.
- **Bounded window:** an in-memory `deque(maxlen=N)` of the last *N* raw envelopes (`N=500` default = `log_cap`). Filter is applied over this window in the view; the `read_log_delta(filter=)` seam is present for upstream delegation.
- **Topic color (hex, mirrors `status_color`):** `lifecycle.*` `#539bf5` · `control.*` `#d29922` · `value` `#3fb950` · other → `#8b949e`. Color is redundant with the always-present topic text.
- **Copy = yank the full envelope** (`format_envelope`) via Textual's OSC 52 `copy_to_clipboard` (SSH/tmux-safe). Bound to `y`. Labeled "yank," not "copy."
- **Async test harness — NO `pytest-asyncio`.** Use `def test_x(): asyncio.run(_x())` + `async def _x(): async with app.run_test() as pilot: …`, exactly like `tests/test_multirun.py`/`test_detail.py`. Never `@pytest.mark.asyncio`.
- **Off-thread + teardown-guarded** exactly as the current screen: the tail worker is `@work(thread=True, exclusive=True)`; every `call_from_thread` marshal is wrapped in `_TEARDOWN_ERRORS` (`RuntimeError`, `concurrent.futures.CancelledError`, `NoMatches`).
- **Model/view line:** the windowed/filtered query lives behind `read_log_delta(after, filter, limit)`; `format_*`/colors/OSC 52/widgets stay in the view.
- **Gates:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` green before each commit; mypy scoped to `runstate_tui/`. Commit trailer:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EZVuQQF3vdynEXvyDApYKp
  ```

## File Structure

- **Modify `runstate_tui/format.py`** — add `topic_color(topic) -> str`; add `format_summary_card(row) -> Text` (the compact two-line card).
- **Modify `runstate_tui/table.py`** — `read_log_delta` gains `filter=`; add `envelope_filter(text, families) -> Callable[[Envelope], bool]`.
- **Rewrite `runstate_tui/detail.py`** — the two-region `DrillDownScreen`; a new `ExpandScreen(ModalScreen)`.
- **Modify `scripts/showcase.py`** — regenerate `scene_drilldown` for the new look.
- **Tests:** `tests/test_format.py`, `tests/test_table.py`, `tests/test_detail.py`, and the `test_detail` snapshot golden.

---

### Task 1: `topic_color` + `format_summary_card`

**Files:** Modify `runstate_tui/format.py`; Test `tests/test_format.py`.

**Interfaces:**
- Produces: `topic_color(topic: str) -> str` (a hex color); `format_summary_card(row: Row) -> Text` (a 2-line Rich `Text`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_format.py`):
```python
def test_topic_color_by_family():
    from runstate_tui.format import topic_color
    assert topic_color("lifecycle.started") == "#539bf5"
    assert topic_color("control.stop") == "#d29922"
    assert topic_color("value") == "#3fb950"
    assert topic_color("something.else") == "#8b949e"


def test_summary_card_is_two_compact_lines_with_counts():
    from runstate_tui.format import format_summary_card
    from rich.text import Text
    row = _row(  # the test module's Row factory; a live run w/ 1 stop, 1 demand
        status=Status.live(),
        frontier=1450,
        freshness=8.0,
        value=("loss", 0.0123, 1450),
        elapsed=20.0,
        episode="local://h/1",
        undischarged_stops=(_env_stub(),),   # len 1
        live_demand=(_env_stub(),),          # len 1
    )
    card = format_summary_card(row)
    assert isinstance(card, Text)
    plain = card.plain
    assert "live" in plain and "loss=0.0123" in plain          # line 1: the summary
    assert "episode local://h/1" in plain                      # line 2: episode
    assert "1 stop pending" in plain and "1 demand" in plain    # line 2: COUNTS, not lists
    assert plain.count("\n") == 1                               # exactly two lines
```
(Use the existing `Row`/`Status` factories in `tests/test_format.py`; `_env_stub` = any `Envelope`-shaped stub already used there, or build one via `build_log`. The card must show *counts*, never the per-stop/per-demand lists.)

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_format.py -q`.

- [ ] **Step 3: Implement in `runstate_tui/format.py`**
```python
from rich.text import Text  # add to imports if absent
from .types import Row, Status  # extend existing import as needed

_TOPIC_COLORS = {"lifecycle": "#539bf5", "control": "#d29922", "value": "#3fb950"}


def topic_color(topic: str) -> str:
    """A hex color for a log topic, by family (mirrors status_color). Redundant with
    the topic text — never the sole signal."""
    return _TOPIC_COLORS.get(topic.split(".")[0], "#8b949e")


def format_summary_card(row: Row) -> Text:
    """The drill-down's compact 2-line header card: the one-line summary (with the
    status dot) + episode and COUNTS. The full stop/demand/issue lists live in the
    enter-expand, not here."""
    from .format import status_color  # local import avoids a cycle if reordered; or module-level

    line1 = Text("● ", style=status_color(row.status))
    line1.append(format_row(row))  # the existing one-line summary
    parts = [f"episode {row.episode}" if row.episode else "episode —"]
    if row.undischarged_stops:
        parts.append(f"■ {len(row.undischarged_stops)} stop pending")
    if row.live_demand:
        parts.append(f"◆ {len(row.live_demand)} demand")
    if row.issues:
        parts.append(f"⚠ {len(row.issues)} issue" + ("s" if len(row.issues) != 1 else ""))
    line2 = Text("     ".join(parts))
    return Text("\n").join([line1, line2])
```
(`status_color` is already in `format.py` — call it directly, no import needed; the local-import comment is only if you reorder. `format_row` is in the same module.)

- [ ] **Step 4: Run tests → pass. Step 5: gates; commit** `feat(format): topic_color + compact format_summary_card`.

---

### Task 2: `read_log_delta(filter=)` seam + `envelope_filter`

**Files:** Modify `runstate_tui/table.py`; Test `tests/test_table.py`.

**Interfaces:**
- Produces: `read_log_delta(ref, after, *, filter=None, limit=None) -> list[Envelope]` (filter applied to the read); `envelope_filter(text: str, families: set[str] | None) -> Callable[[Envelope], bool]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_table.py`):
```python
def test_read_log_delta_applies_filter(tmp_path):
    from runstate import open_channel
    from runstate_tui.table import read_log_delta
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    ch.send({}, topic="control.stop", request_id="webui:x")
    ch.close()
    ref = ("r", str(tmp_path), "sqlite")
    only_control = read_log_delta(ref, after=0, filter=lambda e: e.topic.startswith("control."))
    assert [e.topic for e in only_control] == ["control.stop"]
    all_e = read_log_delta(ref, after=0)  # filter=None -> unchanged behavior
    assert len(all_e) == 2


def test_envelope_filter_text_and_families():
    from runstate_tui.table import envelope_filter
    from types import SimpleNamespace as NS
    started = NS(topic="lifecycle.started", request_id=None, body={"t": 1.0})
    stop = NS(topic="control.stop", request_id="webui:x", body={})
    f_text = envelope_filter("control", None)
    assert f_text(stop) and not f_text(started)                 # substring over topic/request
    f_req = envelope_filter("webui:x", None)
    assert f_req(stop) and not f_req(started)                    # matches request_id
    f_fam = envelope_filter("", {"lifecycle"})                  # families-only
    assert f_fam(started) and not f_fam(stop)
    f_none = envelope_filter("", None)
    assert f_none(started) and f_none(stop)                      # empty -> everything
```

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_table.py -q`.

- [ ] **Step 3: Implement in `runstate_tui/table.py`**

Add `filter=` to `read_log_delta` (apply after the guarded read, before returning):
```python
from collections.abc import Callable  # add to imports

def read_log_delta(
    ref: RunRef,
    after: int,
    *,
    filter: Callable[[Envelope], bool] | None = None,
    limit: int | None = None,
) -> list[Envelope]:
    # ... existing stat/open/read/guard body unchanged, producing `got: list[Envelope]` ...
    # at the return points that currently `return channel.read(after=after, limit=limit)`,
    # capture into `got` then:
    # UPSTREAM(runstate#15): v1 applies the predicate here in Python. When runstate's
    # read() gains filter= (+ before=/max_seq= for backward reads), push `filter` into
    # channel.read so the SUBSTRATE filters and history is retroactively filterable.
    # Discover all revisit sites with: grep -rn "UPSTREAM(runstate#15)"
    return [e for e in got if filter is None or filter(e)]
```
(Keep the existing missing/unreadable/byte-torn → `[]` guards. The filter wraps only the success path.)

Add the predicate builder:
```python
def envelope_filter(text: str, families: set[str] | None) -> Callable[[Envelope], bool]:
    """Build a v1 log-filter predicate from the filter-bar text + the enabled topic
    families. text: a plain substring matched against topic + request_id (+ 'step>N'
    numeric bound over the body's 'step'). families: if not None, restrict to these
    topic families. The daemon/upstream #15 will serve this as read(filter=…)."""
    text = text.strip()
    stepbound: int | None = None
    if text.startswith("step>") and text[5:].strip().isdigit():
        stepbound = int(text[5:].strip())

    def pred(e: Envelope) -> bool:
        if families is not None and e.topic.split(".")[0] not in families:
            return False
        if stepbound is not None:
            step = e.body.get("step") if isinstance(e.body, dict) else None
            return isinstance(step, int) and step > stepbound
        if text and text not in e.topic and text not in (e.request_id or ""):
            return False
        return True

    return pred
```

- [ ] **Step 4: Run tests → pass. Step 5: gates; commit** `feat(table): read_log_delta filter= seam + envelope_filter predicate`.

---

### Task 3: `DrillDownScreen` shell — layout, summary card, log table + `_render_window`

**Files:** Rewrite `runstate_tui/detail.py`; Test `tests/test_detail.py`.

**Interfaces:**
- Consumes: `format_summary_card`/`topic_color` (T1), `envelope_filter` (T2), `render_single`/`read_log_delta` (table.py), `format_envelope` (format.py), `Env`/`RunRef`.
- Produces: `DrillDownScreen(ref, env, tick_interval=1.0, log_cap=500)` (constructor UNCHANGED from Stage 3 — the apps already call it this way); a `_render_window()` method that paints the log `DataTable` newest-first from the in-memory window + current predicate.

- [ ] **Step 1: Write the failing test** (rewrite the render portion of `tests/test_detail.py`; keep the async `asyncio.run` wrapper harness). Seed a rich run (started + heartbeats + value + subscribe + stop on sqlite, à la `scene_drilldown`), push the screen, and assert:
```python
def test_drilldown_renders_card_and_newest_first_table(tmp_path):
    asyncio.run(_renders(tmp_path))

async def _renders(tmp_path):
    ref = _seed_rich(tmp_path)  # helper: started(1)+hb(2)+value(3)+subscribe(4)+stop(5)
    app = _HostApp(ref)          # a tiny App that pushes DrillDownScreen on mount (see test_detail's existing host)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause(); await pilot.pause()
        screen = app.screen
        # summary card present + compact
        card = screen.query_one("#detail-card", Static)
        assert "episode" in card.renderable.plain  # Static holds a Text
        # log table newest-first, colored
        t = screen.query_one("#detail-log", DataTable)
        seqs = [t.get_cell_at(Coordinate(r, 0)) for r in range(t.row_count)]
        assert seqs == ["5", "4", "3", "2", "1"]   # descending = newest first
```
(Adapt `Coordinate`/`get_cell_at` to the Textual 8.2.8 accessor; the point is: the first column is seq, ordered descending. Reuse `test_detail.py`'s existing host-app pattern; add `_seed_rich`.)

- [ ] **Step 2: Run to verify it fails** (old screen has a `RichLog`, not a keyed `DataTable`).

- [ ] **Step 3: Rewrite `runstate_tui/detail.py`** — the shell (compose + card + table + `_render_window`), keeping the `_TEARDOWN_ERRORS`/`_marshal`/worker scaffolding from the current file. Core structure:
```python
from collections import deque
from rich.text import Text
from textual.containers import Vertical
from textual.widgets import DataTable, Input, Static

from .format import format_envelope, format_summary_card, topic_color
from .table import envelope_filter, read_log_delta, render_single

_FAMILIES = ("lifecycle", "value", "control")
_LOG_COLS = ("seq", "topic", "request", "body")


class DrillDownScreen(Screen[None]):
    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, ref, env, tick_interval=1.0, log_cap=500):
        super().__init__()
        self._ref = ref; self._env = env; self._tick_interval = tick_interval
        self._cursor = 0
        self._window: deque = deque(maxlen=log_cap)     # last N RAW envelopes (oldest..newest)
        self._filter_text = ""
        self._enabled = set(_FAMILIES)                  # all families on

    def compose(self):
        yield Static("", id="detail-card")
        with Vertical(id="detail-logbox"):
            yield Static(Text.from_markup("[grey58]/ filter…   topic · request · step>N · text[/]"), id="detail-filter")
            yield DataTable(id="detail-log", zebra_stripes=True, cursor_type="row")
            yield Static("", id="detail-chips")
        yield Static(Text.from_markup(
            "[b]y[/] yank   [b]/[/] filter   [b]enter[/] expand   [b]esc[/] back"), id="detail-foot")

    def on_mount(self):
        self.query_one("#detail-logbox").border_title = "log · live · newest ↑"
        t = self.query_one("#detail-log", DataTable)
        t.add_columns(*_LOG_COLS)
        self._tick()

    def _predicate(self):
        return envelope_filter(self._filter_text, self._enabled)

    def _render_window(self):
        """Repaint the log table newest-first from the in-memory window + predicate,
        preserving the selected seq. Called on the main thread only."""
        t = self.query_one("#detail-log", DataTable)
        sel = None
        if t.row_count:
            sel = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        pred = self._predicate()
        rows = [e for e in reversed(self._window) if pred(e)]  # newest-first, filtered
        t.clear()
        for e in rows:
            t.add_row(str(e.seq), Text(e.topic, style=topic_color(e.topic)),
                      e.request_id or "", _body_text(e), key=str(e.seq))
        if sel is not None and sel in {str(e.seq) for e in rows}:
            t.move_cursor(row=t.get_row_index(sel))
        self._render_chips()

    def _render_chips(self):
        parts = []
        counts = {f: sum(1 for e in self._window if e.topic.split(".")[0] == f) for f in _FAMILIES}
        for f in _FAMILIES:
            on = f in self._enabled
            col = topic_color(f + ".") if on else "grey37"
            parts.append(f"[{col}]●[/] {f} {counts[f]}")
        self.query_one("#detail-chips", Static).update(Text.from_markup("   ".join(parts)))
```
Add a `_body_text(e)` helper (v1: `value` → `f"loss={e.body.get('value')} @ {e.body.get('step')}"` style when the topic is `value` and the body is a dict; else `str(e.body)` — reuse/keep faithful). Keep `_marshal`/`_TEARDOWN_ERRORS`. The live worker comes in Task 4; for this task, `_tick` may do a single synchronous fill (read all after 0 into `_window`, call `_render_window`) so the table renders for the test — Task 4 replaces it with the incremental worker.

- [ ] **Step 4: Run tests → pass (adapt cell accessors to 8.2.8). Step 5: gates; commit** `feat(detail): two-region DrillDownScreen shell — card + newest-first log table`.

---

### Task 4: The windowed live-tail worker

**Files:** Modify `runstate_tui/detail.py`; Test `tests/test_detail.py`.

**Interfaces:**
- Consumes: `_render_window` (T3), `read_log_delta` (T2), `_marshal`/`_TEARDOWN_ERRORS`.

- [ ] **Step 1: Write the failing test** — appending to a `held_writer_sqlite_run` makes new envelopes appear at the **top**, incrementally, bounded:
```python
def test_live_tail_appends_at_top_incrementally(tmp_path):
    asyncio.run(_live(tmp_path))

async def _live(tmp_path):
    from runstate import open_channel
    ref = ("live", str(tmp_path), "sqlite")
    w = open_channel("live", root=tmp_path, backend="sqlite")
    w.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    app = _HostApp(ref, tick_interval=999)   # manual ticks
    async with app.run_test(size=(90, 22)) as pilot:
        screen = app.screen
        await _drive_tick(pilot, screen)     # helper: screen._tick(); await workers; pause
        t = screen.query_one("#detail-log", DataTable)
        assert t.get_cell_at(Coordinate(0, 0)) == "1"
        w.send({"step": 5, "consumed_seq": 0, "t": 2.0}, topic="lifecycle.heartbeat")
        await _drive_tick(pilot, screen)
        assert t.get_cell_at(Coordinate(0, 0)) == "2"    # newest (seq 2) now on TOP
        assert t.row_count == 2
    w.close()
```
(Model `_drive_tick` on `tests/helpers.py::advance_tick`. `_HostApp` pushes the screen and exposes `_tick`.)

- [ ] **Step 2: Run to verify it fails** (T3's synchronous fill doesn't incrementally follow appends).

- [ ] **Step 3: Replace the fill with the incremental worker** in `detail.py`:
```python
    def _tick(self):
        self._refresh()

    @work(thread=True, exclusive=True)
    def _refresh(self):
        # header: re-fold each tick (pure; byte-torn -> loud `corrupt`, no crash).
        row = render_single(self._ref, self._env)
        self._marshal(self._show_card, format_summary_card(row))
        # log: incremental raw delta (unfiltered accumulation -> clean cursor); the
        # filter is applied in _render_window over the bounded window.
        # UPSTREAM(runstate#15): when the substrate filters + supports backward reads,
        # pass filter=self._predicate() here (true retroactive filtering); v1 accumulates
        # raw and filters the window in _render_window. grep -rn "UPSTREAM(runstate#15)"
        delta = read_log_delta(self._ref, after=self._cursor)
        if delta:
            self._window.extend(delta)             # oldest..newest; deque(maxlen) trims the front
            self._cursor = delta[-1].seq
            self._marshal(self._render_window)
        self._marshal(self.set_timer, self._tick_interval, self._tick)

    def _show_card(self, card):
        self.query_one("#detail-card", Static).update(card)
```
(`_render_window` reads `self._window` on the main thread via the marshal.)

- [ ] **Step 4: Run tests → pass (3× non-flaky). Step 5: gates; commit** `feat(detail): incremental windowed live-tail (newest-first, bounded)`.

---

### Task 5: `y`-yank (OSC 52) + `enter`-expand

**Files:** Modify `runstate_tui/detail.py`; Test `tests/test_detail.py`.

**Interfaces:**
- Produces: `ExpandScreen(ModalScreen[None])`; `action_yank`/`action_expand` on `DrillDownScreen`; a `_selected_envelope()` helper.

- [ ] **Step 1: Write the failing tests:**
```python
def test_yank_copies_selected_envelope(tmp_path, monkeypatch):
    asyncio.run(_yank(tmp_path, monkeypatch))

async def _yank(tmp_path, monkeypatch):
    ref = _seed_rich(tmp_path)
    copied = {}
    app = _HostApp(ref)
    monkeypatch.setattr(type(app), "copy_to_clipboard", lambda self, text: copied.setdefault("t", text))
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause(); await pilot.pause()
        await pilot.press("y")                     # yank the selected (top = seq 5, control.stop) row
        assert "control.stop" in copied["t"] and copied["t"].startswith("5") or "5" in copied["t"]

def test_enter_expands_then_escape_returns(tmp_path):
    asyncio.run(_expand(tmp_path))

async def _expand(tmp_path):
    from runstate_tui.detail import ExpandScreen
    ref = _seed_rich(tmp_path)
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause(); await pilot.pause()
        await pilot.press("enter"); await pilot.pause()
        assert isinstance(app.screen, ExpandScreen)
        await pilot.press("escape"); await pilot.pause()
        assert not isinstance(app.screen, ExpandScreen)
```

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement** in `detail.py`. Add to `BINDINGS`: `("y", "yank", "Yank"), ("enter", "expand", "Expand")`. (Note: the `DataTable` intercepts `enter` when focused — same gotcha Stage 4 hit; wire `enter` via `on_data_table_row_selected` → `action_expand`, and keep `y` as a screen binding since the table doesn't bind `y`.)
```python
    def _selected_envelope(self):
        t = self.query_one("#detail-log", DataTable)
        if not t.row_count:
            return None
        seq = int(t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value)
        return next((e for e in self._window if e.seq == seq), None)

    def action_yank(self):
        e = self._selected_envelope()
        if e is not None:
            self.app.copy_to_clipboard(format_envelope(e))   # OSC 52 (verify method name in 8.2.8)

    def action_expand(self):
        e = self._selected_envelope()
        if e is not None:
            self.app.push_screen(ExpandScreen(e))

    def on_data_table_row_selected(self, _msg):
        self.action_expand()


class ExpandScreen(ModalScreen[None]):
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("y", "yank", "Yank")]
    def __init__(self, envelope):
        super().__init__(); self._e = envelope
    def compose(self):
        import json
        body = json.dumps(self._e.body, indent=2, default=str) if isinstance(self._e.body, dict) else str(self._e.body)
        yield Static(Text(f"seq {self._e.seq}   {self._e.topic}   {self._e.request_id or ''}\n\n{body}"), id="expand-body")
    def action_yank(self):
        self.app.copy_to_clipboard(format_envelope(self._e))
```
Verify `App.copy_to_clipboard(text)` exists in Textual 8.2.8 (grep `.venv`); if it's named/located differently, use the correct OSC 52 write. `ModalScreen` import: `from textual.screen import ModalScreen`.

- [ ] **Step 4: Run tests → pass (3×). Step 5: gates; commit** `feat(detail): y-yank envelope (OSC 52) + enter-expand modal`.

---

### Task 6: `/`-filter bar + topic toggles

**Files:** Modify `runstate_tui/detail.py`; Test `tests/test_detail.py`.

**Interfaces:**
- Produces: `action_filter` (focus the filter `Input`), the `Input` submit/change handler → `_filter_text` + `_render_window`; family-toggle bindings → `_enabled` + `_render_window`.

- [ ] **Step 1: Write the failing tests:**
```python
def test_filter_narrows_to_matching_rows(tmp_path):
    asyncio.run(_filter(tmp_path))

async def _filter(tmp_path):
    ref = _seed_rich(tmp_path)  # 5 rows incl. control.* and lifecycle.*
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause(); await pilot.pause()
        screen = app.screen
        screen._filter_text = "control"; screen._render_window()   # simulate typed filter
        t = screen.query_one("#detail-log", DataTable)
        topics = [t.get_cell_at(Coordinate(r, 1)).plain for r in range(t.row_count)]
        assert all("control" in x for x in topics) and t.row_count >= 1

def test_toggle_hides_a_family(tmp_path):
    asyncio.run(_toggle(tmp_path))

async def _toggle(tmp_path):
    ref = _seed_rich(tmp_path)
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause(); await pilot.pause()
        screen = app.screen
        before = screen.query_one("#detail-log", DataTable).row_count
        screen._enabled.discard("lifecycle"); screen._render_window()
        after = screen.query_one("#detail-log", DataTable).row_count
        assert after < before   # lifecycle rows hidden
```

- [ ] **Step 2: Run to verify they fail** (no filter/toggle wiring yet — `_filter_text`/`_enabled` exist from T3 but nothing repaints on change; these tests call `_render_window` directly, so they mainly pin the predicate integration — confirm they fail if `_render_window` ignored the predicate).

- [ ] **Step 3: Wire the interactive controls** in `detail.py`. Add bindings: `("/", "filter", "Filter")` + one key per family, e.g. `("f1", "toggle('lifecycle')", …)` — OR simplest: number keys `("1","toggle_lifecycle"), ("2","toggle_value"), ("3","toggle_control")`. Make the filter `Input` initially hidden (CSS `display: none`); `action_filter` shows + focuses it; on `Input.Submitted`/`Input.Changed`, set `self._filter_text` and `self._render_window()`.
```python
    def action_filter(self):
        inp = self.query_one("#detail-filter-input", Input)
        inp.display = True; inp.focus()

    def on_input_changed(self, msg: Input.Changed):
        self._filter_text = msg.value
        self._render_window()

    def _toggle(self, family):
        self._enabled.symmetric_difference_update({family})
        self._render_window()
```
Add the `Input` to `compose` (replacing the static filter hint with an `Input(placeholder="/ filter… topic · request · step>N · text", id="detail-filter-input")` that starts hidden; the hint text becomes the placeholder). Bind the family toggles.

- [ ] **Step 4: Run tests → pass (3×). Step 5: gates; commit** `feat(detail): /-filter bar + topic-family toggles`.

---

### Task 7: Regenerate the showcase drill-down scene + snapshot + integration

**Files:** Modify `scripts/showcase.py` (if needed), `tests/__snapshots__/test_detail/*`; Test verification.

- [ ] **Step 1:** Confirm the app integration is unbroken — `MultiRunApp`/`SingleRunApp` construct `DrillDownScreen(ref, env, tick_interval)` (unchanged signature). Run the multi-run + detail suites: `uv run pytest tests/test_multirun.py tests/test_detail.py -q` green.
- [ ] **Step 2:** Regenerate the drill-down showcase scene: `uv run python -m scripts.showcase`, then READ `docs/img/drilldown.png` — confirm the new two-region look (card + newest-first colored table + filter/chips/footer). Adjust `scene_drilldown`'s `size` if the taller layout needs it.
- [ ] **Step 3:** Update the `test_detail` snapshot golden with `--snapshot-update` ONLY after diffing to confirm the change is exactly the redesign (a reviewer-verifiable, intentional layout change). Commit `docs/img/drilldown.png`/`.svg` + the snapshot.
- [ ] **Step 4:** FULL gates green; **commit** `feat(showcase): regenerate drill-down scene for the redesign`.

---

## Self-Review

- **Spec coverage:** two-region layout + compact card (T1/T3), topic color (T1), `read_log_delta(filter=)` seam + predicate (T2), newest-at-top colored zebra table + `_render_window` (T3), windowed incremental live-tail (T4), `y`-yank OSC 52 + `enter`-expand (T5), `/`-filter + topic toggles (T6), showcase/integration (T7). Deferred (search `n`/`N`, whole-log scrollback) intentionally absent — `n` is not bound.
- **Placeholders:** T3/T5 flag empirical Textual-8.2.8 points (`get_cell_at`/`Coordinate` accessor, `copy_to_clipboard` name, the `enter`/`DataTable` interception) — the implementer verifies against `.venv`, exactly as prior stages; all logic is concrete.
- **Type consistency:** `topic_color(topic) -> str`, `format_summary_card(row) -> Text`, `read_log_delta(ref, after, *, filter, limit)`, `envelope_filter(text, families) -> predicate`, `DrillDownScreen(ref, env, tick_interval, log_cap)`, `_render_window`/`_selected_envelope`/`ExpandScreen` names match across tasks.
- **Model/view:** the query (`read_log_delta(filter=)`, the window) is frontend-agnostic; colors/OSC 52/widgets are in the screen — the daemon-readiness line holds.
- **Note on the v1 filter:** the window accumulates RAW envelopes (clean incremental cursor); the predicate is applied in `_render_window` over the bounded in-memory window (the spec's "over the bounded window"). The `read_log_delta(filter=)` seam is present but passed `None` in the accumulation read — it becomes the delegation point when upstream `runstate#15` serves filtered + backward reads (true retroactive filtering). This is the design-for-defer line; v1 filters the recent window, not the whole log.
- **Discoverability of the deferred rewiring (`UPSTREAM(runstate#15)` convention).** So the v1 client-side filter doesn't silently become permanent: both revisit sites carry a greppable `# UPSTREAM(runstate#15):` marker (the `read_log_delta` filter seam in T2, and the accumulation call in T4) — `grep -rn "UPSTREAM(runstate#15)"` is the redo list. In T6's `test_filter_narrows_to_matching_rows`, add a `# UPSTREAM(runstate#15): v1 filters the in-memory window; when #15 lands, this flips to assert the filter pushes into read_log_delta's read` comment above the assertion (a test-lock in the `# FINDING:`-style convention the fixture basis already uses). Issue #15 carries the reciprocal back-reference ("Downstream revisit: grep UPSTREAM(runstate#15) in runstate-tui"), so *closing #15* surfaces the downstream work. The convention generalizes to `UPSTREAM(runstate#16)` / `#17` for future deferrals.
