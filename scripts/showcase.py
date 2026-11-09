"""Generate the README showcase PNGs from the real cockpit, headlessly and
deterministically (seeded logs + injected fixed clock). Run: `uv run python -m scripts.showcase`."""

from __future__ import annotations

import asyncio
import re
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from runstate import Outcome, SqliteLogs
from textual.app import App

from runstate_tui.app import SingleRunApp
from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import RunRef, explicit_resolver
from tests.helpers import corrupt_seq, log_path, move_log
from tests.records import Script, write

NOW = 300.0

# Textual's SVG export draws macOS-style red/yellow/green window buttons in the title bar. They
# read as three `●` traffic-lights and collide with the status dots the cockpit itself uses, so we
# strip them. The offset/colors are constants in Textual's template; fail loudly if it ever stops
# matching rather than silently shipping the disturbing chrome.
_WINDOW_BUTTONS = re.compile(r'\s*<g transform="translate\(26,22\)">.*?</g>', re.DOTALL)


def _strip_window_buttons(svg: str) -> str:
    stripped, n = _WINDOW_BUTTONS.subn("", svg, count=1)
    if n != 1:
        raise RuntimeError("window-button chrome missing — Textual SVG template changed?")
    return stripped


def _write(root: Path, rid: str, script: Script) -> RunRef:
    """Write a hand-built log format 0.4.0 run (fixed handles and clocks, so the
    screenshots are deterministic) and return its RunRef."""
    ch = SqliteLogs(root).create(rid)
    write(ch, script.records)
    ch.close()
    return (rid, str(root), "sqlite")


def _training(
    t0: float, t1: float, commits: int, *, handle: str, loss: float | None = None
) -> Script:
    """A claim at t0 and `commits` commits spread up to t1, the last committing `loss`."""
    s = Script()
    s.started(t0, handle)
    for i in range(commits):
        t = t0 + (t1 - t0) * (i + 1) / commits
        if loss is not None and i == commits - 1:
            s.value("loss", loss, t)
        s.commit(t)
    return s


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
        svg = _strip_window_buttons(app.export_screenshot(title=title))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.with_suffix(".svg").write_text(svg)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(out), scale=2.0)
    return out


def _seed_hero(root: Path) -> list[RunRef]:
    mnist = _training(280.0, 292.0, 1450, handle="local://h/1", loss=0.0123)  # live + metric
    cifar = _training(60.0, 140.0, 780, handle="local://h/2")  # held, gone quiet -> stale
    resnet = _training(50.0, 200.0, 5000, handle="local://h/3")
    resnet.stopped(205.0, completed=True)  # done (completed)
    sweep11 = _training(100.0, 180.0, 300, handle="local://h/4")
    sweep11.stopped(185.0, completed=False, error="CUDA OOM")  # errored, the worker's own exit
    sweep03 = _training(90.0, 150.0, 410, handle="local://h/6")
    sweep03.died(Outcome.KILLED, "slurm:TIMEOUT", 260.0)  # no exit: a death report ends it
    sweep07 = _training(294.0, 298.0, 22, handle="local://h/5")
    sweep07.stop("webui:stop1")  # live + undischarged stop -> ■1
    queued = Script()  # pending: subscribed, nothing claimed yet
    queued.subscribe("webui:sub1", "loss", {"every": {"commits": 1}})
    return [
        _write(root, "train-mnist", mnist),
        _write(root, "train-cifar", cifar),
        _write(root, "train-resnet", resnet),
        _write(root, "sweep-11", sweep11),
        _write(root, "sweep-03", sweep03),
        _write(root, "sweep-07", sweep07),
        _write(root, "queued-run", queued),
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
        app, out_dir / "table.png", size=(108, 11), before=before, title="runstate-tui — sweep"
    )


