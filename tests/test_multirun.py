import asyncio
import threading
import time

from rich.text import Span
from runstate import open_channel
from textual.widgets import DataTable, Static

import runstate_tui.multirun as multirun_mod
from runstate_tui.detail import DrillDownScreen
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


def test_app_survives_a_contained_fold_error(tmp_path, monkeypatch):
    # Owner policy (faithful containment): a per-run fold bug is contained to a loud
    # fold-error row (fold_frame is total), so the whole cockpit does NOT crash -- the table
    # still updates (a TableReady still fires) and the errored run coexists with healthy ones.
    # Distinct from the reverted self-heal-retry: here the worker stays fail-fast, the error
    # is surfaced per-run, not masked as ⚠ I/O stalled.
    real_row_for = ChannelPool.row_for

    def flaky_row_for(self, ref, frame_env):
        if ref[0] == "a":
            raise RuntimeError("boom: internal fold bug on run a")
        return real_row_for(self, ref, frame_env)

    monkeypatch.setattr(ChannelPool, "row_for", flaky_row_for)
    asyncio.run(_app_survives_a_contained_fold_error(tmp_path))


async def _app_survives_a_contained_fold_error(tmp_path):
    a = _seed(tmp_path, "a")
    b = _seed(tmp_path, "b")
    app = MultiRunApp(explicit_resolver([a, b]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert app._last_ready is not None  # a TableReady fired -> the cockpit did NOT crash
        assert t.row_count == 2  # the errored run did not sink the frame; both runs present
        # a's status cell is the loud fold-error verdict (label + the exception detail)...
        assert t.get_cell(ref_key(a), "status").startswith("fold-error")
        assert "RuntimeError" in t.get_cell(ref_key(a), "status")
        assert not t.get_cell(ref_key(b), "status").startswith("fold-error")  # ...b is normal


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


def test_watchdog_banner_shows_on_a_first_frame_wedge(tmp_path, monkeypatch):
    # Fix (final review): if the owner thread wedges on its VERY FIRST fold (e.g. every
    # run sits on a hung mount at launch), TableReady never fires -- _last_ready must be
    # baselined at on_mount, or _is_stalled() (which treats None as "not stalled") would
    # never trip and the banner would stay silent forever over a permanently blank table
    # -- the exact §10 failure the watchdog exists to prevent, just at t=0 instead of
    # mid-session. Unlike test_watchdog_banner_shows_and_hides_the_stall, this drives the
    # FIRST-frame case: no successful TableReady has EVER landed before the stall check.
    # _DRAIN_TIMEOUT is shrunk so on_unmount's drain gives up and leaks promptly instead
    # of blocking the full default 5s against a fold that stays wedged through teardown.
    gate = threading.Event()

    def wedged_fold(pool, refs, env, now):
        gate.wait(5.0)  # bounded so a failed test can't hang forever
        return ()

    monkeypatch.setattr(multirun_mod, "fold_frame", wedged_fold)
    monkeypatch.setattr(multirun_mod, "_DRAIN_TIMEOUT", 0.2)
    try:
        asyncio.run(_watchdog_banner_shows_on_a_first_frame_wedge(gate))
    finally:
        gate.set()  # belt-and-braces: release it if the body raised before its own gate.set()


async def _watchdog_banner_shows_on_a_first_frame_wedge(gate):
    clock = {"t": 100_000.0}
    app = MultiRunApp(
        explicit_resolver([]), Env(clock=lambda: clock["t"]), tick_interval=999, stall_ticks=3
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._last_ready is not None  # baselined at mount, not left None forever
        banner = app.query_one("#stall", Static)
        assert not banner.display  # no time has passed yet
        clock["t"] += 3000.0  # > 3 * 999 stall window; fold is STILL wedged, never posted
        app._on_watchdog()
        assert banner.display  # trips even though no fold has EVER completed
        assert "I/O stalled" in str(banner.content)
        # leaving this block triggers unmount; the shrunk _DRAIN_TIMEOUT above makes the
        # drain give up and leak promptly rather than block on the still-wedged fold.
    # Release the wedged worker only AFTER the screen has fully torn down (past this
    # point, not before): releasing it while the app is still mounted would let the
    # freed worker thread post a real TableReady that races the teardown's unmounting
    # -- an intermittent NoMatches on '#runs' (observed empirically). Releasing it here
    # also lets asyncio.run()'s own executor-shutdown return promptly instead of
    # blocking on the still-running thread for its full internal bound.
    gate.set()


def test_table_has_a_colored_status_dot(tmp_path):
    asyncio.run(_table_has_a_colored_status_dot(tmp_path))


async def _table_has_a_colored_status_dot(tmp_path):
    # The leading `dot` column is a redundant traffic-light: a Rich Text "●" styled
    # via status_color(row.status) -- never the sole signal (the text status column
    # still carries the label). A live run's dot must be green.
    ref = _seed(tmp_path, "a")  # a live run under the fixed clock
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        cell = t.get_cell(ref_key(ref), "dot")  # the stored Rich Text, unconverted
        assert "●" in cell.plain and cell.style == "#3fb950"


def test_marker_corrupt_row_is_double_warning_colored_red():
    # The clean split: `_marker` keys on ISSUE severity + stop count ONLY, never
    # row.severity/status severity -- the `dot` cell already carries status color.
    # `_corrupt` plants a HIGH Issue, so a corrupt row still gets the loud ⚠⚠, colored
    # to match the dot's red.
    from runstate_tui.multirun import _marker
    from runstate_tui.table import _corrupt

    marker = _marker(_corrupt(seq=1))
    assert marker.plain == "⚠⚠"
    assert marker.style == "#f85149"


def test_marker_unreadable_row_is_empty_the_dot_carries_it_alone():
    # Pins the clean split's other half: `_bare` rows (missing/unreadable) carry
    # issues=() -- no Issue at all -- so under the new issue-severity-only keying they
    # get NO `!` marker. Before the split, row.severity (which folds in STATUS
    # severity too) would have shown ⚠⚠ here, restating what the red dot already says.
    from runstate_tui.multirun import _marker
    from runstate_tui.table import _bare
    from runstate_tui.types import Status

    marker = _marker(_bare(Status.unreadable()))
    assert marker.plain == ""


def test_marker_malformed_row_is_single_warning_colored_amber():
    # Pins the MEDIUM-severity branch: a single Issue below HIGH but at/above MEDIUM
    # gets the quieter single ⚠, colored amber -- distinct from the HIGH ⚠⚠ red and
    # from the no-issue empty/neutral cases pinned above.
    from runstate_tui.multirun import _marker
    from runstate_tui.types import Issue, IssueKind, Row, Severity, Status

    row = Row(
        status=Status.live(),
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        undischarged_stops=(),
        live_demand=(),
        issues=(
            Issue(kind=IssueKind.MALFORMED, severity=Severity.MEDIUM, message="record malformed"),
        ),
    )
    marker = _marker(row)
    assert marker.plain == "⚠"
    assert marker.style == "#d29922"


def test_marker_undischarged_stop_alone_renders_neutral():
    # A stop with no issue: the ■N badge must NOT inherit any severity color -- it is
    # an orthogonal axis, always neutral/default-colored regardless of what precedes it.
    from runstate.channel import Envelope

    from runstate_tui.multirun import _marker
    from runstate_tui.types import Row, Status

    stop = Envelope(seq=5, topic="control.stop", name=None, request_id="webui:s", body={})
    row = Row(
        status=Status.live(),
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        undischarged_stops=(stop,),
        live_demand=(),
        issues=(),
    )
    marker = _marker(row)
    assert marker.plain == "■1"
    assert marker.style == ""  # no base color
    assert marker.spans == [Span(0, len("■1"), "default")]  # explicit neutral override


def test_enter_opens_drilldown_for_selected_run_and_escape_returns(tmp_path):
    asyncio.run(_enter_opens_drilldown_for_selected_run_and_escape_returns(tmp_path))


async def _enter_opens_drilldown_for_selected_run_and_escape_returns(tmp_path):
    # Pins Task 3's action_detail: `enter` on the SELECTED row (not just any row) opens
    # a DrillDownScreen for that run's ref, and `escape` pops back to the table.
    a = _seed(tmp_path, "a")
    b = _seed(tmp_path, "b")
    app = MultiRunApp(explicit_resolver([a, b]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        # explicitly select b -- a stronger check than "in (a, b)": proves the cursor
        # key is mapped to the RIGHT ref, not just some ref the resolver happens to know.
        t.move_cursor(row=t.get_row_index(ref_key(b)))
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, DrillDownScreen)
        assert app.screen._ref == b
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, DrillDownScreen)
