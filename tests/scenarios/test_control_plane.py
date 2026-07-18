"""Curated control-plane basis (docs/superpowers/notes/2026-07-18-fixture-basis.md,
.superpowers/sdd/task-3-brief.md): adversarial + finding-locking scenarios for the
control plane's read side (`undischarged_stops`, `live_demand`) and its
handshake side (`stop_run` / `dispatch_stop`, which drive `await_consumed`).

Every assertion here was set from the REAL observable's/handshake's ACTUAL
output (verified empirically, not guessed) -- a regression test's "expected" IS
the current behavior. Where a scenario exposes a known-deferred gap (per the
notes doc / brief), the test still locks the CURRENT behavior and names the
gap in a `# FINDING:` comment; it does not try to fix it. No production code
is changed here."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from runstate.observables import live_demand, live_episode, undischarged_stops
from runstate.vocabulary.payloads import Heartbeat, Nak, Stopped, Topic
from runstate.watcher import await_consumed

from runstate_tui.control import StopResult, dispatch_stop, stop_run
from tests.helpers import fake_clock

# --- undischarged_stops ---------------------------------------------------------


def test_naked_stop_still_undischarged(build_log):
    # A malformed stop trigger ({"until": ...} -- control.stop's real grammar is
    # `from`-only, malformed_stop_trigger) gets refused by a real worker's nak.
    # over-reports: no nak discharges a stop -- undischarged_stops has no notion
    # of "refused"; only the NEXT lifecycle.stopped clears anything, so the
    # naked stop stays listed as if still pending.
    ch = build_log(
        [
            ({"until": {"step": 5}}, "control.stop", None, "webui:s1"),
            (
                {"reason": "malformed", "message": "control.stop takes only `from`"},
                "lifecycle.nak",
                None,
                "webui:s1",
            ),
        ]
    )
    stops = undischarged_stops(ch)
    assert len(stops) == 1
    assert stops[0].request_id == "webui:s1"
    assert stops[0].body == {"until": {"step": 5}}


def test_multiple_stops_one_discharge_clears_all(build_log):
    # Two stops -- the second reusing the first's request_id -- both list as
    # pending (no id-based dedup); ONE lifecycle.stopped discharges every
    # pending stop at once, id reuse and all.
    pre = build_log(
        [
            ({}, "control.stop", None, "webui:s1"),
            ({"from": {"step": 5}}, "control.stop", None, "webui:s1"),  # id reuse
        ]
    )
    stops = undischarged_stops(pre)
    assert [s.request_id for s in stops] == ["webui:s1", "webui:s1"]

    post = build_log(
        [
            ({}, "control.stop", None, "webui:s1"),
            ({"from": {"step": 5}}, "control.stop", None, "webui:s1"),
            (
                {"completed": True, "error": None, "final_step": 3, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
        ]
    )
    assert undischarged_stops(post) == []


def test_conditional_vs_immediate_pending_not_due(build_log):
    # An unconditional stop ({}) and a conditional one ({"from": {"step": 100}},
    # nowhere near due) are BOTH listed, indistinguishably.
    # pending != due -- undischarged_stops has no "due" axis; due-evaluation
    # needs the worker's own (step, time, count) coordinates.
    ch = build_log(
        [
            ({}, "control.stop", None, "webui:imm"),
            ({"from": {"step": 100}}, "control.stop", None, "webui:cond"),
        ]
    )
    stops = undischarged_stops(ch)
    assert [(s.request_id, s.body) for s in stops] == [
        ("webui:imm", {}),
        ("webui:cond", {"from": {"step": 100}}),
    ]


def test_undischarged_stop_spans_episode(build_log):
    # FINDING: ghost-stop-badge -- undischarged_stops is NOT episode-scoped. A
    # stop filed against the FIRST episode, never discharged by a stopped, still
    # shows up as pending on a fresh, unrelated, live SECOND episode.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({}, "control.stop", None, "webui:ghost"),
            ({"handle": "h2", "t": 5.0}, "lifecycle.started", None),
        ]
    )
    assert live_episode(ch) == "h2"  # a fresh, live episode with no stop of its own
    stops = undischarged_stops(ch)
    assert len(stops) == 1
    assert stops[0].request_id == "webui:ghost"  # the ghost from episode h1


# --- live_demand -----------------------------------------------------------------


def test_unsubscribed_vs_nullid_nak(build_log):
    # sub-a is cleanly unsubscribed; sub-b stays pending; a null-id nak (a
    # broadcast refusal naming no particular request) answers NOTHING -- the
    # positional answer fold requires an exact id match, so it clears no entry.
    ch = build_log(
        [
            ({}, "control.subscribe", None, "sub-a"),
            ({}, "control.unsubscribe", None, "sub-a"),
            ({}, "control.subscribe", None, "sub-b"),
            ({"reason": "malformed", "message": "x"}, "lifecycle.nak", None, None),
        ]
    )
    demand = live_demand(ch)
    assert [d.request_id for d in demand] == ["sub-b"]


def test_resubscribe_after_answer_is_live(build_log):
    # subscribe(sub-a) -> nak(sub-a) -> subscribe(sub-a) again: an answer never
    # reaches a LATER same-id subscribe (positional, by seq) -- resubscribing
    # after being answered is live again, not permanently silenced by the id.
    ch = build_log(
        [
            ({}, "control.subscribe", None, "sub-a"),
            ({"reason": "malformed", "message": "x"}, "lifecycle.nak", None, "sub-a"),
            ({}, "control.subscribe", None, "sub-a"),
        ]
    )
    demand = live_demand(ch)
    assert len(demand) == 1
    assert demand[0].request_id == "sub-a"
    assert demand[0].seq == 3  # the resubscribe (seq 3), not the answered original (seq 1)


def test_time_lease_voided_by_episode_boundary(build_log):
    # A time-referencing subscribe ("from": {"time_seconds": ...}) is a LEASE,
    # scoped to a single episode: a `started` strictly between it and the
    # latest `started` voids it -- two more episodes after it and live_demand
    # comes back empty.
    voided = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({"from": {"time_seconds": 5}}, "control.subscribe", None, "sub-time"),
            ({"handle": "h2", "t": 5.0}, "lifecycle.started", None),
            ({"handle": "h3", "t": 10.0}, "lifecycle.started", None),
        ]
    )
    assert live_demand(voided) == []

    # Contrast, same episode-boundary shape: a non-time-referencing subscribe is
    # never subject to the lease rule at all -- it stays live across it.
    stays_live = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({"every": {"step": 1}}, "control.subscribe", None, "sub-nontime"),
            ({"handle": "h2", "t": 5.0}, "lifecycle.started", None),
            ({"handle": "h3", "t": 10.0}, "lifecycle.started", None),
        ]
    )
    demand = live_demand(stays_live)
    assert len(demand) == 1
    assert demand[0].request_id == "sub-nontime"


# --- await_consumed / stop_run / dispatch_stop ------------------------------------


def test_accepted_watermark_climb(build_log, answer_on_sleep):
    # Two-phase seed on await_consumed's sleep seam: poll 1 posts a heartbeat
    # NOT YET covering the stop's seq (consumed_seq = seq-1) -- must NOT be
    # mistaken for acceptance; poll 2 posts one that covers it EXACTLY
    # (consumed_seq = seq) -- locks the `>=` watermark boundary.
    ch = build_log([])

    def not_yet(channel):
        stop = channel.latest(Topic.CONTROL_STOP)
        channel.send(
            asdict(Heartbeat(step=1, consumed_seq=stop.seq - 1, t=1.0)),
            topic=Topic.LIFECYCLE_HEARTBEAT,
        )

    def covering(channel):
        stop = channel.latest(Topic.CONTROL_STOP)
        channel.send(
            asdict(Heartbeat(step=1, consumed_seq=stop.seq, t=2.0)),
            topic=Topic.LIFECYCLE_HEARTBEAT,
        )

    sleep = answer_on_sleep(ch, {1: not_yet, 2: covering})
    outcome = stop_run(ch, request_id="webui:climb", timeout=5.0, sleep=sleep)
    assert outcome.result is StopResult.ACCEPTED

    # Not-fooled-by-early-poll control: the SAME not-yet-covering seed alone
    # (never followed by a covering one), under a bounded timeout, must NOT
    # accept -- proves poll 1 alone genuinely doesn't trip acceptance.
    ch2 = build_log([])
    sleep_never_covers = answer_on_sleep(ch2, {1: not_yet})
    outcome2 = stop_run(ch2, request_id="webui:neverfull", timeout=0.2, sleep=sleep_never_covers)
    assert outcome2.result is StopResult.UNSAFE


def test_refused_nak(build_log):
    ch = build_log([])

    def seed(_interval):
        ch.send(
            asdict(Nak(reason="unsatisfiable", message="nope")),
            topic=Topic.LIFECYCLE_NAK,
            request_id="webui:r",
        )

    outcome = stop_run(ch, request_id="webui:r", timeout=5.0, sleep=seed)
    assert outcome.result is StopResult.REFUSED
    assert outcome.detail == "unsatisfiable"


def test_died_terminal_follows(build_log):
    ch = build_log([({"handle": "h", "t": 1.0}, "lifecycle.started", None)])

    def seed(_interval):
        ch.send(
            asdict(Stopped(completed=False, error="killed", final_step=3, t=2.0)),
            topic=Topic.LIFECYCLE_STOPPED,
        )

    outcome = stop_run(ch, request_id="webui:d", timeout=5.0, sleep=seed)
    assert outcome.result is StopResult.DIED
    assert outcome.detail == "errored"


def test_unsafe_timeout(build_log):
    ch = build_log([])
    now, sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    outcome = stop_run(ch, request_id="webui:u", timeout=1.0, now=now, sleep=sleep)
    assert outcome.result is StopResult.UNSAFE


def test_sent_after_terminal_is_unsafe_not_died(build_log):
    # FINDING: UNSAFE conflates "no worker yet" with "run already over" --
    # started -> stopped (seq 2) -> a FRESH stop (seq 3), sent AFTER the
    # terminal. await_consumed only returns the terminal RunResult (DIED) for a
    # request a terminal record FOLLOWS by seq; here the terminal PRECEDES the
    # request, so it just waits for a next episode that never comes and times
    # out -- UNSAFE, indistinguishable from "nobody is running this at all".
    ch = build_log(
        [
            ({"handle": "h", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": True, "error": None, "final_step": 3, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
        ]
    )
    now, sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    outcome = stop_run(ch, request_id="webui:after", timeout=1.0, now=now, sleep=sleep)
    assert outcome.result is StopResult.UNSAFE
    assert outcome.result is not StopResult.DIED


def test_dispatch_stop_missing_no_phantom(tmp_path):
    ref = ("ghost", str(tmp_path), "sqlite")
    outcome = dispatch_stop(ref, request_id="webui:m", timeout=1.0)
    assert outcome.result is StopResult.UNDELIVERED
    # the phantom-db guard: no <run_id>.db was fabricated to write a stop into
    assert not (tmp_path / "ghost.db").exists()


def test_reqid_absent_cannot_be_refused(build_log):
    # FINDING: a request_id=None handshake can't see a refusal -- this locks that
    # stop_run/dispatch_stop always pass a real webui: id. await_consumed's
    # `_answer()` returns None UNCONDITIONALLY when request_id is None (it never
    # even reads the nak topic), so a nak on the log -- even one that directly
    # answers this exact (id-less) stop -- is invisible to the positional answer
    # fold: the call can only ever time out, never resolve REFUSED.
    ch = build_log([])
    seq = ch.send({}, topic=Topic.CONTROL_STOP, request_id=None)
    ch.send(
        asdict(Nak(reason="unsatisfiable", message="nope")),
        topic=Topic.LIFECYCLE_NAK,
        request_id=None,
    )
    now, sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    with pytest.raises(TimeoutError):
        await_consumed(ch, seq, request_id=None, timeout=1.0, now=now, sleep=sleep)
