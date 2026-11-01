import pytest
from runstate import Outcome, Tier

from runstate_tui.types import Issue, IssueKind, Row, Severity, Status, StatusKind


def _term(*outcomes, tier=Tier.EXIT):
    return Status.terminal(frozenset(outcomes), tier, detail=None)


def test_severity_orders_and_maxes():
    assert Severity.OK < Severity.INFO < Severity.MEDIUM < Severity.HIGH
    assert max(Severity.INFO, Severity.HIGH) is Severity.HIGH


def test_issue_is_a_frozen_value():
    a = Issue(
        kind=IssueKind.MALFORMED,
        severity=Severity.MEDIUM,
        message="record malformed at seq 4012",
        seq=4012,
    )
    b = Issue(
        kind=IssueKind.MALFORMED,
        severity=Severity.MEDIUM,
        message="record malformed at seq 4012",
        seq=4012,
    )
    assert a == b
    assert a.detail is None


def test_terminal_wraps_outcomes_and_labels_completed_as_done():
    s = _term(Outcome.COMPLETED)
    assert s.kind is StatusKind.TERMINAL
    assert s.outcomes == {Outcome.COMPLETED} and s.tier is Tier.EXIT
    assert s.label == "done"
    assert _term(Outcome.KILLED).label == "killed"
    assert _term(Outcome.LOST, tier=Tier.DIED).label == "lost"


def test_several_outcomes_are_all_labelled_in_a_fixed_order():
    # death reports that disagree: every outcome, none chosen, in display order
    s = _term(Outcome.KILLED, Outcome.PREEMPTED, Outcome.ERRORED, tier=Tier.DIED)
    assert s.label == "preempted/errored/killed"


def test_a_terminal_status_has_an_outcome():
    with pytest.raises(ValueError):
        Status.terminal(frozenset(), Tier.EXIT, detail=None)


def test_unknown_outcome_renders_honestly_not_a_default():
    # a future/unknown Outcome member must render via its own wire string
    class FakeOutcome:
        value = "suspended"

    s = Status.terminal(frozenset({FakeOutcome()}), Tier.EXIT, detail=None)
    assert s.label == "suspended"


def test_a_log_in_another_format_is_a_loud_typed_status():
    s = Status.log_format("0.3.0", "migrate it (runstate migrate)")
    assert s.kind is StatusKind.LOG_FORMAT and s.severity is Severity.HIGH
    assert s.label == "format 0.3.0" and s.detail == "migrate it (runstate migrate)"
    assert Status.log_format(None, "onboard it").label == "unversioned"


def test_status_severity_map():
    assert Status.unreadable().severity is Severity.HIGH
    assert Status.conflicted().severity is Severity.MEDIUM
    assert Status.pending().severity is Severity.INFO
    assert Status.live().severity is Severity.OK
    assert _term(Outcome.COMPLETED).severity is Severity.OK


def test_corrupt_status_is_loud_not_reused_unreadable():
    s = Status.corrupt()
    assert s.kind is StatusKind.CORRUPT
    assert s.label == "corrupt"
    assert s.severity is Severity.HIGH


def _bare_row(**kw):
    base = dict(
        status=Status.live(),
        frontier=10,
        freshness=1.0,
        value=None,
        elapsed=5.0,
        episode=None,
        undischarged_stops=(),
        live_demand=(),
        issues=(),
    )
    base.update(kw)
    return Row(**base)


def test_row_is_a_frozen_value_for_the_singleton_test():
    assert _bare_row() == _bare_row()


def test_row_severity_is_max_of_status_and_issues():
    torn = Issue(kind=IssueKind.MALFORMED, severity=Severity.MEDIUM, message="torn")
    assert _bare_row().severity is Severity.OK
    assert _bare_row(issues=(torn,)).severity is Severity.MEDIUM
    assert _bare_row(status=Status.unreadable()).severity is Severity.HIGH
