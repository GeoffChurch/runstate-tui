from runstate.observables import progress
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
