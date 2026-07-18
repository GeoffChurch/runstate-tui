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
