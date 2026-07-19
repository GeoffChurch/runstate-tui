import asyncio

from runstate import open_channel
from textual.widgets import DataTable, Static

from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import explicit_resolver, ref_key


def _seed(tmp_path, run_id, t=100.0):
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": t}, topic="lifecycle.started")
    ch.close()
    return (run_id, str(tmp_path), "sqlite")


def test_table_shows_one_keyed_row_per_run(tmp_path):
    asyncio.run(_shows_one_keyed_row_per_run(tmp_path))


async def _shows_one_keyed_row_per_run(tmp_path):
    refs = [_seed(tmp_path, "a"), _seed(tmp_path, "b")]
    app = MultiRunApp(explicit_resolver(refs), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert {k.value for k in t.rows.keys()} == {ref_key(r) for r in refs}


def test_row_updates_and_preserves_cursor(tmp_path):
    asyncio.run(_row_updates_and_preserves_cursor(tmp_path))


async def _row_updates_and_preserves_cursor(tmp_path):
    ref = _seed(tmp_path, "a")
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        before = t.cursor_coordinate
        w = open_channel("a", root=ref[1], backend="sqlite")
        w.send({"step": 5, "consumed_seq": 0, "t": 150.0}, topic="lifecycle.heartbeat")
        w.close()
        app._tick()
        await pilot.pause()
        await pilot.pause()
        assert t.cursor_coordinate == before  # keyed reconcile, cursor kept
        assert t.get_row_index(ref_key(ref)) == 0  # still present, re-sorted


def test_shrinking_resolver_removes_the_row(tmp_path):
    asyncio.run(_shrinking_resolver_removes_the_row(tmp_path))


async def _shrinking_resolver_removes_the_row(tmp_path):
    a = _seed(tmp_path, "a")
    b = _seed(tmp_path, "b")
    live = {"refs": [a, b]}
    app = MultiRunApp(lambda now: list(live["refs"]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.row_count == 2
        live["refs"] = [a]
        app._tick()
        await pilot.pause()
        await pilot.pause()
        assert {k.value for k in t.rows.keys()} == {ref_key(a)}


def test_io_stalled_watchdog_raises_and_clears():
    # Unit-test the watchdog directly (no threads): a stale last_ready under the fake
    # clock raises the banner; a fresh ready clears it.
    clock = {"t": 100.0}
    app = MultiRunApp(
        explicit_resolver([]), Env(clock=lambda: clock["t"]), tick_interval=1.0, stall_ticks=3
    )
    _banner = Static("", id="stall")
    app._last_ready = 100.0
    clock["t"] = 104.0  # 4s > 3 * 1s
    assert app._is_stalled()  # banner condition true
    app._last_ready = 104.0
    assert not app._is_stalled()  # a fresh ready cleared it
