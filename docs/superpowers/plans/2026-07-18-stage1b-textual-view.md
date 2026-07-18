# Stage 1b — the single-run Textual view Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** `runstate-tui <run.db>` — a Textual app that renders one run's `Row` live at ~1 Hz, folding **off the render thread** so a blocking sqlite open never freezes the UI, tested headlessly with `run_test`.

**Architecture:** The app folds via the Stage-1a `render_single(ref, env)` (the singleton path — no bespoke data path) inside a **threaded worker** (`@work(thread=True, exclusive=True)`), formats the `Row` to a line with a pure `format_row`, and updates a single `Static` widget via `self.call_from_thread`. The next tick is rescheduled *inside the worker after the update*, so ticks never overlap (spec §13). `exclusive=True` on the fold worker realizes the "single I/O owner thread" for the |I|=1 case.

**Tech Stack:** Python ≥ 3.11, **textual 8.2.8** (already a dependency), the completed Stage-0/1a `runstate_tui` core, `runstate` (`open_channel`), pytest via `uv run`.

## Global Constraints

- **Python ≥ 3.11; textual ≥ 8.2.8.** The verified pattern (spike):
  - Fold on a **threaded worker** `@work(thread=True, exclusive=True)` — never on the render thread (spec §13).
  - Update a widget from the worker **only** via `self.call_from_thread(widget.update, text)` (Textual forbids touching widgets from a thread).
  - **Reschedule the next tick inside the worker, after the update**, via `self.call_from_thread(self.set_timer, interval, self._tick)` — this prevents overlapping ticks. Do **not** use `set_interval` (fixed cadence → pile-up risk).
  - Fire the **first tick directly** in `on_mount` (`self._tick()`); `set_timer(0, …)` is invalid in textual (raises `ZeroDivisionError`).
- **Render via `render_single(ref, env)`** (Stage 1a) — one data path, the §11 singleton invariant; the view holds no bespoke fold.
- **Tests use `App.run_test()`** and, because threaded workers run on real OS threads, `await app.workers.wait_for_complete()` before asserting. Use plain sync test functions that call `asyncio.run(async_body())` — **no `pytest-asyncio`** dependency.
- **`uv run`** for all commands; keep ruff + mypy-`strict` + pytest-cov green.
- **Out of scope (documented, not stubbed):** the multi-run table + `DataTable` keyed reconcile + LRU pool (Stage 4); the `stop` action + dedicated stop thread + confirm gate (Stage 2); selection/scroll/filter; a metric-picker (the value shows only when `env.objective` is set — the MVP CLI leaves it `None`).

---

## File Structure

- `runstate_tui/format.py` — `format_row(row) -> str` (pure).
- `runstate_tui/resolver.py` — add `ref_from_path(path) -> RunRef`.
- `runstate_tui/app.py` — `SingleRunApp`.
- `runstate_tui/__main__.py` — `main` (the CLI entry).
- `pyproject.toml` — add `[project.scripts] runstate-tui`.
- `tests/test_format.py`, `tests/test_app.py`, tests appended to `tests/test_resolver.py`.

---

### Task 1: `format_row` — the `Row` → display line (pure)

**Files:**
- Create: `runstate_tui/format.py`, `tests/test_format.py`

**Interfaces:**
- Consumes: `Row` (Stage 0).
- Produces: `format_row(row: Row) -> str` — `"<status.label>  step <frontier>  <freshness>s ago  <name>=<value> @ <step>  ran <elapsed>s  ⚠ <issue.message>…"`, omitting any `None`/absent factor.

- [ ] **Step 1: Write the failing test**

`tests/test_format.py`:

