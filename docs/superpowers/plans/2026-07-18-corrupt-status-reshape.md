# Reshape byte-torn: crash → loud `corrupt` status Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Turn a byte-torn record from a process crash into a **loud, unmissable, distinct `corrupt` status** (HIGH, carrying the torn seq) — surfaced via the established status/issue channel, scoped to the run, not the cockpit. The goal was always "unmissable"; crash was an over-drastic means. A distinct `corrupt` (not reusing `unreadable` — that would be lossy) re-unifies the defense (every substrate condition surfaces) and resolves the Stage-4 / control-plane / drill-down-swallow tensions.

**Architecture:** `guarded()` is unchanged (still `MalformedRecordError`-only). `json.JSONDecodeError` still propagates out of `status_fold` (unit-level), but `open_and_fold` now **catches it → a `corrupt` Row** (parallel to the existing substrate → `unreadable` catch), locating the torn seq. The drill-down's header shows `corrupt` loudly via `render_single`; `read_log_delta` catches byte-torn → `[]` (the header is the loud signal). The fold worker stays fail-fast (`exit_on_error=True`) — but byte-torn no longer reaches it (it's a Row now); crash is reserved for the truly-unclassifiable.

**Tech Stack:** Python 3.11, runstate (locked), Textual 8.2.8, uv, ruff, mypy --strict, pytest, pytest-textual-snapshot.

## Global Constraints

- **Distinct `corrupt`, not reused `unreadable`** (reuse is lossy — a torn committed record is a different, scarier thing than an unopenable substrate). `corrupt` is HIGH; it carries the torn seq + detail.
- **Loud + debuggable:** the run's status reads `corrupt` (dominates), and a HIGH `IssueKind.CORRUPT` issue carries `seq` + detail (for the dev who can fix the runstate bug).
- **A torn log can't be trusted** → `corrupt` is a whole-run status (like `unreadable`), not a per-factor issue on an otherwise-live status.
- **Crash is reserved for the truly unclassifiable** — the fold worker keeps `exit_on_error=True`, but byte-torn is now classified (→ `corrupt` Row) and no longer crashes.
- **No back-compat shims**; public-API-only; frozen value types; ruff/format/mypy/pytest green before each commit.

## Verified facts

- `json.JSONDecodeError` subclasses `ValueError` only (not `OSError`/`sqlite3.DatabaseError`), so `open_and_fold`'s substrate `except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError)` provably does NOT catch it — a distinct `except json.JSONDecodeError` is needed and can't collide.
- The torn fixture (`torn_sqlite_channel`) plants an un-decodable body; reads raise `json.JSONDecodeError`.
- `locate_torn_seq` (deleted by the precursor) is needed again to find the torn seq for the `corrupt` issue — reintroduce it.

## File Structure

- **`runstate_tui/types.py`** — `StatusKind.CORRUPT`, `Status.corrupt()`, `_STATUS_SEVERITY[CORRUPT]=HIGH`, `_STATUS…` label "corrupt"; `IssueKind.CORRUPT`.
- **`runstate_tui/fold.py`** — reintroduce `locate_torn_seq`.
- **`runstate_tui/table.py`** — `_corrupt(seq)` Row builder; `open_and_fold` catches `json.JSONDecodeError` → `_corrupt(locate_torn_seq(channel))`; `read_log_delta` catches `json.JSONDecodeError` → `[]`.
- **`runstate_tui/format.py` / `detail.py`** — render `corrupt` loudly; remove the drill-down's dead `is_mounted` guard.
- **`runstate_tui/app.py`** — invert the crash test → `corrupt` renders (no crash).
- Tests: `test_types.py`, `test_fold.py`, `test_table.py`, `test_format.py`, `test_detail.py`, `test_app.py`.

---

### Task 1: byte-torn → `corrupt` status in the fold layer

**Files:** `types.py`, `fold.py`, `table.py`; Test `test_types.py`, `test_table.py`, `test_fold.py`.

