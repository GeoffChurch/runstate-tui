from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from runstate.channel import Envelope
from runstate.observables import Outcome

from .types import _STATUS_TWIN_ISSUES, IssueKind, Row, Severity, Status, StatusKind

_TOPIC_COLORS = {"lifecycle": "#539bf5", "control": "#d29922", "value": "#3fb950"}
_STATUS_COLORS = {
    StatusKind.LIVE: "#3fb950",
    StatusKind.STALE: "#d29922",
    StatusKind.PENDING: "#8b949e",
    StatusKind.MISSING: "#8b949e",
    StatusKind.CORRUPT: "#f85149",
    StatusKind.UNREADABLE: "#f85149",
    StatusKind.ERROR: "#f85149",
    StatusKind.CONFLICTED: "#d29922",
}
_OUTCOME_COLORS = {
    Outcome.COMPLETED: "#539bf5",
    Outcome.PREEMPTED: "#d29922",
    Outcome.ERRORED: "#f85149",
    Outcome.KILLED: "#f85149",
    Outcome.PRESUMED_DEAD: "#f85149",
}


def status_color(status: Status) -> str:
    """A truecolor HEX for a status — the traffic-light dot. Redundant with the
    text label (never the sole signal). Keyed on StatusKind, refined by terminal
    Outcome. HEX (not Rich/ANSI color names) so the palette renders faithfully
    instead of being bent by a terminal's ANSI theme (e.g. `blue` -> purple,
    `yellow` -> amber on some themes)."""
    if status.kind is StatusKind.TERMINAL and status.outcome is not None:
        return _OUTCOME_COLORS.get(status.outcome, "#8b949e")
    return _STATUS_COLORS.get(status.kind, "#8b949e")


def topic_color(topic: str) -> str:
    """A hex color for a log topic, by family (mirrors status_color). Redundant with
    the topic text — never the sole signal."""
    return _TOPIC_COLORS.get(topic.split(".")[0], "#8b949e")


def format_row(row: Row) -> str:
    """Render a Row as one human line; absent factors are omitted."""
    label = row.status.label
    if row.status.detail:
        label += f": {row.status.detail}"  # e.g. "errored: OOM" -- retains RunResult.error
    parts: list[str] = [label]
    if row.frontier is not None:
        parts.append(f"step {row.frontier}")
    if row.freshness is not None:
        parts.append(f"{row.freshness:.0f}s ago")
    if row.value is not None:
        name, value, step = row.value
        parts.append(f"{name}={value}" + (f" @ {step}" if step is not None else ""))
    if row.elapsed is not None:
        parts.append(f"ran {row.elapsed:.0f}s")
    if row.undischarged_stops:
        parts.append(f"■{len(row.undischarged_stops)}")
    for issue in row.issues:
        parts.append(f"⚠ {issue.message}")
    return "  ".join(parts)


def format_summary_card(row: Row) -> Text:
    """The drill-down's compact 2-line header card: the one-line summary (with the
    status dot) + episode and COUNTS. The full stop/demand/issue lists live in the
    enter-expand, not here."""
    line1 = Text()
    line1.append("● ", style=status_color(row.status))  # a Span on an unstyled base, so the
    # summary appended next does NOT inherit the color (Text(text, style=X) sets the base
    # style, which every subsequently-appended plain segment inherits at render time --
    # see test_summary_card_colors_only_the_dot_not_the_whole_line).
    line1.append(format_row(row))  # the existing one-line summary
    parts = [f"episode {row.episode}" if row.episode else "episode —"]
    if row.undischarged_stops:
        parts.append(f"■ {len(row.undischarged_stops)} stop pending")
    if row.live_demand:
        parts.append(f"◆ {len(row.live_demand)} demand")
    if row.issues:
        parts.append(f"⚠ {len(row.issues)} issue" + ("s" if len(row.issues) != 1 else ""))
    line2 = Text("     ".join(parts))
    return Text("\n").join([line1, line2])


def _sev_color(severity: Severity) -> str:
    """The ⚠-tag hue by severity (mirrors _marker): HIGH red, else amber."""
    return "#f85149" if severity >= Severity.HIGH else "#d29922"


def _chip(out: Text, glyph: str, color: str, text: str) -> None:
    """Append one `<glyph> text   ` chip: the glyph carries `color`; the label stays neutral
    (explicit style="default" -- Text.append without a style inherits the base and would paint
    the label, the footgun _marker / format_summary_card also guard)."""
    out.append(f"{glyph} ", style=color)
    out.append(f"{text}   ", style="default")


def format_fleet_summary(rows: Sequence[Row]) -> Text:
    """The always-on fleet legend / roll-up strip: one `<glyph> <name> <count>` chip per
    condition present in `rows`, worst-first `(severity desc, name)`. Statuses partition the
    fleet (each run once; ● + status_color); issues that are NOT a status-twin are tagged
    (⚠, colored by Issue.severity). Names are passthrough (Status.label / IssueKind.value).
    Empty Text for no rows. Pure -- rebuilt each frame in on_table_ready."""
    status_count: dict[str, int] = {}
    status_repr: dict[str, Status] = {}
    for row in rows:
        lbl = row.status.label
        status_count[lbl] = status_count.get(lbl, 0) + 1
        status_repr.setdefault(lbl, row.status)  # a bucket rep -> its color & severity
    issue_count: dict[IssueKind, int] = {}
    issue_sev: dict[IssueKind, Severity] = {}
    for row in rows:
        for kind in {i.kind for i in row.issues} - _STATUS_TWIN_ISSUES:
            issue_count[kind] = issue_count.get(kind, 0) + 1
            issue_sev[kind] = max(
                issue_sev.get(kind, Severity.OK),
                max(i.severity for i in row.issues if i.kind == kind),
            )
    chips: list[tuple[Severity, str, str, str, int]] = [
        (status_repr[lbl].severity, lbl, "●", status_color(status_repr[lbl]), n)
        for lbl, n in status_count.items()
    ] + [(issue_sev[k], k.value, "⚠", _sev_color(issue_sev[k]), n) for k, n in issue_count.items()]
    out = Text()
    for _sev, name, glyph, color, n in sorted(chips, key=lambda c: (-c[0], c[1])):
        _chip(out, glyph, color, f"{name} {n}")  # severity desc, then name
    return out


def format_envelope(env: Envelope) -> str:
    """One compact line for the raw log tail: seq, topic, request_id?, body.

    A normal dict body's string values are already `repr()`'d as part of
    `str(dict)` (an embedded "\\n" prints as the two literal characters
    backslash-n), but an alien non-dict body is interpolated raw — and
    `request_id` is attacker/corruption-reachable too (it rides in every
    envelope, unvalidated). A real control char in EITHER field (an embedded
    newline/CR/tab) would otherwise reach `RichLog.write` and split one
    envelope across multiple physical lines. So the whole assembled line is
    escaped once at the end, covering every field (seq, topic, request_id,
    body) — not just body — keeping the "one envelope = one line" invariant
    even under a corrupted/adversarial request_id."""
    rid = f"  {env.request_id}" if env.request_id else ""
    body = str(env.body)
    line = f"{env.seq:>5}  {env.topic:<20}{rid}  {body}"
    return line.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
