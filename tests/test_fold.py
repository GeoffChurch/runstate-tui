import json

import pytest
from runstate import Outcome, Tier, create_channel, last_activity, peek_terminal, report_death

from runstate_tui import status_fold
from runstate_tui.env import Env
from runstate_tui.fold import RunFolds, guarded, read_elapsed, read_lineage, reconcile_status
from runstate_tui.types import IssueKind, Reading, Row, Severity, StatusKind
from tests.helpers import corrupt_seq
from tests.records import Script, worker, write


def _folds(objective=None):
    return RunFolds.start(objective=objective)


def _held_run(commits, t=1.0):
    """A claim at t, then `commits` commits at t, t+1, ...: a held (live) run."""
    s = Script()
    s.started(t)
    for i in range(commits):
        s.commit(t + i)
    return s


def _sqlite_run(tmp_path, run_id, script):
    writer = create_channel(run_id, root=tmp_path, backend="sqlite")
    write(writer, script.records)
    writer.close()
    return (run_id, str(tmp_path), "sqlite")


def test_guarded_passes_through_a_clean_observable(build_log):
    ch = build_log(_held_run(5).records)
    value, issue = guarded(lambda c: read_lineage(c, _folds())[0], ch)
    assert value == 5 and issue is None


def test_guarded_lets_byte_torn_propagate(torn_sqlite_channel):
    # byte-torn = an atomicity violation (a committed non-JSON body). guarded does not
    # swallow it — it propagates, to become a loud whole-run `corrupt`.
    ch = torn_sqlite_channel(_held_run(1).records, torn_seq=2)
    with pytest.raises(json.JSONDecodeError):
        guarded(lambda c: _folds().head.poll(c), ch)


def test_locate_torn_seq_finds_the_tear(torn_sqlite_channel):
    from runstate_tui.fold import locate_torn_seq

    ch = torn_sqlite_channel(_held_run(1).records, torn_seq=2)
    assert locate_torn_seq(ch) == 2


def test_guarded_degrades_a_malformed_record_to_a_malformed_issue(build_log):
    # valid JSON, invalid Started schema (no handle/resume_from/sources): the head fold
    # raises MalformedRecordError (runstate refuses to guess at a claim it cannot
    # parse); guarded surfaces it as a MALFORMED issue with the record's own seq.
    s = Script()
    s.claim = s.raw({"t": 1.0}, "lifecycle.started")
    s.commit(2.0)
    value, issue = guarded(lambda c: _folds().head.poll(c), build_log(s.records))
    assert value is None
    assert issue.kind is IssueKind.MALFORMED and issue.severity is Severity.MEDIUM
    assert issue.seq == 1
    assert issue.detail is not None and issue.detail.startswith("MalformedRecordError")


def test_guarded_degrades_an_alien_non_dict_body_to_a_malformed_issue(tmp_path):
    # A committed body that's valid JSON but not a dict (`42`) makes a body read raise
    # AttributeError -- guarded catches it as the same decodable-but-wrong-shape
    # MALFORMED class as MalformedRecordError, NOT the byte-torn `corrupt` class
    # (undecodable JSON, still propagates uncaught).
    ref = _sqlite_run(tmp_path, "alien", _held_run(1))
    corrupt_seq(tmp_path, "alien", 2, literal="42")  # the commit
    ch = create_channel(ref[0], root=tmp_path, backend="sqlite")
    try:
        value, issue = guarded(last_activity, ch)
        assert value is None
        assert issue is not None
        assert issue.kind is IssueKind.MALFORMED and issue.severity is Severity.MEDIUM
        assert issue.seq is None  # AttributeError has no .seq
        assert issue.detail is not None and issue.detail.startswith("AttributeError")
    finally:
        ch.close()


def test_open_and_fold_degrades_an_alien_started_body_to_malformed_not_crash(tmp_path):
    # An alien non-dict `lifecycle.started` body (valid JSON, e.g. `42`) must degrade to
    # MALFORMED issues, never escape open_and_fold as a crash -- distinct from the
    # byte-torn `corrupt` class (undecodable JSON, see the next test).
    from runstate_tui import open_and_fold

    s = Script()
    s.started(100.0)
    ref = _sqlite_run(tmp_path, "alien-started", s)
    corrupt_seq(tmp_path, "alien-started", 1, literal="42")

    row = open_and_fold(ref, Env(clock=lambda: 150.0), _folds())  # must not raise

    assert row.status.kind is not StatusKind.CORRUPT  # decoded fine -- not the byte-torn class
    assert any(i.kind is IssueKind.MALFORMED for i in row.issues), row.issues