- [ ] **Step 1: Failing tests**

`tests/test_table.py` — invert the two precursor byte-torn-propagates tests to expect a `corrupt` Row, and keep the substrate test:

```python
def test_open_and_fold_maps_byte_torn_to_corrupt(torn_sqlite_channel, tmp_path):
    torn_sqlite_channel([({"handle": "h", "t": 1.0}, "lifecycle.started", None)], torn_seq=1)
    row = open_and_fold(("torn", str(tmp_path), "sqlite"), Env(clock=lambda: 1.0))
    assert row.status.kind is StatusKind.CORRUPT
    assert row.status.severity is Severity.HIGH
    assert any(i.kind is IssueKind.CORRUPT and i.seq == 1 for i in row.issues)


def test_read_log_delta_byte_torn_is_empty(torn_sqlite_channel, tmp_path):
    torn_sqlite_channel([({"handle": "h", "t": 1.0}, "lifecycle.started", None)], torn_seq=1)
    from runstate_tui.table import read_log_delta
    assert read_log_delta(("torn", str(tmp_path), "sqlite"), after=0) == []
```

`tests/test_fold.py` — KEEP `test_status_fold_lets_byte_torn_propagate` (status_fold still raises; open_and_fold is what catches). Add a `locate_torn_seq` unit test:

```python
def test_locate_torn_seq_finds_the_tear(torn_sqlite_channel):
    from runstate_tui.fold import locate_torn_seq
    ch = torn_sqlite_channel(
        [({"handle": "h", "t": 1.0}, "lifecycle.started", None),
         ({"step": 1, "consumed_seq": 0, "t": 2.0}, "lifecycle.heartbeat", None)],
        torn_seq=2,
    )
    assert locate_torn_seq(ch) == 2
```

`tests/test_types.py` — add a `corrupt` status test (kind, label "corrupt", severity HIGH).

- [ ] **Step 2: Run → fail** (`StatusKind` has no `CORRUPT`; open_and_fold still propagates).

- [ ] **Step 3: `types.py`** — add to `StatusKind`: `CORRUPT = "corrupt"`. Add `_STATUS_SEVERITY[StatusKind.CORRUPT] = Severity.HIGH`. Add `IssueKind.CORRUPT = "corrupt"`. Add `Status.corrupt()` classmethod (`return cls(StatusKind.CORRUPT)`). The `.label` already returns `str(self.kind.value)` for non-terminal kinds → "corrupt".

- [ ] **Step 4: `fold.py`** — reintroduce `locate_torn_seq` (walk `read(after=k, limit=1)`; a raising probe localizes the tear at `k+1`; returns `None` if none). It catches only the decode errors it's probing for — use `(json.JSONDecodeError, sqlite3.DatabaseError)` locally (re-add the `json`/`sqlite3` imports if removed). `guarded` stays unchanged (MalformedRecordError-only).

- [ ] **Step 5: `table.py`** — add:

```python
def _corrupt(seq: int | None) -> Row:
    msg = f"log corrupt at seq {seq}" if seq is not None else "log corrupt"
    issue = Issue(kind=IssueKind.CORRUPT, severity=Severity.HIGH, message=msg, seq=seq)
    return Row(
        status=Status.corrupt(), frontier=None, freshness=None, value=None, elapsed=None,
        episode=None, undischarged_stops=(), live_demand=(), issues=(issue,),
    )
```

In `open_and_fold`, add a catch BEFORE the substrate catch (JSONDecodeError is not a subclass of the substrate errors, so order is for clarity):

```python
    try:
        return status_fold(channel, env)
    except json.JSONDecodeError:
        return _corrupt(locate_torn_seq(channel))
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return _bare(Status.unreadable())
    finally:
        channel.close()
```

