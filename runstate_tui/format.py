from __future__ import annotations

from runstate.channel import Envelope

from .types import Row


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
        parts.append(f"⏹{len(row.undischarged_stops)}")
    for issue in row.issues:
        parts.append(f"⚠ {issue.message}")
    return "  ".join(parts)


def format_envelope(env: Envelope) -> str:
    """One compact line for the raw log tail: seq, topic, request_id?, body."""
    rid = f"  {env.request_id}" if env.request_id else ""
    return f"{env.seq:>5}  {env.topic:<20}{rid}  {env.body}"


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
