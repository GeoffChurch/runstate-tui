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
