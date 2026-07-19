# README showcase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A CI-runnable generator that drives the real cockpit headlessly into seeded states and emits static PNGs, embedded in the README — plus two spike-verified product tweaks (`⏹`→`■`, a `●` traffic-light status-dot) that improve the real UI and the shots.

**Architecture:** Reuse the fixture-basis machinery: seed `runstate` sqlite logs at controlled `t` values, build the real app with an injected fixed clock + fixed console size, drive `Pilot` to each state, `export_screenshot()` → SVG, `cairosvg` → PNG. Deterministic; no manual terminal.

**Tech Stack:** Python 3.11, runstate, Textual 8.2.8 (`run_test`/`Pilot`/`export_screenshot`), Rich (`Text` for colored cells), cairosvg (dev dep), uv, ruff, mypy --strict, pytest.

## Global Constraints

Copied from the spec (`docs/superpowers/specs/2026-07-19-readme-showcase-design.md`).

- **Static PNGs only** this cut; GIFs/animated usage DEFERRED. No pixel-diff CI gate (the generator running without error is the smoke-test value).
- **Determinism:** every scene uses an injected fixed clock (`Env(clock=lambda: NOW)`), a fixed `run_test(size=(W,H))`, and seeded logs at controlled `t`s. `NOW` is chosen relative to the seeded `t`s (`stuck_threshold=60`: `NOW-20` → `live`, `NOW-120` → `stale`).
- **Color is redundant, never the sole signal** (CVD / `NO_COLOR` / piped output degrade to the text status). The `●` dot reinforces the existing status text.
- **Glyphs (spike-verified through cairosvg):** stop badge is `■` (U+25A0), NOT `⏹`. Status dot is `●` (U+25CF) colored via a Rich style (renders as an SVG `fill`). Do NOT use emoji circles (`🔴🟢🟡` — newer, emoji-font-dependent, tofu in cairosvg).
- **Status→color map** (keyed on `StatusKind` + terminal `Outcome`): green `live` · yellow(amber) `stale`/`preempted` · grey `pending`/`missing` · red `corrupt`/`unreadable`/`fold-error`(=`StatusKind.ERROR`)/`errored`/`killed`/`presumed_dead` · blue `completed`. `conflicted` → yellow (unused today).
- **Gates:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` green before each commit. `scripts/` is NOT under mypy's `files=["runstate_tui"]` scope, but keep it ruff-clean and typed. Commit trailer:
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01EZVuQQF3vdynEXvyDApYKp
  ```

## File Structure

- **Modify `runstate_tui/format.py`** — `format_row`'s stop marker `⏹`→`■`; add `status_color(status) -> str`.
- **Modify `runstate_tui/multirun.py`** — `_marker` `⏹`→`■`; add the leading `●` dot column.
- **Create `scripts/showcase.py`** — the generator: a `capture` helper + one function per scene + `main()`.
- **Modify `pyproject.toml`** — add `cairosvg` to a dev dependency group.
- **Create `docs/img/`** — the committed PNGs (+ their SVGs).
- **Modify `README.md`** — a "Screens" section embedding the PNGs.
- **Modify `.github/workflows/ci.yml`** — run the generator (smoke test).
- **Tests:** `tests/test_format.py`, `tests/test_multirun.py` (glyph/dot), `tests/test_showcase.py` (new — the generator writes files).

---

### Task 1: `⏹` → `■` stop-badge swap

**Files:** Modify `runstate_tui/format.py`, `runstate_tui/multirun.py`; Test `tests/test_format.py`, `tests/test_multirun.py`.

- [ ] **Step 1: Update the failing tests.** Grep for `⏹` in tests (`grep -rn "⏹" tests/`) and re-point every assertion to `■`. In `tests/test_format.py`, the undischarged-stops test asserts the marker; change its expected `⏹` → `■`. In `tests/test_multirun.py`, the `_marker` test (a row with `undischarged_stops`) asserts `■1` (was `⏹1`).

