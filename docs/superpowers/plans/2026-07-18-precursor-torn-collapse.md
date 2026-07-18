# Precursor — collapse the torn defense (fail-fast on byte-torn) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Replace the over-lumped `Torn` defense with three honest homes for the three failures it conflated — **byte-torn → crash, substrate error → `unreadable`, malformed record → a `Malformed` issue** — because a committed record with an invalid-JSON body is an atomicity violation (sqlite gives committed-rows-only isolation, so it cannot be a race), i.e. a runstate bug we want to expose, not gracefully absorb.

**Architecture:** `guarded()` shrinks from a 3-error wrapper to catching **only** `MalformedRecordError`; `json.JSONDecodeError` (byte-torn) propagates all the way out and crashes the cockpit; `sqlite3.DatabaseError`/`OSError` mid-read is caught at the `open_and_fold` boundary → `unreadable` (a corrupt db fails every read, so per-read granularity buys nothing). The single-run fold worker reverts to fail-fast (`exit_on_error=True`) so byte-torn is fatal rather than self-healed into a silent retry.

**Tech Stack:** Python 3.11, runstate (locked), Textual 8.2.8, uv, ruff, mypy --strict, pytest.

## Global Constraints

- **Three homes, decided at the boundary where each can originate:**
  - `json.JSONDecodeError` (byte-torn: a committed body that isn't valid JSON) → **not caught anywhere** → propagates → crash.
  - `sqlite3.DatabaseError` / `sqlite3.OperationalError` / `OSError` mid-read (substrate fault) → caught in `open_and_fold` around `status_fold` → `Status.unreadable()` (mirrors the existing open-time guard).
  - `MalformedRecordError` (runstate's own typed, deliberately-propagated signal; possibly version-skew) → caught in `guarded()` → an `Issue(kind=IssueKind.MALFORMED, …)`; the run's other factors survive.
- **`IssueKind.TORN` and `locate_torn_seq` are deleted** (no in-tree tear-locating; the crash traceback carries the seq, and `MalformedRecordError.seq` carries its own).
- **Fail-fast fold worker:** `SingleRunApp._fold` uses `@work(thread=True, exclusive=True)` (default `exit_on_error=True`) and reschedules on the success path — an escaping fold exception (byte-torn or a genuine bug) crashes the cockpit rather than self-healing. This deliberately reverts the Stage-1b self-heal (`bf80414`): post-collapse the fold yields a `Row` for every legitimate condition (missing/unreadable/pending/live/stale/terminal/conflicted/malformed-issue), so an escaping exception is definitionally a bug to expose. A crash is not a freeze (§10 holds — the app exits, it does not hang).
- **Public-API-only** (no raw `sqlite3` in runtime except exception classes). **No back-compat shims** (delete the old path). Value types stay `@dataclass(frozen=True)`.
- **Gates:** `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q` — all green before each commit.

## Verified facts (spiked)

- sqlite (WAL or rollback, separate reader connection) returns **committed rows only** — no partial/mid-write row is ever visible. So a JSON-decode failure on a read is a committed malformed body = a writer bug, never a tear race.
- The torn fixture (`tests/conftest.py::torn_sqlite_channel`) plants `"{not json"` in a body column → `channel.read`/`latest` raise `json.JSONDecodeError` when decoding it.
- `MalformedRecordError` carries a `.seq` (valid-JSON but schema-invalid record; `test_guarded_recovers_seq…` relies on it).
- Under `exit_on_error=True`, a crashing thread-worker makes `app.run_test()` raise `textual.worker.WorkerFailed` (with `__cause__` the original error) and sets `return_code == 1`.

## File Structure

- **Modify `runstate_tui/types.py`** — `IssueKind`: drop `TORN`, add `MALFORMED`.
- **Modify `runstate_tui/fold.py`** — delete `locate_torn_seq` + `_DECODE_ERRORS`; `guarded()` catches only `MalformedRecordError`.
- **Modify `runstate_tui/table.py`** — `open_and_fold` catches substrate errors around `status_fold` → `unreadable`.
- **Modify `runstate_tui/app.py`** — fail-fast fold worker.
- **Tests:** `tests/test_fold.py`, `tests/test_types.py`, `tests/test_format.py` (IssueKind rename + byte-torn→raise), `tests/test_table.py` (substrate→unreadable, byte-torn→propagate), `tests/test_app.py` (replace the self-heal test with a crash test). `tests/test_fixtures.py::test_torn_channel_raises_jsondecodeerror` is unchanged (the fixture still produces byte-torn).

---

### Task 1: Collapse the fold defense (`types.py` + `fold.py` + fold/types/format tests)

**Files:**
- Modify: `runstate_tui/types.py` (IssueKind), `runstate_tui/fold.py` (guarded, remove locate_torn_seq)
- Test: `tests/test_fold.py`, `tests/test_types.py`, `tests/test_format.py`

**Interfaces:**
- Produces: `IssueKind.MALFORMED` (replaces `TORN`); `guarded(fn, channel) -> (T | None, Issue | None)` now catches ONLY `MalformedRecordError`; `locate_torn_seq` removed.

- [ ] **Step 1: Update the tests first (they encode the new contract)**

In `tests/test_fold.py`: add `import json` and `import pytest` at the top. Replace the two guarded tests and the three `test_status_fold_degrades_a_torn_*` tests as follows (leave every other test unchanged):

```python
def test_guarded_lets_byte_torn_propagate(torn_sqlite_channel):
    # byte-torn = an atomicity violation (a committed non-JSON body). guarded no
    # longer swallows it — it propagates to crash the cockpit.
    ch = torn_sqlite_channel(
        [({"step": 5, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)],
        torn_seq=1,
    )
    with pytest.raises(json.JSONDecodeError):
        guarded(progress, ch)


def test_guarded_degrades_a_malformed_record_to_a_malformed_issue(build_log):
    # valid JSON, invalid Stopped schema (missing error/final_step/t) -> peek_terminal
    # raises MalformedRecordError (runstate's typed, deliberately-propagated signal);
    # guarded surfaces it as a MALFORMED issue with the record's own seq.
    ch = build_log([({"completed": True}, "lifecycle.stopped", None)])
    value, issue = guarded(peek_terminal, ch)
    assert value is None
    assert issue.kind is IssueKind.MALFORMED and issue.severity is Severity.MEDIUM
    assert issue.seq == 1


def test_status_fold_lets_byte_torn_propagate(torn_sqlite_channel):
    # a byte-torn record anywhere in the log crashes the fold (no granular degradation
    # for corruption): the first read that decodes it raises.
    ch = torn_sqlite_channel(
        [
            ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
            ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
        ],
        torn_seq=2,
    )
    with pytest.raises(json.JSONDecodeError):
        status_fold(ch, _env(150.0))
```

In `tests/test_types.py`: replace every `IssueKind.TORN` with `IssueKind.MALFORMED` (lines ~13, ~16, ~66) and adjust any `message="log torn…"` strings to a neutral `message="record malformed at seq 4012"` where the test asserts on the message; the severity assertions are unchanged.

In `tests/test_format.py`: replace the `IssueKind.TORN` issue (line ~36) with `IssueKind.MALFORMED` and its `message` with `"record malformed at seq 4012"`; the assertion becomes `assert "⚠ record malformed at seq 4012" in text` (format_row is generic — it renders `⚠ <message>` for any issue).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fold.py tests/test_types.py tests/test_format.py -q`
Expected: FAIL — `IssueKind` has no `MALFORMED`; `guarded` still swallows byte-torn into a `TORN` issue.

- [ ] **Step 3: Update `types.py`**

In `runstate_tui/types.py`, change `IssueKind`:

```python
class IssueKind(Enum):
    MALFORMED = "malformed"
    SKEW_SUSPECTED = "skew_suspected"
    UNSAFE_STOP = "unsafe_stop"
```

- [ ] **Step 4: Update `fold.py`**

In `runstate_tui/fold.py`: delete `locate_torn_seq` entirely and delete the `_DECODE_ERRORS` tuple. Rewrite `guarded` to catch only `MalformedRecordError`:

```python
def guarded(fn: Callable[[Channel], T], channel: Channel) -> tuple[T | None, Issue | None]:
    """Run a read; a MalformedRecordError (runstate's typed schema-invalid signal, e.g.
    version skew) degrades to a MALFORMED issue so the run's other factors survive. A
    byte-torn body (json.JSONDecodeError) and a substrate fault (sqlite3.DatabaseError)
    are NOT caught here — the former propagates to crash (an atomicity violation), the
    latter is caught at the open_and_fold boundary as `unreadable`."""
    try:
        return fn(channel), None
    except MalformedRecordError as exc:
        seq = getattr(exc, "seq", None)
        message = f"record malformed at seq {seq}" if seq is not None else "record malformed"
        return None, Issue(
            kind=IssueKind.MALFORMED, severity=Severity.MEDIUM, message=message, seq=seq
        )
```

Remove the now-unused imports: `json`, `sqlite3` (if unused elsewhere in the file — check; keep any still referenced). Keep `MalformedRecordError` imported. In `read_elapsed`, rename the local `torn_issue` to `malformed_issue` for clarity (behavior identical — a malformed `started` still returns `(None, malformed_issue)`; a byte-torn `started` now propagates).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fold.py tests/test_types.py tests/test_format.py -q`
Expected: PASS.

- [ ] **Step 6: Full gates + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: `test_table.py` and `test_app.py` may still reference torn behavior — if they fail here it is because Tasks 2/3 haven't run yet; confirm the ONLY failures are in those two files and commit anyway (the suite goes green after Task 3). If `test_types.py`/`test_fold.py`/`test_format.py` are green and mypy/ruff pass:

```bash
git add runstate_tui/types.py runstate_tui/fold.py tests/test_fold.py tests/test_types.py tests/test_format.py
git commit -m "refactor(fold): collapse Torn -> Malformed issue; byte-torn propagates"
```

---

### Task 2: Substrate mid-read → `unreadable` (`table.py`)

**Files:**
- Modify: `runstate_tui/table.py`
- Test: `tests/test_table.py`

**Interfaces:**
- Consumes: `status_fold`, `Status.unreadable`. Produces: `open_and_fold` maps a mid-read substrate error → `unreadable`; byte-torn propagates.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_table.py` (adapt imports to the file's existing style; it already imports `open_and_fold`, `Env`, `Status`/`StatusKind`):

```python
import json

import pytest


def test_open_and_fold_maps_a_substrate_read_fault_to_unreadable(tmp_path, monkeypatch):
    # a real, openable run; status_fold raises a substrate error mid-read (injected)
    ch = open_channel("sub", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    ch.close()

    def boom(channel, env):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr("runstate_tui.table.status_fold", boom)
    row = open_and_fold(("sub", str(tmp_path), "sqlite"), Env(clock=lambda: 1.0))
    assert row.status.kind is StatusKind.UNREADABLE


def test_open_and_fold_lets_byte_torn_propagate(torn_sqlite_channel, tmp_path):
    # byte-torn is NOT unreadable — it crashes. Build a torn run and fold its ref.
    torn_sqlite_channel(
        [({"handle": "h", "t": 1.0}, "lifecycle.started", None)], torn_seq=1
    )
    ref = ("torn", str(tmp_path), "sqlite")  # torn_sqlite_channel writes run_id "torn" under tmp_path
    with pytest.raises(json.JSONDecodeError):
        open_and_fold(ref, Env(clock=lambda: 1.0))
```

Add `import sqlite3` to `tests/test_table.py` if not present. (Note: `torn_sqlite_channel` uses `tmp_path` internally and writes `torn.db`; both fixtures share the test's `tmp_path`, so the ref resolves.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_table.py -q`
Expected: FAIL — the substrate error currently propagates (no mid-read catch), and byte-torn currently yields a row-with-issue, not a raise.

- [ ] **Step 3: Update `open_and_fold`**

In `runstate_tui/table.py`, wrap the fold in a substrate catch (leave the stat-before-open and open guards unchanged):

```python
    try:
        return status_fold(channel, env)
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return _bare(Status.unreadable())  # substrate fault mid-read (a corrupt db
        # fails every read; byte-torn's json.JSONDecodeError is NOT caught -> it crashes)
    finally:
        channel.close()
```

(`sqlite3` is already imported in `table.py`.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_table.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
(`test_app.py`'s self-heal test may still fail until Task 3 — confirm it is the only remaining failure.)

```bash
git add runstate_tui/table.py tests/test_table.py
git commit -m "fix(table): substrate read fault -> unreadable; byte-torn propagates"
```

---

### Task 3: Fail-fast fold worker (`app.py`)

**Files:**
- Modify: `runstate_tui/app.py`
- Test: `tests/test_app.py` (replace the self-heal test)

**Interfaces:**
- Produces: `SingleRunApp._fold` crashes on an escaping fold exception (byte-torn) instead of self-healing.

- [ ] **Step 1: Replace the self-heal test with a crash test**

In `tests/test_app.py`: DELETE `test_fold_error_does_not_crash_and_loop_recovers` and its `_recovers` helper (its premise — self-heal on an unanticipated fold exception — is inverted by fail-fast). Add:

```python
from textual.worker import WorkerFailed


def test_byte_torn_crashes_the_cockpit(tmp_path):
    # a byte-torn record is an atomicity violation: the fold worker must crash the
    # cockpit (WorkerFailed), never self-heal it into a silent retry.
    import sqlite3

    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    conn = sqlite3.connect(str(tmp_path / "r.db"))
    conn.execute("UPDATE log SET body = ? WHERE seq = 1", ("{not json",))
    conn.commit()
    conn.close()

    ref = ("r", str(tmp_path), "sqlite")
    with pytest.raises(WorkerFailed):
        asyncio.run(_crash(ref))


async def _crash(ref):
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)
```

(`pytest` is already imported? if not, add `import pytest`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_app.py::test_byte_torn_crashes_the_cockpit -q`
Expected: FAIL — under the current `exit_on_error=False`, the byte-torn is swallowed and the loop self-heals; no `WorkerFailed`.

- [ ] **Step 3: Make the fold worker fail-fast**

In `runstate_tui/app.py`, replace the `_fold` worker (and drop the now-obsolete self-heal docstring paragraph):

```python
    @work(thread=True, exclusive=True)
    def _fold(self) -> None:
        # fail-fast: the fold yields a Row for every legitimate condition, so an
        # escaping exception (byte-torn = a runstate atomicity violation, or a genuine
        # bug) crashes the cockpit rather than self-healing into a silent retry. A
        # crash is not a freeze — the app exits (§10 holds).
        row = render_single(self._ref, self._env)  # byte-torn -> raises -> crash
        text = format_row(row)
        self.call_from_thread(self._show, text)  # query + update via call_from_thread
        self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
```

Update the `SingleRunApp` class docstring's "self-healing" sentence to describe the fail-fast behavior.

- [ ] **Step 4: Run to verify it passes (+ rerun for stability)**

Run: `uv run pytest tests/test_app.py -q` (rerun 3×; the new crash test drives Textual's error path — confirm non-flaky).
Expected: PASS.

- [ ] **Step 5: Full gates + commit**

Run: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q`
Expected: ALL green (whole suite).

```bash
git add runstate_tui/app.py tests/test_app.py
git commit -m "refactor(app): fail-fast fold worker — byte-torn crashes, never self-heals"
```

---

## Self-Review

- **Coverage:** the three failure homes (crash / unreadable / malformed-issue) each have a test; the deleted `TORN`/`locate_torn_seq` leave no dangling reference (grep `TORN|locate_torn` after Task 1).
- **Placeholder scan:** none.
- **Type consistency:** `IssueKind.MALFORMED` used identically in `types.py`, `fold.py`, and the three test files; `guarded`'s signature unchanged.
- **Scope:** does NOT touch the incremental log pane / Stage 3 (separate) — this only collapses the existing fold defense.
```
