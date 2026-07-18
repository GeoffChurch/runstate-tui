from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Static

from .env import Env
from .format import format_row
from .resolver import RunRef
from .table import render_single


class SingleRunApp(App[None]):
    """The single-run cockpit: folds one run OFF the render thread at ~1 Hz and
    shows its Row. The fold is a threaded (exclusive) worker, so a blocking
    sqlite open never freezes the UI; the next tick is rescheduled inside a
    `finally` after the fold, so ticks never overlap (spec §13) AND the loop
    keeps running even if a fold raises an unanticipated exception — the
    widget just keeps its last-good content until a later tick recovers
    (spec §3/§10, never crash / never freeze)."""

    def __init__(self, ref: RunRef, env: Env, tick_interval: float = 1.0) -> None:
        super().__init__()
        self._ref = ref
        self._env = env
        self._tick_interval = tick_interval

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="run")

    def on_mount(self) -> None:
        self._tick()  # first tick now (set_timer(0, …) is invalid in textual)

    def _tick(self) -> None:
        self._fold()

    def _show(self, text: str) -> None:
        self.query_one("#run", Static).update(text)

    @work(thread=True, exclusive=True, exit_on_error=False)
    def _fold(self) -> None:
        try:
            row = render_single(self._ref, self._env)  # blocking fold, off the render thread
            text = format_row(row)
            self.call_from_thread(self._show, text)  # query + update via call_from_thread
        finally:
            # always reschedule, even on an unanticipated fold error: the widget
            # keeps its last-good content and the loop retries next tick (§3/§10).
            self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
