"""Curated fold-plane basis (docs/superpowers/notes/2026-07-18-fixture-basis.md,
.superpowers/sdd/task-2-brief.md): adversarial + finding-locking scenarios for the
PURE fold (lifecycle/verdict, integrity/corruption, time/clocks, episodes/ordering),
in log format 0.4.0.

Every assertion here was set from the fold's ACTUAL output (verified empirically,
not guessed) -- a regression test's "expected" IS the current behavior. Where a
scenario exposes a known-deferred gap, the test still locks the CURRENT behavior and
names the gap in a `# FINDING:` comment; it does not try to fix it. Where 0.4.0 changed
a behavior a 0.3.0 scenario pinned, the scenario says what changed (`0.4.0:`).

Records are built by `tests.records.Script` from runstate's typed bodies, so each is
schema-valid unless a scenario deliberately plants a bad one (`Script.raw`)."""

from __future__ import annotations

import os
import sqlite3
import time

import pytest
from runstate import Outcome, Reap, Tier, create_channel

from runstate_tui import open_and_fold, status_fold
from runstate_tui.env import Env
from runstate_tui.fold import RunFolds, read_elapsed, reconcile_status
from runstate_tui.types import IssueKind, Severity, StatusKind
from tests.helpers import corrupt_seq, log_path, move_log
from tests.records import Script, write


def _env(now, **kw):
    return Env(clock=lambda: now, stuck_threshold=kw.pop("stuck_threshold", 60.0), **kw)


def _fold(ch, env):
    return status_fold(ch, env, RunFolds.start(objective=env.objective))


def _open(ref, env):
    return open_and_fold(ref, env, RunFolds.start(objective=env.objective))


def _sqlite_run(tmp_path, run_id, script):
    """Write `script`'s records to a fresh sqlite-backed run and return its RunRef
    `(run_id, root, "sqlite")` -- the integrity scenarios below need a real file to
    tear with `corrupt_seq` or overlay with `foreign_db`, unlike `build_log`'s
    in-memory channel."""
    writer = create_channel(run_id, root=tmp_path, backend="sqlite")
    write(writer, script.records)
    writer.close()
    return (run_id, str(tmp_path), "sqlite")


def _claim(t, handle="h1"):
    s = Script()
    s.started(t, handle)
    return s


# --- Lifecycle / verdict ------------------------------------------------------


def test_stale_threshold_boundary(build_log):
    # t=0 not falsy (see also test_t_exactly_zero_not_falsy); three clocks straddle
    # the threshold to lock the `<=` (inclusive) freshness boundary in FreshnessSignal.
    ch = build_log(_claim(0.0).records)
    live_at, live_at_boundary, stale_just_past = 59.999, 60.0, 60.001
    status, freshness, issues = reconcile_status(ch, _env(live_at, stuck_threshold=60.0), live_at)
    assert status.kind is StatusKind.LIVE and freshness == 59.999 and issues == []
    status, freshness, issues = reconcile_status(
        ch, _env(live_at_boundary, stuck_threshold=60.0), live_at_boundary
    )
    assert status.kind is StatusKind.LIVE and freshness == 60.0  # <=, inclusive
    status, freshness, issues = reconcile_status(
        ch, _env(stale_just_past, stuck_threshold=60.0), stale_just_past
    )
    assert status.kind is StatusKind.STALE and freshness == 60.001


