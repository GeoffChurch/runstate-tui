"""Curated incremental-log-plane basis (docs/superpowers/notes/2026-07-18-fixture-basis.md,
.superpowers/sdd/task-4-brief.md): adversarial + finding-locking scenarios for
`DrillDownScreen`'s live incremental raw-envelope log tail (cursor + `read_log_delta`,
manual-tick driven).

Every assertion here was set from the screen's ACTUAL output (verified empirically, not
guessed) -- a regression test's "expected" IS the current behavior. Where a scenario
exposes a known-deferred gap (per the notes doc), the test still locks the CURRENT
behavior and names the gap in a `# FINDING:` comment; it does not try to fix it. No
production code is changed here.

The manual-tick discipline: every screen below is built with `tick_interval=999.0` so
its own timer never fires unsupervised, and is driven by `advance_tick` (tests/helpers.py)
for exact append-vs-tick ordering. `on_mount` fires ONE automatic first tick -- `_settle`
below waits for it (`pilot.pause()` THEN `workers.wait_for_complete()`) before any manual
`advance_tick`, else the exclusive `_refresh` worker cancellation races (see test_detail.py
/ test_helpers.py, which pin the same idiom). Header/log are queried off the pushed
`screen`, never `app.query_one(...)` -- see test_detail.py's NOTE for why."""

from __future__ import annotations

import asyncio
import json
import threading
import time

from runstate import open_channel
from textual.app import App, ComposeResult
from textual.widgets import RichLog, Static

from runstate_tui import detail as detail_module
from runstate_tui.detail import DrillDownScreen
from runstate_tui.env import Env
from tests.helpers import advance_tick, corrupt_seq, log_text


class _Host(App[None]):
    def compose(self) -> ComposeResult:
        yield Static("host")


def _sqlite_run(tmp_path, run_id, records):
    """Write `records` (3- or 4-tuples: body, topic, name[, request_id]) to a fresh
    sqlite-backed run and return its RunRef `(run_id, root, "sqlite")` -- a real file
    is needed here (not `build_log`'s in-memory channel) so later steps can reopen a
    writer, `corrupt_seq`, or hold a live connection across the test."""
    writer = open_channel(run_id, root=tmp_path, backend="sqlite")
    for record in records:
        body, topic, name, *rest = record
        writer.send(body, topic=topic, name=name, request_id=rest[0] if rest else None)
    writer.close()
    return (run_id, str(tmp_path), "sqlite")


def _append(tmp_path, run_id, body, topic, **kw):
    """Open-send-close on the same sqlite run log -- a closed-writer append between
    ticks (distinct from `sqlite_wal_held_writer`'s live-held-writer path below)."""
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    try:
        return ch.send(body, topic=topic, **kw)
    finally:
        ch.close()


async def _settle(pilot, screen):
    """Wait out the screen's ONE automatic on_mount tick -- required before the first
    manual `advance_tick` (see module docstring)."""
    await pilot.pause()
    await pilot.app.workers.wait_for_complete()


def _head(screen):
    return str(screen.query_one("#detail-head", Static).content)


def _log(screen):
    return log_text(screen.query_one("#detail-log", RichLog))


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
        lines = _log(screen)
        assert len(lines) == 3  # ALL 3 pre-existing records -- not a tail-seek to the end
        assert "lifecycle.started" in lines[0]
        assert "lifecycle.heartbeat" in lines[1]
        assert "lifecycle.heartbeat" in lines[2]
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
        lines = _log(screen)
        assert len(lines) == 2
        assert "lifecycle.heartbeat" in lines[1]  # this tick's delta included it
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
        assert len(_log(screen)) == 1

        hb = {"step": 1, "consumed_seq": 0, "t": 2.0}
        _append(tmp_path, "tba", hb, topic="lifecycle.heartbeat")
        # the two-directional delta boundary: BETWEEN the append and the next tick,
        # the pane and cursor must NOT have moved just because a record landed on disk.
        assert screen._cursor == 1
        assert len(_log(screen)) == 1

        await advance_tick(pilot, screen)  # only NOW does the next tick pick it up
        assert screen._cursor == 2
        assert len(_log(screen)) == 2


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
        assert _log(screen) == []

        for step in range(5):
            _append(
                tmp_path,
                "batch",
                {"step": step, "consumed_seq": 0, "t": float(step)},
                topic="lifecycle.heartbeat",
            )
        await advance_tick(pilot, screen)  # all 5 land in ONE gap before this tick
        lines = _log(screen)
        assert len(lines) == 5  # one batched delta, not 5 separate ticks
        seqs = [int(line.split()[0]) for line in lines]
        assert seqs == [1, 2, 3, 4, 5]  # seq order preserved
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
        await _settle(pilot, screen)  # one drain of all 8, evicted down to the RichLog's cap
        lines = _log(screen)
        assert len(lines) == 5
        seqs = [int(line.split()[0]) for line in lines]
        assert seqs == [4, 5, 6, 7, 8]  # only the last 5 physically survive
        assert screen._cursor == 8  # cursor watermark is NOT capped -- pane and cursor diverge


