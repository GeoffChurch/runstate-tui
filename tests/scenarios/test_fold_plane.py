"""Curated fold-plane basis (docs/superpowers/notes/2026-07-18-fixture-basis.md,
.superpowers/sdd/task-2-brief.md): adversarial + finding-locking scenarios for the
PURE fold (lifecycle/verdict, integrity/corruption, time/clocks, episodes/ordering).

Every assertion here was set from the fold's ACTUAL output (verified empirically,
not guessed) -- a regression test's "expected" IS the current behavior. Where a
scenario exposes a known-deferred gap (per the notes doc's "real CODE findings"),
the test still locks the CURRENT behavior and names the gap in a `# FINDING:`
comment; it does not try to fix it.

Fixture landmine (see the notes doc): `lifecycle.stopped` / `launcher.terminated`
are verdict-plane topics needing a schema-valid body -- a bare body trips an
incidental MALFORMED issue unrelated to what a given scenario is pinning."""

from __future__ import annotations

import sqlite3
import time

import pytest
from runstate import open_channel
from runstate.observables import Outcome

from runstate_tui import open_and_fold, status_fold
from runstate_tui.env import Env
from runstate_tui.fold import read_elapsed, reconcile_status
from runstate_tui.types import IssueKind, Severity, StatusKind
from tests.helpers import corrupt_seq


def _env(now, **kw):
    return Env(clock=lambda: now, stuck_threshold=kw.pop("stuck_threshold", 60.0), **kw)


def _sqlite_run(tmp_path, run_id, records):
    """Write `records` (3- or 4-tuples: body, topic, name[, request_id]) to a
    fresh sqlite-backed run and return its RunRef `(run_id, root, "sqlite")` --
    the integrity scenarios below need a real file to tear with `corrupt_seq`
    or overlay with `foreign_db`, unlike `build_log`'s in-memory channel."""
    writer = open_channel(run_id, root=tmp_path, backend="sqlite")
    for record in records:
        body, topic, name, *rest = record
        writer.send(body, topic=topic, name=name, request_id=rest[0] if rest else None)
    writer.close()
    return (run_id, str(tmp_path), "sqlite")


# --- Lifecycle / verdict ------------------------------------------------------


def test_stale_threshold_boundary(build_log):
    # t=0 not falsy (see also test_t_exactly_zero_not_falsy); three clocks straddle
    # the threshold to lock the `<=` (inclusive) freshness boundary in FreshnessSignal.
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 0.0}, "lifecycle.heartbeat", None)])
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
    # A heartbeat FAR after a clean stop must not resurrect liveness: peek_terminal
    # wins unconditionally over last_activity. freshness is still computed (from the
    # stray heartbeat, the largest dated t) -- large, but the status stays TERMINAL.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": True, "error": None, "final_step": 3, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
            ({"step": 99, "consumed_seq": 0, "t": 1_000_000.0}, "lifecycle.heartbeat", None),
        ]
    )
    row = status_fold(ch, _env(2_000_000.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcome is Outcome.COMPLETED
    assert row.freshness == 1_000_000.0  # large -- from the stray heartbeat -- yet terminal wins
    assert row.issues == ()


def test_terminal_superseded_by_new_episode(build_log):
    # started(h1) -> stopped -> started(h2), no further activity: the new episode's
    # CLAIM alone supersedes the old terminal (peek_terminal's episode-aware rule) --
    # LIVE again, episode is h2, but elapsed still dates from the FIRST started.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": True, "error": None, "final_step": 3, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
            ({"handle": "h2", "t": 3.0}, "lifecycle.started", None),
        ]
    )
    row = status_fold(ch, _env(10.0))
    assert row.status.kind is StatusKind.LIVE
    assert row.episode == "h2"
    assert row.elapsed == 9.0  # now(10) - FIRST started.t(1), not h2's t(3)
    assert row.freshness == 7.0  # now(10) - la(3, h2's started -- the latest dated t)
    assert row.issues == ()


