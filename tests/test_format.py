from runstate.observables import Outcome

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


def test_format_row_appends_terminal_detail_when_present():
    row = _row(status=Status.terminal(Outcome.ERRORED, detail="OOM killed"))
    text = format_row(row)
    assert text.startswith("errored: OOM killed")


def test_format_row_omits_detail_suffix_when_absent():
    row = _row(status=Status.terminal(Outcome.COMPLETED))
    assert format_row(row) == "done"


def test_format_row_renders_corrupt_status_prominently():
    torn = Issue(
        kind=IssueKind.CORRUPT,
        severity=Severity.HIGH,
        message="log corrupt at seq 1",
        seq=1,
    )
    row = _row(status=Status.corrupt(), issues=(torn,))
    text = format_row(row)
    assert text.startswith("corrupt")  # the status label leads — loud, not buried
    assert "⚠ log corrupt at seq 1" in text
    assert row.severity == Severity.HIGH


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
    assert "■1" in text


def test_format_envelope_is_a_compact_one_liner():
    from runstate.channel import Envelope

    from runstate_tui.format import format_envelope

    e = Envelope(seq=4, topic="control.stop", name=None, request_id="webui:x", body={})
    line = format_envelope(e)
    assert "4" in line and "control.stop" in line and "webui:x" in line


def test_format_envelope_escapes_embedded_newline_in_request_id():
    # A corrupted/adversarial request_id containing raw control chars must NOT
    # split one envelope across multiple RichLog lines (invariant #6) -- the
    # whole assembled line is escaped, not just body, so request_id is covered.
    from runstate.channel import Envelope

    from runstate_tui.format import format_envelope

    e = Envelope(seq=4, topic="control.stop", name=None, request_id="webui:evil\nline2", body={})
    line = format_envelope(e)
    assert "\n" not in line
    assert "webui:evil\\nline2" in line


def test_format_envelope_normal_dict_body_unchanged():
    # A normal dict body renders exactly as before -- the final whole-line
    # escape must not introduce spurious escaping for the common case.
    from runstate.channel import Envelope

    from runstate_tui.format import format_envelope

    e = Envelope(seq=4, topic="control.stop", name=None, request_id="webui:x", body={"a": 1})
    line = format_envelope(e)
    assert line == "    4  control.stop          webui:x  {'a': 1}"


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


def test_format_detail_shows_terminal_error_diagnostic():
    from runstate_tui.format import format_detail

    row = _row(status=Status.terminal(Outcome.ERRORED, detail="OOM killed"))
    text = format_detail(row)
    assert "errored: OOM killed" in text


def test_status_color_maps_kinds_and_outcomes():
    from runstate_tui.format import status_color

    assert status_color(Status.live()) == "green"
    assert status_color(Status.stale()) == "yellow"
    assert status_color(Status.pending()) == "grey58"
    assert status_color(Status.missing()) == "grey58"
    assert status_color(Status.corrupt()) == "red"
    assert status_color(Status.unreadable()) == "red"
    assert status_color(Status.error()) == "red"
    assert status_color(Status.terminal(Outcome.COMPLETED)) == "blue"
    assert status_color(Status.terminal(Outcome.ERRORED)) == "red"
