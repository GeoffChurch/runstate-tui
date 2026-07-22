# Glob Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add live, recursive directory discovery to the shipped multi-run table — `runstate-tui <dir>` watches every `*.db` run under a directory, re-scanned each frame.

**Architecture:** A `glob_resolver(root)` (a `Path.rglob` re-scan) drops into the existing `MultiRunApp` resolver seam with zero changes to the fold, pool, reconcile, or concurrency model. A pure `disambiguate(refs)` function gives colliding run-id stems (common under recursive globbing) a minimal unique path label; it is a no-op when stems are unique, so it applies globally with no churn. A quiet zero-match placeholder covers "watching before any run starts." One new CLI dispatch branch (`Path.is_dir()`) wires it up.

**Tech Stack:** Python 3.11, `pathlib`, runstate (`attach_channel`), Textual 8.2.8 (`DataTable`, `run_test`/`Pilot`), Rich (`Text`), uv, ruff, mypy `--strict`, pytest.

## Global Constraints

- **Python floor `>=3.11`.** No 3.13-only APIs (e.g. `Path.glob(recurse_symlinks=…)` does not exist here).
- **Use `pathlib.Path.rglob`, never `glob.glob(recursive=True)`.** Measured (2026-07-21 design): `rglob` does not recurse into symlinked directories (so a cyclic symlink neither hangs nor explodes the scan) but still matches symlinked files. `glob.glob(recursive=True)` follows symlinked dirs and explodes on a cycle.
- **No pre-filtering of glob matches** (faithful representation). A foreign / empty / half-written `.db` opens via `attach_channel` (never `create_channel`) and reads `missing`/`unreadable`, byte-identical — the fold classifies it; the resolver does not.
- **`RunRef` is the semantic boundary.** The disambiguation label and the placeholder are view-only, derived from the `RunRef` — never part of the fold, the pool key, or the `attach_channel` inputs.
- **Tests use `asyncio.run(...)` wrappers, never `@pytest.mark.asyncio`** (the repo has no pytest-asyncio). Reuse the `_seed` helper pattern already in `tests/test_multirun.py`.
- **Gates (all must pass before every commit):** during implementation run `uv run ruff format .`
  to *apply* formatting (idempotent), then `uv run ruff check .`, `uv run mypy`, `uv run pytest`. CI
  verifies formatting with `uv run ruff format --check .`, so applying it locally keeps CI green.

## File Structure

- `runstate_tui/resolver.py` (modify) — add `glob_resolver` (live discovery) and `disambiguate` (the pure minimal-backtrack labeler), beside the existing `const_resolver`/`explicit_resolver`/`ref_from_path`/`ref_key`.
- `runstate_tui/multirun.py` (modify) — `_cells` takes the display `label`; `on_table_ready` builds the per-frame label map; the `empty_hint` param + `#empty` placeholder toggle.
- `runstate_tui/__main__.py` (modify) — the `Path.is_dir()` dispatch branch + usage string.
- `tests/test_resolver.py`, `tests/test_multirun.py`, `tests/test_cli.py` (modify) — append tests.

No `runstate_tui/__init__.py` change: `glob_resolver`/`disambiguate` are imported directly from `.resolver` by their consumers.

---

### Task 1: `glob_resolver` — live recursive discovery

**Files:**
- Modify: `runstate_tui/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `RunRef`, `Resolver`, `ref_from_path` (all existing in `resolver.py`); `pathlib.Path` (already imported).
- Produces: `glob_resolver(root: str) -> Resolver`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_resolver.py`:

```python
def test_glob_resolver_discovers_nested_db_files(tmp_path):
    from runstate_tui.resolver import glob_resolver, ref_from_path

    (tmp_path / "exp1").mkdir()
    (tmp_path / "a.db").write_text("")
    (tmp_path / "exp1" / "trial.db").write_text("")
    refs = glob_resolver(str(tmp_path))(0.0)
    assert set(refs) == {
        ref_from_path(str(tmp_path / "a.db")),
        ref_from_path(str(tmp_path / "exp1" / "trial.db")),
    }


def test_glob_resolver_is_live_reflecting_new_files(tmp_path):
    from runstate_tui.resolver import glob_resolver

    resolve = glob_resolver(str(tmp_path))
    assert resolve(0.0) == []
    (tmp_path / "new.db").write_text("")
    assert [r[0] for r in resolve(1.0)] == ["new"]


def test_glob_resolver_dedupes_matches(tmp_path):
    # rglob won't emit a path twice today, but the resolver contract is a deduped IndexSet;
    # pin it so a future change can't leak a duplicate RunRef -> a duplicate DataTable row.
    from runstate_tui.resolver import glob_resolver

    (tmp_path / "a.db").write_text("")
    refs = glob_resolver(str(tmp_path))(0.0)
    assert len(refs) == len(set(refs))


def test_glob_resolver_is_symlink_cycle_safe(tmp_path):
    import os

    from runstate_tui.resolver import glob_resolver

    (tmp_path / "sub").mkdir()
    (tmp_path / "a.db").write_text("")
    (tmp_path / "sub" / "b.db").write_text("")
    os.symlink(tmp_path, tmp_path / "sub" / "loop")  # a DIRECTORY cycle
    # Must RETURN (not hang) and NOT explode into sub/loop/sub/loop/... entries:
    # pathlib.rglob does not recurse into symlinked directories.
    refs = glob_resolver(str(tmp_path))(0.0)
    assert sorted(r[0] for r in refs) == ["a", "b"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_resolver.py -k glob -v`
Expected: FAIL — `ImportError: cannot import name 'glob_resolver'`.

- [ ] **Step 3: Implement `glob_resolver`** — add to `runstate_tui/resolver.py` (below `explicit_resolver`, above `ref_key`):

```python
def glob_resolver(root: str) -> Resolver:
    """A LIVE resolver over a directory: each frame, discover every ``*.db`` run under
    `root` (recursively) and return their RunRefs. Uses ``Path.rglob`` -- which does NOT
    recurse into symlinked directories -- so a cyclic symlink can neither hang nor explode
    the scan (verified 2026-07-21). Matches open via ``attach_channel`` (never create), so
    a stale / foreign / half-written ``.db`` reads ``missing`` / ``unreadable`` and is left
    byte-identical -- the fold classifies it, the resolver does not pre-filter. Order is
    irrelevant: the table sorts on the (disambiguated) run column."""
    root_path = Path(root)

    def resolve(_now: float) -> list[RunRef]:
        refs = [ref_from_path(str(p)) for p in root_path.rglob("*.db")]
        return list(dict.fromkeys(refs))  # dedup, order preserved

    return resolve
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resolver.py -k glob -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add runstate_tui/resolver.py tests/test_resolver.py
git commit -m "feat(resolver): glob_resolver — live recursive Path.rglob discovery"
```

---

### Task 2: `disambiguate` — minimal-backtrack run labels

**Files:**
- Modify: `runstate_tui/resolver.py`
- Test: `tests/test_resolver.py`

**Interfaces:**
- Consumes: `RunRef`, `ref_key` (existing); `pathlib.Path`; `collections.abc.Sequence` (new import).
- Produces: `disambiguate(refs: Sequence[RunRef]) -> dict[str, str]` — keyed by `ref_key(ref)`, value is the display label.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_resolver.py`:

```python
def test_disambiguate_is_a_noop_when_stems_are_unique():
    from runstate_tui.resolver import disambiguate, ref_key

    refs = [("a", "/root", "sqlite"), ("b", "/root", "sqlite")]
    labels = disambiguate(refs)
    assert labels[ref_key(refs[0])] == "a"
    assert labels[ref_key(refs[1])] == "b"


def test_disambiguate_grows_only_the_colliding_group():
    # 99 unique stems + one colliding pair: ONLY the pair grows a parent level; the rest
    # stay bare (ragged-minimal, not uniform-depth).
    from runstate_tui.resolver import disambiguate, ref_key

    refs = [(f"run{i:03d}", "/runs/g1", "sqlite") for i in range(1, 100)]
    refs += [("run000", "/runs/g1", "sqlite"), ("run000", "/runs/g2", "sqlite")]
    labels = disambiguate(refs)
    assert labels[ref_key(("run050", "/runs/g1", "sqlite"))] == "run050"  # untouched
    assert labels[ref_key(("run000", "/runs/g1", "sqlite"))] == "g1/run000"
    assert labels[ref_key(("run000", "/runs/g2", "sqlite"))] == "g2/run000"


