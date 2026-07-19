from __future__ import annotations

import concurrent.futures
import json
from collections import deque
from collections.abc import Callable
from typing import Any

from rich.text import Text
from runstate.channel import Envelope
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import DataTable, Static

from .env import Env
from .format import format_envelope, format_summary_card, topic_color
from .resolver import RunRef
from .table import envelope_filter, read_log_delta, render_single

# a pop mid-tick can race the marshal below onto a torn-down screen three ways:
# the event loop already closed (RuntimeError), the callback was cancelled
# (concurrent.futures.CancelledError), or the queried widget already unmounted
# (textual NoMatches) -- see app.py's analogous stop-teardown guard.
_TEARDOWN_ERRORS = (RuntimeError, concurrent.futures.CancelledError, NoMatches)

_FAMILIES = ("lifecycle", "value", "control")
_LOG_COLS = ("seq", "topic", "request", "body")


def _body_text(e: Envelope) -> str:
    """The log table's body cell: a `value` envelope prettifies to `name=value @ step`
    (mirroring format_row's own value display) -- everything else renders as the raw
    body (faithful default, matching format_envelope's convention; the table never
    invents a shape a topic doesn't have)."""
    if e.topic == "value" and isinstance(e.body, dict):
        name = e.name or "value"
        step = e.body.get("step")
        return f"{name}={e.body.get('value')}" + (f" @ {step}" if step is not None else "")
    return str(e.body)


