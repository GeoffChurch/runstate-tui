"""Curated incremental-log-plane basis (docs/superpowers/notes/2026-07-18-fixture-basis.md,
.superpowers/sdd/task-4-brief.md): adversarial + finding-locking scenarios for
`DrillDownScreen`'s live incremental raw-envelope log tail (cursor + `read_log_delta`,
manual-tick driven).

REBUILT against the Task-3 tabular redesign: the original (pre-Task-3) version of this
file drove an old `RichLog`-backed screen (`git show 9233b11:tests/scenarios/
test_log_plane.py`) and was deleted as a side-effect of the RichLog->DataTable rewrite
(Task 3). Every scenario below is the SAME behavior, re-asserted against the CURRENT
shape: the `#detail-log` `DataTable` (newest-first; column 0 = seq) and the
`#detail-card` `Static` (a 2-line `Text`; line 1 carries the status label). Two
scenarios from the original file are intentionally NOT duplicated here: `pop-mid-tick`
(already ported forward to `tests/test_detail.py::test_pop_mid_tick_does_not_crash`)
and `unobserved-topics` completeness (rebuilt as `tests/test_detail.py::
test_unknown_family_topics_always_shown_in_render_window`, which discriminates the
restrict-to-vs-subtractive `_predicate` bug that a plain "still shows" test can't).

Every assertion here was set from the screen's ACTUAL output (verified empirically, not
guessed) -- a regression test's "expected" IS the current behavior.

The manual-tick discipline: every screen below is built with `tick_interval=999.0` so
its own timer never fires unsupervised, and is driven by `advance_tick`
(tests/helpers.py) for exact append-vs-tick ordering. `on_mount` fires ONE automatic
first tick -- `_settle` below waits for it (`pilot.pause()` THEN
`workers.wait_for_complete()`) before any manual `advance_tick`, else the exclusive
`_refresh` worker cancellation races (see test_detail.py / test_helpers.py, which pin
the same idiom). Card/log are queried off the pushed `screen`, never
`app.query_one(...)` -- see test_detail.py's NOTE for why."""

from __future__ import annotations

import asyncio
import json

from rich.text import Text
from runstate import create_channel
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Static

from runstate_tui.detail import DrillDownScreen
from runstate_tui.env import Env
from tests.helpers import advance_tick, corrupt_seq


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


def _sqlite_run(tmp_path, run_id, records):
    """Write `records` (3- or 4-tuples: body, topic, name[, request_id]) to a fresh
    sqlite-backed run and return its RunRef `(run_id, root, "sqlite")` -- a real file
    is needed here (not `build_log`'s in-memory channel) so later steps can reopen a
    writer, `corrupt_seq`, or hold a live connection across the test."""
    writer = create_channel(run_id, root=tmp_path, backend="sqlite")
    for record in records:
        body, topic, name, *rest = record
        writer.send(body, topic=topic, name=name, request_id=rest[0] if rest else None)
    writer.close()
    return (run_id, str(tmp_path), "sqlite")


def _append(tmp_path, run_id, body, topic, **kw):
    """Open-send-close on the same sqlite run log -- a closed-writer append between
    ticks (distinct from `sqlite_wal_held_writer`'s live-held-writer path below)."""
    ch = create_channel(run_id, root=tmp_path, backend="sqlite")
    try:
        return ch.send(body, topic=topic, **kw)
    finally:
        ch.close()


