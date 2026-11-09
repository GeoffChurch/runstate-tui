from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from runstate import Heartbeat, Nak, NakReason, SqliteLogs, Topic

from runstate_tui.control import StopOutcome, StopResult, dispatch_stop, stop_run
from runstate_tui.types import Severity
from tests.helpers import corrupt_seq, log_path, move_log
from tests.records import Script, worker, write


def _run(tmp_path: Path, run_id: str):
    return SqliteLogs(str(tmp_path)).create(run_id)


def _claim(ch, t=1.0):
    """A held claim at seq 1 (0.4.0's await_consumed reads a claim's watermarks)."""
    s = Script()
    s.started(t, "h")
    write(ch, s.records)
    return s


def test_stop_run_accepted_when_a_heartbeat_watermark_covers_the_stop(tmp_path):
    ch = _run(tmp_path, "accepted")
    try:
        _claim(ch)

        def seed(_interval):
            # await_consumed's poll gap: the fresh stop stop_run just sent is now
            # on the log — assert it (right topic + id), then post the claim's
            # watermark (its drain's consumed_seq) AFTER it.
            stop = ch.latest(Topic.CONTROL_STOP)
            assert stop is not None and stop.request_id == "webui:a"
            ch.send(
                asdict(Heartbeat(claim_seq=1, consumed_seq=stop.seq, t=1.0)),
                topic=Topic.LIFECYCLE_HEARTBEAT,
            )

        outcome = stop_run(ch, request_id="webui:a", timeout=5.0, sleep=seed)
        assert outcome.result is StopResult.ACCEPTED
        assert outcome.request_id == "webui:a"
    finally:
        ch.close()


def test_stop_run_accepted_by_a_real_worker_that_honors_it(tmp_path):
    # runstate's own Worker: its next commit drains the stop (the decision holds), and
    # its exit names the stop in `honored` -- await_consumed answers None (accepted)
    ch = _run(tmp_path, "real")
    try:
        w = worker(ch)

        def seed(_interval):
            if w.commit():  # True: the stop held at this node
                w.stopped(completed=False, error=None)

        outcome = stop_run(ch, request_id="webui:real", timeout=5.0, sleep=seed)
        assert outcome.result is StopResult.ACCEPTED
        exit_ = ch.latest(Topic.LIFECYCLE_STOPPED)
        assert exit_ is not None and exit_.body["honored"] == ["webui:real"]
    finally:
        ch.close()


def test_stop_run_refused_surfaces_the_nak_reason(tmp_path):
    ch = _run(tmp_path, "refused")
    try:
        _claim(ch)

        def seed(_interval):
            ch.send(
                asdict(Nak(reason=NakReason.UNSATISFIABLE, message="nope")),
                topic=Topic.LIFECYCLE_NAK,
                request_id="webui:r",
            )

        outcome = stop_run(ch, request_id="webui:r", timeout=5.0, sleep=seed)
        assert outcome.result is StopResult.REFUSED
        assert outcome.detail == "unsatisfiable"
    finally:
        ch.close()


def test_stop_run_died_when_the_claim_exits_without_taking_the_stop(tmp_path):
    # 0.4.0: the claim ended by its own exit above the request, with no watermark of it
    # reaching the request -> the terminal RunResult (it saw the stop and ended anyway)
    ch = _run(tmp_path, "died")
    try:
        s = _claim(ch)

        def seed(_interval):
            before = len(s.records)
            s.stopped(2.0, completed=False, error="killed")  # torn: an errored exit
            write(ch, s.records[before:])

        outcome = stop_run(ch, request_id="webui:d", timeout=5.0, sleep=seed)
        assert outcome.result is StopResult.DIED
        assert outcome.detail == "errored"
    finally:
        ch.close()


def test_stop_run_unsafe_on_timeout(tmp_path):
    ch = _run(tmp_path, "unsafe")
    try:
        ch.send({}, topic=Topic.CONTROL_STOP, request_id="webui:u")
        ticks = iter([100.0, 100.0, 101.0, 102.0, 103.0, 104.0])
        outcome = stop_run(
            ch,
            request_id="webui:u",
            timeout=1.0,
            now=lambda: next(ticks),
            sleep=lambda _s: None,
        )
        assert outcome.result is StopResult.UNSAFE
        assert outcome.severity is Severity.HIGH
    finally:
        ch.close()


def test_stop_run_moot_when_already_ended_no_control_stop_appended(tmp_path):
    ch = _run(tmp_path, "already-ended")
    try:
        s = _claim(ch)
        s.stopped(2.0, completed=True)
        write(ch, s.records[1:])
        outcome = stop_run(ch, request_id="webui:moot", timeout=1.0, sleep=lambda _s: None)
        assert outcome.result is StopResult.MOOT
        assert outcome.detail == "run already ended (done)"
        assert ch.read(topics=[Topic.CONTROL_STOP]) == []  # never sent
    finally:
        ch.close()


def test_already_ended_malformed_terminal_degrades_to_none_not_moot(tmp_path):
    # An unclaimed run whose launch's `supervisor.terminated` is unreadable makes
    # peek_terminal raise MalformedRecordError (that record is the run's present
    # verdict; runstate refuses to guess) -- _already_ended can't confirm the run ended
    # from that, so it degrades to None and stop_run proceeds to send as normal (never
    # MOOT). No worker answers -> UNSAFE.
    ch = _run(tmp_path, "malformed-terminal")
    try:
        s = Script()
        s.launched("L1", 1.0)
        s.raw({"reason": "exited"}, Topic.SUPERVISOR_TERMINATED, None, "L1")
        write(ch, s.records)
        ticks = iter([100.0, 100.0, 101.0, 102.0, 103.0, 104.0])
        outcome = stop_run(
            ch,
            request_id="webui:malformed",
            timeout=1.0,
            now=lambda: next(ticks),
            sleep=lambda _s: None,
        )
        assert outcome.result is StopResult.UNSAFE
        assert outcome.result is not StopResult.MOOT
    finally:
        ch.close()


