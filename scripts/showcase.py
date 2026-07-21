"""Generate the README showcase PNGs from the real cockpit, headlessly and
deterministically (seeded logs + injected fixed clock). Run: `uv run python -m scripts.showcase`."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from runstate import create_channel
from textual.app import App

from runstate_tui.app import SingleRunApp
from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import RunRef, explicit_resolver

NOW = 300.0


def _corrupt(root: Path, run_id: str, seq: int, literal: str) -> None:
    conn = sqlite3.connect(str(root / f"{run_id}.db"))
    conn.execute("UPDATE log SET body = ? WHERE seq = ?", (literal, seq))
    conn.commit()
    conn.close()


def _ch(root: Path, rid: str):
    return create_channel(rid, root=root, backend="sqlite")


async def capture(
    app: App[None],
    out: Path,
    *,
    size: tuple[int, int] = (110, 18),
    pauses: int = 3,
    title: str = "runstate-tui",
    before: Callable[[object], Awaitable[None]] | None = None,
) -> Path:
    import cairosvg

    async with app.run_test(size=size) as pilot:
        for _ in range(pauses):
            await pilot.pause()
        if before is not None:
            await before(pilot)
            await pilot.pause()
        svg = app.export_screenshot(title=title)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".svg").write_text(svg)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(out), scale=2.0)
    return out


def _seed_hero(root: Path) -> list[RunRef]:
    c = _ch(root, "train-mnist")  # live + metric
    c.send({"handle": "local://h/1", "t": 280.0}, topic="lifecycle.started")
    c.send({"step": 1450, "consumed_seq": 0, "t": 292.0}, topic="lifecycle.heartbeat")
    c.send({"value": 0.0123, "step": 1450, "t": 292.0}, topic="value", name="loss")
    c.close()
    c = _ch(root, "train-cifar")  # stale
    c.send({"handle": "local://h/2", "t": 60.0}, topic="lifecycle.started")
    c.send({"step": 780, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
    c.close()
    c = _ch(root, "train-resnet")  # done (completed)
    c.send({"handle": "local://h/3", "t": 50.0}, topic="lifecycle.started")
    c.send({"step": 5000, "consumed_seq": 0, "t": 200.0}, topic="lifecycle.heartbeat")
    c.send(
        {"completed": True, "error": None, "final_step": 5000, "t": 205.0},
        topic="lifecycle.stopped",
    )
    c.close()
    c = _ch(root, "sweep-11")  # errored
    c.send({"handle": "local://h/4", "t": 100.0}, topic="lifecycle.started")
    c.send({"step": 300, "consumed_seq": 0, "t": 180.0}, topic="lifecycle.heartbeat")
    c.send(
        {"completed": False, "error": "CUDA OOM", "final_step": 300, "t": 185.0},
        topic="lifecycle.stopped",
    )
    c.close()
    c = _ch(root, "sweep-07")  # live + undischarged stop -> ■1
    c.send({"handle": "local://h/5", "t": 294.0}, topic="lifecycle.started")
    c.send({"step": 22, "consumed_seq": 0, "t": 298.0}, topic="lifecycle.heartbeat")
    c.send({}, topic="control.stop", request_id="webui:stop1")
    c.close()
    c = _ch(root, "queued-run")  # pending (subscribed, not started)
    c.send({"schedule": {}, "names": ["loss"]}, topic="control.subscribe", request_id="webui:sub1")
    c.close()
    return [
        (r, str(root), "sqlite")
        for r in (
            "train-mnist",
            "train-cifar",
            "train-resnet",
            "sweep-11",
            "sweep-07",
            "queued-run",
        )
    ]


async def scene_table(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    refs = _seed_hero(root)
    app = MultiRunApp(
        explicit_resolver(refs), Env(clock=lambda: NOW, objective="loss"), tick_interval=999
    )

    async def before(pilot: object) -> None:  # cursor OFF -- every row's dot shows undimmed
        t = pilot.app.query_one("#runs")  # type: ignore[attr-defined]
        t.cursor_type = "none"

    return await capture(
        app, out_dir / "table.png", size=(108, 10), before=before, title="runstate-tui — sweep"
    )


async def scene_single(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    c = _ch(root, "train-mnist")  # one healthy live run
    c.send({"handle": "local://h/1", "t": 280.0}, topic="lifecycle.started")
    c.send({"step": 1450, "consumed_seq": 0, "t": 292.0}, topic="lifecycle.heartbeat")
    c.send({"value": 0.0123, "step": 1450, "t": 292.0}, topic="value", name="loss")
    c.close()
    ref: RunRef = ("train-mnist", str(root), "sqlite")
    app = SingleRunApp(ref, Env(clock=lambda: NOW, objective="loss"), tick_interval=999)
    return await capture(
        app, out_dir / "single.png", size=(104, 4), title="runstate-tui — single run"
    )


async def scene_integrity(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())

    # Realistic run names — the STATUS column + the colored dot carry the integrity state,
    # NOT the run name (the `run` column is just the run_id; naming files after their state
    # would misleadingly imply a name<->status coupling that does not exist).
    c = _ch(root, "train-gpt2")  # byte-torn hb -> corrupt (⚠⚠ + red dot)
    c.send({"handle": "local://h/1", "t": 280.0}, topic="lifecycle.started")
    c.send({"step": 10, "consumed_seq": 0, "t": 292.0}, topic="lifecycle.heartbeat")
    c.close()
    _corrupt(root, "train-gpt2", 2, "{not json")

    # not a sqlite database at all -- attach_channel's records probe raises
    # sqlite3.DatabaseError, caught by open_and_fold's _OPEN_ERRORS -> unreadable (red
    # dot, no marker). Distinct from a foreign *valid* sqlite db, which attach_channel
    # now safely reads as `missing` (no `log` table -> RunNotFound) leaving it
    # byte-identical -- the old mutate-on-open gap is fixed (pinned in
    # tests/scenarios/test_fold_plane.py, the foreign-valid-db test).
    (root / "eval-glue.db").write_bytes(b"this is not a sqlite database")  # -> unreadable (red dot)

    # "finetune-t5": its .db is never created -> missing: grey dot, no marker.

    c = _ch(
        root, "sweep-lr9"
    )  # started + hb + value, then the value body -> alien int -> live + malformed ⚠
    c.send({"handle": "local://h/1", "t": 280.0}, topic="lifecycle.started")
    c.send({"step": 10, "consumed_seq": 0, "t": 292.0}, topic="lifecycle.heartbeat")
    c.send({"value": 0.5, "step": 10, "t": 292.0}, topic="value", name="loss")
    c.close()
    _corrupt(root, "sweep-lr9", 3, "42")

    refs: list[RunRef] = [
        (r, str(root), "sqlite") for r in ("train-gpt2", "eval-glue", "finetune-t5", "sweep-lr9")
    ]
    app = MultiRunApp(
        explicit_resolver(refs), Env(clock=lambda: NOW, objective="loss"), tick_interval=999
    )

    async def before(pilot: object) -> None:  # cursor OFF -- every row's dot shows undimmed
        t = pilot.app.query_one("#runs")  # type: ignore[attr-defined]
        t.cursor_type = "none"

    return await capture(
        app,
        out_dir / "integrity.png",
        size=(106, 8),
        before=before,
        title="runstate-tui — integrity",
    )


async def scene_drilldown(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    c = _ch(root, "train-mnist")  # a rich run: hb + value + a subscribe + an undischarged stop
    c.send({"handle": "local://h/1", "t": 280.0}, topic="lifecycle.started")
    c.send({"step": 1450, "consumed_seq": 0, "t": 292.0}, topic="lifecycle.heartbeat")
    c.send({"value": 0.0123, "step": 1450, "t": 292.0}, topic="value", name="loss")
    c.send({"schedule": {}, "names": ["loss"]}, topic="control.subscribe", request_id="webui:sub1")
    c.send({}, topic="control.stop", request_id="webui:stop1")
    c.close()
    ref: RunRef = ("train-mnist", str(root), "sqlite")
    app = MultiRunApp(
        explicit_resolver([ref]), Env(clock=lambda: NOW, objective="loss"), tick_interval=999
    )

    async def before(pilot: object) -> None:  # `enter` on the (sole, pre-selected) row
        await pilot.press("enter")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        await pilot.app.workers.wait_for_complete()  # type: ignore[attr-defined]

    return await capture(
        app,
        out_dir / "drilldown.png",
        size=(108, 16),
        before=before,
        title="runstate-tui — drill-down",
    )


async def scene_stop(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    c = _ch(root, "train-mnist")  # a live run
    c.send({"handle": "local://h/1", "t": 280.0}, topic="lifecycle.started")
    c.send({"step": 1450, "consumed_seq": 0, "t": 292.0}, topic="lifecycle.heartbeat")
    c.send({"value": 0.0123, "step": 1450, "t": 292.0}, topic="value", name="loss")
    c.close()
    ref: RunRef = ("train-mnist", str(root), "sqlite")
    app = SingleRunApp(ref, Env(clock=lambda: NOW, objective="loss"), tick_interval=999)

    async def before(pilot: object) -> None:  # `s` -> the confirm-stop gate
        await pilot.press("s")  # type: ignore[attr-defined]

    return await capture(
        app, out_dir / "stop.png", size=(104, 4), before=before, title="runstate-tui — stop"
    )


SCENES: dict[str, Callable[[Path], Awaitable[Path]]] = {
    "table": scene_table,
    "single": scene_single,
    "integrity": scene_integrity,
    "drilldown": scene_drilldown,
    "stop": scene_stop,
}


def main(out_dir: str = "docs/img") -> list[Path]:
    out = Path(out_dir)
    return [asyncio.run(scene(out)) for scene in SCENES.values()]


if __name__ == "__main__":
    for p in main():
        print(f"wrote {p}")