- [ ] **Step 2: Run to verify they fail** — `uv run pytest tests/test_format.py tests/test_multirun.py -q` (assert-mismatch on the still-`⏹` production code).

- [ ] **Step 3: Swap the glyph in production.**
  - `runstate_tui/format.py`, in `format_row`: `parts.append(f"⏹{len(row.undischarged_stops)}")` → `f"■{len(row.undischarged_stops)}"`.
  - `runstate_tui/multirun.py`, in `_marker`: replace both `⏹` occurrences (`stops = f"⏹{len(...)}"`) with `■`.

- [ ] **Step 4: Run tests → pass. Step 5: full gates; commit** `feat(ui): stop badge ⏹ → ■ (portable glyph, renders in cairosvg)`.

---

### Task 2: `status_color` + the `●` traffic-light dot column

**Files:** Modify `runstate_tui/format.py` (add `status_color`), `runstate_tui/multirun.py` (dot column); Test `tests/test_format.py`, `tests/test_multirun.py`.

**Interfaces:**
- Produces: `status_color(status: Status) -> str` (a Rich color name); a leading `dot` column on the `MultiRunApp` table whose cell is `Text("●", style=status_color(row.status))`.

- [ ] **Step 1: Write the failing tests.**

Add to `tests/test_format.py`:
```python
def test_status_color_maps_kinds_and_outcomes():
    from runstate_tui.format import status_color
    from runstate.observables import Outcome
    assert status_color(Status.live()) == "green"
    assert status_color(Status.stale()) == "yellow"
    assert status_color(Status.pending()) == "grey58"
    assert status_color(Status.missing()) == "grey58"
    assert status_color(Status.corrupt()) == "red"
    assert status_color(Status.unreadable()) == "red"
    assert status_color(Status.error()) == "red"
    assert status_color(Status.terminal(Outcome.COMPLETED)) == "blue"
    assert status_color(Status.terminal(Outcome.ERRORED)) == "red"
```
Add to `tests/test_multirun.py` (async, following the file's harness): after the table populates, assert the `dot` column of a `live` run's row is a `Text` with `●` and style `"green"`:
```python
@pytest.mark.asyncio
async def test_table_has_a_colored_status_dot(tmp_path):
    ref = _seed(tmp_path, "a")  # a live run under the fixed clock
    app = MultiRunApp(explicit_resolver([ref]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause(); await pilot.pause()
        t = app.query_one("#runs", DataTable)
        cell = t.get_cell(ref_key(ref), "dot")     # Rich Text
        assert "●" in cell.plain and cell.style == "green"
```
(Adapt `get_cell`/style access to the Textual 8.2.8 API — `DataTable.get_cell(row_key, column_key)` returns the stored renderable; a `rich.text.Text`'s color is on `cell.style`. Verify the exact accessor; the requirement is "the dot cell is a green `●` for a live run.")

- [ ] **Step 2: Run to verify they fail** (`status_color` undefined; no `dot` column).

- [ ] **Step 3: Add `status_color` to `runstate_tui/format.py`.**
```python
from runstate.observables import Outcome  # add to imports
from .types import Row, Status, StatusKind  # extend existing import

_STATUS_COLORS = {
    StatusKind.LIVE: "green",
    StatusKind.STALE: "yellow",
    StatusKind.PENDING: "grey58",
    StatusKind.MISSING: "grey58",
    StatusKind.CORRUPT: "red",
    StatusKind.UNREADABLE: "red",
    StatusKind.ERROR: "red",
    StatusKind.CONFLICTED: "yellow",
}
_OUTCOME_COLORS = {
    Outcome.COMPLETED: "blue",
    Outcome.PREEMPTED: "yellow",
    Outcome.ERRORED: "red",
    Outcome.KILLED: "red",
    Outcome.PRESUMED_DEAD: "red",
}

def status_color(status: Status) -> str:
    """A Rich color name for a status — the traffic-light dot. Redundant with the
    text label (never the sole signal). Keyed on StatusKind, refined by terminal Outcome."""
    if status.kind is StatusKind.TERMINAL and status.outcome is not None:
        return _OUTCOME_COLORS.get(status.outcome, "blue")
    return _STATUS_COLORS.get(status.kind, "grey58")
```

- [ ] **Step 4: Add the dot column in `runstate_tui/multirun.py`.**
  - Import: `from rich.text import Text`; `from .format import status_color` (add to the existing `.format` import if present, else new).
  - Prepend the key to `_COLUMNS`: `_COLUMNS = ("dot", "run", "status", "step", "age", "value", "elapsed", "!")`.
  - In `on_mount`, give the dot column a blank header but a real key:
    ```python
    t.add_columns(("", "dot"), *[(c, c) for c in _COLUMNS[1:]])
    ```
  - In `_cells`, prepend the dot cell (return 8 values):
    ```python
    dot = Text("●", style=status_color(row.status))
    return (dot, run_id, status, step, age, value, elapsed, _marker(row))
    ```
  - The reconcile loop (`zip(_COLUMNS, cells)` + `update_cell`/`add_row`) is unchanged — it now carries the extra leading column automatically. (`update_cell(key, "dot", dot)` updates the Text.)

- [ ] **Step 5: Run tests → pass (3× for the async test). Step 6: full gates; commit** `feat(ui): leading ● traffic-light status-dot column + status_color`.

---

### Task 3: The showcase generator (infra + hero scene) + `cairosvg` dep

**Files:** Create `scripts/showcase.py`; Modify `pyproject.toml`; Create `tests/test_showcase.py`; Create `docs/img/`.

**Interfaces:**
- Produces: `scripts/showcase.py` with `async def capture(app, out: Path, *, size, pauses, title, before=None)`, scene functions (`scene_table(out_dir)` first), and `def main(out_dir="docs/img") -> list[Path]`.

- [ ] **Step 1: Add `cairosvg` as a dev dependency** in `pyproject.toml` (a `[dependency-groups] dev = [...]` entry or an optional-dependencies extra, matching the repo's existing dev-dep convention — check the file). Run `uv sync` (or the repo's equivalent) so it resolves.

- [ ] **Step 2: Write the failing test.** Create `tests/test_showcase.py`:
```python
import pytest


@pytest.mark.asyncio
async def test_showcase_writes_the_hero_png(tmp_path):
    import importlib.util
    if importlib.util.find_spec("cairosvg") is None:
        pytest.skip("cairosvg not installed")
    from scripts.showcase import scene_table   # scripts importable via pythonpath="."
    out = await scene_table(tmp_path)
    assert out.exists() and out.stat().st_size > 0
    assert out.with_suffix(".svg").exists()
```
(If `scripts/` isn't import-reachable, add it to `[tool.pytest.ini_options] pythonpath` or import by file path with `importlib`. The requirement: a scene renders a non-empty PNG + its SVG.)

- [ ] **Step 3: Run to verify it fails** (`scripts.showcase` undefined).

- [ ] **Step 4: Implement `scripts/showcase.py`** — the capture helper, the seed helpers, and the hero scene:
```python
"""Generate the README showcase PNGs from the real cockpit, headlessly and
deterministically (seeded logs + injected fixed clock). Run: `uv run python -m scripts.showcase`."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from runstate import open_channel

from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import explicit_resolver

NOW = 300.0


def _corrupt(root: Path, run_id: str, seq: int, literal: str) -> None:
    conn = sqlite3.connect(str(root / f"{run_id}.db"))
    conn.execute("UPDATE log SET body = ? WHERE seq = ?", (literal, seq))
    conn.commit()
    conn.close()


def _ch(root: Path, rid: str):
    return open_channel(rid, root=root, backend="sqlite")


async def capture(
    app,
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


def _seed_hero(root: Path) -> list[tuple[str, str, str]]:
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
    c.send({"completed": True, "error": None, "final_step": 5000, "t": 205.0}, topic="lifecycle.stopped")
    c.close()
    c = _ch(root, "sweep-11")  # errored
    c.send({"handle": "local://h/4", "t": 100.0}, topic="lifecycle.started")
    c.send({"step": 300, "consumed_seq": 0, "t": 180.0}, topic="lifecycle.heartbeat")
    c.send({"completed": False, "error": "CUDA OOM", "final_step": 300, "t": 185.0}, topic="lifecycle.stopped")
    c.close()
    c = _ch(root, "sweep-07")  # live + undischarged stop -> ■1
    c.send({"handle": "local://h/5", "t": 294.0}, topic="lifecycle.started")
    c.send({"step": 22, "consumed_seq": 0, "t": 298.0}, topic="lifecycle.heartbeat")
    c.send({}, topic="control.stop", request_id="webui:stop1")
    c.close()
    c = _ch(root, "queued-run")  # pending (subscribed, not started)
    c.send({"schedule": {}, "names": ["loss"]}, topic="control.subscribe", request_id="webui:sub1")
    c.close()
    return [(r, str(root), "sqlite") for r in
            ("train-mnist", "train-cifar", "train-resnet", "sweep-11", "sweep-07", "queued-run")]


async def scene_table(out_dir: Path) -> Path:
    import tempfile

    root = Path(tempfile.mkdtemp())
    refs = _seed_hero(root)
    app = MultiRunApp(explicit_resolver(refs), Env(clock=lambda: NOW, objective="loss"), tick_interval=999)

    async def before(pilot):  # park the cursor on a healthy (live) row
        t = pilot.app.query_one("#runs")
        from runstate_tui.resolver import ref_key
        t.move_cursor(row=t.get_row_index(ref_key(refs[0])))

    return await capture(app, out_dir / "table.png", before=before, title="runstate-tui — sweep")


SCENES: dict[str, Callable[[Path], Awaitable[Path]]] = {"table": scene_table}


def main(out_dir: str = "docs/img") -> list[Path]:
    out = Path(out_dir)
    return [asyncio.run(scene(out)) for scene in SCENES.values()]


if __name__ == "__main__":
    for p in main():
        print(f"wrote {p}")
```

- [ ] **Step 5: Run the test → pass. Generate for real** (`uv run python -m scripts.showcase` → `docs/img/table.png`). Eyeball it (dots colored, `■`/`⚠` intact, cursor on the live row). **Step 6: gates; commit** `feat(showcase): generator infra + hero multi-run-table scene` (commit `docs/img/table.png` + `table.svg`).

---

### Task 4: The remaining four scenes

**Files:** Modify `scripts/showcase.py`; Modify `tests/test_showcase.py`.

Add four scene functions and register them in `SCENES`. Each reuses `capture` + its own seed. Follow the Task-3 pattern exactly.

- [ ] **Step 1: Extend the test** to assert all five PNGs render (parametrize over `SCENES`):
```python
@pytest.mark.parametrize("name", ["table", "single", "integrity", "drilldown", "stop"])
@pytest.mark.asyncio
async def test_every_scene_renders(name, tmp_path):
    import importlib.util
    if importlib.util.find_spec("cairosvg") is None:
        pytest.skip("cairosvg not installed")
    from scripts.showcase import SCENES
    out = await SCENES[name](tmp_path)
    assert out.exists() and out.stat().st_size > 0
```

- [ ] **Step 2: fail (scenes undefined). Step 3: implement the four scenes** in `scripts/showcase.py`:
  - **`scene_single`** — the single-run `SingleRunApp` on one healthy live run (started `t=280` + hb `t=292,step=1450` + `value loss=0.0123`), clock `NOW`, `objective="loss"`. `capture(app, out/"single.png")`. (Import `SingleRunApp` from `runstate_tui.app`; construct as the CLI does — `SingleRunApp(ref, Env(clock=lambda: NOW, objective="loss"))`; verify its ctor signature.)
  - **`scene_integrity`** — a `MultiRunApp` over four refs: `corrupt` (seed started+hb, then `_corrupt(root, id, 2, "{not json")`), `unreadable` (a foreign sqlite: `CREATE TABLE unrelated(...)` written directly, per `conftest.foreign_db`), `missing` (a ref to a file never created), `malformed` (seed started+hb+value, then `_corrupt(root, id, 3, "42")` — alien int body → malformed issue → `⚠`). `capture(app, out/"integrity.png")`.
  - **`scene_drilldown`** — a rich run (started + hb + `value` + `control.subscribe` + `control.stop`, like `conftest.rich_run` but on sqlite), `MultiRunApp` over `[ref]`, and a `before` hook that presses `enter` to open the `DrillDownScreen`, then `pause`. `capture(..., out/"drilldown.png")`.
  - **`scene_stop`** — a live run, `SingleRunApp`, `before` presses `s` to open `ConfirmStopScreen`. `capture(..., out/"stop.png")`. (Confirm `s` is the stop binding and the modal id; adapt from `tests/test_app.py`.)
  Register all four in `SCENES`.

- [ ] **Step 4: pass. Generate all** (`uv run python -m scripts.showcase`), eyeball each. **Step 5: gates; commit** `feat(showcase): single-run / integrity / drill-down / stop scenes` (+ the 4 PNGs/SVGs).

---

### Task 5: README "Screens" section + CI smoke-test

**Files:** Modify `README.md`, `.github/workflows/ci.yml`.

- [ ] **Step 1: Add a "Screens" section to `README.md`** embedding the five PNGs in scene order, each with a one-line caption:
```markdown
## Screens

The multi-run table — the whole sweep at a glance (a `●` traffic-light per run):

![multi-run table](docs/img/table.png)

A single run · the integrity taxonomy (one bad run is a loud row, never a crash) · the drill-down · the confirm-gated stop:

![single run](docs/img/single.png)
![integrity taxonomy](docs/img/integrity.png)
![drill-down](docs/img/drilldown.png)
![stop confirm](docs/img/stop.png)
```
(Place it high — right after the intro/tagline. Match the README's existing heading style.)

- [ ] **Step 2: Wire the generator into CI as a smoke-test** in `.github/workflows/ci.yml`: after the existing test step, add a step that installs the dev group (with `cairosvg`) and runs `uv run python -m scripts.showcase` into a temp dir, e.g.:
```yaml
      - name: Showcase renders (smoke)
        run: uv run python -m scripts.showcase
```
(It regenerates into `docs/img/`; CI asserts only that it exits 0 — no pixel-diff. If regenerating tracked files in CI is undesirable, point `main()` at a temp `out_dir` via an env var / arg and pass that. Choose the least-surprising option and note it.)

- [ ] **Step 3: Verify** — `uv run python -m scripts.showcase` regenerates cleanly; the README renders locally (paths resolve); `uv run pytest -q` green; full gates green.

- [ ] **Step 4: Commit** `docs(readme): Screens section + CI showcase smoke-test`.

---

## Self-Review

- **Spec coverage:** product tweaks — `⏹`→`■` (T1), `status_color` + `●` dot column (T2); generator infra + hero (T3); the other 4 scenes (T4); README + CI smoke-test (T5). GIFs + pixel-diff intentionally absent.
- **Placeholders:** T2/T3/T4 flag "verify the exact Textual 8.2.8 accessor" points (`DataTable.get_cell`, `SingleRunApp`/`ConfirmStopScreen` ctors/bindings, scripts importability) — the implementer confirms empirically; all seed data + logic is concrete (terminal vocab: `lifecycle.stopped {completed,error,final_step,t}`; corrupt via raw `UPDATE`).
- **Type consistency:** `status_color(status) -> str`, `_COLUMNS` (8 keys, `"dot"` first), `_cells` (8 values), `capture(...)`, `scene_*`, `SCENES`, `main(out_dir)` names match across tasks.
- **Determinism:** every scene = seeded logs + `Env(clock=lambda: NOW)` + fixed `size`. `NOW=300` chosen against the seeded `t`s for the intended live/stale spread.
