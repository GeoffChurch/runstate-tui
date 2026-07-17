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
