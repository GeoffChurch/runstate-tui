from runstate_tui.types import Severity, IssueKind, Issue


def test_severity_orders_and_maxes():
    assert Severity.OK < Severity.INFO < Severity.MEDIUM < Severity.HIGH
    assert max(Severity.INFO, Severity.HIGH) is Severity.HIGH


def test_issue_is_a_frozen_value():
    a = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    b = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    assert a == b
    assert a.detail is None