class DrillDownScreen(Screen[None]):
    """The drill-down detail view: a compact summary CARD (a pure re-fold each tick,
    `format_summary_card`) over a colored/zebra/newest-at-top log DataTable painted
    from a bounded in-memory window (`log_view = window ∘ filter ∘ read`, spec
    2026-07-19-drilldown-redesign-design.md). `escape` returns.

    `_refresh` is an incremental delta-cursor live-tail worker that self-reschedules:
    each tick re-folds the card (pure), then reads only the RAW tail since
    `self._cursor` (`read_log_delta(after=self._cursor)`), extends the bounded window
    (`deque(maxlen=log_cap)` trims the front), and advances the cursor to the delta's
    last seq -- never re-reading what's already been drained."""

    # `enter` is NOT bound here: DataTable (focused, cursor_type="row") binds `enter`
    # itself (-> its own action_select_cursor -> a RowSelected message) and intercepts
    # the key before it ever reaches a screen binding -- the same gotcha Stage 4 hit.
    # Expand is wired off that message instead (`on_data_table_row_selected` below).
    BINDINGS = [("escape", "app.pop_screen", "Back"), ("y", "yank", "Yank")]

    CSS = """
    #detail-card { border: round $panel; height: auto; padding: 0 1; }
    #detail-logbox { border: round $panel; }
    #detail-log { height: 1fr; }
    #detail-filter { height: auto; padding: 0 1; }
    #detail-chips { height: auto; padding: 0 1; }
    #detail-foot { height: auto; padding: 0 1; color: $text-muted; }
    """

    def __init__(
        self, ref: RunRef, env: Env, tick_interval: float = 1.0, log_cap: int = 500
    ) -> None:
        super().__init__()
        self._ref = ref
        self._env = env
        self._tick_interval = tick_interval
        self._cursor = 0
        self._window: deque[Envelope] = deque(maxlen=log_cap)  # last N envelopes, oldest..newest
        self._filter_text = ""
        self._enabled: set[str] = set(_FAMILIES)  # all topic families on

    def compose(self) -> ComposeResult:
        yield Static("", id="detail-card")
        with Vertical(id="detail-logbox"):
            yield Static(
                Text.from_markup("[grey58]/ filter…   topic · request · step>N · text[/]"),
                id="detail-filter",
            )
            yield DataTable(id="detail-log", zebra_stripes=True, cursor_type="row")
            yield Static("", id="detail-chips")
        yield Static(
            Text.from_markup("[b]y[/] yank   [b]/[/] filter   [b]enter[/] expand   [b]esc[/] back"),
            id="detail-foot",
        )

    def on_mount(self) -> None:
        self.query_one("#detail-card").border_title = self._ref[0]  # run_id
        self.query_one("#detail-logbox").border_title = "log · live · newest ↑"
        t = self.query_one("#detail-log", DataTable)
        t.add_columns(*_LOG_COLS)
        self._tick()

    def _tick(self) -> None:
        self._refresh()

    def _predicate(self) -> Callable[[Envelope], bool]:
        # subtractive: hide only the toggled-off KNOWN families (_FAMILIES - _enabled);
        # a topic in an unknown family (e.g. launcher.*) is never in that hidden set, so
        # it always shows — the log streams every record (Finding #1).
        return envelope_filter(self._filter_text, set(_FAMILIES) - self._enabled)

    def _show_card(self, card: Text) -> None:
        self.query_one("#detail-card", Static).update(card)

    def _render_window(self) -> None:
        """Repaint the log table newest-first from the in-memory window + predicate,
        preserving the selected seq. Called on the main thread only (marshaled)."""
        t = self.query_one("#detail-log", DataTable)
        sel: str | None = None
        if t.row_count:
            sel = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        pred = self._predicate()
        rows = [e for e in reversed(self._window) if pred(e)]  # newest-first, filtered
        t.clear()
        for e in rows:
            t.add_row(
                str(e.seq),
                Text(e.topic, style=topic_color(e.topic)),
                e.request_id or "",
                _body_text(e),
                key=str(e.seq),
            )
        if sel is not None and sel in {str(e.seq) for e in rows}:
            t.move_cursor(row=t.get_row_index(sel))
        self._render_chips()

    def _render_chips(self) -> None:
        parts: list[str] = []
        counts = {f: sum(1 for e in self._window if e.topic.split(".")[0] == f) for f in _FAMILIES}
        for f in _FAMILIES:
            on = f in self._enabled
            col = topic_color(f + ".") if on else "grey37"
            parts.append(f"[{col}]●[/] {f} {counts[f]}")
        self.query_one("#detail-chips", Static).update(Text.from_markup("   ".join(parts)))

    def _selected_envelope(self) -> Envelope | None:
        """The Envelope under the DataTable cursor, mapped back through its row key
        (`str(e.seq)`, set in `_render_window`) to `self._window` -- the table only
        ever holds the seq as a string cell/key, never the Envelope itself."""
        t = self.query_one("#detail-log", DataTable)
        if not t.row_count:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        if key is None:
            return None
        return next((e for e in self._window if e.seq == int(key)), None)

    def action_yank(self) -> None:
        e = self._selected_envelope()
        if e is not None:
            self.app.copy_to_clipboard(format_envelope(e))  # OSC 52, via App.copy_to_clipboard

    def action_expand(self) -> None:
        e = self._selected_envelope()
        if e is not None:
            self.app.push_screen(ExpandScreen(e))

    def on_data_table_row_selected(self, _msg: DataTable.RowSelected) -> None:
        # `enter` on the focused DataTable fires this (its own binding, not ours --
        # see the BINDINGS comment above); route it to the same expand action.
        self.action_expand()

    def _marshal(self, fn: Callable[..., Any], *args: Any) -> None:
        # a pop mid-tick can tear the screen down before this marshals; the
        # card/log update is best-effort (the fold is re-run each tick anyway).
        try:
            self.app.call_from_thread(fn, *args)
        except _TEARDOWN_ERRORS:
            pass

    @work(thread=True, exclusive=True)
    def _refresh(self) -> None:
        # no `is_mounted` guard here: a pop mid-tick can only race the _marshal calls
        # below onto a torn-down screen, which _marshal already guards
        # (_TEARDOWN_ERRORS) -- including the final self-reschedule, so a popped screen
        # simply stops ticking rather than raising.
        # card: the Row, re-folded off-thread each tick (pure) -- byte-torn surfaces as
        # a loud `corrupt` card, not a crash.
        row = render_single(self._ref, self._env)
        self._marshal(self._show_card, format_summary_card(row))
        # log: incremental raw delta (unfiltered accumulation -> clean cursor); the
        # filter is applied in _render_window over the bounded window.
        # UPSTREAM(runstate#15): when the substrate filters + supports backward reads,
        # pass filter=self._predicate() here (true retroactive filtering); v1 accumulates
        # raw and filters the window in _render_window. grep -rn "UPSTREAM(runstate#15)"
        delta = read_log_delta(self._ref, after=self._cursor)
        if delta:
            self._window.extend(delta)  # oldest..newest; deque(maxlen) trims the front
            self._cursor = delta[-1].seq
            self._marshal(self._render_window)
        self._marshal(self.set_timer, self._tick_interval, self._tick)


class ExpandScreen(ModalScreen[None]):
    """The full pretty-printed envelope for one log row, pushed by `enter`
    (`DrillDownScreen.action_expand`). `escape` pops back; `y` yanks the same
    `format_envelope` line the underlying screen's yank would (not the pretty body --
    one canonical clipboard shape, faithful to the raw envelope either way)."""

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("y", "yank", "Yank")]

    def __init__(self, envelope: Envelope) -> None:
        super().__init__()
        self._e = envelope

    def compose(self) -> ComposeResult:
        e = self._e
        body = (
            json.dumps(e.body, indent=2, default=str) if isinstance(e.body, dict) else str(e.body)
        )
        yield Static(
            Text(f"seq {e.seq}   {e.topic}   {e.request_id or ''}\n\n{body}"), id="expand-body"
        )

    def action_yank(self) -> None:
        self.app.copy_to_clipboard(format_envelope(self._e))