def test_open_and_fold_still_marks_byte_torn_started_as_corrupt(torn_sqlite_channel, tmp_path):
    # a byte-torn `started` (undecodable JSON, an atomicity violation) is a different
    # class of failure from an alien-but-decodable body -- a loud `corrupt` Row.
    from runstate_tui import open_and_fold

    s = Script()
    s.started(100.0)
    torn_sqlite_channel(s.records, torn_seq=1).close()

    row = open_and_fold(("torn", str(tmp_path), "sqlite"), Env(clock=lambda: 150.0), _folds())
    assert row.status.kind is StatusKind.CORRUPT


def _env(now, **kw):
    return Env(clock=lambda: now, stuck_threshold=60.0, **kw)


def test_terminal_wins(build_log):
    s = _held_run(3)
    s.stopped(5.0, completed=True)
    status, freshness, issues = reconcile_status(build_log(s.records), _env(100.0), now=100.0)
    assert status.kind is StatusKind.TERMINAL and status.outcomes == {Outcome.COMPLETED}
    assert status.tier is Tier.EXIT and status.detail is None
    assert issues == []


def test_an_errored_exit_carries_its_error(build_log):
    s = _held_run(1)
    s.stopped(2.0, completed=False, error="OOM")
    status, _f, _i = reconcile_status(build_log(s.records), _env(100.0), now=100.0)
    assert status.outcomes == {Outcome.ERRORED} and status.label == "errored"
    assert status.detail == "OOM"


def test_a_death_report_is_the_verdict_and_carries_its_evidence(build_log):
    # 0.4.0: a claim that left no exit ends by a death report naming it, with the
    # reporter's evidence; the cockpit shows the evidence as the verdict's detail.
    s = _held_run(2)
    s.died(Outcome.KILLED, "slurm:TIMEOUT", 9.0)
    status, freshness, _i = reconcile_status(build_log(s.records), _env(100.0), now=100.0)
    assert status.tier is Tier.DIED and status.outcomes == {Outcome.KILLED}
    assert status.label == "killed" and status.detail == "slurm:TIMEOUT"
    # last_activity never dates a death report (it is dated when its writer saw the
    # end): the age is the last commit's, at t=2.0
    assert freshness == 98.0


def test_disagreeing_death_reports_show_every_outcome(build_log):
    # runstate keeps every report and chooses none: the verdict is their set
    s = _held_run(1)
    s.died(Outcome.PREEMPTED, "slurm:PREEMPTED", 5.0)
    s.died(Outcome.KILLED, "reap: signal 9", 6.0)
    status, _f, _i = reconcile_status(build_log(s.records), _env(100.0), now=100.0)
    assert status.outcomes == {Outcome.PREEMPTED, Outcome.KILLED}
    assert status.label == "preempted/killed"
    assert status.detail == "slurm:PREEMPTED; reap: signal 9"


def test_an_unclaimed_launch_reads_its_terminated_and_says_so(build_log):
    s = Script()
    s.launched("L1", 1.0)
    s.terminated("L1", 2.0, exit_code=0)
    status, _f, _i = reconcile_status(build_log(s.records), _env(100.0), now=100.0)
    assert status.tier is Tier.TERMINATED and status.outcomes == {Outcome.COMPLETED}
    assert status.label == "done" and status.detail == "unclaimed launch"


def test_an_unreadable_exit_ends_the_claim_as_lost_and_is_flagged(build_log):
    # An unreadable `stopped` whose claim_seq parses still ends its claim, as a `lost`
    # (runstate reports it in RunResult.malformed rather than raising): the verdict
    # stands and the cockpit flags the skipped record as a MEDIUM issue.
    s = _held_run(1)
    s.raw({"claim_seq": 1, "completed": True}, "lifecycle.stopped")
    status, _f, issues = reconcile_status(build_log(s.records), _env(100.0), now=100.0)
    assert status.outcomes == {Outcome.LOST} and status.tier is Tier.DIED
    skipped = [i for i in issues if i.kind is IssueKind.MALFORMED]
    assert len(skipped) == 1 and skipped[0].seq == 3 and skipped[0].severity is Severity.MEDIUM
    assert skipped[0].message == "verdict skipped unreadable record at seq 3"


def test_pending_when_no_dated_activity(build_log):
    status, freshness, _ = reconcile_status(build_log([]), _env(100.0), now=100.0)
    assert status.kind is StatusKind.PENDING and freshness is None


def test_live_then_stale_by_freshness(build_log):
    ch = build_log(_held_run(1, t=100.0).records)
    status, freshness, _ = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.LIVE and freshness == 0.0
    assert reconcile_status(ch, _env(1000.0), now=1000.0)[0].kind is StatusKind.STALE


