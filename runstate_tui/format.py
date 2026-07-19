from __future__ import annotations

from runstate.channel import Envelope
from runstate.observables import Outcome

from .types import Row, Status, StatusKind

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


def format_detail(row: Row) -> str:
    """The drill-down header: every Row factor + full issues + stops + demand. Pure."""
    lines = [format_row(row)]  # the one-line summary at the top
    lines.append(f"episode: {row.episode}" if row.episode else "episode: —")
    if row.undischarged_stops:
        lines.append(f"undischarged stops ({len(row.undischarged_stops)}):")
        lines += [f"  {format_envelope(e)}" for e in row.undischarged_stops]
    if row.live_demand:
        lines.append(f"live demand ({len(row.live_demand)}):")
        lines += [f"  {format_envelope(e)}" for e in row.live_demand]
    if row.issues:
        lines.append("issues:")
        lines += [f"  ⚠ {i.message}" for i in row.issues]
    return "\n".join(lines)