def test_stop_run_byte_torn_terminal_propagates(tmp_path):
    # A byte-torn body (undecodable JSON) on the terminal record must NOT be
    # swallowed by _already_ended's MalformedRecordError catch -- it's a
    # different, uncatchable class (json.JSONDecodeError), consistent with the
    # corrupt taxonomy elsewhere (fold.py's locate_torn_seq / guarded()).
    writer = _run(tmp_path, "byte-torn-terminal")
    s = _claim(writer)
    seq = s.stopped(2.0, completed=True)
    write(writer, s.records[1:])
    writer.close()
    corrupt_seq(tmp_path, "byte-torn-terminal", seq)
    ch = _run(tmp_path, "byte-torn-terminal")
    try:
        before = len(ch.read(topics=[Topic.CONTROL_STOP]))
        with pytest.raises(json.JSONDecodeError):
            stop_run(ch, request_id="webui:torn", timeout=1.0, sleep=lambda _s: None)
        # Pin that the crash fires BEFORE any control.stop is written -- not just
        # that SOME JSONDecodeError eventually surfaces. Without this, a future
        # widening of _already_ended's `except MalformedRecordError` to `except
        # Exception` would swallow the byte-torn, proceed to channel.send (durably
        # writing a pointless control.stop into the corrupt log), and only THEN
        # crash in await_consumed -- still satisfying pytest.raises(...) by
        # accident, undetected.
        after = len(ch.read(topics=[Topic.CONTROL_STOP]))
        assert after == before == 0  # no control.stop was ever appended
    finally:
        ch.close()


def test_dispatch_stop_missing_run_is_undelivered_and_fabricates_no_db(tmp_path):
    ref = ("ghost", str(tmp_path), "sqlite")
    outcome = dispatch_stop(ref, request_id="webui:m", timeout=1.0)
    assert outcome.result is StopResult.UNDELIVERED
    assert outcome.severity is Severity.HIGH
    # the phantom-db guard: no <run_id>.db was fabricated to write a stop into
    assert not log_path(tmp_path, "ghost").exists()


def test_dispatch_stop_to_an_older_format_log_is_undelivered_naming_the_migration(tmp_path):
    seed = _run(tmp_path, "old")
    _claim(seed)
    seed.close()
    move_log(tmp_path, "old", to=log_path(tmp_path, "old", version="0.3.0"))
    outcome = dispatch_stop(("old", str(tmp_path), "sqlite"), request_id="webui:o", timeout=1.0)
    assert outcome.result is StopResult.UNDELIVERED and outcome.severity is Severity.HIGH
    assert "format 0.3.0" in outcome.label and "runstate migrate" in outcome.label


def test_dispatch_stop_opens_sends_and_reports_unsafe_when_unserved(tmp_path):
    # a real, existing run with no worker: dispatch attaches it, sends the stop, and
    # the bounded handshake times out -> UNSAFE (proves attach+send+close wiring
    # end-to-end). Seed one record so the run HAS records -> attach finds it
    # (a run exists iff it has records; an empty run would attach as RunNotFound).
    seed = _run(tmp_path, "live")
    _claim(seed)
    seed.close()
    ref = ("live", str(tmp_path), "sqlite")
    ticks = iter([100.0, 100.0, 101.0, 102.0, 103.0, 104.0])
    outcome = dispatch_stop(
        ref, request_id="webui:l", timeout=1.0, now=lambda: next(ticks), sleep=lambda _s: None
    )
    assert outcome.result is StopResult.UNSAFE


def test_dispatch_stop_unopenable_run_is_undelivered(tmp_path):
    # a real file that IS present but is NOT a valid sqlite db (non-sqlite bytes):
    # attach's connect succeeds, then the records probe raises
    # sqlite3.DatabaseError ("file is not a database") -- NOT a RunNotFound miss --
    # which dispatch_stop's `except _OPEN_ERRORS` maps to UNDELIVERED (unopenable).
    log_path(tmp_path, "garbage").parent.mkdir()
    log_path(tmp_path, "garbage").write_bytes(b"not a sqlite database\x00\x01")
    outcome = dispatch_stop(("garbage", str(tmp_path), "sqlite"), request_id="webui:g", timeout=1.0)
    assert outcome.result is StopResult.UNDELIVERED
    assert outcome.detail == "run could not be opened"


def test_dispatch_stop_not_a_dir_root_is_undelivered_missing(tmp_path):
    # root points at a regular file: no format's address under it exists (it is not a
    # directory), so attach raises RunNotFound (same class as an absent file)
    # -> dispatch_stop's `except RunNotFound` -> UNDELIVERED (missing).
    not_a_dir = tmp_path / "not_a_dir"
    not_a_dir.write_text("x")
    outcome = dispatch_stop(("run", str(not_a_dir), "sqlite"), request_id="webui:n", timeout=1.0)
    assert outcome.result is StopResult.UNDELIVERED
    assert outcome.detail == "no such run (missing)"
    assert not log_path(not_a_dir, "run").exists()  # never fabricated


def test_stop_outcome_label_and_severity():
    assert StopOutcome(StopResult.ACCEPTED, "webui:x").severity is Severity.OK
    assert StopOutcome(StopResult.ACCEPTED, "webui:x").label == "✓ stop accepted"
    unsafe = StopOutcome(StopResult.UNSAFE, "webui:x", "no live worker?")
    assert unsafe.severity is Severity.HIGH
    assert unsafe.label == "⚠ unsafe stop: no live worker?"
