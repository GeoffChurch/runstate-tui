"""Curated control-plane basis (docs/superpowers/notes/2026-07-18-fixture-basis.md,
.superpowers/sdd/task-3-brief.md): adversarial + finding-locking scenarios for the
control plane's read side (`undischarged_stops`, `live_demand`) and its
handshake side (`stop_run` / `dispatch_stop`, which drive `await_consumed`), in log
format 0.4.0.

Every assertion here was set from the REAL observable's/handshake's ACTUAL
output (verified empirically, not guessed) -- a regression test's "expected" IS
the current behavior. Where a scenario exposes a known-deferred gap, the test still
locks the CURRENT behavior and names the gap in a `# FINDING:` comment. Where 0.4.0
changed what a 0.3.0 scenario pinned -- several 0.3.0 findings are closed upstream by
reference by name -- the scenario says what changed (`0.4.0:`). No production code
is changed here."""

from __future__ import annotations

from dataclasses import asdict

import pytest
from runstate import (
    Heartbeat,
    Nak,
    NakReason,
    Outcome,
    Topic,
    await_consumed,
    live_demand,
    live_episode,
    undischarged_stops,
)

from runstate_tui.control import StopResult, dispatch_stop, stop_run
from tests.helpers import fake_clock, log_path
from tests.records import Script, write

# --- undischarged_stops ---------------------------------------------------------


def test_naked_stop_is_spent_by_its_nak(build_log):
    # A malformed stop trigger (control.stop takes only `from`) gets refused by a real
    # worker's nak. 0.4.0: a nak naming a request SPENDS it by name, so the refused stop
    # is no longer pending. (0.3.0 over-reported it as pending until the next exit.)
    s = Script()
    s.stop("webui:s1", {"until": {"commits": 5}})
    s.raw(
        asdict(Nak(reason=NakReason.MALFORMED, message="control.stop takes only `from`")),
        Topic.LIFECYCLE_NAK,
        None,
        "webui:s1",
    )
    assert undischarged_stops(build_log(s.records)) == []


def test_stops_are_pending_by_name_until_an_answer_names_them(build_log):
    # Two stops reusing one request_id are ONE pending stop: each id's newest send.
    # 0.4.0: an exit discharges only the stops its `honored` names, so an exit that
    # names neither leaves the stop pending (the next claim honors it), and one that
    # names it clears it. (0.3.0 listed both sends, and any exit cleared every stop.)
    s = Script()
    s.started(1.0, "h")
    s.stop("webui:s1")
    s.stop("webui:s1", {"from": {"commits": 5}})  # id reuse: the newest send stands
    pre = undischarged_stops(build_log(s.records))
    assert [(e.request_id, e.seq) for e in pre] == [("webui:s1", 3)]

    unnamed = Script(records=list(s.records), claim=1)
    unnamed.stopped(2.0, completed=False)  # honors nothing
    assert [e.request_id for e in undischarged_stops(build_log(unnamed.records))] == ["webui:s1"]

    named = Script(records=list(s.records), claim=1)
    named.stopped(2.0, completed=False, honored=("webui:s1",))
    assert undischarged_stops(build_log(named.records)) == []


def test_a_withdrawal_spends_a_stop_and_a_death_report_spends_nothing(build_log):
    # 0.4.0's two new answers: the asker's own `control.withdraw` spends its stop; a
    # death report ends the claim but answers no request.
    s = Script()
    s.started(1.0, "h")
    s.stop("webui:kept")
    s.stop("webui:withdrawn")
    s.withdraw("webui:withdrawn", 2.0)
    s.died(Outcome.LOST, "probe: h", 3.0)
    assert [e.request_id for e in undischarged_stops(build_log(s.records))] == ["webui:kept"]


def test_conditional_vs_immediate_pending_not_due(build_log):
    # An unconditional stop ({}) and a conditional one ({"from": {"commits": 100}},
    # nowhere near due) are BOTH listed, indistinguishably.
    # pending != due -- undischarged_stops has no "due" axis; due-evaluation
    # needs the worker's own coordinates (its lineage's counts, its clock).
    s = Script()
    s.stop("webui:imm")
    s.stop("webui:cond", {"from": {"commits": 100}})
    stops = undischarged_stops(build_log(s.records))
    assert [(e.request_id, e.body) for e in stops] == [
        ("webui:imm", {}),
        ("webui:cond", {"from": {"commits": 100}}),
    ]