async def _settle(pilot, screen):
    """Wait out the screen's ONE automatic on_mount tick -- required before the first
    manual `advance_tick` (see module docstring)."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()


def _card_text(screen) -> str:
    return str(screen.query_one("#detail-card", Static).content)


def _rows(screen) -> list[tuple[str, str, str, str]]:
    """Every row currently in the log `DataTable`, in TABLE order (newest-first,
    per `_render_window`) -- (seq, topic, request, body), topic un-styled."""
    t = screen.query_one("#detail-log", DataTable)
    out = []
    for r in range(t.row_count):
        seq = t.get_cell_at(Coordinate(r, 0))
        topic = t.get_cell_at(Coordinate(r, 1))
        topic = topic.plain if isinstance(topic, Text) else topic
        request = t.get_cell_at(Coordinate(r, 2))
        body = t.get_cell_at(Coordinate(r, 3))
        out.append((seq, topic, request, body))
    return out


def _seqs(screen) -> list[int]:
    return [int(seq) for seq, *_ in _rows(screen)]


# --- cold open / delta boundary --------------------------------------------------


def test_cold_open_full_drain(tmp_path):
    asyncio.run(_cold_open_full_drain(tmp_path))


async def _cold_open_full_drain(tmp_path):
    ref = _sqlite_run(
        tmp_path,
        "cold",
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat", None),
            ({"step": 2, "consumed_seq": 0, "t": 3.0}, "lifecycle.heartbeat", None),
        ],
    )
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)  # the automatic on_mount tick IS the first real tick
        rows = _rows(screen)
        assert len(rows) == 3  # ALL 3 pre-existing records -- not a tail-seek to the end
        assert _seqs(screen) == [3, 2, 1]  # newest-first
        assert rows[2][1] == "lifecycle.started"  # oldest (seq 1) is the LAST row
        assert rows[1][1] == "lifecycle.heartbeat"
        assert rows[0][1] == "lifecycle.heartbeat"  # newest (seq 3) is the FIRST row
        assert screen._cursor == 3  # cursor caught all the way up to last_seq()


def test_append_before_tick_included(tmp_path):
    asyncio.run(_append_before_tick_included(tmp_path))


async def _append_before_tick_included(tmp_path):
    ref = _sqlite_run(tmp_path, "abt", [({"handle": "h1", "t": 1.0}, "lifecycle.started", None)])
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        assert screen._cursor == 1

        hb = {"step": 1, "consumed_seq": 0, "t": 2.0}
        _append(tmp_path, "abt", hb, topic="lifecycle.heartbeat")
        await advance_tick(pilot, screen)  # the append landed BEFORE this tick
        rows = _rows(screen)
        assert len(rows) == 2
        assert rows[0][1] == "lifecycle.heartbeat"  # this tick's delta included it, on TOP
        assert screen._cursor == 2


def test_tick_before_append_deferred(tmp_path):
    asyncio.run(_tick_before_append_deferred(tmp_path))


async def _tick_before_append_deferred(tmp_path):
    ref = _sqlite_run(tmp_path, "tba", [({"handle": "h1", "t": 1.0}, "lifecycle.started", None)])
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        assert screen._cursor == 1

        await advance_tick(pilot, screen)  # a tick with nothing new to drain
        assert screen._cursor == 1
        assert len(_rows(screen)) == 1

        hb = {"step": 1, "consumed_seq": 0, "t": 2.0}
        _append(tmp_path, "tba", hb, topic="lifecycle.heartbeat")
        # the two-directional delta boundary: BETWEEN the append and the next tick,
        # the window and cursor must NOT have moved just because a record landed on disk.
        assert screen._cursor == 1
        assert len(_rows(screen)) == 1

        await advance_tick(pilot, screen)  # only NOW does the next tick pick it up
        assert screen._cursor == 2
        assert len(_rows(screen)) == 2


def test_batch_append_in_one_gap(tmp_path):
    asyncio.run(_batch_append_in_one_gap(tmp_path))


async def _batch_append_in_one_gap(tmp_path):
    ref = _sqlite_run(tmp_path, "batch", [])
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        assert screen._cursor == 0
        assert _rows(screen) == []

        for step in range(5):
            _append(
                tmp_path,
                "batch",
                {"step": step, "consumed_seq": 0, "t": float(step)},
                topic="lifecycle.heartbeat",
            )
        await advance_tick(pilot, screen)  # all 5 land in ONE gap before this tick
        rows = _rows(screen)
        assert len(rows) == 5  # one batched delta, not 5 separate ticks
        assert _seqs(screen) == [5, 4, 3, 2, 1]  # newest-first, seq order preserved
        assert screen._cursor == 5


# --- ring eviction / embedded content ---------------------------------------------


def test_ring_eviction(tmp_path):
    asyncio.run(_ring_eviction(tmp_path))


async def _ring_eviction(tmp_path):
    records = [
        ({"step": i, "consumed_seq": 0, "t": float(i)}, "lifecycle.heartbeat", None)
        for i in range(8)
    ]
    ref = _sqlite_run(tmp_path, "ring", records)
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0, log_cap=5)
        await app.push_screen(screen)
        await _settle(pilot, screen)  # one drain of all 8, evicted down to the window's cap
        rows = _rows(screen)
        assert len(rows) == 5
        assert screen.query_one("#detail-log", DataTable).row_count == 5  # bounded, deque(maxlen)
        assert _seqs(screen) == [8, 7, 6, 5, 4]  # only the last 5 physically survive
        assert screen._cursor == 8  # cursor watermark is NOT capped -- window and cursor diverge


def test_one_envelope_is_one_row(tmp_path):
    # format_envelope-era invariant, ported to the tabular design (replaces the old
    # RichLog `embedded_newline_splits` scenario): a normal dict body's string values
    # are already `repr()`'d as part of `str(dict)`, but an alien NON-dict body -- e.g.
    # a bare JSON string, planted here via corrupt_seq -- is interpolated raw by
    # `_body_text`. Under the old RichLog, a real embedded newline would split ONE
    # envelope's line into two physical lines. The DataTable has no such failure mode:
    # `_render_window` calls `add_row` exactly once per envelope in the window, so one
    # envelope is structurally always exactly one table ROW, regardless of what a
    # multi-line cell renders as internally. This test locks that invariant against the
    # REAL widget (not just the construction), and pins the raw (unescaped) cell value.
    asyncio.run(_one_envelope_is_one_row(tmp_path))


async def _one_envelope_is_one_row(tmp_path):
    ref = _sqlite_run(tmp_path, "nl", [({"handle": "h1", "t": 1.0}, "launcher.launched", None)])
    corrupt_seq(tmp_path, "nl", 1, literal=json.dumps("line one\nline two"))
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        t = screen.query_one("#detail-log", DataTable)
        # ONE envelope on the log is exactly ONE DataTable row -- never split.
        assert t.row_count == 1
        rows = _rows(screen)
        assert len(rows) == 1
        assert rows[0][1] == "launcher.launched"
        assert rows[0][3] == "line one\nline two"  # the raw body, embedded newline intact


# --- delta-read failure / header-tail coherence ------------------------------------


def test_byte_torn_in_delta(tmp_path):
    # This is the scenario that DISCRIMINATES the incremental delta-cursor worker
    # (Task 4) from Task 3's interim synchronous fill: the synchronous fill re-reads
    # `after=0` (the WHOLE log) every tick and unconditionally `window.clear()`s, so
    # once a tear appears ANYWHERE in the log -- even behind already-drained,
    # already-displayed good records -- the very next tick wipes the window down to
    # EMPTY. The incremental worker only re-reads `after=self._cursor` (the NEW tail)
    # and only touches the window `if delta:` -- so a torn delta (`read_log_delta`
    # returns `[]`) leaves the previously-drained good records exactly where they are.
    asyncio.run(_byte_torn_in_delta(tmp_path))


async def _byte_torn_in_delta(tmp_path):
    # A clean drain first (an ordinary started+heartbeat pair) establishes cursor > 0.
    ref = _sqlite_run(
        tmp_path,
        "torndelta",
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat", None),
        ],
    )
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)  # the clean drain
        assert screen._cursor == 2
        assert len(_rows(screen)) == 2

        # AFTER the drain: two more launcher.launched records land -- the OLDER
        # (seq 3) is torn, the NEWER (seq 4) stays clean.
        _append(tmp_path, "torndelta", {"handle": "old", "t": 3.0}, topic="launcher.launched")
        _append(tmp_path, "torndelta", {"handle": "new", "t": 4.0}, topic="launcher.launched")
        corrupt_seq(tmp_path, "torndelta", 3)  # byte-torn -- default literal="{not json"

        await advance_tick(pilot, screen)
        # read_log_delta's RAW sequential read (after=2) hits the torn seq 3 partway
        # through and returns [] for the WHOLE delta -- seq 4 too, not just the torn
        # record (table.py's documented TODO: no partial passthrough yet).
        assert len(_rows(screen)) == 2  # unchanged -- nothing new appeared this tick
        assert screen._cursor == 2  # unchanged -- the empty delta never advanced it
        assert "corrupt" not in _card_text(screen).lower()  # header stayed clean -- NO crash


def test_card_status_flips_live_to_terminal(tmp_path):
    asyncio.run(_card_status_flips_live_to_terminal(tmp_path))


async def _card_status_flips_live_to_terminal(tmp_path):
    ref = _sqlite_run(
        tmp_path,
        "flips",
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat", None),
        ],
    )
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        line1 = _card_text(screen).split("\n")[0]
        assert line1.startswith("● live")

        _append(
            tmp_path,
            "flips",
            {"completed": True, "error": None, "final_step": 1, "t": 3.0},
            topic="lifecycle.stopped",
        )
        await advance_tick(pilot, screen)
        line1 = _card_text(screen).split("\n")[0]
        assert line1.startswith("● done")  # completed -> the "done" display label
        assert _rows(screen)[0][1] == "lifecycle.stopped"  # AND on top of the tail


def test_re_entry_resets_cursor(tmp_path):
    asyncio.run(_re_entry_resets_cursor(tmp_path))


async def _re_entry_resets_cursor(tmp_path):
    ref = _sqlite_run(
        tmp_path, "reentry", [({"handle": "h1", "t": 1.0}, "lifecycle.started", None)]
    )
    app = _Host()
    async with app.run_test() as pilot:
        screen1 = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen1)
        await _settle(pilot, screen1)
        assert screen1._cursor == 1
        assert len(_rows(screen1)) == 1

        await app.pop_screen()
        await pilot.pause()

        # a record lands while NO screen is watching this ref
        hb = {"step": 1, "consumed_seq": 0, "t": 2.0}
        _append(tmp_path, "reentry", hb, topic="lifecycle.heartbeat")

        screen2 = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen2)
        await _settle(pilot, screen2)
        # a FRESH screen for the SAME ref starts cursor=0 and its mount tick does a
        # full re-drain from seq 0 -- both the original record and the one appended
        # while unwatched -- not a resume from screen1's old cursor.
        assert screen2._cursor == 2
        assert len(_rows(screen2)) == 2


def test_sqlite_wal_held_writer(held_writer_sqlite_run):
    asyncio.run(_sqlite_wal_held_writer(held_writer_sqlite_run))


async def _sqlite_wal_held_writer(held_writer_sqlite_run):
    ref, send = held_writer_sqlite_run
    send({"handle": "h1", "t": 1.0}, "lifecycle.started")
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)  # a fresh reader connection sees the held writer's commit
        assert len(_rows(screen)) == 1

        send({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat")
        await advance_tick(pilot, screen)  # each tick opens its OWN fresh reader
        assert len(_rows(screen)) == 2
        assert screen._cursor == 2