def test_an_unreported_crash_reads_held_and_ages_to_stale(build_log):
    # 0.4.0 reads no process: a worker that died with nobody reporting it is a held
    # claim -- no verdict -- and only its freshness says anything, ageing it to `stale`.
    ch = build_log(_held_run(3, t=100.0).records)  # its last commit at t=102, then silence
    assert peek_terminal(ch) is None
    status, freshness, _ = reconcile_status(ch, _env(500.0), now=500.0)
    assert status.kind is StatusKind.STALE and freshness == 398.0


def test_reconcile_status_flags_skew_when_last_activity_is_in_the_future(build_log):
    # last_activity dated ahead of `now` (clock skew): flag it, clamp freshness >= 0,
    # and don't let the skew read as staleness (age clamps to 0 -> LIVE).
    ch = build_log(_held_run(1, t=500.0).records)
    status, freshness, issues = reconcile_status(ch, _env(100.0), now=100.0)
    assert any(i.kind is IssueKind.SKEW_SUSPECTED for i in issues)
    assert freshness == 0.0  # max(0.0, now - la), never negative
    assert status.kind is StatusKind.LIVE


def _started_at(t):
    s = Script()
    s.raw(
        {"handle": "local://h/1", "t": t, "resume_from": None, "sources": []}, "lifecycle.started"
    )
    return s


def test_reconcile_status_inf_last_activity_is_pending_not_live(build_log):
    # +inf would otherwise read as freshness=0.0 -> LIVE (the dangerous false-fresh
    # case): guard it to pending with a loud HIGH malformed issue, retaining the raw
    # value in `detail`.
    status, freshness, issues = reconcile_status(
        build_log(_started_at(float("inf")).records), _env(100.0), now=100.0
    )
    assert status.kind is StatusKind.PENDING
    assert freshness is None
    matches = [
        i
        for i in issues
        if i.kind is IssueKind.MALFORMED
        and i.severity is Severity.HIGH
        and "not finite" in i.message
    ]
    assert len(matches) == 1
    assert "inf" in matches[0].detail.lower()


@pytest.mark.parametrize("t", [float("nan"), -5.0])
def test_a_junk_activity_clock_is_skipped_upstream_and_reads_pending(build_log, t):
    # runstate's last_activity skips a junk `t` (NaN, negative) itself: no dated activity
    status, freshness, _ = reconcile_status(build_log(_started_at(t).records), _env(100.0), 100.0)
    assert status.kind is StatusKind.PENDING and freshness is None


def test_reading_is_named_and_none_without_an_objective(build_log):
    s = Script()
    s.started(1.0)
    s.value("loss", 0.5, 1.0)
    s.value("acc", 0.9, 1.0)
    s.commit(1.0)
    s.commit(2.0)
    ch = build_log(s.records)
    assert read_lineage(ch, _folds(None))[1] is None  # never nameless
    assert read_lineage(ch, _folds("loss"))[1] == Reading("loss", 0.5, 1)  # at commit 1
    assert read_lineage(ch, _folds("missing"))[1] is None


def test_a_value_emitted_but_not_committed_is_not_read(build_log):
    # a value belongs to the node that commits it: a pending one is not on the lineage
    s = Script()
    s.started(1.0)
    s.value("loss", 0.5, 1.0)
    s.commit(1.0)
    s.value("loss", 0.25, 2.0)  # emitted, not yet committed (the claim is still open)
    assert read_lineage(build_log(s.records), _folds("loss"))[1] == Reading("loss", 0.5, 1)


def test_frontier_is_the_commit_count_and_zero_on_the_empty_lineage(build_log):
    s = Script()
    s.started(1.0)
    assert read_lineage(build_log(s.records), _folds())[0] == 0
    for i in range(4):
        s.commit(2.0 + i)
    assert read_lineage(build_log(s.records), _folds())[0] == 4


def test_a_malformed_value_degrades_the_reading_alone(build_log):
    # value-v0.3 requires {value, t}; a body missing `value` raises when it is read
    # (never when counted), so the reading degrades to one MALFORMED issue and the
    # frontier, counted from the nodes alone, survives
    s = Script()
    s.started(1.0)
    s.pending.append(s.raw({"t": 2.0}, "value", "loss"))
    s.commit(2.0)
    frontier, reading, issues = read_lineage(build_log(s.records), _folds("loss"))
    assert frontier == 1 and reading is None
    assert [(i.kind, i.seq) for i in issues] == [(IssueKind.MALFORMED, 2)]