```python
from runstate_tui.format import format_row
from runstate_tui.types import Issue, IssueKind, Row, Severity, Status


def _row(**kw):
    base = dict(status=Status.live(), frontier=None, freshness=None, value=None,
                elapsed=None, episode=None, issues=())
    base.update(kw)
    return Row(**base)


def test_format_row_full_quintet():
    row = _row(status=Status.live(), frontier=7, freshness=10.0,
               value=("loss", 0.03, 7), elapsed=50.0)
    text = format_row(row)
    assert "live" in text
    assert "step 7" in text
    assert "10s ago" in text
    assert "loss=0.03 @ 7" in text
    assert "ran 50s" in text


def test_format_row_missing_is_just_the_label():
    assert format_row(_row(status=Status.missing())) == "missing"


def test_format_row_surfaces_issue_badges():
    torn = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    text = format_row(_row(frontier=3, issues=(torn,)))
    assert "⚠ log torn at seq 4012" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_format.py -v`
Expected: FAIL — `ModuleNotFoundError` for `runstate_tui.format`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/format.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_format.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/format.py tests/test_format.py
git commit -m "feat(format): format_row — Row -> display line"
```

---

### Task 2: `ref_from_path` — a run's `.db` path → `RunRef`

**Files:**
- Modify: `runstate_tui/resolver.py`
- Modify: `tests/test_resolver.py`

**Interfaces:**
- Produces: `ref_from_path(path: str) -> RunRef` — `<root>/<run_id>.db` → `(run_id, root, "sqlite")` (run_id = the filename stem, root = the parent dir).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_resolver.py`:

```python
from runstate_tui.resolver import ref_from_path


def test_ref_from_path_splits_a_sqlite_db_path():
    assert ref_from_path("/tmp/runs/lattice-b6.1.db") == ("lattice-b6.1", "/tmp/runs", "sqlite")
    assert ref_from_path("run.db") == ("run", ".", "sqlite")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolver.py -k ref_from_path -v`
Expected: FAIL — `ImportError` for `ref_from_path`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/resolver.py` (add `from pathlib import Path` at the top with the existing imports):

```python
from pathlib import Path  # add to the existing imports at the top of the file


def ref_from_path(path: str) -> RunRef:
    """A sqlite run log lives at ``<root>/<run_id>.db``; split a path into its RunRef."""
    p = Path(path)
    return (p.stem, str(p.parent), "sqlite")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/resolver.py tests/test_resolver.py
git commit -m "feat(resolver): ref_from_path — <root>/<run_id>.db -> RunRef"
```

---

### Task 3: `SingleRunApp` — the live view (threaded fold, run_test)

**Files:**
- Create: `runstate_tui/app.py`, `tests/test_app.py`

**Interfaces:**
- Consumes: `render_single` (Stage 1a), `format_row` (Task 1), `Env`, `RunRef`; `textual`.
- Produces: `class SingleRunApp(App[None])` with `__init__(self, ref: RunRef, env: Env, tick_interval: float = 1.0)`, a `Static(id="run")`, a threaded `_fold` worker, and the `on_mount`→`_tick`→`_fold`→reschedule cycle.

- [ ] **Step 1: Write the failing test**

`tests/test_app.py`:

```python
import asyncio

from runstate import open_channel
from textual.widgets import Static

from runstate_tui.app import SingleRunApp
from runstate_tui.env import Env


