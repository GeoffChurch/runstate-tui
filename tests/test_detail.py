import asyncio
import threading
import time

from rich.text import Text
from runstate import open_channel
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static

from runstate_tui import detail as detail_module
from runstate_tui.detail import DrillDownScreen
from runstate_tui.env import Env
from runstate_tui.format import topic_color
from tests.helpers import advance_tick


def _seed_rich(tmp_path):
    """A rich run -- started(seq 1) + heartbeat(seq 2) + value(seq 3) + subscribe(seq 4)
    + stop(seq 5) -- à la conftest.py's `rich_run` fixture / showcase.py's
    `scene_drilldown` seed, but SQLITE-backed (not memory): DrillDownScreen and
    read_log_delta take a RunRef and read from disk, not an already-open channel."""
    run_id = "rich"
    writer = open_channel(run_id, root=tmp_path, backend="sqlite")
    writer.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    writer.send({"step": 7, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
    writer.send({"value": 0.03, "step": 7, "t": 140.0}, topic="value", name="loss")
    writer.send(
        {"schedule": {}, "names": ["loss"]}, topic="control.subscribe", request_id="webui:sub1"
    )
    writer.send({}, topic="control.stop", request_id="webui:stop1")
    writer.close()
    return (run_id, str(tmp_path), "sqlite")


class _HostApp(App[None]):
    """A tiny host App that pushes DrillDownScreen on mount (the pattern every
    DrillDownScreen test in this file reuses)."""

    def __init__(self, ref, tick_interval: float = 999.0) -> None:
        super().__init__()
        self._ref = ref
        self._tick_interval = tick_interval

    def compose(self) -> ComposeResult:
        yield Static("host")

    def on_mount(self) -> None:
        self.push_screen(
            DrillDownScreen(self._ref, Env(clock=lambda: 150.0), tick_interval=self._tick_interval)
        )


def test_drilldown_renders_card_and_newest_first_table(tmp_path):
    asyncio.run(_renders(tmp_path))


async def _renders(tmp_path):
    ref = _seed_rich(tmp_path)
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        # NOTE: query via the pushed `screen`, not `app.query_one(...)` -- in Textual
        # 8.2.8, App.query_one resolves against `_compose_screen`, the screen captured
        # at the app's *initial* compose, which push_screen never updates; it never sees
        # widgets on a later-pushed screen (confirmed empirically: NoMatches 100% of the
        # time, not a race -- carried over from the pre-redesign test's NOTE).
        # Screen.query_one has no such indirection.
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DrillDownScreen)
        t = screen.query_one("#detail-log", DataTable)
        for _ in range(60):
            await pilot.pause(0.02)
            if t.row_count == 5:
                break

        # summary card present + compact (exactly 2 lines, the design's compact card)
        card = screen.query_one("#detail-card", Static)
        assert isinstance(card.content, Text)  # Static.content holds the last update()'d Text
        assert "episode" in card.content.plain
        assert card.content.plain.count("\n") == 1

        # log table newest-first (seq descending) and topic-colored
        seqs = [t.get_cell_at(Coordinate(r, 0)) for r in range(t.row_count)]
        assert seqs == ["5", "4", "3", "2", "1"]  # descending = newest first
        topics = [t.get_cell_at(Coordinate(r, 1)) for r in range(t.row_count)]
        assert [tx.plain for tx in topics] == [
            "control.stop",
            "control.subscribe",
            "value",
            "lifecycle.heartbeat",
            "lifecycle.started",
        ]
        assert topics[0].style == topic_color("control.stop")
        assert topics[2].style == topic_color("value")
        assert topics[4].style == topic_color("lifecycle.started")


def test_render_window_preserves_the_selected_seq_across_a_repaint(tmp_path):
    asyncio.run(_preserves_selection(tmp_path))


async def _preserves_selection(tmp_path):
    ref = _seed_rich(tmp_path)
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DrillDownScreen)
        t = screen.query_one("#detail-log", DataTable)
        for _ in range(60):
            await pilot.pause(0.02)
            if t.row_count == 5:
                break

        # select seq 3 (topic "value", at index 2 in the newest-first [5,4,3,2,1] order)
        t.move_cursor(row=2)
        assert t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value == "3"

        # narrow the window (drop the "control" family -> seqs 4 and 5 disappear) and
        # repaint directly -- `_render_window` must track the SELECTED KEY (seq 3) to
        # its NEW physical row, not leave the cursor pinned to the old row index (which
        # is now a different envelope after the narrowing).
        screen._enabled.discard("control")
        screen._render_window()
        assert t.row_count == 3
        assert t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value == "3"
        assert t.cursor_coordinate.row == 0  # seq 3 is now the newest surviving row


def test_unknown_family_topics_always_shown_in_render_window(tmp_path):
    asyncio.run(_unknown_family_always_shown(tmp_path))


async def _unknown_family_always_shown(tmp_path):
    # Finding #1: a topic outside the three known families (e.g. `launcher.terminated`,
    # written onto the same run channel by runstate's Launcher) must NEVER be silently
    # dropped, no matter which known families are toggled off -- `_predicate` hides only
    # the toggled-off KNOWN families (`_FAMILIES - _enabled`), so an unknown family is
    # never in that hidden set. This goes through `DrillDownScreen._render_window` (not
    # `envelope_filter` directly) so it discriminates the restrict-to-vs-subtractive bug
    # in `_predicate` itself.
    from runstate.channel import Envelope

    ref = _seed_rich(tmp_path)
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DrillDownScreen)
        t = screen.query_one("#detail-log", DataTable)
        for _ in range(60):
            await pilot.pause(0.02)
            if t.row_count == 5:
                break

        launcher = Envelope(seq=6, topic="launcher.terminated", name=None, request_id=None, body={})
        screen._window.append(launcher)
        screen._enabled = set()  # toggle OFF every known family
        screen._render_window()

        # every known-family row is hidden; the unknown-family row is the sole survivor
        assert t.row_count == 1
        assert t.get_cell_at(Coordinate(0, 1)).plain == "launcher.terminated"