def test_disambiguate_backtracks_deeper_when_the_parent_also_collides():
    from runstate_tui.resolver import disambiguate, ref_key

    a = ("trial", "/runs/a/g", "sqlite")
    b = ("trial", "/runs/b/g", "sqlite")  # same stem AND same parent dir name "g"
    labels = disambiguate([a, b])
    assert labels[ref_key(a)] == "a/g/trial"
    assert labels[ref_key(b)] == "b/g/trial"


def test_disambiguate_terminates_on_suffix_overlap():
    # One run's full path is a suffix of the other's -> the shorter maxes out while the
    # longer keeps growing; must terminate and disambiguate, not loop forever.
    from runstate_tui.resolver import disambiguate, ref_key

    short = ("trial", "/x", "sqlite")   # parts end (..., "x", "trial")
    long_ = ("trial", "/y/x", "sqlite")  # parts end (..., "y", "x", "trial")
    labels = disambiguate([short, long_])
    assert labels[ref_key(short)] != labels[ref_key(long_)]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_resolver.py -k disambiguate -v`
Expected: FAIL — `ImportError: cannot import name 'disambiguate'`.

- [ ] **Step 3: Implement `disambiguate`** — first extend the import at the top of `runstate_tui/resolver.py`:

```python
from collections.abc import Callable, Sequence
```

Then add the function (below `glob_resolver`, above `ref_key`):

```python
def disambiguate(refs: Sequence[RunRef]) -> dict[str, str]:
    """Map each ref (by ``ref_key``) to the SHORTEST trailing path suffix that is unique
    across `refs`. Start every run at its bare stem; any group that still collides grows
    one more parent level; repeat until no group collides. Ragged-minimal -- a lone
    collision never lengthens the labels of already-unique runs. A NO-OP when every stem
    is unique (each label is the bare stem), so applying it globally never changes a table
    whose stems don't collide. Distinct refs have distinct part-tuples and `grew` only
    flips when a depth actually increases, so the loop always terminates (worst case: the
    full path)."""
    parts: dict[str, tuple[str, ...]] = {ref_key(r): Path(r[1], r[0]).parts for r in refs}
    depth: dict[str, int] = {k: 1 for k in parts}

    def label(k: str) -> str:
        return "/".join(parts[k][-depth[k] :])

    while True:
        groups: dict[str, list[str]] = {}
        for k in parts:
            groups.setdefault(label(k), []).append(k)
        grew = False
        for members in groups.values():
            if len(members) > 1:
                for k in members:
                    if depth[k] < len(parts[k]):
                        depth[k] += 1
                        grew = True
        if not grew:
            break
    return {k: label(k) for k in parts}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resolver.py -k disambiguate -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add runstate_tui/resolver.py tests/test_resolver.py
git commit -m "feat(resolver): disambiguate — minimal-backtrack unique run labels"
```

---

### Task 3: wire the disambiguation label into the table

**Files:**
- Modify: `runstate_tui/multirun.py`
- Test: `tests/test_multirun.py`

**Interfaces:**
- Consumes: `disambiguate` (Task 2); the existing `_cells`, `on_table_ready`, `_COLUMNS`, `ref_key`.
- Produces: `_cells(row: Row, label: str) -> tuple[Text, str, str, str, str, str, str, Text]` (new signature — `ref` dropped, `label` added); `on_table_ready` builds `disambiguate([ref for ref, _ in msg.table])` and passes `labels[key]` into `_cells`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_multirun.py` (the module already defines `_seed`, and imports `asyncio`, `DataTable`, `Env`, `MultiRunApp`, `explicit_resolver`, `ref_key`):