def test_undischarged_stop_spans_episode(build_log):
    # The observable is not episode-scoped, by design: a stop is a request about the
    # process, so one filed against the FIRST claim and never honored is still pending
    # on the SECOND -- whose first drain reads it and honors it. The fold-plane now
    # shows it too (test_fold_plane.py::test_undischarged_stop_spans_episode).
    s = Script()
    s.started(1.0, "h1")
    s.stop("webui:earlier")
    s.died(Outcome.LOST, "probe: h1", 3.0)
    s.started(5.0, "h2")
    ch = build_log(s.records)
    assert live_episode(ch) == "h2"  # a fresh, live claim
    assert [e.request_id for e in undischarged_stops(ch)] == ["webui:earlier"]


# --- live_demand -----------------------------------------------------------------


def test_withdrawn_vs_nullid_nak(build_log):
    # sub-a is withdrawn by its asker; sub-b stays live; a null-id nak names no request
    # and answers NOTHING. 0.4.0: `control.unsubscribe` is retired; the asker's
    # cancellation is `control.withdraw`, by name. A pure demand (name None) with no
    # `until` lasts until withdrawn.
    s = Script()
    s.subscribe("sub-a", None, {})
    s.withdraw("sub-a", 1.0)
    s.subscribe("sub-b", None, {})
    s.raw(asdict(Nak(reason=NakReason.MALFORMED, message="x")), Topic.LIFECYCLE_NAK, None, None)
    assert [d.request_id for d in live_demand(build_log(s.records))] == ["sub-b"]


def test_resubscribe_after_answer_stays_spent(build_log):
    # subscribe(sub-a) -> nak(sub-a) -> subscribe(sub-a) again. 0.4.0: request ids are
    # one namespace and a spent id stays spent, so the re-send is dead on arrival and
    # live_demand is empty. (0.3.0's positional fold read the re-send as live again.)
    s = Script()
    s.subscribe("sub-a", None, {})
    s.raw(asdict(Nak(reason=NakReason.MALFORMED, message="x")), Topic.LIFECYCLE_NAK, None, "sub-a")
    s.subscribe("sub-a", None, {})
    assert live_demand(build_log(s.records)) == []


def test_a_lease_is_void_once_its_claim_has_ended(build_log):
    # A schedule with a time atom is a LEASE: bound by `lifecycle.bound` to the claim
    # that registered it, and void once that claim has ended. 0.4.0: voiding is by name
    # (the binding), not by position -- an episode boundary alone voids nothing.
    s = Script()
    s.started(1.0, "h1")
    s.subscribe("sub-time", None, {"until": {"time_seconds": 5}})
    s.raw({"claim_seq": 1}, Topic.LIFECYCLE_BOUND, None, "sub-time")  # h1 binds it
    s.died(Outcome.LOST, "probe: h1", 4.0)  # h1 ends
    s.started(5.0, "h2")
    assert live_demand(build_log(s.records)) == []

    # Contrast: a durable subscription (no time or count atom) is never bound, and
    # stays live across the claims.
    s = Script()
    s.started(1.0, "h1")
    s.subscribe("sub-durable", "loss", {"every": {"commits": 1}})
    s.died(Outcome.LOST, "probe: h1", 4.0)
    s.started(5.0, "h2")
    assert [d.request_id for d in live_demand(build_log(s.records))] == ["sub-durable"]


# --- await_consumed / stop_run / dispatch_stop ------------------------------------


def _claimed(build_log):
    s = Script()
    s.started(1.0, "h")
    return build_log(s.records)


def test_accepted_watermark_climb(build_log, answer_on_sleep):
    # Two-phase seed on await_consumed's sleep seam: poll 1 posts the claim's heartbeat
    # NOT YET covering the stop's seq (consumed_seq = seq-1) -- must NOT be mistaken
    # for acceptance; poll 2 posts one that covers it EXACTLY (consumed_seq = seq) --
    # locks the `>=` watermark boundary.
    ch = _claimed(build_log)

    def not_yet(channel):
        stop = channel.latest(Topic.CONTROL_STOP)
        channel.send(
            asdict(Heartbeat(claim_seq=1, consumed_seq=stop.seq - 1, t=1.0)),
            topic=Topic.LIFECYCLE_HEARTBEAT,
        )

    def covering(channel):
        stop = channel.latest(Topic.CONTROL_STOP)
        channel.send(
            asdict(Heartbeat(claim_seq=1, consumed_seq=stop.seq, t=2.0)),
            topic=Topic.LIFECYCLE_HEARTBEAT,
        )

    sleep = answer_on_sleep(ch, {1: not_yet, 2: covering})
    outcome = stop_run(ch, request_id="webui:climb", timeout=5.0, sleep=sleep)
    assert outcome.result is StopResult.ACCEPTED

    # Not-fooled-by-early-poll control: the SAME not-yet-covering seed alone
    # (never followed by a covering one), under a bounded timeout, must NOT
    # accept -- proves poll 1 alone genuinely doesn't trip acceptance.
    ch2 = _claimed(build_log)
    sleep_never_covers = answer_on_sleep(ch2, {1: not_yet})
    outcome2 = stop_run(ch2, request_id="webui:neverfull", timeout=0.2, sleep=sleep_never_covers)
    assert outcome2.result is StopResult.UNSAFE


