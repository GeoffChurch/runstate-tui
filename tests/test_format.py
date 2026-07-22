from runstate.observables import Outcome

from runstate_tui.format import format_row
from runstate_tui.types import Issue, IssueKind, Row, Severity, Status


def _env_stub():
    from runstate.channel import Envelope

    return Envelope(seq=1, topic="control.stop", name=None, request_id=None, body={})


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


def test_status_color_maps_kinds_and_outcomes():
    from runstate_tui.format import status_color

    assert status_color(Status.live()) == "#3fb950"
    assert status_color(Status.stale()) == "#d29922"
    assert status_color(Status.pending()) == "#8b949e"
    assert status_color(Status.missing()) == "#8b949e"
    assert status_color(Status.corrupt()) == "#f85149"
    assert status_color(Status.unreadable()) == "#f85149"
    assert status_color(Status.error()) == "#f85149"
    assert status_color(Status.terminal(Outcome.COMPLETED)) == "#539bf5"
    assert status_color(Status.terminal(Outcome.ERRORED)) == "#f85149"


def test_topic_color_by_family():
    from runstate_tui.format import topic_color

    assert topic_color("lifecycle.started") == "#539bf5"
    assert topic_color("control.stop") == "#d29922"
    assert topic_color("value") == "#3fb950"
    assert topic_color("something.else") == "#8b949e"


def test_summary_card_colors_only_the_dot_not_the_whole_line():
    # `Text("● ", style=X)` sets the Text's BASE style, which every subsequently
    # appended plain-string segment inherits at render time (the Rich base-style-
    # inheritance footgun -- the same class the multirun `_marker` chip guards
    # against, see test_marker_undischarged_stop_alone_renders_neutral). That would
    # paint the WHOLE first line (dot + status/step/loss summary) in the status
    # color instead of just the dot, contradicting the design: the dot carries the
    # color, the text stays uncolored (redundant signal, not a christmas tree).
    # `Text.join` folds each joined Text's base style into an explicit Span over
    # that Text's exact character range in the result, so this is checkable
    # directly on `format_summary_card`'s returned Text without rendering to
    # segments: a correctly-scoped fix produces exactly one Span, 2 characters
    # wide ("● "), carrying the status color; a base-style leak instead produces
    # one Span spanning the whole first line.
    from rich.text import Span

    from runstate_tui.format import format_summary_card, status_color

    row = _row(
        status=Status.live(),
        frontier=7,
        freshness=10.0,
        value=("loss", 0.03, 7),
        elapsed=50.0,
    )
    card = format_summary_card(row)
    color = status_color(row.status)
    assert color == "#3fb950"  # live -- pins the discriminating color from the bug report

    assert card.spans == [Span(0, len("● "), color)]


def test_summary_card_is_two_compact_lines_with_counts():
    from rich.text import Text

    from runstate_tui.format import format_summary_card

    row = _row(  # a live run w/ 1 stop, 1 demand
        status=Status.live(),
        frontier=1450,
        freshness=8.0,
        value=("loss", 0.0123, 1450),
        elapsed=20.0,
        episode="local://h/1",
        undischarged_stops=(_env_stub(),),  # len 1
        live_demand=(_env_stub(),),  # len 1
    )
    card = format_summary_card(row)
    assert isinstance(card, Text)
    plain = card.plain
    assert "live" in plain and "loss=0.0123" in plain  # line 1: the summary
    assert "episode local://h/1" in plain  # line 2: episode
    assert "1 stop pending" in plain and "1 demand" in plain  # line 2: COUNTS, not lists
    assert plain.count("\n") == 1  # exactly two lines


def test_fleet_summary_orders_worst_first_and_counts():
    from runstate_tui.format import format_fleet_summary

    rows = (
        [_row(status=Status.unreadable()) for _ in range(30)]
        + [
            _row(
                status=Status.corrupt(),
                issues=(Issue(IssueKind.CORRUPT, Severity.HIGH, "log corrupt", seq=1),),
            )
            for _ in range(2)
        ]
        + [_row(status=Status.live(), issues=(Issue(IssueKind.MALFORMED, Severity.MEDIUM, "bad"),))]
        + [_row(status=Status.live()) for _ in range(93)]
        + [_row(status=Status.terminal(Outcome.COMPLETED)) for _ in range(3)]
    )
    plain = format_fleet_summary(rows).plain
    assert "unreadable 30" in plain
    assert "corrupt 2" in plain
    assert "malformed 1" in plain
    assert "live 94" in plain  # 93 pure-live + the 1 live-with-malformed
    assert "done 3" in plain
    # worst-first: HIGH (corrupt < unreadable) before MEDIUM (malformed) before OK (done < live)
    i = plain.index
    assert i("corrupt") < i("unreadable") < i("malformed") < i("done") < i("live")


def test_fleet_summary_corrupt_counts_once_as_status_not_issue():
    from runstate_tui.format import format_fleet_summary

    torn = Issue(IssueKind.CORRUPT, Severity.HIGH, "log corrupt at seq 5", seq=5)
    plain = format_fleet_summary(
        [_row(status=Status.corrupt(), issues=(torn,)) for _ in range(2)]
    ).plain
    assert "corrupt 2" in plain
    assert plain.count("corrupt") == 1  # ONLY the status chip -- the CORRUPT issue-twin is skipped
    assert "⚠" not in plain  # no issue chip at all


def test_fleet_summary_malformed_shows_under_status_and_as_a_tag():
    from runstate_tui.format import format_fleet_summary

    m = Issue(IssueKind.MALFORMED, Severity.MEDIUM, "bad record")
    plain = format_fleet_summary([_row(status=Status.live(), issues=(m,))]).plain
    assert "live 1" in plain  # counted under its status...
    assert "malformed 1" in plain  # ...AND tagged -- two genuinely-different facts


def test_fleet_summary_issue_name_is_kind_value_verbatim():
    from runstate_tui.format import format_fleet_summary

    s = Issue(IssueKind.SKEW_SUSPECTED, Severity.MEDIUM, "clock skew")
    assert (
        "skew_suspected 1" in format_fleet_summary([_row(status=Status.live(), issues=(s,))]).plain
    )


def test_fleet_summary_empty_is_empty_text():
    from runstate_tui.format import format_fleet_summary

    assert format_fleet_summary([]).plain == ""


def test_fleet_summary_colors_the_glyph_only_not_the_label():
    from runstate_tui.format import format_fleet_summary, status_color

    text = format_fleet_summary([_row(status=Status.live())])
    green = [s for s in text.spans if s.style == status_color(Status.live())]
    assert green  # the ● glyph is colored
    assert all(s.end <= len("● ") for s in green)  # color covers only the glyph, not the label
    assert any(s.style == "default" for s in text.spans)  # the label text is neutral


def test_fleet_summary_order_is_stable_regardless_of_counts():
    from runstate_tui.format import format_fleet_summary

    few = [_row(status=Status.unreadable())] + [_row(status=Status.live()) for _ in range(2)]
    many = [_row(status=Status.unreadable()) for _ in range(50)] + [_row(status=Status.live())]
    for rows in (few, many):
        plain = format_fleet_summary(rows).plain
        assert plain.index("unreadable") < plain.index("live")  # severity order, never count
