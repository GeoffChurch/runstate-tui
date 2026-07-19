from __future__ import annotations

import concurrent.futures
from collections import deque
from collections.abc import Callable
from typing import Any

from rich.text import Text
from runstate.channel import Envelope
from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import DataTable, Static

from .env import Env
from .format import format_summary_card, topic_color
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

    This task (Task 3 of the drill-down redesign) builds the shell + `_render_window`;
    `_tick` here is a single synchronous fill (read everything after 0 into the window,
    render once) so the table renders for the test. Task 4 replaces `_refresh`'s body
    with the incremental delta-cursor live-tail worker (+ reschedule loop) that keeps
    `self._window`/`self._cursor` growing tick-over-tick -- the fields already exist so
    that swap touches no other state."""

    BINDINGS = [("escape", "app.pop_screen", "Back")]

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

    def _marshal(self, fn: Callable[..., Any], *args: Any) -> None:
        # a pop mid-tick can tear the screen down before this marshals; the
        # card/log update is best-effort (the fold is re-run each tick anyway).
        try:
            self.app.call_from_thread(fn, *args)
        except _TEARDOWN_ERRORS:
            pass

    @work(thread=True, exclusive=True)
    def _refresh(self) -> None:
        # no `is_mounted` guard here: this task's fill runs exactly once (no
        # self-reschedule, see class docstring) -- a pop mid-fill can only race the
        # _marshal calls below onto a torn-down screen, which _marshal already guards
        # (_TEARDOWN_ERRORS). card: the Row, re-folded off-thread -- byte-torn now
        # surfaces as a loud `corrupt` card, not a crash, so there is no exception to
        # race a pop.
        row = render_single(self._ref, self._env)
        self._marshal(self._show_card, format_summary_card(row))
        # Task 3: a single synchronous fill (not yet the incremental live-tail --
        # Task 4 replaces this with a delta-cursor read + reschedule loop). Still
        # off-thread + _TEARDOWN_ERRORS-guarded like every other fold/read worker here.
        envelopes = read_log_delta(self._ref, after=0)
        self._window.clear()
        self._window.extend(envelopes)
        if envelopes:
            self._cursor = envelopes[-1].seq
        self._marshal(self._render_window)