def test_embedded_newline_splits(tmp_path):
    asyncio.run(_embedded_newline_splits(tmp_path))


async def _embedded_newline_splits(tmp_path):
    # FINDING: format_envelope should defensively single-line its output. A normal
    # dict body's string values are `repr()`'d as part of `str(dict)` (an embedded
    # "\n" prints as the two literal characters backslash-n), but an alien NON-dict
    # body -- e.g. a bare JSON string, planted here via corrupt_seq -- is interpolated
    # RAW (`f"...{env.body}"`, no repr()): a real embedded newline character survives
    # into the formatted line and RichLog.write splits it into multiple physical
    # lines for what is really ONE envelope.
    ref = _sqlite_run(tmp_path, "nl", [({"handle": "h1", "t": 1.0}, "launcher.launched", None)])
    corrupt_seq(tmp_path, "nl", 1, literal=json.dumps("line one\nline two"))
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        lines = _log(screen)
        # ONE envelope on the log, but its embedded newline splits it into >1
        # physical RichLog line -- exactly the defect the FINDING above names.
        assert len(lines) > 1
        assert any("line one" in line for line in lines)
        assert any("line two" in line for line in lines)


# --- fold-visibility of the raw tail ------------------------------------------------


def test_unobserved_topics_only_in_tail(tmp_path):
    asyncio.run(_unobserved_topics_only_in_tail(tmp_path))


async def _unobserved_topics_only_in_tail(tmp_path):
    # nak / launcher.launched / control.unsubscribe records: each is read by SOME
    # fold sub-query (last_activity touches launcher.launched; live_demand touches
    # nak and unsubscribe to discharge a pending subscribe) -- but none is ever
    # RENDERED as a raw envelope in the header (format_detail only echoes
    # undischarged_stops / live_demand *survivors*, and none of these three is one).
    # The raw tail streams every record regardless.
    ref = _sqlite_run(
        tmp_path,
        "unobs",
        [
            (
                {"reason": "unsatisfiable", "message": "NAK-MARKER"},
                "lifecycle.nak",
                None,
                "req-nak",
            ),
            ({"handle": "LAUNCH-MARKER", "t": 1.0}, "launcher.launched", None, "req-launch"),
            ({}, "control.unsubscribe", None, "UNSUB-MARKER"),
        ],
    )
    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=999.0)
        await app.push_screen(screen)
        await _settle(pilot, screen)
        head = _head(screen)
        lines = _log(screen)
        joined = "\n".join(lines)
        assert len(lines) == 3
        for marker in ("NAK-MARKER", "LAUNCH-MARKER", "UNSUB-MARKER"):
            assert marker not in head  # the header excludes them
            assert marker in joined  # the raw tail includes them