def test_terminal_errored_via_stopped(build_log):
    # completed=False + error="OOM" -> Outcome.ERRORED. peek_terminal returns a
    # RunResult carrying `.error`, but status_fold only threads `.outcome` into
    # Status.terminal(...) -- the error text itself is dropped, nowhere on the Row.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": False, "error": "OOM", "final_step": None, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
        ]
    )
    row = status_fold(ch, _env(10.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcome is Outcome.ERRORED
    # FINDING: RunResult.error diagnostic is dropped -- Status.terminal() only threads .outcome
    assert "OOM" not in repr(row)  # the error string is nowhere on the Row


def test_error_empty_string_is_errored(build_log):
    # error="" is falsy but `is not None` -- peek_terminal's outcome check is
    # `s.error is not None`, not truthiness, so an empty-string error still reads
    # as ERRORED rather than falling through to the completed/preempted branches.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": False, "error": "", "final_step": 3, "t": 5.0},
                "lifecycle.stopped",
                None,
            ),
        ]
    )
    row = status_fold(ch, _env(10.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcome is Outcome.ERRORED


def test_schema_invalid_stopped_completed_with_error(build_log):
    # completed=True + error set violates Stopped's own invariant (completed => no
    # error) -> verdict_parse raises MalformedRecordError -> guarded() degrades it
    # to a MALFORMED issue instead of a terminal verdict. Status falls through to a
    # LATER heartbeat's LIVE (degrades, doesn't collapse the whole row), and frontier
    # comes from that same heartbeat.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": True, "error": "OOM", "final_step": 3, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
            ({"step": 5, "consumed_seq": 0, "t": 3.0}, "lifecycle.heartbeat", None),
        ]
    )
    row = status_fold(ch, _env(10.0))
    assert row.status.kind is StatusKind.LIVE  # degraded past the invalid stopped
    assert row.frontier == 5  # from the heartbeat
    assert row.freshness == 7.0  # now(10) - heartbeat.t(3)
    malformed = [i for i in row.issues if i.kind is IssueKind.MALFORMED]
    assert len(malformed) == 1
    assert malformed[0].seq == 2
    assert malformed[0].severity is Severity.MEDIUM
    assert malformed[0].detail is not None and malformed[0].detail.startswith(
        "MalformedRecordError"
    )


# --- Integrity / corruption ---------------------------------------------------


def test_corrupt_at_seq_1(tmp_path):
    ref = _sqlite_run(
        tmp_path, "cseq1", [({"handle": "h1", "t": 100.0}, "lifecycle.started", None)]
    )
    corrupt_seq(tmp_path, "cseq1", 1)
    row = open_and_fold(ref, _env(150.0))
    assert row.status.kind is StatusKind.CORRUPT
    assert len(row.issues) == 1
    assert row.issues[0].kind is IssueKind.CORRUPT
    assert row.issues[0].seq == 1
    assert row.severity is Severity.HIGH


def test_corrupt_deep(tmp_path):
    # ~2000 records, torn near the middle: undischarged_stops's unfiltered
    # `read(topics=[CONTROL_STOP])` (no `.latest()`, no limit) is the fold read
    # that actually decodes every record on the topic -- so it hits the tear,
    # raises json.JSONDecodeError, and open_and_fold degrades to CORRUPT via
    # locate_torn_seq's one-row-at-a-time scan. Assert it terminates promptly
    # (a generous bound, not a tight timing pin) and lands the RIGHT seq --
    # not a full eager re-decode of the whole 2000-record tail.
    records = [({}, "control.stop", None) for _ in range(2000)]
    ref = _sqlite_run(tmp_path, "cdeep", records)
    corrupt_seq(tmp_path, "cdeep", 1000)
    started = time.monotonic()
    row = open_and_fold(ref, _env(3000.0))
    elapsed_wall = time.monotonic() - started
    assert elapsed_wall < 5.0  # terminates -- no full re-decode blowup
    assert row.status.kind is StatusKind.CORRUPT
    assert row.issues[0].seq == 1000


