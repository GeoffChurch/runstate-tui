"""Plain (non-fixture) reusable test helpers — importable as `tests.helpers`
(see pyproject.toml's `[tool.pytest.ini_options] pythonpath = ["."]` +
`tests/__init__.py` / `tests/scenarios/__init__.py`, which make `tests` a real
importable package from the repo root). Fixtures that need pytest machinery
(tmp_path, teardown) live in tests/conftest.py instead; these are the ones
that don't."""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runstate import LOG_FORMAT


def log_path(root: Path | str, run_id: str, *, version: str = LOG_FORMAT) -> Path:
    """A sqlite run log's address in format `version` (runstate log-formats.md §3):
    `<root>/v<version>/<run_id>.db`. Test tooling, for planting bytes at an address."""
    return Path(root) / f"v{version}" / f"{run_id}.db"


def move_log(root: Path | str, run_id: str, *, to: Path) -> Path:
    """Move a run's 0.4.0 log (and any -wal/-shm sidecar) to address `to` -- an older
    format's `<root>/v0.3.0/<rid>.db`, or the legacy `<root>/<rid>.db`. Format 0.4.0's
    sqlite table is 0.3.0's (and 0.2.0's), so a log moved to an older address is
    byte-for-byte a log of that format: the address is the format (log-formats.md §3).
    The writer must be closed first."""
    src = log_path(root, run_id)
    to.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.move(str(side), str(to) + suffix)
    return to


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
    `<tmp_path>/v0.4.0/<run_id>.db` — test tooling only, bypassing the substrate to
    plant a body it would never write itself. The writer may still be open
    (WAL tolerates a second connection). `literal="{not json"` (the default)
    is byte-torn (raises JSONDecodeError on read); `literal="42"` is valid
    JSON but an alien non-dict body (decodes to a bare int)."""
    conn = sqlite3.connect(str(log_path(tmp_path, run_id)))
    try:
        conn.execute("UPDATE log SET body = ? WHERE seq = ?", (literal, seq))
        conn.commit()
    finally:
        conn.close()


__all__ = ["fake_clock", "advance_tick", "log_text", "corrupt_seq", "log_path", "move_log"]
