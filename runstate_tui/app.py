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
    sqlite open never freezes the UI; the next tick is rescheduled inside the
    worker after the update, so ticks never overlap (spec §13)."""

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

    @work(thread=True, exclusive=True)
    def _fold(self) -> None:
        row = render_single(self._ref, self._env)  # blocking fold, off the render thread
        text = format_row(row)
        self.call_from_thread(self._show, text)  # query + update both go via call_from_thread
        self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