async def scene_single(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    ref = _write(  # one healthy live run
        root, "train-mnist", _training(280.0, 292.0, 1450, handle="local://h/1", loss=0.0123)
    )
    app = SingleRunApp(ref, Env(clock=lambda: NOW, objective="loss"), tick_interval=999)
    return await capture(
        app, out_dir / "single.png", size=(104, 4), title="runstate-tui — single run"
    )


async def scene_integrity(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())

    # Realistic run names — the STATUS column + the colored dot carry the integrity state,
    # NOT the run name (the `run` column is just the run_id; naming files after their state
    # would misleadingly imply a name<->status coupling that does not exist).
    _write(root, "train-gpt2", _training(280.0, 292.0, 10, handle="local://h/1"))
    corrupt_seq(root, "train-gpt2", 2)  # a byte-torn commit -> corrupt (⚠⚠ + red dot)

    # not a sqlite database at all, at the current format's address -- the reader's
    # records probe raises sqlite3.DatabaseError, caught by open_and_fold's _OPEN_ERRORS ->
    # unreadable (red dot, no marker). Distinct from a foreign *valid* sqlite db, which
    # a reader safely reads it as `missing` (no `log` table -> RunNotFound) leaving it
    # byte-identical (pinned in tests/scenarios/test_fold_plane.py, the foreign-valid-db test).
    log_path(root, "eval-glue").write_bytes(b"this is not a sqlite database")

    # "finetune-t5": its .db is never created -> missing: grey dot, no marker.

    # a run whose log is still log format 0.3.0: runstate refuses it at the open, by design,
    # naming `runstate migrate` -> a loud format row (⚠⚠ + red dot), never a crash.
    _write(root, "bert-base", _training(100.0, 200.0, 40, handle="local://h/2"))
    move_log(root, "bert-base", to=log_path(root, "bert-base", version="0.3.0"))

    # live, with its committed loss value's body replaced by an alien int -> live + malformed ⚠
    _write(root, "sweep-lr9", _training(280.0, 292.0, 10, handle="local://h/1", loss=0.5))
    corrupt_seq(root, "sweep-lr9", 11, literal="42")

    refs: list[RunRef] = [
        (r, str(root), "sqlite")
        for r in ("train-gpt2", "eval-glue", "finetune-t5", "bert-base", "sweep-lr9")
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
        size=(106, 9),
        before=before,
        title="runstate-tui — integrity",
    )


async def scene_drilldown(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    # a rich run: commits + a loss value + a subscription + an undischarged stop
    rich = _training(280.0, 292.0, 1450, handle="local://h/1", loss=0.0123)
    rich.subscribe("webui:sub1", "loss", {"every": {"commits": 1}})
    rich.stop("webui:stop1")
    _write(root, "train-mnist", rich)
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
    _write(root, "train-mnist", _training(280.0, 292.0, 1450, handle="local://h/1", loss=0.0123))
    ref: RunRef = ("train-mnist", str(root), "sqlite")
    app = SingleRunApp(ref, Env(clock=lambda: NOW, objective="loss"), tick_interval=999)

    async def before(pilot: object) -> None:  # `s` -> the confirm-stop gate
        await pilot.press("s")  # type: ignore[attr-defined]

    return await capture(
        app, out_dir / "stop.png", size=(104, 4), before=before, title="runstate-tui — stop"
    )


async def scene_grouped(out_dir: Path) -> Path:
    root = Path(tempfile.mkdtemp())
    # Four cells across two scenarios; the resolver supplies (RunRef, attrs) and the table sections
    # on `scenario` (the group) while the `run` column shows `variant` (the non-group attr = label).
    _write(root, "run-a", _training(280.0, 292.0, 1450, handle="local://h/1", loss=0.0123))
    done = _training(50.0, 200.0, 5000, handle="local://h/2")  # en-ru-16M / fb_10k -- done
    done.stopped(205.0, completed=True)
    _write(root, "run-b", done)
    _write(root, "run-c", _training(60.0, 140.0, 780, handle="local://h/3"))  # fr-en-4M -- stale
    oom = _training(100.0, 180.0, 300, handle="local://h/4")  # fr-en-4M / fb_10k -- errored
    oom.stopped(185.0, completed=False, error="CUDA OOM")
    _write(root, "run-d", oom)
    items = [
        (("run-a", str(root), "sqlite"), {"scenario": "en-ru-16M", "variant": "fb_5k"}),
        (("run-b", str(root), "sqlite"), {"scenario": "en-ru-16M", "variant": "fb_10k"}),
        (("run-c", str(root), "sqlite"), {"scenario": "fr-en-4M", "variant": "fb_5k"}),
        (("run-d", str(root), "sqlite"), {"scenario": "fr-en-4M", "variant": "fb_10k"}),
    ]
    app = MultiRunApp(
        lambda now: items,
        Env(clock=lambda: NOW, objective="loss"),
        tick_interval=999,
        group_by="scenario",
    )

    async def before(pilot: object) -> None:  # cursor OFF -- every row's dot shows undimmed
        t = pilot.app.query_one("#runs")  # type: ignore[attr-defined]
        t.cursor_type = "none"

    return await capture(
        app,
        out_dir / "grouped.png",
        size=(108, 10),
        before=before,
        title="runstate-tui — grouped by scenario",
    )


SCENES: dict[str, Callable[[Path], Awaitable[Path]]] = {
    "table": scene_table,
    "grouped": scene_grouped,
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
