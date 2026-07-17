from runstate.observables import Outcome

from runstate_tui.types import Severity, IssueKind, Issue, Status, StatusKind


def test_severity_orders_and_maxes():
    assert Severity.OK < Severity.INFO < Severity.MEDIUM < Severity.HIGH
    assert max(Severity.INFO, Severity.HIGH) is Severity.HIGH


def test_issue_is_a_frozen_value():
    a = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    b = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    assert a == b
    assert a.detail is None


def test_terminal_wraps_outcome_and_labels_completed_as_done():
    s = Status.terminal(Outcome.COMPLETED)
    assert s.kind is StatusKind.TERMINAL
    assert s.outcome is Outcome.COMPLETED
    assert s.label == "done"
    assert Status.terminal(Outcome.KILLED).label == "killed"


def test_unknown_outcome_renders_honestly_not_a_default():
    # a future/unknown Outcome member must render via its own wire string
    class FakeOutcome:
        value = "suspended"
    s = Status.terminal(FakeOutcome())
    assert s.label == "suspended"


def test_status_severity_map():
    assert Status.unreadable().severity is Severity.HIGH
    assert Status.conflicted().severity is Severity.MEDIUM
    assert Status.pending().severity is Severity.INFO
    assert Status.live().severity is Severity.OK
    assert Status.terminal(Outcome.COMPLETED).severity is Severity.OK


from runstate_tui.types import Row


def _bare_row(**kw):
    base = dict(status=Status.live(), frontier=10, freshness=1.0, value=None,
                elapsed=5.0, episode=None, issues=())
    base.update(kw)
    return Row(**base)


def test_row_is_a_frozen_value_for_the_singleton_test():
    assert _bare_row() == _bare_row()


def test_row_severity_is_max_of_status_and_issues():
    torn = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="torn")
    assert _bare_row().severity is Severity.OK
    assert _bare_row(issues=(torn,)).severity is Severity.MEDIUM
    assert _bare_row(status=Status.unreadable()).severity is Severity.HIGH