In `read_log_delta`, add `json.JSONDecodeError` to the mid-read catch returning `[]` (the drill-down header surfaces `corrupt` via `render_single`; raw-tail-up-to-tear is a deferred raw-passthrough follow-up — note it in a comment). Add the needed imports (`json`, `Issue`, `IssueKind`, `Severity`, `Status`, `locate_torn_seq`).

- [ ] **Step 6: Run → pass; full gates; commit** `refactor(fold): byte-torn -> loud corrupt status (not crash)`

---

### Task 2: render `corrupt` loudly + drill-down cleanup + app test

**Files:** `format.py`, `detail.py`, `app.py`; Test `test_format.py`, `test_detail.py`, `test_app.py`.

- [ ] **Step 1: Failing/updated tests**

`tests/test_app.py` — replace `test_byte_torn_crashes_the_cockpit` with:

```python
def test_byte_torn_renders_corrupt_not_crash(tmp_path):
    import sqlite3
    ch = open_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    conn = sqlite3.connect(str(tmp_path / "r.db"))
    conn.execute("UPDATE log SET body = ? WHERE seq = 1", ("{not json",))
    conn.commit(); conn.close()
    asyncio.run(_shows_corrupt(("r", str(tmp_path), "sqlite")))


async def _shows_corrupt(ref):
    app = SingleRunApp(ref, Env(clock=lambda: 150.0), tick_interval=999.0)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause(0.05)
        assert "corrupt" in str(app.query_one("#run", Static).content)  # loud, no crash
```

`tests/test_format.py` — add a test that `format_row` renders a `corrupt` status prominently (the label "corrupt" appears; the row severity is HIGH). Update the drill-down snapshot baseline if the layout of a corrupt run is snapshotted (regenerate via `--snapshot-update` only if intentional).

- [ ] **Step 2: Run → fail** (app crashes today; no corrupt rendering).

- [ ] **Step 3: `app.py`** — no code change needed to `_fold` (it stays `@work(thread=True, exclusive=True)` fail-fast; byte-torn is now a `corrupt` Row and never reaches the worker). Just confirm the inverted test passes. If the class docstring mentions "byte-torn crashes", update it to "byte-torn → a loud `corrupt` Row; only a truly-unclassifiable exception crashes."

- [ ] **Step 4: `format.py`** — ensure `format_row` surfaces the `corrupt` status label prominently (it already prints `row.status.label` = "corrupt"; add a leading marker if the file's style warrants, e.g. the existing severity-driven formatting). `format_detail` already lists issues, so the CORRUPT issue (seq/detail) shows in the drill-down header.

- [ ] **Step 5: `detail.py`** — remove the dead `if not self.is_mounted: return` guard (verified: `is_mounted` is never reset to `False` in Textual 8.2.8, so the guard is unreachable; the tick already stops on pop via Textual's screen-close timer cleanup). Add a one-line comment noting the tick stops on unmount via Textual, and that a byte-torn now surfaces as a `corrupt` header (no crash to race a pop).

- [ ] **Step 6: Run → pass (rerun test_detail 3×); full gates; commit** `feat(detail): render corrupt loudly; drop dead is_mounted guard`

---

## Self-Review

- **Coverage:** open_and_fold→corrupt + seq (Task 1), read_log_delta→[] on torn (Task 1), locate_torn_seq (Task 1), corrupt renders + no-crash (Task 2). `status_fold` still raises (unit) — the boundary catch is what classifies.
- **Consistency:** `corrupt` joins the integrity family (`missing`/`unreadable`/`corrupt`) as a whole-run status; `MalformedRecordError`→per-factor issue unchanged; the fail-fast worker unchanged (byte-torn no longer reaches it).
- **Deferred (noted):** raw-tail-up-to-tear in `read_log_delta` (record-by-record read to show readable envelopes before the tear — a raw-passthrough improvement); revisiting the fail-fast worker for genuinely-unclassifiable exceptions (surface vs crash) — a separate call.
```
