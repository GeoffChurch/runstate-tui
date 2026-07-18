from __future__ import annotations

from .types import Row


def format_row(row: Row) -> str:
    """Render a Row as one human line; absent factors are omitted."""
    parts: list[str] = [row.status.label]
    if row.frontier is not None:
        parts.append(f"step {row.frontier}")
    if row.freshness is not None:
        parts.append(f"{row.freshness:.0f}s ago")
    if row.value is not None:
        name, value, step = row.value
        parts.append(f"{name}={value}" + (f" @ {step}" if step is not None else ""))
    if row.elapsed is not None:
        parts.append(f"ran {row.elapsed:.0f}s")
    for issue in row.issues:
        parts.append(f"⚠ {issue.message}")
    return "  ".join(parts)
