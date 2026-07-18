import asyncio

from runstate import open_channel
from textual.widgets import Static

from runstate_tui.app import SingleRunApp
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
