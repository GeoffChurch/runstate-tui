import asyncio
import threading
import time

from runstate import open_channel
from textual.widgets import DataTable, Static

import runstate_tui.multirun as multirun_mod
from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.pool import ChannelPool
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


def test_reconcile_preserves_selected_row_key_across_reorder(tmp_path):
    asyncio.run(_preserves_selected_row_key_across_reorder(tmp_path))


async def _preserves_selected_row_key_across_reorder(tmp_path):
    # The real cursor-preservation test: with >=3 rows, select the LAST-sorting row, then
    # append a run that sorts FIRST so the reconcile REORDERS and the selected row's INDEX
    # shifts. The move_cursor(get_row_index(sel)) restore must keep the cursor on the same
    # row KEY (not the same numeric coordinate). Vacuous with one row -- the point is the
    # index change. Seed run_ids that sort b < c < d, select d, then prepend a.
    b = _seed(tmp_path, "b")
    c = _seed(tmp_path, "c")
    d = _seed(tmp_path, "d")
    live = {"refs": [b, c, d]}
    app = MultiRunApp(lambda now: list(live["refs"]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.row_count == 3
        # move the cursor onto d -- the last-sorting, NON-first row
        t.move_cursor(row=t.get_row_index(ref_key(d)))
        assert t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value == ref_key(d)
        d_index_before = t.cursor_coordinate.row  # == 2
        # append a run that sorts before everything -> the reconcile reorders, d shifts down
        a = _seed(tmp_path, "a")
        live["refs"] = [b, c, d, a]
        app._tick()
        await pilot.pause()
        await pilot.pause()
        assert t.row_count == 4
        # d's INDEX must have changed (proves a genuine reorder -- the test isn't vacuous)...
        assert t.get_row_index(ref_key(d)) != d_index_before
        # ...but the SELECTED ROW KEY must still be d (the cursor followed the key, not the
        # index). This is what FAILS if move_cursor(get_row_index(sel)) is stubbed to pass.
        assert t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value == ref_key(d)


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


def test_duplicate_ref_from_resolver_yields_one_row_no_crash(tmp_path):
    asyncio.run(_duplicate_ref_yields_one_row(tmp_path))


async def _duplicate_ref_yields_one_row(tmp_path):
    # A non-dedup resolver (Resolver is a public Callable) can hand the frame [a, a]. The
    # reconcile must add a's row once and UPDATE (not re-add) on the duplicate, never
    # raising DuplicateKey. Bypass explicit_resolver (which dedups) with a raw lambda.
    a = _seed(tmp_path, "a")
    app = MultiRunApp(lambda now: [a, a], Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.row_count == 1  # exactly one row for a, no DuplicateKey crash
        assert {k.value for k in t.rows.keys()} == {ref_key(a)}


def test_fold_raise_self_heals_the_tick_chain(tmp_path, monkeypatch):
    # Finding 4: an unexpected raise from a fold must NOT permanently kill the tick chain.
    # With the reschedule in the finally (+ exit_on_error=False) the loop self-heals: the
    # first fold raises, a later fold succeeds and populates the row. If the reschedule sat
    # in the try (skipped by the raise), row_count would stay 0 forever.
    real_fold = multirun_mod.fold_frame
    calls = {"n": 0}
    lock = threading.Lock()

    def flaky_fold(pool, refs, env, now):
        with lock:
            calls["n"] += 1
            first = calls["n"] == 1
        if first:
            raise RuntimeError("boom: unexpected fold raise on the first frame")
        return real_fold(pool, refs, env, now)

    monkeypatch.setattr(multirun_mod, "fold_frame", flaky_fold)
    asyncio.run(_fold_raise_self_heals(tmp_path))


async def _fold_raise_self_heals(tmp_path):
    ref = _seed(tmp_path, "a")
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=0.05)
    async with app.run_test() as pilot:
        t = app.query_one("#runs", DataTable)
        healed = False
        for _ in range(100):
            await pilot.pause(0.02)
            if t.row_count == 1:
                healed = True
                break
        assert healed  # tick chain survived the unexpected fold raise and re-populated


def test_teardown_drain_blocks_until_fold_finishes(tmp_path, monkeypatch):
    # Finding 2(a): the on_unmount drain must BLOCK on the in-flight fold -- close_all runs
    # only AFTER the owner thread has finished touching the pool (no use-after-close).
    real_fold = multirun_mod.fold_frame
    gate = threading.Event()  # released to let the wedged fold finish
    in_fold = threading.Event()  # signals the fold is actually in flight
    order: list[str] = []
    order_lock = threading.Lock()

    def blocking_fold(pool, refs, env, now):
        in_fold.set()
        gate.wait(5.0)  # bounded so a failed test can never hang forever
        with order_lock:
            order.append("fold-returned")
        return real_fold(pool, refs, env, now)

    real_close = ChannelPool.close_all

    def spy_close(self):
        with order_lock:
            order.append("close_all")
        return real_close(self)

    monkeypatch.setattr(multirun_mod, "fold_frame", blocking_fold)
    monkeypatch.setattr(ChannelPool, "close_all", spy_close)
    try:
        asyncio.run(_drain_blocks_until_fold_finishes(tmp_path, gate, in_fold))
    finally:
        gate.set()  # release the wedged fold worker even if the test body raised
    assert order == ["fold-returned", "close_all"]  # close_all strictly AFTER the fold


async def _drain_blocks_until_fold_finishes(tmp_path, gate, in_fold):
    ref = _seed(tmp_path, "a")
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if in_fold.is_set():
                break
            await pilot.pause(0.01)
        assert in_fold.is_set()  # the owner thread is wedged inside fold_frame
        # release the fold ~0.2s from now -- WHILE on_unmount's drain is already waiting --
        # so the drain must genuinely block on it before close_all can run.
        threading.Timer(0.2, gate.set).start()
        # leaving this block triggers unmount -> the bounded drain


def test_teardown_drain_times_out_and_leaks(tmp_path, monkeypatch):
    # Finding 2(b): a wedged owner thread must NOT hang quit forever. The drain times out at
    # _DRAIN_TIMEOUT and LEAKS the pool (does not close_all() into a live reader).
    gate = threading.Event()
    in_fold = threading.Event()
    close_calls: list[int] = []

    def wedged_fold(pool, refs, env, now):
        in_fold.set()
        gate.wait(30.0)  # never released until the test's cleanup
        return ()

    def spy_close(self):
        close_calls.append(1)

    monkeypatch.setattr(multirun_mod, "fold_frame", wedged_fold)
    monkeypatch.setattr(multirun_mod, "_DRAIN_TIMEOUT", 0.2)  # short: keep the test fast
    monkeypatch.setattr(ChannelPool, "close_all", spy_close)
    try:
        elapsed = asyncio.run(_drain_times_out_and_leaks(tmp_path, gate, in_fold))
    finally:
        gate.set()  # release the wedged fold worker so it can't leak into other tests
    assert elapsed < 3.0  # bounded: quit did NOT hang on the wedged owner thread
    assert close_calls == []  # pool LEAKED, not closed under the in-flight read


async def _drain_times_out_and_leaks(tmp_path, gate, in_fold):
    ref = _seed(tmp_path, "a")
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        for _ in range(200):
            if in_fold.is_set():
                break
            await pilot.pause(0.01)
        assert in_fold.is_set()
        start = time.monotonic()
        # leaving this block triggers unmount; the drain must TIME OUT at ~_DRAIN_TIMEOUT
    elapsed = time.monotonic() - start
    gate.set()  # release the wedged worker before returning (before asyncio.run shutdown)
    return elapsed


def test_io_stalled_watchdog_raises_and_clears():
    # Unit-test the stall CONDITION directly (no threads): a stale last_ready under the fake
    # clock is stalled; a fresh ready is not.
    clock = {"t": 100.0}
    app = MultiRunApp(
        explicit_resolver([]), Env(clock=lambda: clock["t"]), tick_interval=1.0, stall_ticks=3
    )
    app._last_ready = 100.0
    clock["t"] = 104.0  # 4s > 3 * 1s
    assert app._is_stalled()  # banner condition true
    app._last_ready = 104.0
    assert not app._is_stalled()  # a fresh ready cleared it


def test_watchdog_banner_shows_and_hides_the_stall(tmp_path):
    asyncio.run(_watchdog_banner_shows_and_hides(tmp_path))


async def _watchdog_banner_shows_and_hides(tmp_path):
    # Finding 5: exercise _on_watchdog's VISIBLE branch (never hit by the condition-only
    # unit test above). A stale _last_ready DISPLAYS the banner with the stalled text; a
    # fresh _last_ready hides it again.
    clock = {"t": 100_000.0}
    app = MultiRunApp(
        explicit_resolver([]), Env(clock=lambda: clock["t"]), tick_interval=999, stall_ticks=3
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        banner = app.query_one("#stall", Static)
        assert not banner.display  # hidden on mount
        app._last_ready = clock["t"] - 3000.0  # 3000s ago > 3 * 999 stall window
        app._on_watchdog()
        assert banner.display  # banner shown
        assert "I/O stalled" in str(banner.content)
        app._last_ready = clock["t"]  # a fresh ready
        app._on_watchdog()
        assert not banner.display  # banner hidden again
