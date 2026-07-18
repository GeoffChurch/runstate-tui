"""Plain (non-fixture) reusable test helpers — importable as `tests.helpers`
(see pyproject.toml's `[tool.pytest.ini_options] pythonpath = ["."]` +
`tests/__init__.py` / `tests/scenarios/__init__.py`, which make `tests` a real
importable package from the repo root). Fixtures that need pytest machinery
(tmp_path, teardown) live in tests/conftest.py instead; these are the ones
that don't."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any


def fake_clock(*times: float) -> tuple[Callable[[], float], Callable[[float], None]]:
    """A `now` that yields `times` in order (raises StopIteration past the end)
    + a no-op `sleep` — for bounded `await_consumed` timeout tests where the
    clock must advance deterministically without wall-clock waiting."""
    it = iter(times)

    def now() -> float:
        return next(it)

    def sleep(_seconds: float) -> None:
        return None

    return now, sleep


async def advance_tick(pilot: Any, screen: Any) -> None:
    """Fire one manual fold tick on `screen` and settle: run with a screen built
    at `tick_interval=999` so only manual ticks fire, keeping the fold
    deterministic under test."""
    screen._tick()
    await pilot.app.workers.wait_for_complete()
    await pilot.pause()


def log_text(richlog: Any) -> list[str]:
    """The rendered text of every line currently in a Textual RichLog."""
    return [strip.text for strip in richlog.lines]


def corrupt_seq(tmp_path: Path, run_id: str, seq: int, *, literal: str = "{not json") -> None:
    """Raw `UPDATE log SET body=? WHERE seq=?` on the sqlite run log at
    `<tmp_path>/<run_id>.db` — test tooling only, bypassing the substrate to
    plant a body it would never write itself. The writer may still be open
    (WAL tolerates a second connection). `literal="{not json"` (the default)
    is byte-torn (raises JSONDecodeError on read); `literal="42"` is valid
    JSON but an alien non-dict body (decodes to a bare int)."""
    conn = sqlite3.connect(str(Path(tmp_path) / f"{run_id}.db"))
    try:
        conn.execute("UPDATE log SET body = ? WHERE seq = ?", (literal, seq))
        conn.commit()
    finally:
        conn.close()


__all__ = ["fake_clock", "advance_tick", "log_text", "corrupt_seq"]
