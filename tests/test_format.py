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
        undischarged_stops=(),
        live_demand=(),
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
        kind=IssueKind.MALFORMED,
        severity=Severity.MEDIUM,
        message="record malformed at seq 4012",
        seq=4012,
    )
    text = format_row(_row(frontier=3, issues=(torn,)))
    assert "⚠ record malformed at seq 4012" in text


def test_format_row_flags_undischarged_stops():
    from runstate.channel import Envelope

    stop = Envelope(seq=5, topic="control.stop", name=None, request_id="webui:s", body={})
    text = format_row(_row(frontier=3, undischarged_stops=(stop,)))
    assert "⏹1" in text


def test_format_envelope_is_a_compact_one_liner():
    from runstate.channel import Envelope

    from runstate_tui.format import format_envelope

    e = Envelope(seq=4, topic="control.stop", name=None, request_id="webui:x", body={})
    line = format_envelope(e)
    assert "4" in line and "control.stop" in line and "webui:x" in line


def test_format_detail_shows_all_factors_and_lists():
    from runstate.channel import Envelope

    from runstate_tui.format import format_detail

    stop = Envelope(seq=5, topic="control.stop", name=None, request_id="webui:s", body={})
    row = _row(
        frontier=7,
        value=("loss", 0.03, 7),
        elapsed=50.0,
        episode="local://h/1",
        undischarged_stops=(stop,),
    )
    text = format_detail(row)
    assert "local://h/1" in text  # episode
    assert "loss" in text  # value
    assert "webui:s" in text  # the undischarged stop
    assert "undischarged stop" in text.lower()


def test_format_detail_shows_live_demand_and_issues_no_episode():
    from runstate.channel import Envelope

    from runstate_tui.format import format_detail

    sub = Envelope(seq=3, topic="control.subscribe", name=None, request_id="webui:sub1", body={})
    torn = Issue(
        kind=IssueKind.MALFORMED,
        severity=Severity.MEDIUM,
        message="record malformed at seq 4012",
        seq=4012,
    )
    row = _row(live_demand=(sub,), issues=(torn,))
    text = format_detail(row)
    assert "episode: —" in text
    assert "webui:sub1" in text  # the live demand
    assert "live demand" in text.lower()
    assert "record malformed at seq 4012" in text
