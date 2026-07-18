import asyncio

from runstate import open_channel
from textual.widgets import RichLog, Static

from runstate_tui.detail import DrillDownScreen
from runstate_tui.env import Env


def _sqlite_rich(tmp_path):
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    ch.send({"step": 7, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
    ch.send({}, topic="control.stop", request_id="webui:stop1")
    ch.close()
    return ("r", str(tmp_path), "sqlite")


def test_drilldown_renders_header_and_streams_the_log(tmp_path):
    asyncio.run(_renders(tmp_path))


async def _renders(tmp_path):
    from textual.app import App, ComposeResult
    from textual.widgets import Static as S

    ref = _sqlite_rich(tmp_path)

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield S("host")

    app = Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 150.0), tick_interval=0.05)
        await app.push_screen(screen)
        # NOTE: query via the pushed `screen`, not `app.query_one(...)` — in Textual
        # 8.2.8, App.query_one resolves against `_compose_screen`, the screen captured
        # at the app's *initial* compose, which is never updated by push_screen; it
        # never sees widgets on a later-pushed screen (confirmed empirically: NoMatches
        # 100% of the time, not a race). Screen.query_one has no such indirection.
        for _ in range(60):
            await pilot.pause(0.02)
            head = str(screen.query_one("#detail-head", Static).content)
            log_lines = screen.query_one("#detail-log", RichLog).lines
            if "local://h/1" in head and len(log_lines) >= 3:
                break
        head = str(screen.query_one("#detail-head", Static).content)
        assert "local://h/1" in head  # episode in header
        assert "webui:stop1" in head  # the stop
        assert len(screen.query_one("#detail-log", RichLog).lines) >= 3  # log streamed


def test_drilldown_snapshot(snap_compare, tmp_path):
    # SVG snapshot of the drill-down layout (headless). First run writes the baseline;
    # subsequent runs diff against it. Run `uv run pytest --snapshot-update` to refresh
    # after an intentional layout change.
    #
    # snap_compare's installed signature (pytest-textual-snapshot 1.1.0) takes an App
    # instance directly (or a path) plus an optional `run_before(pilot)` coroutine run
    # before the screenshot — used here to poll until the header/log have populated,
    # the same convergence loop as the behavioral test above, so the baseline is captured
    # in a settled state rather than mid-fold.
    ref = _sqlite_rich(tmp_path)

    from textual.app import App, ComposeResult
    from textual.widgets import Static as S

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield S("host")

        def on_mount(self) -> None:
            self.push_screen(DrillDownScreen(ref, Env(clock=lambda: 150.0), tick_interval=0.05))

    async def _settle(pilot):
        # query via the app's active screen, not `app.query_one(...)` — see the NOTE
        # in the behavioral test above for why the App-level query is the wrong tool
        # once a second screen has been pushed.
        screen = pilot.app.screen
        for _ in range(60):
            await pilot.pause(0.02)
            head = str(screen.query_one("#detail-head", Static).content)
            log_lines = screen.query_one("#detail-log", RichLog).lines
            if "local://h/1" in head and len(log_lines) >= 3:
                break

    assert snap_compare(Host(), run_before=_settle)
