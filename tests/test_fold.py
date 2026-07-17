from runstate.observables import peek_terminal, progress
from runstate_tui.fold import guarded
from runstate_tui.types import IssueKind, Severity


def test_guarded_passes_through_a_clean_observable(build_log):
    ch = build_log([({"step": 5, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    value, issue = guarded(progress, ch)
    assert value == 5 and issue is None


def test_guarded_degrades_a_torn_read_to_a_torn_issue_with_seq(torn_sqlite_channel):
    ch = torn_sqlite_channel(
        [({"step": 5, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)],
        torn_seq=1,
    )
    value, issue = guarded(progress, ch)
    assert value is None
    assert issue.kind is IssueKind.TORN and issue.severity is Severity.MEDIUM
    assert issue.seq == 1  # located in-tree, no upstream ask


def test_guarded_recovers_seq_from_a_schema_invalid_record_via_exc_seq(build_log):
    # valid JSON, invalid Stopped schema (missing error/final_step/t) -- peek_terminal
    # raises MalformedRecordError, which locate_torn_seq (JSON/DB decode errors only)
    # cannot find; guarded's `exc.seq` fast path is the only recovery for this case.
    ch = build_log([({"completed": True}, "lifecycle.stopped", None)])
    value, issue = guarded(peek_terminal, ch)
    assert value is None
    assert issue.kind is IssueKind.TORN
    assert issue.seq == 1


from runstate.observables import Outcome
from runstate_tui.env import Env
from runstate_tui.fold import reconcile_status
from runstate_tui.types import StatusKind


def _env(now, **kw):
    return Env(clock=lambda: now, stuck_threshold=60.0, **kw)


def test_terminal_wins(build_log):
    ch = build_log([
        ({"handle": "local://h/1", "t": 1.0}, "lifecycle.started", None),
        ({"completed": True, "error": None, "final_step": 3, "t": 2.0}, "lifecycle.stopped", None),
    ])
    status, freshness, issues = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.TERMINAL and status.outcome is Outcome.COMPLETED
    assert issues == []


def test_pending_when_no_dated_activity(build_log):
    ch = build_log([])
    status, freshness, _ = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.PENDING and freshness is None


def test_live_then_stale_by_freshness(build_log):
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 100.0}, "lifecycle.heartbeat", None)])
    status, freshness, _ = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.LIVE and freshness == 0.0
    assert reconcile_status(ch, _env(1000.0), now=1000.0)[0].kind is StatusKind.STALE


from runstate_tui.fold import read_value, read_elapsed


def test_value_is_named_and_none_without_an_objective(build_log):
    ch = build_log([
        ({"value": 0.5, "step": 4, "t": 1.0}, "value", "loss"),
        ({"value": 0.9, "step": 4, "t": 1.0}, "value", "acc"),
    ])
    assert read_value(ch, objective=None) is None            # never nameless
    assert read_value(ch, objective="loss") == ("loss", 0.5, 4)
    assert read_value(ch, objective="missing") is None


def test_elapsed_is_wall_age_from_first_started(build_log):
    ch = build_log([
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"handle": "local://h/2", "t": 200.0}, "lifecycle.started", None),  # a later episode
    ])
    elapsed, issue = read_elapsed(ch, now=250.0)
    assert elapsed == 150.0 and issue is None  # from the FIRST started (100.0), not the latest


def test_elapsed_none_without_a_started(build_log):
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    assert read_elapsed(ch, now=9.0) == (None, None)


def test_elapsed_never_negative_and_flags_skew(build_log):
    ch = build_log([({"handle": "local://h/1", "t": 500.0}, "lifecycle.started", None)])
    elapsed, issue = read_elapsed(ch, now=100.0)  # started stamped in the future
    assert elapsed == 0.0
    assert issue.kind is IssueKind.SKEW_SUSPECTED