```python
def test_table_disambiguates_colliding_run_stems(tmp_path):
    asyncio.run(_table_disambiguates_colliding_run_stems(tmp_path))


async def _table_disambiguates_colliding_run_stems(tmp_path):
    # Two runs with the SAME stem "trial" in different subdirs (the recursive-glob sweep
    # case). Distinct rows (keyed by full RunRef); the `run` column must show the minimal
    # disambiguating path, not two identical "trial"s.
    g1 = tmp_path / "g1"
    g1.mkdir()
    g2 = tmp_path / "g2"
    g2.mkdir()
    a = _seed(g1, "trial")
    b = _seed(g2, "trial")
    app = MultiRunApp(explicit_resolver([a, b]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.get_cell(ref_key(a), "run") == "g1/trial"
        assert t.get_cell(ref_key(b), "run") == "g2/trial"


def test_table_run_column_is_bare_stem_when_unique(tmp_path):
    asyncio.run(_table_run_column_is_bare_stem_when_unique(tmp_path))


async def _table_run_column_is_bare_stem_when_unique(tmp_path):
    # The no-op property: unique stems -> the `run` cell is the bare stem, exactly as
    # before the disambiguation label existed (no churn to existing behavior).
    a = _seed(tmp_path, "alpha")
    b = _seed(tmp_path, "beta")
    app = MultiRunApp(explicit_resolver([a, b]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        t = app.query_one("#runs", DataTable)
        assert t.get_cell(ref_key(a), "run") == "alpha"
        assert t.get_cell(ref_key(b), "run") == "beta"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_multirun.py -k "disambiguat or bare_stem" -v`
Expected: FAIL — `test_table_disambiguates_colliding_run_stems` asserts `"g1/trial"` but the current `_cells` puts the bare stem `"trial"` in the `run` cell.

- [ ] **Step 3: Implement the label wiring** — three edits in `runstate_tui/multirun.py`:

Edit the resolver import (add `disambiguate`):

```python
from .resolver import Resolver, RunRef, disambiguate, ref_key
```

Replace `_cells` (drop the unused `ref`, take `label`):

```python
def _cells(row: Row, label: str) -> tuple[Text, str, str, str, str, str, str, Text]:
    """The 8 column cells — same field semantics as format_row, one field per column.
    `label` is the run's display name (the disambiguated minimal path from
    `disambiguate`; a bare stem when the run_id is unique). The leading `dot` cell is a
    traffic-light ● redundant with the `status` text cell (never the sole signal)."""
    dot = Text("●", style=status_color(row.status))
    status = row.status.label + (f": {row.status.detail}" if row.status.detail else "")
    step = "" if row.frontier is None else str(row.frontier)
    age = "" if row.freshness is None else f"{row.freshness:.0f}s"
    if row.value is None:
        value = ""
    else:
        name, val, vstep = row.value
        value = f"{name}={val}" + (f"@{vstep}" if vstep is not None else "")
    elapsed = "" if row.elapsed is None else f"{row.elapsed:.0f}s"
    return (dot, label, status, step, age, value, elapsed, _marker(row))
```

In `on_table_ready`, build the label map right after `want`, and change the `_cells` call. The `want` line and the reconcile loop become:

```python
        want = {ref_key(ref) for ref, _ in msg.table}
        labels = disambiguate([ref for ref, _ in msg.table])
```

and inside the `for ref, row in msg.table:` loop, replace `cells = _cells(ref, row)` with:

```python
                cells = _cells(row, labels[key])
```

(the surrounding `key = ref_key(ref)` / `add_row` / `update_cell` lines are unchanged).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_multirun.py -v`
Expected: PASS — the two new tests pass and every existing `test_multirun` test (which uses unique stems) stays green, proving the no-op property.

- [ ] **Step 5: Run the gates**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`
Expected: all green (the full suite + showcase scenes, unique-stemmed, are unchanged).

- [ ] **Step 6: Commit**

```bash
git add runstate_tui/multirun.py tests/test_multirun.py
git commit -m "feat(multirun): disambiguated run-column labels for colliding stems"
```

---

### Task 4: zero-match placeholder

**Files:**
- Modify: `runstate_tui/multirun.py`
- Test: `tests/test_multirun.py`

**Interfaces:**
- Consumes: `MultiRunApp` (existing `__init__`, `compose`, `on_mount`, `on_table_ready`).
- Produces: `MultiRunApp(__init__(..., empty_hint: str | None = None))`; a `#empty` `Static` shown (and the `#runs` table hidden) exactly when `empty_hint is not None and the frame resolved to no runs`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_multirun.py` (the module already imports `Static`):

```python
def test_zero_match_shows_placeholder_then_swaps_to_table(tmp_path):
    asyncio.run(_zero_match_shows_placeholder_then_swaps(tmp_path))


