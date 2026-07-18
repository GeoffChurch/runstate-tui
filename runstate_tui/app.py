from __future__ import annotations

import threading
import uuid
from collections.abc import Callable

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Static

from .confirm import ConfirmStopScreen
from .control import StopOutcome, StopResult, dispatch_stop
from .env import Env
from .format import format_row
from .resolver import RunRef
from .table import render_single

StopDispatch = Callable[[RunRef, str, float], StopOutcome]


def _default_stop_dispatch(ref: RunRef, request_id: str, timeout: float) -> StopOutcome:
    return dispatch_stop(ref, request_id=request_id, timeout=timeout)


class SingleRunApp(App[None]):
    """The single-run cockpit: folds one run OFF the render thread at ~1 Hz and
    shows its Row (see `_fold`). It also carries the one effectful arrow — a
    confirm-gated `stop` (spec §6.2) run on a DEDICATED thread (spec §13) so a
    data-plane stall can never starve the stop key. `_fold` is fail-fast: the
    fold yields a Row for every legitimate condition — byte-torn -> a loud
    `corrupt` Row, never a crash — so only a truly-unclassifiable exception
    (a genuine bug) escapes and crashes the cockpit rather than self-healing
    into a silent retry (§10 — a crash is not a freeze)."""

    BINDINGS = [("s", "stop", "Stop run")]

    def __init__(
        self,
        ref: RunRef,
        env: Env,
        tick_interval: float = 1.0,
        stop_timeout: float = 5.0,
        stop_dispatch: StopDispatch = _default_stop_dispatch,
    ) -> None:
        super().__init__()
        self._ref = ref
        self._env = env
        self._tick_interval = tick_interval
        self._stop_timeout = stop_timeout
        self._stop_dispatch = stop_dispatch
        self._stop_in_flight = False

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="run")
        yield Static("", id="stop")

    def on_mount(self) -> None:
        self._tick()  # first tick now (set_timer(0, …) is invalid in textual)

    def _tick(self) -> None:
        self._fold()

    def _show(self, text: str) -> None:
        self.query_one("#run", Static).update(text)

    @work(thread=True, exclusive=True)
    def _fold(self) -> None:
        # fail-fast: the fold yields a Row for every legitimate condition — byte-torn
        # is now a loud `corrupt` Row from open_and_fold, never reaching this worker —
        # so only a truly-unclassifiable exception (a genuine bug) escapes and crashes
        # the cockpit rather than self-healing into a silent retry. A crash is not a
        # freeze — the app exits (§10 holds).
        row = render_single(self._ref, self._env)
        text = format_row(row)
        self.call_from_thread(self._show, text)  # query + update via call_from_thread
        self.call_from_thread(self.set_timer, self._tick_interval, self._tick)

    # ---- stop: the one effectful arrow (spec §6.2, §13) -----------------
    def action_stop(self) -> None:
        if self._stop_in_flight:
            return  # one stop at a time
        run_id = self._ref[0]
        self.push_screen(ConfirmStopScreen(f"Stop run {run_id}? y/n"), self._on_confirm)

    def _on_confirm(self, confirmed: bool | None) -> None:
        # runs on the UI thread (push_screen callback); no off-thread widget touch here.
        if not confirmed:
            return  # the confirm gate declined — no stop is sent
        self._stop_in_flight = True
        request_id = f"webui:{uuid.uuid4()}"
        self.query_one("#stop", Static).update("stopping…")
        # a DEDICATED thread for the handshake — never the fold worker, never
        # Textual's shared executor (spec §13): a slow fold must not starve stop.
        threading.Thread(
            target=self._run_stop, args=(request_id,), daemon=True, name="stop-handshake"
        ).start()

    def _run_stop(self, request_id: str) -> None:
        # pre-initialised so the `finally` can always report SOMETHING and reset
        # the guard — the stop key must reset even if _stop_dispatch raises a
        # BaseException (KeyboardInterrupt/SystemExit); otherwise a latched
        # _stop_in_flight wedges the key shut forever.
        outcome = StopOutcome(StopResult.UNDELIVERED, request_id, "stop did not complete")
        try:
            outcome = self._stop_dispatch(self._ref, request_id, self._stop_timeout)
        except Exception as exc:  # a total dispatch shouldn't raise; report, don't die silently
            outcome = StopOutcome(
                StopResult.UNDELIVERED, request_id, f"stop dispatch error: {exc!r}"
            )
        finally:
            try:
                self.call_from_thread(self._finish_stop, outcome)
            except Exception:
                # best-effort teardown marshal: if the app is tearing down while a
                # stop was in-flight, the result-update can fail three ways depending
                # on timing — loop closed (RuntimeError), callback cancelled
                # (concurrent.futures.CancelledError), or the #stop widget already
                # unmounted (textual NoMatches). The stop was already SENT; suppress
                # rather than crash the daemon thread with a teardown traceback.
                pass

    def _finish_stop(self, outcome: StopOutcome) -> None:
        self._stop_in_flight = False
        self.query_one("#stop", Static).update(outcome.label)  # UI thread; query is safe
