import asyncio
import threading

from runstate import open_channel
from textual.widgets import Static

from runstate_tui.app import SingleRunApp
from runstate_tui.confirm import ConfirmStopScreen
from runstate_tui.control import StopOutcome, StopResult
from runstate_tui.env import Env


def _live_sqlite_run(tmp_path):
    ch = open_channel("r", root=tmp_path, backend="sqlite")
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


def test_fold_error_does_not_crash_and_loop_recovers(tmp_path, monkeypatch):
    asyncio.run(_recovers(tmp_path, monkeypatch))


async def _recovers(tmp_path, monkeypatch):
    import runstate_tui.app as appmod

    ref = _live_sqlite_run(tmp_path)
    real = appmod.render_single
    calls = {"n": 0}

    def flaky(r, e):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # unanticipated fold error on the first tick
        return real(r, e)

    monkeypatch.setattr(appmod, "render_single", flaky)
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)  # let the failed tick + a recovery tick run
        await app.workers.wait_for_complete()
        content = str(app.query_one("#run", Static).content)
        assert calls["n"] >= 2  # it kept ticking after the error
        assert not app._exit  # the app did not crash/exit
        assert "live" in content  # recovered and rendered the run


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