def test_byte_torn_in_delta(tmp_path):
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
        assert len(_log(screen)) == 2

        # AFTER the drain: two more launcher.launched records land -- the OLDER
        # (seq 3) is torn, the NEWER (seq 4) stays clean. The header's last_activity
        # reads ONLY `latest(LAUNCHER_LAUNCHED)` -- a single-row, highest-seq query --
        # so it never touches seq 3's body at all: the topic IS fold-observed in
        # general, but this specific (older, superseded) record is fold-invisible.
        _append(tmp_path, "torndelta", {"handle": "old", "t": 3.0}, topic="launcher.launched")
        _append(tmp_path, "torndelta", {"handle": "new", "t": 4.0}, topic="launcher.launched")
        corrupt_seq(tmp_path, "torndelta", 3)  # byte-torn -- default literal="{not json"

        await advance_tick(pilot, screen)
        # read_log_delta's RAW sequential read (after=2) hits the torn seq 3 partway
        # through and drops the WHOLE batched delta -- seq 4 too, not just the torn
        # record (table.py's documented TODO: no partial passthrough yet).
        assert len(_log(screen)) == 2  # unchanged -- nothing new appeared this tick
        assert screen._cursor == 2  # unchanged -- the delta never advanced it
        assert "corrupt" not in _head(screen).lower()  # header stayed clean -- NO crash


# --- header/tail coherence across a status change -----------------------------------


def test_header_status_flips_live_to_terminal(tmp_path):
    asyncio.run(_header_status_flips_live_to_terminal(tmp_path))


async def _header_status_flips_live_to_terminal(tmp_path):
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
        assert _head(screen).startswith("live")

        _append(
            tmp_path,
            "flips",
            {"completed": True, "error": None, "final_step": 1, "t": 3.0},
            topic="lifecycle.stopped",
        )
        await advance_tick(pilot, screen)
        assert _head(screen).startswith("done")  # completed -> the "done" display label
        assert any("lifecycle.stopped" in line for line in _log(screen))  # AND in the tail


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
        assert len(_log(screen1)) == 1

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
        assert len(_log(screen2)) == 2


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
        assert len(_log(screen)) == 1

        send({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat")
        await advance_tick(pilot, screen)  # each tick opens its OWN fresh reader
        assert len(_log(screen)) == 2
        assert screen._cursor == 2


# --- pop mid-tick (teardown race) ---------------------------------------------------


def test_pop_mid_tick_does_not_crash(tmp_path, monkeypatch):
    # Covers detail.py's teardown guard (_TEARDOWN_ERRORS / _marshal's try/except):
    # a pop mid-tick can race _refresh's off-thread _marshal calls onto a torn-down
    # screen. Deterministic instead of a blind sleep: render_single is patched to
    # block on a threading.Event, so the pop is GUARANTEED to land while the tick
    # is still mid-flight, and _marshal's call_from_thread calls are GUARANTEED to
    # run only after the screen has been popped (confirmed empirically: this races
    # self.app -- NoActiveAppError, a RuntimeError subclass already inside
    # _TEARDOWN_ERRORS -- 100% of the time under this synchronization, not
    # occasionally). Mirrors test_app.py's stop-teardown discipline: the
    # threading.excepthook is installed OUTSIDE asyncio.run so it survives loop
    # teardown, in case anything escapes _marshal's own guard.
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
    ref = _sqlite_run(tmp_path, "popmid", [({"handle": "h1", "t": 1.0}, "lifecycle.started", None)])
    entered = threading.Event()
    release = threading.Event()
    orig_render_single = detail_module.render_single

    def slow_render_single(ref, env):
        entered.set()
        release.wait(2.0)  # keep the tick in-flight until after the pop below
        return orig_render_single(ref, env)

    monkeypatch.setattr(detail_module, "render_single", slow_render_single)

    app = _Host()
    async with app.run_test() as pilot:
        screen = DrillDownScreen(ref, Env(clock=lambda: 10.0), tick_interval=0.02)
        await app.push_screen(screen)  # on_mount fires the first tick
        for _ in range(200):
            await pilot.pause(0.01)
            if entered.is_set():
                break
        assert entered.is_set(), "the tick never entered render_single -- can't test the race"
        await app.pop_screen()  # pop WHILE render_single is still blocked mid-tick
        release.set()  # let the in-flight tick proceed -> its _marshal calls now race the pop
        await pilot.pause(0.3)  # give the worker thread time to finish and marshal (or not crash)