async def _zero_match_shows_placeholder_then_swaps(tmp_path):
    # Glob mode with an empty_hint: an empty frame shows the placeholder and hides the
    # table; when a run appears, the placeholder hides and the table shows.
    live = {"refs": []}
    app = MultiRunApp(
        lambda now: list(live["refs"]),
        Env(clock=lambda: 150.0),
        tick_interval=999,
        empty_hint="watching /runs/**/*.db — no runs yet",
    )
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        empty = app.query_one("#empty", Static)
        t = app.query_one("#runs", DataTable)
        assert empty.display and not t.display
        assert "no runs yet" in str(empty.content)
        live["refs"] = [_seed(tmp_path, "a")]
        app._tick()
        await pilot.pause()
        await pilot.pause()
        assert t.display and not empty.display
        assert t.row_count == 1


def test_no_empty_hint_never_shows_placeholder(tmp_path):
    asyncio.run(_no_empty_hint_never_shows_placeholder(tmp_path))


async def _no_empty_hint_never_shows_placeholder(tmp_path):
    # explicit/single mode passes no empty_hint: even a (degenerate) empty frame must not
    # pop a placeholder -- the table stays the shown widget.
    live = {"refs": [_seed(tmp_path, "a")]}
    app = MultiRunApp(lambda now: list(live["refs"]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        empty = app.query_one("#empty", Static)
        assert not empty.display
        live["refs"] = []
        app._tick()
        await pilot.pause()
        await pilot.pause()
        assert not empty.display  # no hint -> never shown
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_multirun.py -k "placeholder or empty_hint" -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'empty_hint'` (and no `#empty` widget).

- [ ] **Step 3: Implement the placeholder** — four edits in `runstate_tui/multirun.py`:

Add the `empty_hint` parameter and store it — in `__init__`, extend the signature and body:

```python
    def __init__(
        self,
        resolver: Resolver,
        env: Env,
        *,
        tick_interval: float = 1.0,
        pool_cap: int = 128,
        stall_ticks: int = 3,
        empty_hint: str | None = None,
    ) -> None:
        super().__init__()
        self._resolver = resolver
        self._env = env
        self._empty_hint = empty_hint
```

(the rest of `__init__` — `self._tick_interval = …` onward — is unchanged.)

Add the placeholder widget in `compose` (between the stall banner and the table):

```python
    def compose(self) -> ComposeResult:
        yield Static("", id="stall")  # the watchdog banner (hidden via display, see on_mount)
        yield Static("", id="empty")  # the zero-match placeholder (glob mode; toggled in reconcile)
        yield DataTable(id="runs")
```

Seed its text and hide it in `on_mount` — add these two lines right after the `#stall` line (`self.query_one("#stall", Static).display = False`):

```python
        empty = self.query_one("#empty", Static)
        empty.update(self._empty_hint or "")
        empty.display = False
```

Toggle it at the end of `on_table_ready`, AFTER the `with self.batch_update():` block (same indentation as the `t = self.query_one(...)` line, i.e. inside the method, outside the `with`):

```python
        empty = self.query_one("#empty", Static)
        if self._empty_hint is not None and not want:
            empty.display = True
            t.display = False
        else:
            empty.display = False
            t.display = True
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_multirun.py -k "placeholder or empty_hint" -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add runstate_tui/multirun.py tests/test_multirun.py
git commit -m "feat(multirun): zero-match placeholder for live glob discovery"
```

---

### Task 5: CLI dispatch — directory positional

**Files:**
- Modify: `runstate_tui/__main__.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `glob_resolver` (Task 1); `MultiRunApp(empty_hint=…)` (Task 4); existing `explicit_resolver`, `ref_from_path`, `SingleRunApp`, `Env`.
- Produces: `main()` routes a single **directory** arg to `MultiRunApp(glob_resolver(dir), …, empty_hint=…)`; a single **file** arg still routes to `SingleRunApp`; `≥2` args still route to `explicit_resolver`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_cli.py`:

```python
def test_directory_argument_constructs_multirun_with_glob(monkeypatch, tmp_path):
    import runstate_tui.__main__ as m
    from runstate_tui.resolver import ref_from_path

    made = {}

    def fake_run(self):
        made["multi"] = self
        made["refs"] = self._resolver(0.0)  # prove main() built the glob resolver

    monkeypatch.setattr(m.MultiRunApp, "run", fake_run)
    (tmp_path / "exp1").mkdir()
    (tmp_path / "a.db").write_text("")
    (tmp_path / "exp1" / "trial.db").write_text("")
    m.main([str(tmp_path)])
    assert "multi" in made
    assert set(made["refs"]) == {
        ref_from_path(str(tmp_path / "a.db")),
        ref_from_path(str(tmp_path / "exp1" / "trial.db")),
    }
    assert made["multi"]._empty_hint is not None  # glob mode wires a placeholder hint


def test_single_db_file_still_constructs_single(monkeypatch, tmp_path):
    # A single .db FILE (not a dir) still routes to SingleRunApp -- the is_dir() branch
    # must not swallow the single-file case.
    import runstate_tui.__main__ as m

    made = {}
    monkeypatch.setattr(m.SingleRunApp, "run", lambda self: made.setdefault("single", self))
    f = tmp_path / "a.db"
    f.write_text("")
    m.main([str(f)])
    assert "single" in made
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli.py -k "directory or single_db_file" -v`
Expected: FAIL — `test_directory_argument_constructs_multirun_with_glob` fails because a single dir arg currently routes to `SingleRunApp` (so `"multi"` is never set).

- [ ] **Step 3: Implement the dispatch** — replace the body of `runstate_tui/__main__.py` with:

```python
from __future__ import annotations

import sys
import time
from pathlib import Path

from .app import SingleRunApp
from .env import Env
from .multirun import MultiRunApp
from .resolver import explicit_resolver, glob_resolver, ref_from_path


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) < 1:
        print("usage: runstate-tui <run.db> | <dir> | <run.db> [<run.db> ...]", file=sys.stderr)
        return 2
    if len(args) == 1 and Path(args[0]).is_dir():
        root = args[0]
        MultiRunApp(
            glob_resolver(root),
            Env(clock=time.time),
            empty_hint=f"watching {root}/**/*.db — no runs yet",
        ).run()  # real wall-clock; blocks until quit
        return 0
    if len(args) >= 2:
        resolver = explicit_resolver([ref_from_path(p) for p in args])
        MultiRunApp(resolver, Env(clock=time.time)).run()  # real wall-clock; blocks until quit
        return 0
    ref = ref_from_path(args[0])
    SingleRunApp(ref, Env(clock=time.time)).run()  # real wall-clock; blocks until quit
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli.py -v`
Expected: PASS — the two new tests pass and the existing ones (`test_no_argument_prints_usage_and_returns_2`, `test_two_paths_construct_multirun`, `test_one_path_still_constructs_single`, `test_no_args_is_usage_error`) stay green.

- [ ] **Step 5: Run the gates**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy && uv run pytest`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add runstate_tui/__main__.py tests/test_cli.py
git commit -m "feat(cli): route a directory arg to the live glob table"
```

---

## Self-Review (spec coverage)

| Spec section | Task |
|---|---|
| Invocation — directory positional, `is_dir()` dispatch, usage string | Task 5 |
| The glob resolver — `Path.rglob`, recursive, per-frame, dedup, `attach_channel`-safe | Task 1 |
| Symlinks — `rglob` non-recursion into symlinked dirs (cycle-safety regression pin) | Task 1 (`test_glob_resolver_is_symlink_cycle_safe`) |
| Minimal-backtrack label — pure `disambiguate`, ragged-minimal, terminates | Task 2 |
| No-op-on-unique-stems / global application / zero churn | Task 2 (unit) + Task 3 (`test_table_run_column_is_bare_stem_when_unique` + existing suite green) |
| Label is display-only, main-thread, threaded into `_cells`; fold/pool untouched | Task 3 |
| Zero-match placeholder + `empty_hint` | Task 4 |
| What does NOT change (concurrency/reconcile/teardown/fold/pool) | No task touches them — Tasks 3/4 edit only `_cells`/`compose`/`on_mount`/`on_table_ready` |

**Deferred (not in this plan, by design):** `--glob 'PATTERN'` flag, `--follow-symlinks`, tree-size cap + truncation banner, the `cells` resolver, uniform-depth labeling (rejected). See the spec's Deferred section.

**Placeholder scan:** none — every step contains complete code and exact commands.

**Type consistency:** `disambiguate(refs) -> dict[str, str]` keyed by `ref_key` (Task 2) is consumed as `labels[key]` where `key = ref_key(ref)` (Task 3); `_cells(row, label)` (Task 3) matches its single call site; `empty_hint: str | None` (Task 4) matches the `_empty_hint is not None` guard and the CLI keyword (Task 5).