def test_refused_nak(build_log):
    ch = _claimed(build_log)

    def seed(_interval):
        ch.send(
            asdict(Nak(reason=NakReason.UNSATISFIABLE, message="nope")),
            topic=Topic.LIFECYCLE_NAK,
            request_id="webui:r",
        )

    outcome = stop_run(ch, request_id="webui:r", timeout=5.0, sleep=seed)
    assert outcome.result is StopResult.REFUSED
    assert outcome.detail == "unsatisfiable"


def test_died_terminal_follows(build_log):
    # The claim exits above the stop without taking it: the terminal RunResult.
    s = Script()
    s.started(1.0, "h")
    ch = build_log(s.records)

    def seed(_interval):
        before = len(s.records)
        s.stopped(2.0, completed=False, error="killed")
        write(ch, s.records[before:])

    outcome = stop_run(ch, request_id="webui:d", timeout=5.0, sleep=seed)
    assert outcome.result is StopResult.DIED
    assert outcome.detail == "errored"


def test_a_death_report_alone_does_not_answer_a_stop(build_log):
    # 0.4.0: a death report shows its writer read the request, not that the dead claim
    # did, so await_consumed waits for the NEXT claim -- here none comes: UNSAFE.
    s = Script()
    s.started(1.0, "h")
    ch = build_log(s.records)

    def seed(_interval):
        before = len(s.records)
        if before == 2:  # once: the stop is seq 2
            s.died(Outcome.KILLED, "reap: signal 9", 2.0)
            write(ch, s.records[before:])

    now, _sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    outcome = stop_run(ch, request_id="webui:dead", timeout=1.0, now=now, sleep=seed)
    assert outcome.result is StopResult.UNSAFE


def test_unsafe_timeout(build_log):
    ch = build_log([])
    now, sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    outcome = stop_run(ch, request_id="webui:u", timeout=1.0, now=now, sleep=sleep)
    assert outcome.result is StopResult.UNSAFE


def test_sent_after_terminal_is_moot_not_died(build_log):
    # A stop dispatched AFTER the run already ended is MOOT, not UNSAFE: stop_run
    # peeks for an existing terminal BEFORE sending, so this never reaches
    # await_consumed at all -- and no control.stop is appended to a log that already
    # has its verdict.
    s = Script()
    s.started(1.0, "h")
    s.stopped(2.0, completed=True)
    ch = build_log(s.records)
    now, sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    outcome = stop_run(ch, request_id="webui:after", timeout=1.0, now=now, sleep=sleep)
    assert outcome.result is StopResult.MOOT
    assert outcome.detail == "run already ended (done)"
    assert ch.read(topics=[Topic.CONTROL_STOP]) == []  # short-circuited before send


def test_dispatch_stop_missing_no_phantom(tmp_path):
    ref = ("ghost", str(tmp_path), "sqlite")
    outcome = dispatch_stop(ref, request_id="webui:m", timeout=1.0)
    assert outcome.result is StopResult.UNDELIVERED
    # the phantom-db guard: no <run_id>.db was fabricated to write a stop into
    assert not log_path(tmp_path, "ghost").exists()


def test_a_nameless_request_is_refused_up_front(build_log):
    # 0.4.0: a stop with no request_id is nameless -- never pending, and nothing can
    # answer it -- so await_consumed raises ValueError at once rather than wait for a
    # timeout (0.3.0's FINDING: it could only ever time out). The cockpit always mints
    # a `webui:<uuid>` id, so it never sends one.
    ch = build_log([])
    seq = ch.send({}, topic=Topic.CONTROL_STOP, request_id=None)
    now, sleep = fake_clock(100.0, 100.0, 101.0, 102.0, 103.0, 104.0)
    with pytest.raises(ValueError, match="names no request"):
        await_consumed(ch, seq, timeout=1.0, now=now, sleep=sleep)
