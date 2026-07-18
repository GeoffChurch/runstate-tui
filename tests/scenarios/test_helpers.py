"""Self-tests for the reusable fixture/helper building blocks (tests/conftest.py
+ tests/helpers.py). Each test asserts one helper does exactly what it claims —
this is the payoff-critical layer a later curated scenario suite composes from,
so the interfaces get pinned here before anything else builds on them."""

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest


def test_build_log_accepts_request_id(build_log):
    from runstate.observables import undischarged_stops

    ch = build_log([({}, "control.stop", None, "webui:s1")])
    stops = undischarged_stops(ch)
    assert len(stops) == 1 and stops[0].request_id == "webui:s1"


def test_build_log_still_accepts_three_tuples(build_log):
    from runstate.observables import progress

    ch = build_log([({"step": 1, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    assert progress(ch) == 1


def test_counting_env_counts_calls(counting_env):
    env, calls = counting_env(base=100.0)
    assert env.clock() == 101.0 and env.clock() == 102.0 and calls["n"] == 2


def test_counting_env_carries_the_threshold(counting_env):
    env, _calls = counting_env(base=0.0, threshold=42.0)
    assert env.stuck_threshold == 42.0


def test_fake_clock_yields_then_noop_sleep():
    from tests.helpers import fake_clock

    now, sleep = fake_clock(10.0, 11.0, 12.0)
    assert now() == 10.0
    sleep(999)  # no-op, returns immediately
    assert now() == 11.0
    assert now() == 12.0


def test_answer_on_sleep_fires_on_the_kth_call(build_log, answer_on_sleep):
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    fired = []
    sleep = answer_on_sleep(ch, {2: lambda channel: fired.append(channel)})
    sleep(0.05)  # 1st call: no seed registered
    assert fired == []
    sleep(0.05)  # 2nd call: seed fires
    assert fired == [ch]
    sleep(0.05)  # 3rd call: nothing registered, no error
    assert fired == [ch]


def test_log_text_extracts_strip_text():
    # a real RichLog with NON-empty written lines -- blank Strips render "" regardless
    # of what log_text does, so a prior version of this test (Strip.blank(0)/Strip([]))
    # would still pass even if log_text returned ["" for _ in richlog.lines]. This
    # version fails if log_text stops pulling .text off each Strip.
    asyncio.run(_log_text_extracts_strip_text())


async def _log_text_extracts_strip_text():
    from textual.app import App, ComposeResult
    from textual.widgets import RichLog

    from tests.helpers import log_text

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield RichLog(id="log")

    app = Host()
    async with app.run_test() as pilot:
        rl = app.query_one("#log", RichLog)
        rl.write("hello world")
        rl.write("second line")
        await pilot.pause()
        assert log_text(rl) == ["hello world", "second line"]


def test_corrupt_seq_plants_torn_and_alien(tmp_path, build_log):
    from runstate import open_channel

    from tests.helpers import corrupt_seq

    w = open_channel("r", root=tmp_path, backend="sqlite")
    w.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    w.close()
    corrupt_seq(tmp_path, "r", 1, literal="42")
    r = open_channel("r", root=tmp_path, backend="sqlite")
    got = r.read(after=0)
    assert got[0].body == 42  # alien body decoded as a bare int
    r.close()


def test_corrupt_seq_plants_byte_torn_body(tmp_path):
    from runstate import open_channel

    from tests.helpers import corrupt_seq

    w = open_channel("torn", root=tmp_path, backend="sqlite")
    w.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    w.close()
    corrupt_seq(tmp_path, "torn", 1)  # default literal="{not json"
    r = open_channel("torn", root=tmp_path, backend="sqlite")
    with pytest.raises(json.JSONDecodeError):
        r.latest("lifecycle.started")
    r.close()


def test_foreign_db_is_valid_sqlite_with_alien_schema(foreign_db):
    ref = foreign_db  # (run_id, root, backend)
    assert (Path(ref[1]) / f"{ref[0]}.db").exists()
    conn = sqlite3.connect(str(Path(ref[1]) / f"{ref[0]}.db"))
    try:
        rows = conn.execute("SELECT id, note FROM unrelated").fetchall()
        assert rows  # a real row is present -> a VALID db, just alien schema
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM log")  # no runstate schema here
    finally:
        conn.close()


def test_held_writer_sqlite_run_stays_open_and_appends_live(held_writer_sqlite_run):
    from runstate import open_channel

    ref, send = held_writer_sqlite_run
    send({"handle": "h", "t": 1.0}, "lifecycle.started")
    run_id, root, backend = ref
    reader = open_channel(run_id, root=root, backend=backend)
    try:
        assert reader.latest("lifecycle.started") is not None
    finally:
        reader.close()


def test_advance_tick_runs_a_manual_tick_and_log_text_reads_it(tmp_path):
    asyncio.run(_advance_tick_and_log_text(tmp_path))


async def _advance_tick_and_log_text(tmp_path):
    from runstate import open_channel
    from textual.app import App, ComposeResult
    from textual.widgets import RichLog
    from textual.widgets import Static as S

    from runstate_tui.detail import DrillDownScreen
    from runstate_tui.env import Env
    from tests.helpers import advance_tick, log_text

    ch = open_channel("adv", root=tmp_path, backend="sqlite")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    ref = ("adv", str(tmp_path), "sqlite")

    class Host(App[None]):
        def compose(self) -> ComposeResult:
            yield S("host")

    app = Host()
    async with app.run_test() as pilot:
        # tick_interval=999 so only manual ticks fire from here on
        screen = DrillDownScreen(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
        await app.push_screen(screen)
        # settle the automatic on_mount tick first (same idiom as test_app.py's
        # tick_interval=999 tests) so advance_tick's manual _tick() below is the
        # only in-flight `_refresh` worker -- otherwise the exclusive worker
        # cancels the still-running mount tick and wait_for_complete() raises.
        await pilot.pause()
        await pilot.app.workers.wait_for_complete()
        await advance_tick(pilot, screen)
        lines = log_text(screen.query_one("#detail-log", RichLog))
        assert any("local://h/1" in line for line in lines)