def test_a_fold_held_across_polls_equals_a_cold_one(tmp_path):
    # the polled path: one RunFolds across ticks, and each tick's answers equal a cold
    # fold's (runstate's contract: incremental equals pure after every step)
    ch = create_channel("held", root=tmp_path, backend="sqlite")
    w = worker(ch)
    held = _folds("loss")
    for i in range(5):
        w.emit("loss", 1.0 / (i + 1))
        w.commit()
        assert read_lineage(ch, held) == read_lineage(ch, _folds("loss"))
    assert read_lineage(ch, held)[:2] == (5, Reading("loss", 0.2, 5))
    ch.close()


def test_elapsed_is_wall_age_from_first_started(build_log):
    s = Script()
    s.started(100.0)
    s.died(Outcome.LOST, "probe: local://h/1", 150.0)
    s.started(200.0, "local://h/2")  # a later episode
    elapsed, issue = read_elapsed(build_log(s.records), now=250.0)
    assert elapsed == 150.0 and issue is None  # from the FIRST started (100.0), not the latest


def test_elapsed_none_without_a_started(build_log):
    s = Script()
    s.launched("L1", 1.0)
    assert read_elapsed(build_log(s.records), now=9.0) == (None, None)


def test_elapsed_never_negative_and_flags_skew(build_log):
    s = Script()
    s.started(500.0)
    elapsed, issue = read_elapsed(build_log(s.records), now=100.0)  # stamped in the future
    assert elapsed == 0.0
    assert issue.kind is IssueKind.SKEW_SUSPECTED


def test_status_fold_on_a_healthy_live_run(build_log):
    s = Script()
    s.started(100.0)
    for i in range(6):
        s.commit(101.0 + i)
    s.value("loss", 0.03, 140.0)
    s.commit(140.0)
    row = status_fold(build_log(s.records), _env(150.0, objective="loss"), _folds("loss"))
    assert isinstance(row, Row)
    assert row.status.kind is StatusKind.LIVE
    assert row.frontier == 7
    assert row.value == Reading("loss", 0.03, 7)
    assert row.elapsed == 50.0
    assert row.freshness == 10.0
    assert row.issues == ()


def test_status_fold_populates_episode_stops_and_demand(rich_run):
    row = status_fold(rich_run(), _env(150.0, objective="loss"), _folds("loss"))
    assert row.episode == "local://h/1"
    assert len(row.undischarged_stops) == 1
    assert row.undischarged_stops[0].topic == "control.stop"
    assert len(row.live_demand) == 1
    assert row.live_demand[0].topic == "control.subscribe"


def test_a_stop_a_crashed_claim_never_honored_stays_pending(build_log):
    # 0.4.0: a stop is a request about the process, spent only by an exit's `honored`, a
    # withdrawal or a nak. One sent to a claim that then died is still pending, and the
    # next claim's first drain honors it -- the cockpit shows it (no episode filter).
    s = Script()
    s.started(100.0)
    s.stop("webui:s1")
    s.died(Outcome.LOST, "probe: local://h/1", 120.0)
    s.started(130.0, "local://h/2")
    row = status_fold(build_log(s.records), _env(150.0), _folds())
    assert [e.request_id for e in row.undischarged_stops] == ["webui:s1"]


def test_a_withdrawn_stop_is_spent(build_log):
    s = Script()
    s.started(100.0)
    s.stop("webui:s1")
    s.withdraw("webui:s1", 101.0)
    assert status_fold(build_log(s.records), _env(150.0), _folds()).undischarged_stops == ()


def test_a_real_worker_and_a_real_death_report(tmp_path):
    # end to end through runstate's writers: the Worker commits; report_death ends it
    ch = create_channel("real", root=tmp_path, backend="sqlite")
    w = worker(ch, clock=100.0)
    w.emit("loss", 0.5)
    w.commit()
    claim = ch.latest("lifecycle.started").seq
    report_death(ch, claim, outcome=Outcome.ERRORED, evidence="reap: exit 1", force=True)
    row = status_fold(ch, _env(150.0, objective="loss"), _folds("loss"))
    assert row.status.label == "errored" and row.status.detail == "reap: exit 1"
    assert row.frontier == 1 and row.value == Reading("loss", 0.5, 1)
    ch.close()


def test_status_fold_lets_byte_torn_propagate(torn_sqlite_channel):
    # a byte-torn record anywhere in the log crashes the fold (no granular degradation
    # for corruption): the first read that decodes it raises.
    ch = torn_sqlite_channel(_held_run(2, t=100.0).records, torn_seq=2)
    with pytest.raises(json.JSONDecodeError):
        status_fold(ch, _env(150.0), _folds())