def _live_sqlite_run(tmp_path):
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    ch.send({"step": 7, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
    ch.close()
    return ("r", str(tmp_path), "sqlite")


def test_single_run_app_renders_the_folded_row(tmp_path):
    asyncio.run(_render(tmp_path))


async def _render(tmp_path):
    ref = _live_sqlite_run(tmp_path)
    # a large tick_interval so only ONE fold runs during the test
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=30.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()  # threaded worker runs off the loop
        content = str(app.query_one("#run", Static).content)
        assert "live" in content
        assert "step 7" in content
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -v`
Expected: FAIL — `ModuleNotFoundError` for `runstate_tui.app`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/app.py`:

```python
from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Static

from .env import Env
from .format import format_row
from .resolver import RunRef
from .table import render_single


class SingleRunApp(App[None]):
    """The single-run cockpit: folds one run OFF the render thread at ~1 Hz and
    shows its Row. The fold is a threaded (exclusive) worker, so a blocking
    sqlite open never freezes the UI; the next tick is rescheduled inside the
    worker after the update, so ticks never overlap (spec §13)."""

    def __init__(self, ref: RunRef, env: Env, tick_interval: float = 1.0) -> None:
        super().__init__()
        self._ref = ref
        self._env = env
        self._tick_interval = tick_interval

    def compose(self) -> ComposeResult:
        yield Static("loading…", id="run")

    def on_mount(self) -> None:
        self._tick()  # first tick now (set_timer(0, …) is invalid in textual)

    def _tick(self) -> None:
        self._fold()

    @work(thread=True, exclusive=True)
    def _fold(self) -> None:
        row = render_single(self._ref, self._env)  # blocking fold, off the render thread
        text = format_row(row)
        display = self.query_one("#run", Static)
        self.call_from_thread(display.update, text)  # widget touch must go via call_from_thread
        self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
```

> Implementer note: if `mypy --strict` flags the `@work` decorator or `App[None]`, resolve it with the textual-provided types (textual ships `py.typed`) — do not add `# type: ignore` without first checking the real signature. The spike confirmed this exact code runs under textual 8.2.8.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/app.py tests/test_app.py
git commit -m "feat(app): SingleRunApp — live single-run view (threaded fold)"
```

---

### Task 4: the CLI entry — `runstate-tui <run.db>`

**Files:**
- Create: `runstate_tui/__main__.py`
- Modify: `pyproject.toml` (add `[project.scripts]`)
- Modify: `runstate_tui/__init__.py`
- Create: `tests/test_cli.py`

**Interfaces:**
- Produces: `main(argv: list[str] | None = None) -> int` — with exactly one arg, parses it via `ref_from_path` and runs `SingleRunApp(ref, Env(clock=time.time))`; otherwise prints usage and returns `2`. Registered as the `runstate-tui` console script.

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
from runstate_tui.__main__ import main


def test_no_argument_prints_usage_and_returns_2(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_too_many_arguments_returns_2():
    assert main(["a.db", "b.db"]) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError` for `runstate_tui.__main__`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/__main__.py`:

```python
from __future__ import annotations

import sys
import time

from .app import SingleRunApp
from .env import Env
from .resolver import ref_from_path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: runstate-tui <run.db>", file=sys.stderr)
        return 2
    ref = ref_from_path(args[0])
    SingleRunApp(ref, Env(clock=time.time)).run()  # real wall-clock; blocks until quit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Add to `pyproject.toml` (a new top-level table):

```toml
[project.scripts]
runstate-tui = "runstate_tui.__main__:main"
```

- [ ] **Step 4: Run test to verify it passes + full suite + gates**

Run:
```bash
uv run pytest tests/test_cli.py -v
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
```
Expected: PASS (all green).

- [ ] **Step 5: Re-export and commit**

Append to `runstate_tui/__init__.py`:

```python
from .format import format_row  # noqa: F401
from .app import SingleRunApp  # noqa: F401
from .resolver import ref_from_path  # noqa: F401
```

```bash
git add runstate_tui/__main__.py runstate_tui/__init__.py pyproject.toml uv.lock tests/test_cli.py
git commit -m "feat(cli): runstate-tui <run.db> entry point"
```

---

## Self-Review

- **Spec coverage:** the single-run view renders via `render_single` (singleton path) ✓; fold off the render thread on a threaded exclusive worker, reschedule-inside-worker (no pile-up), first-tick-in-on_mount ✓ (§13); `format_row` renders the truth-quintet + issue badges ✓; `runstate-tui <run.db>` entry ✓. Deferred per Global Constraints: the multi-run DataTable/pool (Stage 4), `stop` + stop thread (Stage 2), selection/filter, metric-picker.
- **Placeholder scan:** none — complete code + exact `uv run` commands. The one implementer-note (mypy on `@work`/`App[None]`) points at the real fix, not a placeholder; the spike confirmed the code runs.
- **Type consistency:** `format_row`/`ref_from_path`/`SingleRunApp`/`main` signatures are consistent across tasks; `SingleRunApp` consumes the Stage-1a `render_single` and Stage-0 `Env`/`Row` unchanged; `Env(clock=time.time)` uses the real clock in the CLI while tests inject a fixed clock.