def test_corrupt_value_wrong_name_invisible(tmp_path):
    # FINDING: corruption-invisibility -- a torn `value` record whose `name` is
    # NOT the fold's objective is filtered out at the SQL level (name != "loss")
    # and never decoded at all. The row reads as fully clean: no corrupt status,
    # no issue, zero signal that a corrupted record exists anywhere on this log.
    ref = _sqlite_run(
        tmp_path,
        "cwrong",
        [
            ({"handle": "local://h/1", "t": 1.0}, "lifecycle.started", None),
            ({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat", None),
            ({"value": 0.9, "step": 1, "t": 2.0}, "value", "acc"),  # torn below, name != objective
        ],
    )
    corrupt_seq(tmp_path, "cwrong", 3)
    run_id, root, backend = ref
    ch = open_channel(run_id, root=root, backend=backend)
    try:
        row = status_fold(ch, _env(10.0, objective="loss"))
    finally:
        ch.close()
    assert row.status.kind is StatusKind.LIVE
    assert row.issues == ()  # fully clean -- the corruption is invisible
    assert row.value is None  # objective="loss" was never written
    assert row.frontier == 1
    assert row.episode == "local://h/1"


def test_corrupt_discharged_stop_invisible(tmp_path):
    # FINDING: corruption-invisibility -- undischarged_stops reads control.stop
    # only AFTER the latest `stopped`'s seq; a torn but already-discharged stop
    # sits below that floor and is never fetched, let alone decoded. A clean
    # terminal Row with zero issues, no trace the log ever held a torn record.
    ref = _sqlite_run(
        tmp_path,
        "cstop",
        [
            ({"handle": "local://h/1", "t": 1.0}, "lifecycle.started", None),
            ({}, "control.stop", None, "webui:s1"),  # torn below, then discharged by the stop
            (
                {"completed": True, "error": None, "final_step": 3, "t": 5.0},
                "lifecycle.stopped",
                None,
            ),
        ],
    )
    corrupt_seq(tmp_path, "cstop", 2)
    row = open_and_fold(ref, _env(10.0))
    assert row.status.kind is StatusKind.TERMINAL and row.status.outcome is Outcome.COMPLETED
    assert row.undischarged_stops == ()
    assert row.issues == ()  # fully clean -- the corruption is invisible


def test_alien_started_body_malformed(tmp_path):
    # A committed-but-alien `lifecycle.started` body (valid JSON, non-dict `42`)
    # is a distinct failure class from byte-torn: guarded() catches the resulting
    # AttributeError as MALFORMED, not CORRUPT -- the run survives. In practice
    # THREE separate fold reads each independently touch this one alien record
    # (last_activity, read_elapsed's _started_t, and the episode handle read) --
    # three MALFORMED issues, all reporting the same underlying AttributeError.
    ref = _sqlite_run(
        tmp_path, "alienst", [({"handle": "h1", "t": 100.0}, "lifecycle.started", None)]
    )
    corrupt_seq(tmp_path, "alienst", 1, literal="42")
    row = open_and_fold(ref, _env(150.0))
    assert row.status.kind is StatusKind.PENDING  # no other dated activity -- not a crash
    malformed = [i for i in row.issues if i.kind is IssueKind.MALFORMED]
    assert len(malformed) == 3
    assert all(i.detail is not None and i.detail.startswith("AttributeError") for i in malformed)
    assert row.frontier is None and row.elapsed is None and row.episode is None


def test_foreign_valid_db_reads_pending(foreign_db):
    # FINDING: §8 foreign-db gap -- a VALID sqlite file with an alien (non-runstate)
    # schema is not detected as foreign at all: open_channel's `CREATE TABLE IF NOT
    # EXISTS log` silently adds the runstate schema to someone else's database, and
    # the fold then reads that fresh, empty `log` table as an ordinary bare PENDING
    # run -- indistinguishable from a never-started one. The open MUTATES the file.
    row = open_and_fold(foreign_db, _env(150.0))
    assert row.status.kind is StatusKind.PENDING
    assert row.issues == ()
    run_id, root, _backend = foreign_db
    conn = sqlite3.connect(str(root) + f"/{run_id}.db")
    try:
        tables = {
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        conn.close()
    assert "log" in tables  # the file now carries a runstate schema it never had
    assert "unrelated" in tables  # the alien table is untouched, not replaced


def test_malformed_value_silently_tolerated(build_log):
    # FINDING: malformed surfaces ONLY from peek_terminal; tolerant reads
    # (value/heartbeat) silently return None -- a `value` record missing its own
    # `"value"` key isn't a verdict-plane record (read_value never raises,
    # MalformedRecordError-style), so it's read as a present-but-null value with
    # NO issue at all, not surfaced as MALFORMED the way a bad `stopped` would be.
    ch = build_log([({"step": 4, "t": 140.0}, "value", "loss")])
    row = status_fold(ch, _env(200.0, objective="loss"))
    assert row.value == ("loss", None, 4)
    assert row.status.kind is StatusKind.PENDING  # unaffected by the malformed value
    assert row.issues == ()


# --- Time / clocks -------------------------------------------------------------


def test_nan_heartbeat_not_falsely_live(build_log):
    # A NaN `t` must not read as freshness=0.0 -> LIVE (the false-fresh trap):
    # last_activity's non-finite guard forces PENDING with a loud HIGH issue.
    ch = build_log(
        [({"step": 1, "consumed_seq": 0, "t": float("nan")}, "lifecycle.heartbeat", None)]
    )
    row = status_fold(ch, _env(100.0))
    assert row.status.kind is StatusKind.PENDING
    assert row.freshness is None
    matches = [
        i
        for i in row.issues
        if i.kind is IssueKind.MALFORMED
        and i.severity is Severity.HIGH
        and "not finite" in i.message
    ]
    assert len(matches) == 1
    assert "nan" in matches[0].detail.lower()


@pytest.mark.parametrize("t", [float("inf"), float("-inf")])
def test_inf_asymmetric(build_log, t):
    # +inf would otherwise read as freshness=0.0 -> LIVE; -inf as freshness=+inf ->
    # STALE. Both are silently wrong for a garbage clock: both must land PENDING
    # with the same non-finite MALFORMED issue -- no directional asymmetry.
    ch = build_log([({"step": 1, "consumed_seq": 0, "t": t}, "lifecycle.heartbeat", None)])
    row = status_fold(ch, _env(100.0))
    assert row.status.kind is StatusKind.PENDING
    assert row.freshness is None
    assert len(row.issues) == 1
    assert row.issues[0].kind is IssueKind.MALFORMED and row.issues[0].severity is Severity.HIGH
    assert "not finite" in row.issues[0].message


def test_elapsed_skew_isolated_from_freshness(build_log):
    # FIRST started (by seq -- what read_elapsed reads) is stamped in the future;
    # the LATEST started (by seq -- what last_activity reads) is stamped cleanly
    # in the past. elapsed clamps to 0 with its own skew issue; freshness stays
    # a clean, un-flagged number -- the two skew checks are fully independent.
    ch = build_log(
        [
            ({"handle": "h1", "t": 500.0}, "lifecycle.started", None),  # first-by-seq, future
            ({"handle": "h2", "t": 10.0}, "lifecycle.started", None),  # latest-by-seq, past
        ]
    )
    row = status_fold(ch, _env(100.0))
    assert row.elapsed == 0.0
    assert row.freshness == 90.0  # now(100) - la(10) -- clean, no clamping needed
    skew_issues = [i for i in row.issues if i.kind is IssueKind.SKEW_SUSPECTED]
    assert len(skew_issues) == 1  # only from elapsed's future check, not freshness's
    assert "run epoch" in skew_issues[0].message


def test_both_future_skew_fires_twice(build_log):
    # A `started` AND a `heartbeat` both stamped after `now`: the Row carries TWO
    # independent SKEW_SUSPECTED issues -- one from reconcile_status's freshness
    # `la > now` path (la is the heartbeat, the latest dated t) and one from
    # read_elapsed's own `started.t > now` path. The two skew checks don't share
    # or dedup an issue even when they're both tripped by the same future clock.
    ch = build_log(
        [
            ({"handle": "h1", "t": 500.0}, "lifecycle.started", None),
            ({"step": 1, "consumed_seq": 0, "t": 900.0}, "lifecycle.heartbeat", None),
        ]
    )
    row = status_fold(ch, _env(100.0))
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
    ch = build_log(
        [
            ({"handle": "h1", "t": 90.0}, "lifecycle.started", None),
            ({"step": 3, "consumed_seq": 0, "t": 95.0}, "lifecycle.heartbeat", None),
        ]
    )
    row = status_fold(ch, env)
    assert calls["n"] == 1
    now = 100.0 + calls["n"]  # 101.0, the single sampled `now`
    assert row.freshness == now - 95.0
    assert row.elapsed == now - 90.0


def test_t_exactly_zero_not_falsy(build_log):
    # t=0.0 is falsy in Python; read_elapsed must branch on `is None`, not
    # truthiness, or a genuinely-epoch-zero started would wrongly read as "no
    # started at all" (elapsed=None) instead of a real (large) elapsed.
    ch = build_log([({"handle": "h1", "t": 0.0}, "lifecycle.started", None)])
    elapsed, issue = read_elapsed(ch, now=50.0)
    assert elapsed == 50.0  # computed, not dropped
    assert issue is None


def test_clock_moves_backward(build_log):
    # each fold is stateless over now -- a backward observer clock flags skew on
    # a previously-clean run. Same log (one heartbeat, t=150), folded TWICE with
    # a LATER now first and an EARLIER now second: the first fold is clean; the
    # second, with the observer's clock having regressed, reads the SAME activity
    # as now being in the future and raises SKEW_SUSPECTED -- nothing is cached
    # or carried between the two folds, the flip comes purely from `now`.
    ch = build_log([({"step": 1, "consumed_seq": 0, "t": 150.0}, "lifecycle.heartbeat", None)])
    row1 = status_fold(ch, _env(200.0))
    assert row1.status.kind is StatusKind.LIVE
    assert row1.freshness == 50.0
    assert row1.issues == ()

    row2 = status_fold(ch, _env(100.0))
    assert row2.status.kind is StatusKind.LIVE
    assert row2.freshness == 0.0  # max(0, now - la) clamps the negative age
    skew_issues = [i for i in row2.issues if i.kind is IssueKind.SKEW_SUSPECTED]
    assert len(skew_issues) == 1
    assert "clock skew" in skew_issues[0].message


# --- Episodes / ordering ---------------------------------------------------


def test_two_live_episodes_no_stop(build_log):
    # FINDING: conflicted unimplemented (§4.1 row 3, notes doc finding #1) -- two
    # started episodes with no stop between them is exactly the "two live
    # episodes" shape the spec's Status.conflicted() exists for, but status_fold
    # never calls it (dead code): the fold just reports the latest episode's
    # ordinary LIVE, silently discarding the fact that an earlier claim was never
    # resolved.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            ({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat", None),
            ({"handle": "h2", "t": 3.0}, "lifecycle.started", None),
            ({"step": 2, "consumed_seq": 0, "t": 4.0}, "lifecycle.heartbeat", None),
        ]
    )
    row = status_fold(ch, _env(10.0))
    assert row.status.kind is StatusKind.LIVE  # not CONFLICTED -- the gap
    assert row.status.kind is not StatusKind.CONFLICTED
    assert row.episode == "h2"
    assert row.issues == ()  # no flag at all for the unresolved earlier episode


def test_activity_after_terminal_no_restart(build_log):
    # FINDING: conflicted / §4.1 row3-vs-row4 tension (notes doc finding #1) --
    # activity strictly after a terminal, with no intervening restart, is the
    # OTHER conflicted trigger the spec names (§4.1 row 3) -- and it directly
    # tensions with row 4's "terminal wins over a stray heartbeat" (locked above
    # in test_terminal_wins_over_stray_heartbeat), which the current code
    # implements instead: peek_terminal wins unconditionally, no restart, no
    # flag. freshness alone (computed from the stray heartbeat) would misleadingly
    # suggest a live run; status stays flatly TERMINAL with zero issues.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"completed": True, "error": None, "final_step": 5, "t": 2.0},
                "lifecycle.stopped",
                None,
            ),
            ({"step": 9, "consumed_seq": 0, "t": 1000.0}, "lifecycle.heartbeat", None),
        ]
    )
    row = status_fold(ch, _env(1001.0))
    assert row.status.kind is StatusKind.TERMINAL
    assert row.status.kind is not StatusKind.CONFLICTED
    assert row.episode == "h1"  # unaffected by the post-terminal heartbeat
    assert row.freshness == 1.0  # looks fresh...
    assert row.issues == ()  # ...yet no conflict is ever raised


def test_out_of_order_t_seq_latest_wins(build_log):
    # Two heartbeats where the LATER-appended (higher seq) one carries the
    # SMALLER `t`: last_activity/progress both key off `.latest()` -- the
    # highest-seq record for the topic -- never `max(t)`. Locks that ordering is
    # seq-latest, not t-largest.
    ch = build_log(
        [
            ({"handle": "h1", "t": 1.0}, "lifecycle.started", None),
            (
                {"step": 5, "consumed_seq": 0, "t": 300.0},
                "lifecycle.heartbeat",
                None,
            ),  # seq2, t=300
            (
                {"step": 9, "consumed_seq": 0, "t": 200.0},
                "lifecycle.heartbeat",
                None,
            ),  # seq3, t=200
        ]
    )
    row = status_fold(ch, _env(250.0))
    assert row.frontier == 9  # progress() reads ONLY the latest (seq3) heartbeat's step
    assert row.freshness == 50.0  # now(250) - la(200, seq3's t) -- NOT now - 300
    assert row.issues == ()
