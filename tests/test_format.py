from runstate_tui.format import format_row
from runstate_tui.types import Issue, IssueKind, Row, Severity, Status


def _row(**kw):
    base = dict(
        status=Status.live(),
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        issues=(),
    )
    base.update(kw)
    return Row(**base)


def test_format_row_full_quintet():
    row = _row(
        status=Status.live(), frontier=7, freshness=10.0, value=("loss", 0.03, 7), elapsed=50.0
    )
    text = format_row(row)
    assert "live" in text
    assert "step 7" in text
    assert "10s ago" in text
    assert "loss=0.03 @ 7" in text
    assert "ran 50s" in text


def test_format_row_missing_is_just_the_label():
    assert format_row(_row(status=Status.missing())) == "missing"


def test_format_row_surfaces_issue_badges():
    torn = Issue(
        kind=IssueKind.TORN,
        severity=Severity.MEDIUM,
        message="log torn at seq 4012",
        seq=4012,
    )
    text = format_row(_row(frontier=3, issues=(torn,)))
    assert "⚠ log torn at seq 4012" in text