def test_terminal_wins_over_stray_heartbeat(build_log):
    # A heartbeat naming the claim FAR after its clean exit must not resurrect
    # liveness: the claim's readable exit is the verdict, whatever follows it.
    # freshness is still computed (from the stray heartbeat, the largest dated t) --
    # large, but the status stays TERMINAL. (0.4.0: a conforming worker writes nothing
    # after its exit, so the stray record is a writer bug the verdict ignores.)
    s = _claim(1.0)
    s.stopped(2.0, completed=True)
    s.heartbeat(1_000_000.0)
    row = _fold(build_log(s.records), _env(2_000_000.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcomes == {Outcome.COMPLETED}
    assert row.freshness == 1_000_000.0  # large -- from the stray heartbeat -- yet terminal wins
    assert row.issues == ()


def test_terminal_superseded_by_new_episode(build_log):
    # started(h1) -> stopped -> started(h2), no further activity: the new claim alone
    # supersedes the old verdict (the verdict is the NEWEST claim's) -- LIVE again,
    # episode is h2, but elapsed still dates from the FIRST started.
    s = _claim(1.0)
    s.stopped(2.0, completed=False)
    s.started(3.0, "h2")
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.kind is StatusKind.LIVE
    assert row.episode == "h2"
    assert row.elapsed == 9.0  # now(10) - FIRST started.t(1), not h2's t(3)
    assert row.freshness == 7.0  # now(10) - la(3, h2's started -- the latest dated t)
    assert row.issues == ()


def test_terminal_errored_via_stopped(build_log):
    # completed=False + error="OOM" -> Outcome.ERRORED, under the claim's own exit; the
    # error text is retained on the Row (faithful-representation: the diagnostic is
    # not dropped).
    s = _claim(1.0)
    s.stopped(2.0, completed=False, error="OOM")
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcomes == {Outcome.ERRORED}
    assert row.status.tier is Tier.EXIT
    assert row.status.detail == "OOM"  # the error string is retained on the Row


def test_error_empty_string_is_errored(build_log):
    # error="" is falsy but `is not None` -- the self-report reads `error is not None`,
    # not truthiness, so an empty-string error still reads as ERRORED rather than
    # falling through to the completed/preempted branches.
    s = _claim(1.0)
    s.stopped(5.0, completed=False, error="")
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcomes == {Outcome.ERRORED}


def test_schema_invalid_stopped_completed_with_error(build_log):
    # completed=True + error set violates Stopped's own invariant (completed => no
    # error), so the exit is unreadable. 0.4.0: an unreadable exit whose claim_seq
    # parses still ENDS its claim, as a `lost` (runstate reports it in
    # RunResult.malformed rather than raising), so it can neither poison the run nor
    # strand the claim. The verdict stands -- lost, by the viewpoint that a record
    # ended the claim -- and the cockpit flags the skipped record as a MEDIUM issue. A
    # later heartbeat does not resurrect it. (0.3.0 degraded past it to LIVE.)
    s = _claim(1.0)
    s.raw(
        {
            "completed": True,
            "error": "OOM",
            "claim_seq": 1,
            "honored": [],
            "parent": None,
            "values": None,
            "fired": [],
            "expired": [],
            "t": 2.0,
        },
        "lifecycle.stopped",
    )
    s.heartbeat(3.0)
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcomes == {Outcome.LOST}
    assert row.status.tier is Tier.DIED
    assert row.freshness == 7.0  # now(10) - heartbeat.t(3)
    malformed = [i for i in row.issues if i.kind is IssueKind.MALFORMED]
    assert len(malformed) == 1
    assert malformed[0].seq == 2
    assert malformed[0].severity is Severity.MEDIUM
    assert malformed[0].message == "verdict skipped unreadable record at seq 2"


def test_death_report_ends_a_claim_that_left_no_exit(build_log):
    # 0.4.0: the claim's exit is its own word; a death report is anyone's evidence that
    # it ended without one. Its evidence is the verdict's detail, and it is never
    # dated as activity (it is dated when its writer saw the end, maybe hours later).
    s = _claim(1.0)
    s.commit(2.0)
    s.died(Outcome.KILLED, "slurm:TIMEOUT", 5_000.0)
    row = _fold(build_log(s.records), _env(5_010.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.tier is Tier.DIED
    assert row.status.label == "killed" and row.status.detail == "slurm:TIMEOUT"
    assert row.freshness == 5_008.0  # now - the commit's t(2), not the report's t(5000)
    assert row.frontier == 1
    assert row.issues == ()


def test_disagreeing_death_reports_are_all_kept(build_log):
    # Several parties report one death: runstate keeps every report, chooses none, and
    # drops `lost` beside a cause. The cockpit renders the whole set, in the worst color.
    s = _claim(1.0)
    s.died(Outcome.LOST, "probe: h1", 3.0)
    s.died(Outcome.PREEMPTED, "slurm:PREEMPTED", 4.0)
    s.died(Outcome.KILLED, "reap: signal 9", 5.0)
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.outcomes == {Outcome.PREEMPTED, Outcome.KILLED}
    assert row.status.label == "preempted/killed"
    assert row.status.detail == "probe: h1; slurm:PREEMPTED; reap: signal 9"


def test_unclaimed_launch_takes_its_terminated(build_log):
    # A launch whose worker never claimed (a startup crash): the verdict is the
    # launch's `terminated`, a fact about a process, so the cockpit says so.
    s = Script()
    s.launched("L1", 1.0)
    s.terminated("L1", 2.0, reason=Reap.EXITED, exit_code=3)
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.tier is Tier.TERMINATED and row.status.outcomes == {Outcome.ERRORED}
    assert row.status.label == "errored" and row.status.detail == "unclaimed launch"
    assert row.freshness == 9.0  # dated by `launched` only: a `terminated` dates nothing
    assert row.episode is None and row.elapsed is None


# --- Integrity / corruption ---------------------------------------------------


def test_corrupt_at_seq_1(tmp_path):
    ref = _sqlite_run(tmp_path, "cseq1", _claim(100.0))
    corrupt_seq(tmp_path, "cseq1", 1)
    row = _open(ref, _env(150.0))
    assert row.status.kind is StatusKind.CORRUPT
    assert len(row.issues) == 1
    assert row.issues[0].kind is IssueKind.CORRUPT
    assert row.issues[0].seq == 1
    assert row.severity is Severity.HIGH


def test_corrupt_deep(tmp_path):
    # ~2000 records, torn near the middle: the stop fold (PendingStops) reads every
    # record on the control.stop topic from seq 0, so it hits the tear, raises
    # json.JSONDecodeError, and open_and_fold degrades to CORRUPT via
    # locate_torn_seq's one-row-at-a-time scan. Assert it terminates promptly
    # (a generous bound, not a tight timing pin) and lands the RIGHT seq.
    s = Script()
    for i in range(2000):
        s.stop(f"webui:s{i}")
    ref = _sqlite_run(tmp_path, "cdeep", s)
    corrupt_seq(tmp_path, "cdeep", 1000)
    started = time.monotonic()
    row = _open(ref, _env(3000.0))
    elapsed_wall = time.monotonic() - started
    assert elapsed_wall < 5.0  # terminates -- no full re-decode blowup
    assert row.status.kind is StatusKind.CORRUPT
    assert row.issues[0].seq == 1000


def test_corrupt_value_of_another_name(tmp_path):
    # 0.4.0: the objective's fold (LastElement) reads EVERY value record, whatever its
    # name, since membership is decided per committing claim -- so a torn `acc` value
    # is decoded and the row is loudly CORRUPT. 0.3.0's name-filtered `latest` never
    # decoded it, and the row read fully clean.
    # FINDING (narrower than in 0.3.0): with NO objective, no fold reads values, and the
    # same tear stays invisible -- visibility depends on which folds read which topics.
    s = _claim(1.0)
    s.value("acc", 0.9, 2.0)  # torn below, name != objective
    s.commit(2.0)
    ref = _sqlite_run(tmp_path, "cwrong", s)
    corrupt_seq(tmp_path, "cwrong", 2)
    assert _open(ref, _env(10.0, objective="loss")).status.kind is StatusKind.CORRUPT
    unwatched = _open(ref, _env(10.0))
    assert unwatched.status.kind is StatusKind.LIVE and unwatched.issues == ()


def test_corrupt_discharged_stop_is_visible(tmp_path):
    # 0.4.0: a stop is spent by name wherever its answer sits, so the stop fold reads
    # the whole control.stop topic -- a torn stop the exit already honored is decoded,
    # and the row is loudly CORRUPT. (0.3.0 read stops only after the latest `stopped`,
    # so this tear was invisible behind a clean terminal row.)
    s = _claim(1.0)
    s.stop("webui:s1")  # torn below, then honored by the exit
    s.stopped(5.0, completed=False, honored=("webui:s1",))
    ref = _sqlite_run(tmp_path, "cstop", s)
    corrupt_seq(tmp_path, "cstop", 2)
    row = _open(ref, _env(10.0))
    assert row.status.kind is StatusKind.CORRUPT and row.issues[0].seq == 2


def test_alien_started_body_malformed(tmp_path):
    # A committed-but-alien `lifecycle.started` body (valid JSON, non-dict `42`) is a
    # distinct failure class from byte-torn: guarded() catches it as MALFORMED, not
    # CORRUPT -- the run survives. FIVE independent fold reads each touch this one
    # record, each a MALFORMED issue of its own: three read the body directly
    # (last_activity, read_elapsed, the episode handle -> AttributeError, no seq), and
    # two parse it as the claim through runstate (the head fold and the demand fold's
    # own head -> MalformedRecordError at seq 1).
    ref = _sqlite_run(tmp_path, "alienst", _claim(100.0))
    corrupt_seq(tmp_path, "alienst", 1, literal="42")
    row = _open(ref, _env(150.0))
    assert row.status.kind is StatusKind.PENDING  # no other dated activity -- not a crash
    assert all(i.kind is IssueKind.MALFORMED for i in row.issues)
    assert sorted(i.seq or 0 for i in row.issues) == [0, 0, 0, 1, 1]
    assert row.frontier is None and row.elapsed is None and row.episode is None


def test_foreign_valid_db_reads_missing_and_is_left_byte_identical(foreign_db):
    # THE PAYOFF (spec channel-locators.md; the PR #14 harm, now pinned FIXED): a VALID
    # sqlite file with an alien (non-runstate) schema reached through a stale pointer is
    # never schema-mutated. attach_channel opens `mode=rw` and only PROBES for a `log`
    # table; "no such table: log" is RunNotFound -> `missing`, and NOTHING is written.
    run_id, root, _backend = foreign_db
    dbfile = str(log_path(root, run_id))
    with open(dbfile, "rb") as f:
        before = f.read()

    row = _open(foreign_db, _env(150.0))
    assert row.status.kind is StatusKind.MISSING  # no `log` table -> RunNotFound
    assert row.issues == ()

    with open(dbfile, "rb") as f:
        after = f.read()
    assert after == before  # byte-identical -- attach mutated nothing (the fix)
    assert not os.path.exists(dbfile + "-wal")  # no WAL side-file was created either

    conn = sqlite3.connect(dbfile)
    try:
        tables = {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "log" not in tables  # the runstate schema was NOT added (the mutation is gone)
    assert "unrelated" in tables  # the alien table is untouched


def test_malformed_value_is_flagged_and_the_frontier_survives(build_log):
    # 0.4.0: a committed `value` missing its own `value` key raises when its value is
    # READ (never when counted), so the reading degrades to one MALFORMED issue while
    # the frontier, counted from the nodes alone, survives. (0.3.0's tolerant `latest`
    # read it as a present-but-null value with no issue at all.)
    s = _claim(100.0)
    s.pending.append(s.raw({"t": 140.0}, "value", "loss"))
    s.commit(140.0)
    row = _fold(build_log(s.records), _env(200.0, objective="loss"))
    assert row.value is None and row.frontier == 1
    assert row.status.kind is StatusKind.LIVE  # unaffected by the malformed value
    assert [(i.kind, i.seq) for i in row.issues] == [(IssueKind.MALFORMED, 2)]


# --- Time / clocks -------------------------------------------------------------


def _started_at(t):
    s = Script()
    s.raw({"handle": "h1", "t": t, "resume_from": None, "sources": []}, "lifecycle.started")
    return s


def test_nan_clock_not_falsely_live(build_log):
    # A NaN `t` must not read as freshness=0.0 -> LIVE (the false-fresh trap). 0.4.0:
    # runstate's last_activity skips a junk `t` itself (a measurement fold), so the
    # run has no dated activity and reads PENDING.
    # FINDING: the skip is silent -- the cockpit's own non-finite guard no longer sees a
    # NaN, so the row carries no issue naming the garbage clock.
    row = _fold(build_log(_started_at(float("nan")).records), _env(100.0))
    assert row.status.kind is StatusKind.PENDING
    assert row.freshness is None
    assert not [i for i in row.issues if "not finite" in i.message]


def test_inf_asymmetric(build_log):
    # +inf passes runstate's junk filter (`t >= 0`), so it reaches the cockpit's
    # non-finite guard: PENDING with a loud HIGH issue rather than freshness=0.0 ->
    # LIVE. -inf fails the filter and is skipped upstream: PENDING, silently. Both land
    # PENDING (never a false LIVE or STALE); only one is flagged.
    row = _fold(build_log(_started_at(float("inf")).records), _env(100.0))
    assert row.status.kind is StatusKind.PENDING and row.freshness is None
    not_finite = [i for i in row.issues if "not finite" in i.message]
    assert len(not_finite) == 1 and not_finite[0].severity is Severity.HIGH

    row = _fold(build_log(_started_at(float("-inf")).records), _env(100.0))
    assert row.status.kind is StatusKind.PENDING and row.freshness is None
    assert not [i for i in row.issues if "not finite" in i.message]


def test_elapsed_skew_isolated_from_freshness(build_log):
    # FIRST started (by seq -- what read_elapsed reads) is stamped in the future;
    # the LATEST started (by seq -- what last_activity reads) is stamped cleanly
    # in the past. elapsed clamps to 0 with its own skew issue; freshness stays
    # a clean, un-flagged number -- the two skew checks are fully independent.
    s = _claim(500.0)  # first-by-seq, future
    s.started(10.0, "h2")  # latest-by-seq, past
    row = _fold(build_log(s.records), _env(100.0))
    assert row.elapsed == 0.0
    assert row.freshness == 90.0  # now(100) - la(10) -- clean, no clamping needed
    skew_issues = [i for i in row.issues if i.kind is IssueKind.SKEW_SUSPECTED]
    assert len(skew_issues) == 1  # only from elapsed's future check, not freshness's
    assert "run epoch" in skew_issues[0].message


def test_both_future_skew_fires_twice(build_log):
    # A `started` AND a `heartbeat` both stamped after `now`: the Row carries TWO
    # independent SKEW_SUSPECTED issues -- one from reconcile_status's freshness
    # `la > now` path (la is the heartbeat, the latest dated t) and one from
    # read_elapsed's own `started.t > now` path.
    s = _claim(500.0)
    s.heartbeat(900.0)
    row = _fold(build_log(s.records), _env(100.0))
    skew_issues = [i for i in row.issues if i.kind is IssueKind.SKEW_SUSPECTED]
    assert len(skew_issues) == 2
    assert "last activity" in skew_issues[0].message
    assert "run epoch" in skew_issues[1].message


def test_captured_once_per_frame(build_log, counting_env):
    # `now = env.clock()` is captured ONCE at the top of status_fold and threaded
    # everywhere -- not re-read per field. counting_env's clock returns base+n on
    # its n-th call; calls["n"] == 1 proves a single sample, and freshness/elapsed
    # both being consistent with that ONE now (base+1) proves they share it.
    env, calls = counting_env(base=100.0, threshold=60.0)
    s = _claim(90.0)
    s.commit(95.0)
    row = _fold(build_log(s.records), env)
    assert calls["n"] == 1
    now = 100.0 + calls["n"]  # 101.0, the single sampled `now`
    assert row.freshness == now - 95.0
    assert row.elapsed == now - 90.0


def test_t_exactly_zero_not_falsy(build_log):
    # t=0.0 is falsy in Python; read_elapsed must branch on `is None`, not
    # truthiness, or a genuinely-epoch-zero started would wrongly read as "no
    # started at all" (elapsed=None) instead of a real (large) elapsed.
    elapsed, issue = read_elapsed(build_log(_claim(0.0).records), now=50.0)
    assert elapsed == 50.0  # computed, not dropped
    assert issue is None


def test_clock_moves_backward(build_log):
    # each fold is stateless over now -- a backward observer clock flags skew on
    # a previously-clean run. Same log (one commit, t=150), folded TWICE with a LATER
    # now first and an EARLIER now second: the first fold is clean; the second, with
    # the observer's clock having regressed, reads the SAME activity as now being in
    # the future and raises SKEW_SUSPECTED.
    s = _claim(100.0)
    s.commit(150.0)
    ch = build_log(s.records)
    row1 = _fold(ch, _env(200.0))
    assert row1.status.kind is StatusKind.LIVE
    assert row1.freshness == 50.0
    assert row1.issues == ()

    row2 = _fold(ch, _env(100.0))
    assert row2.status.kind is StatusKind.LIVE
    assert row2.freshness == 0.0  # max(0, now - la) clamps the negative age
    skew_issues = [i for i in row2.issues if i.kind is IssueKind.SKEW_SUSPECTED]
    assert len(skew_issues) == 1
    assert "clock skew" in skew_issues[0].message


# --- Episodes / ordering ---------------------------------------------------


def test_two_live_episodes_no_stop(build_log):
    # Two claims with neither ended: the run's claim is the newest (the claim CAS orders
    # claims), and the fold reports its ordinary LIVE. 0.4.0 removes the false trigger
    # 0.3.0 had here (a crash+relaunch with no exit looked identical): the older claim
    # is stranded until a death report ends it, which is a record, not an inference.
    # FINDING: still no `conflicted` -- the cockpit cannot tell a stranded claim whose
    # process lives on (split-brain) from one whose process is gone; that needs the
    # liveness overlay's probe (docs/backlog/liveness-overlay.md).
    s = _claim(1.0)
    s.commit(2.0)
    s.started(3.0, "h2")
    s.commit(4.0)
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.kind is StatusKind.LIVE  # not CONFLICTED -- the gap
    assert row.episode == "h2"
    assert row.frontier == 1  # the newest claim's lineage: it started fresh
    assert row.issues == ()  # no flag at all for the unresolved earlier claim


def test_activity_after_terminal_no_restart(build_log):
    # Activity strictly after a claim's exit, no new claim: the exit is the verdict,
    # and the post-exit heartbeat does not resurrect it. In 0.4.0 this is a writer
    # bug (a claim writes nothing after its exit; a released worker fences itself), so
    # the 0.3.0 §4.1 row-3-vs-row-4 tension is settled upstream: terminal wins.
    # FINDING: the fresh age (freshness from the stray heartbeat) beside a terminal
    # status is still unflagged.
    s = _claim(1.0)
    s.stopped(2.0, completed=True)
    s.heartbeat(1000.0)
    row = _fold(build_log(s.records), _env(1001.0))
    assert row.status.kind is StatusKind.TERMINAL
    assert row.episode == "h1"  # unaffected by the post-terminal heartbeat
    assert row.freshness == 1.0  # looks fresh...
    assert row.issues == ()  # ...yet no conflict is ever raised


def test_out_of_order_t_seq_newest_per_topic_then_max(build_log):
    # Two commits where the LATER-appended (higher seq) one carries the SMALLER `t`:
    # last_activity takes the NEWEST record of each dated topic (by seq), then the
    # largest `t` among those few -- never a max over the whole log. So the seq-2
    # commit's t=300 is not a finalist; the frontier counts both commits.
    s = _claim(1.0)
    s.commit(300.0)  # seq 2, t=300
    s.commit(200.0)  # seq 3, t=200
    row = _fold(build_log(s.records), _env(250.0))
    assert row.frontier == 2
    assert row.freshness == 50.0  # now(250) - la(200, seq3's t) -- NOT now - 300
    assert row.issues == ()


def test_undischarged_stop_spans_episode(build_log):
    # 0.4.0: a stop is a request about the process, spent only by name (an exit's
    # `honored`, a withdrawal, a nak). A stop sent to the FIRST claim, which then died
    # without honoring it, is still pending -- the second claim's first drain reads it
    # and honors it -- so the cockpit shows BOTH stops. (0.3.0's cockpit dropped the
    # first as a ghost of a prior episode.)
    s = _claim(1.0)
    s.stop("webui:earlier")
    s.died(Outcome.LOST, "probe: h1", 4.0)
    s.started(5.0, "h2")
    s.stop("webui:current")
    row = _fold(build_log(s.records), _env(10.0))
    assert row.episode == "h2"
    assert [e.request_id for e in row.undischarged_stops] == ["webui:earlier", "webui:current"]


def test_undischarged_stop_before_any_episode_still_present(build_log):
    # No lifecycle.started at all: a stop with nothing to answer it yet still shows.
    s = Script()
    s.stop("webui:preepisode")
    row = _fold(build_log(s.records), _env(10.0))
    assert row.episode is None
    assert len(row.undischarged_stops) == 1
    assert row.undischarged_stops[0].request_id == "webui:preepisode"


def test_stopped_error_detail_retained_on_row(build_log):
    # Faithful-representation: RunResult.error survives onto the Row's status,
    # not just the closed Outcome -- the operator-facing diagnostic text.
    s = _claim(1.0)
    s.stopped(2.0, completed=False, error="OOM killed")
    row = _fold(build_log(s.records), _env(10.0))
    assert row.status.detail == "OOM killed"


@pytest.mark.parametrize("version", ["0.3.0", "0.2.0"])
def test_an_older_format_log_is_a_loud_row_not_a_crash(tmp_path, version):
    # The cockpit on 0.4.0 refuses every older-format log, by design (LogFormatMismatch,
    # naming `runstate migrate`): a loud typed row, never a crash and never `missing`.
    _sqlite_run(tmp_path, "old", _claim(1.0))
    move_log(tmp_path, "old", to=log_path(tmp_path, "old", version=version))
    row = _open(("old", str(tmp_path), "sqlite"), _env(10.0))
    assert row.status.kind is StatusKind.LOG_FORMAT and row.status.label == f"format {version}"
    assert row.severity is Severity.HIGH
    assert "runstate migrate" in row.issues[0].message
