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
