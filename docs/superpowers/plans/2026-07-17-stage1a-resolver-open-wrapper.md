# Stage 1a — Resolver + Open-Wrapper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Complete the `table(const[r])` data path — resolve a run reference, stat-before-open it (→ `missing`), guard the open (→ `unreadable`), else fold it (`status_fold`) — so one run produces a full `Row`, and the single-run view is literally the table at `|I|=1`. No UI.

**Architecture:** Two small modules on top of Stage 0. `resolver.py` = the `RunRef`/`Resolver` types + the trivial `const_resolver`. `table.py` = `open_and_fold` (the resolve→open→fold pipeline per run) and `render_table`/`render_single` (map it over a resolver; the single-run view is `render_table(const_resolver(ref))[0]`). Everything stays a pure function of `(ref/resolver, env)` — env's clock is the only ambient input.

**Tech Stack:** Python ≥ 3.11, `runstate` (`open_channel`), pytest. Builds directly on the completed Stage-0 `runstate_tui` package (`types`, `env`, `fold`).

## Global Constraints

- **Python ≥ 3.11.**
- **Stat-before-open — no phantom.** A missing pointer yields `Status.missing()` and **must not call `open_channel`** (which would create a `<run_id>.db`). (spec §4, §11)
- **Guard the open → `unreadable`.** A substrate error on open (`sqlite3.DatabaseError`/`OperationalError`/`PermissionError`/`OSError`) yields `Status.unreadable()`, never a crash. (spec §3.1, §4)
- **The singleton invariant.** `render_single(ref, env)` is **defined as** `render_table(const_resolver(ref), env)[0]` — one code path, no bespoke single-run screen. This is the spec §11 singleton test. (spec §1, §11)
- **`Row` stays a frozen value** — a `missing`/`unreadable` row is `Row(status, None, None, None, None, None, ())`.
- **Public-API-only runtime** — `os`/`pathlib` for the stat, `open_channel` for the open; `sqlite3` is imported only to *name* the exception types (same as Stage 0's `fold.py`), never for raw DB access.
- **Out of scope (documented, not stubbed):** the Textual UI (Stage 1b); the LRU channel pool + `glob`/`cells` resolvers (Stage 4); `conflicted` detection; the residual foreign-valid-db mutation (gated on the `create=False` upstream fix — Stage-1a resolves a *named* run, which is lower risk; note it).

---

## File Structure

- `runstate_tui/resolver.py` — `RunRef`, `Resolver`, `const_resolver`.
- `runstate_tui/table.py` — `open_and_fold`, `render_table`, `render_single`.
- `tests/test_resolver.py`, `tests/test_table.py`.

---

### Task 1: `RunRef`, `Resolver`, `const_resolver`

**Files:**
- Create: `runstate_tui/resolver.py`, `tests/test_resolver.py`

**Interfaces:**
- Produces:
  - `RunRef = tuple[str, str, str]` — `(run_id, root, backend)` (all three are what `open_channel` needs).
  - `Resolver = Callable[[float], list[RunRef]]` — `Time -> IndexSet`.
  - `const_resolver(ref: RunRef) -> Resolver` — returns a resolver that yields exactly `[ref]` for any `now`.

- [ ] **Step 1: Write the failing test**

`tests/test_resolver.py`:

```python
from runstate_tui.resolver import const_resolver


def test_const_resolver_yields_the_single_ref_regardless_of_now():
    ref = ("run-1", "/tmp/runs", "sqlite")
    resolve = const_resolver(ref)
    assert resolve(0.0) == [ref]
    assert resolve(9999.0) == [ref]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: FAIL — `ModuleNotFoundError` for `runstate_tui.resolver`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/resolver.py`:

```python
from __future__ import annotations

from collections.abc import Callable

RunRef = tuple[str, str, str]          # (run_id, root, backend) — open_channel's three inputs
Resolver = Callable[[float], list[RunRef]]  # Time -> IndexSet (re-resolved each frame)


def const_resolver(ref: RunRef) -> Resolver:
    """The singleton resolver: always exactly `[ref]`. The single-run view is the
    table taken over this (spec §1: single-run = table at |I|=1)."""
    return lambda now: [ref]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_resolver.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/resolver.py tests/test_resolver.py
git commit -m "feat(resolver): RunRef, Resolver, const_resolver"
```

---

### Task 2: `open_and_fold` — stat-before-open, guarded open, else fold

**Files:**
- Create: `runstate_tui/table.py`, `tests/test_table.py`

**Interfaces:**
- Consumes: `runstate.open_channel`; `Env` (Stage 0); `status_fold` (Stage 0); `Row`, `Status` (Stage 0); `RunRef` (Task 1).
- Produces: `open_and_fold(ref: RunRef, env: Env) -> Row` — for a sqlite ref, `os.path`-stats `<root>/<run_id>.db` first: absent → `Status.missing()` (never opens); present but open raises a substrate error → `Status.unreadable()`; else `status_fold(channel, env)` (channel closed after). Non-sqlite backends (test-only memory) skip the stat and open directly.

- [ ] **Step 1: Write the failing test**

`tests/test_table.py`:

```python
from pathlib import Path

from runstate import open_channel
from runstate_tui.env import Env
from runstate_tui.table import open_and_fold
from runstate_tui.types import StatusKind


def _env(now=150.0, **kw):
    return Env(clock=lambda: now, stuck_threshold=60.0, **kw)


def _sqlite_run(tmp_path, run_id, records):
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    for body, topic, name in records:
        ch.send(body, topic=topic, name=name)
    ch.close()


def test_open_and_fold_healthy_run(tmp_path):
    _sqlite_run(tmp_path, "r", [
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
    ])
    row = open_and_fold(("r", str(tmp_path), "sqlite"), _env())
    assert row.status.kind is StatusKind.LIVE
    assert row.frontier == 7


def test_missing_pointer_is_missing_and_creates_no_phantom(tmp_path):
    ref = ("ghost", str(tmp_path), "sqlite")
    row = open_and_fold(ref, _env())
    assert row.status.kind is StatusKind.MISSING
    assert row.frontier is None and row.issues == ()
    assert not (Path(tmp_path) / "ghost.db").exists()  # stat-before-open never opened it


def test_corrupt_db_is_unreadable(tmp_path):
    (Path(tmp_path) / "corrupt.db").write_bytes(b"this is not a sqlite database")
    row = open_and_fold(("corrupt", str(tmp_path), "sqlite"), _env())
    assert row.status.kind is StatusKind.UNREADABLE
    assert row.frontier is None and row.issues == ()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_table.py -v`
Expected: FAIL — `ModuleNotFoundError` for `runstate_tui.table`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/table.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from runstate import open_channel

from .env import Env
from .fold import status_fold
from .resolver import RunRef
from .types import Row, Status

_OPEN_ERRORS = (sqlite3.DatabaseError, sqlite3.OperationalError, PermissionError, OSError)


def _bare(status: Status) -> Row:
    """A row with a verdict but no derivable observables (missing / unreadable)."""
    return Row(status=status, frontier=None, freshness=None, value=None,
               elapsed=None, episode=None, issues=())


def open_and_fold(ref: RunRef, env: Env) -> Row:
    run_id, root, backend = ref
    # stat-before-open (sqlite): a missing pointer must NOT open_channel (that would
    # fabricate a phantom <run_id>.db into a content-addressed store) — spec §4/§11.
    if backend == "sqlite" and not (Path(root) / f"{run_id}.db").exists():
        return _bare(Status.missing())
    try:
        channel = open_channel(run_id, root=root, backend=backend)
    except _OPEN_ERRORS:
        return _bare(Status.unreadable())  # corrupt/foreign/unopenable db
    try:
        return status_fold(channel, env)
    finally:
        channel.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_table.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/table.py tests/test_table.py
git commit -m "feat(table): open_and_fold — stat-before-open + guarded open"
```

---

### Task 3: `render_table` + `render_single` (the singleton invariant)

**Files:**
- Modify: `runstate_tui/table.py`
- Modify: `runstate_tui/__init__.py`
- Modify: `tests/test_table.py`

**Interfaces:**
- Consumes: `open_and_fold` (Task 2); `Resolver`, `const_resolver` (Task 1); `Env`, `RunRef`, `Row`.
- Produces:
  - `render_table(resolver: Resolver, env: Env) -> list[Row]` — `[open_and_fold(ref, env) for ref in resolver(env.clock())]`.
  - `render_single(ref: RunRef, env: Env) -> Row` — **defined as** `render_table(const_resolver(ref), env)[0]`, so the single-run view *is* the table at `|I|=1` (one code path). Re-exported from the package.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_table.py`:

```python
from runstate_tui.resolver import const_resolver
from runstate_tui.table import render_single, render_table


def test_render_table_maps_over_the_resolver_in_order(tmp_path):
    _sqlite_run(tmp_path, "a", [({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None)])
    # "b" is a missing pointer
    resolver = lambda now: [("a", str(tmp_path), "sqlite"), ("b", str(tmp_path), "sqlite")]
    rows = render_table(resolver, _env())
    assert len(rows) == 2
    assert rows[0].status.kind is StatusKind.LIVE
    assert rows[1].status.kind is StatusKind.MISSING


def test_singleton_test_single_run_is_the_table_at_one(tmp_path):
    _sqlite_run(tmp_path, "r", [
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"step": 3, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
    ])
    ref = ("r", str(tmp_path), "sqlite")
    env = _env()  # fixed clock -> both fold passes see the same `now`
    assert render_single(ref, env) == render_table(const_resolver(ref), env)[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_table.py -k "render or singleton" -v`
Expected: FAIL — `ImportError` for `render_table`/`render_single`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/table.py`:

```python
from .resolver import Resolver, const_resolver


def render_table(resolver: Resolver, env: Env) -> list[Row]:
    return [open_and_fold(ref, env) for ref in resolver(env.clock())]


def render_single(ref: RunRef, env: Env) -> Row:
    # the single-run view IS the table at |I|=1 — one code path, no bespoke screen (spec §11)
    return render_table(const_resolver(ref), env)[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest -q`
Expected: PASS (full suite green).

- [ ] **Step 5: Re-export and commit**

Append to `runstate_tui/__init__.py`:

```python
from .resolver import RunRef, Resolver, const_resolver  # noqa: F401
from .table import open_and_fold, render_table, render_single  # noqa: F401
```

Then confirm the gates:

```bash
uv run ruff check . && uv run mypy && uv run pytest -q
git add runstate_tui/table.py runstate_tui/resolver.py runstate_tui/__init__.py tests/test_table.py
git commit -m "feat(table): render_table + render_single (singleton invariant)"
```

---

## Self-Review

- **Spec coverage:** `RunRef`/`Resolver`/`const_resolver` ✓ (Task 1); stat-before-open→`missing`, no phantom ✓ (Task 2); guarded open→`unreadable` ✓ (Task 2); `render_table` = colimit over resolver ✓, `render_single` = table at `|I|=1` (singleton invariant) ✓ (Task 3). Deferred per Global Constraints: Textual UI (1b), pool + glob/cells (Stage 4), `conflicted`, foreign-valid-db mutation (create=False upstream).
- **Placeholder scan:** none — every step has complete code and an exact `uv run` command.
- **Type consistency:** `RunRef`/`Resolver`/`const_resolver`/`open_and_fold`/`render_table`/`render_single` names and signatures are consistent across tasks and match spec §14; `open_and_fold` reuses the existing Stage-0 `status_fold`, `Row`, `Status`, `Env` unchanged.
