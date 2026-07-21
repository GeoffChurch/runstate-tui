import asyncio
import threading
import time

from runstate import create_channel
from textual.widgets import Static

from runstate_tui.app import SingleRunApp
from runstate_tui.confirm import ConfirmStopScreen
from runstate_tui.control import StopOutcome, StopResult
from runstate_tui.detail import DrillDownScreen
from runstate_tui.env import Env


def _live_sqlite_run(tmp_path):
    ch = create_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    ch.send({"step": 7, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
    ch.close()
    return ("r", str(tmp_path), "sqlite")


def test_single_run_app_renders_the_folded_row(tmp_path):
    asyncio.run(_render(tmp_path))


async def _render(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    # a large tick_interval so only ONE fold runs during the test
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=30.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()  # threaded worker runs off the loop
        content = str(app.query_one("#run", Static).content)
        assert "live" in content
        assert "step 7" in content


def test_ticks_reschedule_without_pile_up(tmp_path):
    asyncio.run(_reschedules(tmp_path))


async def _reschedules(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)  # let several ticks elapse in real time
        await app.workers.wait_for_complete()
        content = str(app.query_one("#run", Static).content)
        assert "live" in content  # still rendering correctly after many ticks


def test_byte_torn_renders_corrupt_not_crash(tmp_path):
    import sqlite3

    ch = create_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    conn = sqlite3.connect(str(tmp_path / "r.db"))
    conn.execute("UPDATE log SET body = ? WHERE seq = 1", ("{not json",))
    conn.commit()
    conn.close()
    asyncio.run(_shows_corrupt(("r", str(tmp_path), "sqlite")))


async def _shows_corrupt(ref):
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)
        assert "corrupt" in str(app.query_one("#run", Static).content)  # loud, no crash


def test_alien_started_renders_malformed_not_crash(tmp_path):
    import sqlite3

    ch = create_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    conn = sqlite3.connect(str(tmp_path / "r.db"))
    conn.execute("UPDATE log SET body = ? WHERE seq = 1", ("42",))  # valid JSON, non-dict
    conn.commit()
    conn.close()
    asyncio.run(_shows_no_crash(("r", str(tmp_path), "sqlite")))


async def _shows_no_crash(ref):
    # the app-level regression for the alien-started crash: `read_elapsed`'s
    # `.body.get("t")` used to run OUTSIDE guarded(), so this AttributeError escaped
    # open_and_fold entirely and crashed the fail-fast worker (and the whole cockpit).
    # Fixed, the fold worker completes and renders a Row -- no crash, and this is NOT
    # the byte-torn `corrupt` class since the body decoded fine.
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)
        content = str(app.query_one("#run", Static).content)
        assert "corrupt" not in content


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


class _RecordingDispatch:
    """A fake stop_dispatch that records its call and returns a chosen outcome.
    Runs on the app's dedicated stop thread, so guard the record with a lock."""

    def __init__(self, outcome: StopOutcome) -> None:
        self._outcome = outcome
        self._lock = threading.Lock()
        self.calls: list[tuple[object, str, float]] = []

    def __call__(self, ref, request_id: str, timeout: float) -> StopOutcome:
        with self._lock:
            self.calls.append((ref, request_id, timeout))
        return self._outcome


def test_pressing_s_opens_the_confirm_gate(tmp_path):
    asyncio.run(_opens_gate(tmp_path))


async def _opens_gate(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        assert isinstance(app.screen, ConfirmStopScreen)


def test_confirming_dispatches_a_webui_stop_and_shows_the_outcome(tmp_path):
    asyncio.run(_confirms(tmp_path))


async def _confirms(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    fake = _RecordingDispatch(StopOutcome(StopResult.ACCEPTED, "webui:x"))
    app = SingleRunApp(
        ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_timeout=3.0, stop_dispatch=fake
    )
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause(0.01)
            if fake.calls:
                break
        await pilot.pause(0.05)
        assert len(fake.calls) == 1
        called_ref, request_id, timeout = fake.calls[0]
        assert called_ref == ref
        assert request_id.startswith("webui:")
        assert timeout == 3.0
        assert str(app.query_one("#stop", Static).content) == "✓ stop accepted"


def test_declining_the_gate_dispatches_nothing(tmp_path):
    asyncio.run(_declines(tmp_path))


async def _declines(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    fake = _RecordingDispatch(StopOutcome(StopResult.ACCEPTED, "webui:x"))
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_dispatch=fake)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("n")
        await pilot.pause(0.1)
        assert fake.calls == []
        assert str(app.query_one("#stop", Static).content) == ""


def test_unsafe_outcome_is_shown_high(tmp_path):
    asyncio.run(_unsafe(tmp_path))


async def _unsafe(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    fake = _RecordingDispatch(StopOutcome(StopResult.UNSAFE, "webui:x", "no live worker?"))
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_dispatch=fake)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause(0.01)
            if fake.calls:
                break
        await pilot.pause(0.05)
        assert "⚠ unsafe stop" in str(app.query_one("#stop", Static).content)


def test_stop_runs_on_the_dedicated_thread(tmp_path):
    asyncio.run(_dedicated_thread(tmp_path))


async def _dedicated_thread(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    seen: dict[str, str] = {}

    def dispatch(r, request_id, timeout):
        seen["thread"] = threading.current_thread().name
        return StopOutcome(StopResult.ACCEPTED, request_id)

    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_dispatch=dispatch)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause(0.01)
            if "thread" in seen:
                break
    # the handshake ran on the DEDICATED 'stop-handshake' thread — proving it is
    # neither the UI/MainThread (a sync dispatch would freeze the cockpit) nor a
    # Textual worker-pool thread shared with the fold (§13). Either regression
    # would surface a different thread name here.
    assert seen.get("thread") == "stop-handshake"


def test_stop_guard_resets_after_a_raising_dispatch(tmp_path):
    asyncio.run(_guard_resets(tmp_path))


async def _guard_resets(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    calls: list[str] = []

    def raising(r, request_id, timeout):
        calls.append(request_id)
        if len(calls) == 1:
            raise RuntimeError("boom")  # first dispatch explodes
        return StopOutcome(StopResult.ACCEPTED, request_id)

    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_dispatch=raising)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause(0.01)
            if len(calls) == 1:
                break
        await pilot.pause(0.05)
        # the guard must have reset despite the raise -> a second stop dispatches
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause(0.01)
            if len(calls) == 2:
                break
    assert len(calls) == 2  # no latch: the stop key survived a failed dispatch


def test_second_stop_ignored_while_one_is_in_flight(tmp_path):
    asyncio.run(_in_flight_guard(tmp_path))


async def _in_flight_guard(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    release = threading.Event()
    calls: list[str] = []

    def blocking(r, request_id, timeout):
        calls.append(request_id)
        release.wait(2.0)  # hold the stop in-flight until released
        return StopOutcome(StopResult.ACCEPTED, request_id)

    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_dispatch=blocking)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(50):
            await pilot.pause(0.01)
            if len(calls) == 1:
                break
        # a second press while the first stop is still in-flight must NOT dispatch
        await pilot.press("s")
        await pilot.pause(0.05)
        assert len(calls) == 1
        release.set()
        await pilot.pause(0.05)


def test_quitting_mid_stop_does_not_raise_on_the_stop_thread(tmp_path):
    errors: list[str] = []
    old_hook = threading.excepthook
    # install the capturing hook OUTSIDE asyncio.run so it stays live through the
    # loop's teardown — that is WHEN the mid-stop call_from_thread actually fails.
    threading.excepthook = lambda a: errors.append(a.exc_type.__name__)
    try:
        asyncio.run(_quit_mid_stop(tmp_path))
        time.sleep(0.2)  # let any teardown exception on the stop thread fire
    finally:
        threading.excepthook = old_hook
    assert errors == [], f"stop thread raised at teardown: {errors}"


async def _quit_mid_stop(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    release = threading.Event()
    entered = threading.Event()

    def blocking(r, request_id, timeout):
        entered.set()
        release.wait(2.0)  # keep the stop in-flight until after the app tears down
        return StopOutcome(StopResult.ACCEPTED, request_id)

    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0, stop_dispatch=blocking)
    async with app.run_test() as pilot:
        await pilot.press("s")
        await pilot.pause()
        await pilot.press("y")
        for _ in range(200):
            await pilot.pause(0.01)
            if entered.is_set():
                break
    release.set()  # let the in-flight dispatch return -> finally marshals onto the dying loop