def test_pop_mid_tick_does_not_crash(tmp_path, monkeypatch):
    # Covers detail.py's teardown guard (_TEARDOWN_ERRORS / _marshal's try/except): a
    # pop mid-tick can race _refresh's off-thread _marshal calls onto a torn-down
    # screen. Deterministic instead of a blind sleep: render_single is patched to
    # block on a threading.Event, so the pop is GUARANTEED to land while the tick is
    # still mid-flight, and _marshal's call_from_thread calls are GUARANTEED to run
    # only after the screen has been popped (confirmed empirically pre-redesign: this
    # races self.app -- NoActiveAppError, a RuntimeError subclass already inside
    # _TEARDOWN_ERRORS -- 100% of the time under this synchronization, not
    # occasionally). threading.excepthook is installed OUTSIDE asyncio.run so it
    # survives loop teardown, in case anything escapes _marshal's own guard (mirrors
    # test_app.py's stop-teardown discipline).
    errors: list[str] = []
    old_hook = threading.excepthook
    threading.excepthook = lambda a: errors.append(a.exc_type.__name__)
    try:
        asyncio.run(_pop_mid_tick(tmp_path, monkeypatch))
        time.sleep(0.2)  # let any teardown exception on the tick's thread fire
    finally:
        threading.excepthook = old_hook
    assert errors == [], f"tick thread raised at teardown: {errors}"


async def _pop_mid_tick(tmp_path, monkeypatch):
    ref = _seed_rich(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    orig_render_single = detail_module.render_single

    def slow_render_single(ref, env):
        entered.set()
        release.wait(2.0)
        return orig_render_single(ref, env)

    monkeypatch.setattr(detail_module, "render_single", slow_render_single)

    app = _HostApp(ref, tick_interval=0.02)
    async with app.run_test() as pilot:  # on_mount fires the first (only) tick
        for _ in range(200):
            await pilot.pause(0.01)
            if entered.is_set():
                break
        assert entered.is_set(), "the tick never entered render_single -- can't test the race"
        await pilot.app.pop_screen()  # pop WHILE render_single is still blocked mid-tick
        release.set()  # let the in-flight tick proceed -> its _marshal calls now race the pop
        await pilot.pause(0.3)  # give the worker thread time to finish and marshal (or not crash)


def test_live_tail_appends_at_top_incrementally(tmp_path):
    # Task 4's core: the incremental delta-cursor live-tail worker (not T3's interim
    # synchronous full-fill) -- a fresh append on a HELD writer appears at the TOP of
    # the log table on the very next manual tick, and the table stays bounded to
    # exactly what's been drained so far (not the whole log re-read each time).
    asyncio.run(_live_tail(tmp_path))


async def _live_tail(tmp_path):
    ref = ("live", str(tmp_path), "sqlite")
    w = open_channel("live", root=tmp_path, backend="sqlite")
    w.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    app = _HostApp(ref, tick_interval=999.0)
    async with app.run_test(size=(90, 22)) as pilot:
        screen = app.screen
        assert isinstance(screen, DrillDownScreen)
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()  # settle the automatic on_mount tick
        t = screen.query_one("#detail-log", DataTable)
        assert t.get_cell_at(Coordinate(0, 0)) == "1"
        assert t.row_count == 1
        assert screen._cursor == 1

        w.send({"step": 5, "consumed_seq": 0, "t": 2.0}, topic="lifecycle.heartbeat")
        await advance_tick(pilot, screen)
        assert t.get_cell_at(Coordinate(0, 0)) == "2"  # newest (seq 2) now on TOP
        assert t.row_count == 2
        assert screen._cursor == 2
    w.close()


def test_yank_copies_selected_envelope(tmp_path, monkeypatch):
    asyncio.run(_yank(tmp_path, monkeypatch))


async def _yank(tmp_path, monkeypatch):
    ref = _seed_rich(tmp_path)
    copied = {}
    app = _HostApp(ref)
    monkeypatch.setattr(
        type(app), "copy_to_clipboard", lambda self, text: copied.setdefault("t", text)
    )
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("y")  # yank the selected (top = seq 5, control.stop) row
        assert "control.stop" in copied["t"] and "5" in copied["t"]


def test_enter_expands_then_escape_returns(tmp_path):
    asyncio.run(_expand(tmp_path))


async def _expand(tmp_path):
    from runstate_tui.detail import ExpandScreen

    ref = _seed_rich(tmp_path)
    app = _HostApp(ref)
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ExpandScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, ExpandScreen)


def test_expand_screen_yank_copies_envelope(tmp_path, monkeypatch):
    asyncio.run(_expand_yank(tmp_path, monkeypatch))


async def _expand_yank(tmp_path, monkeypatch):
    from runstate_tui.detail import ExpandScreen

    ref = _seed_rich(tmp_path)
    copied = {}
    app = _HostApp(ref)
    monkeypatch.setattr(
        type(app), "copy_to_clipboard", lambda self, text: copied.setdefault("t", text)
    )
    async with app.run_test(size=(90, 22)) as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ExpandScreen)
        await pilot.press("y")
        assert "control.stop" in copied["t"] and "5" in copied["t"]
