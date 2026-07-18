# Stage 0 — Defended Fold → `Row` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure, headless observer core — `status_fold(channel, env) -> Row` — that folds one runstate log into a status row, with per-observable defense and the freshness-only verdict lattice. No UI, no threads.

**Architecture:** A single Python package `runstate_tui` with three modules — `types` (the value types `Severity`/`Issue`/`Status`/`Row`), `env` (the injected `Env` + the committed liveness seam), and `fold` (`status_fold` + guarded sub-folds). Everything is a pure function of `(channel, env)`; `env.clock()` is the only ambient input and is injectable for tests. This is the initial object of the whole design (spec §1); Stages 1 (Textual view) and 2 (stop) attach to it later.

**Tech Stack:** Python ≥ 3.11, `runstate` (sibling repo, editable install), `pytest`. Uses only runstate's **public** API: `open_channel`, `Channel.read/latest`, and the observables `peek_terminal`, `progress`, `last_activity`.

## Global Constraints

- **Python ≥ 3.11** (uses `StrEnum`, `X | None`, `from __future__ import annotations`).
- **Public-API-only in runtime code** — no raw `sqlite3`, no `?mode=ro`, no `_`-prefixed runstate calls. (Test *fixtures* may use raw `sqlite3` to inject a torn record — that is test tooling, not runtime.)
- **`Row` is a `@dataclass(frozen=True)`** with value equality — the Stage-1 singleton test depends on `==`.
- **Value is never nameless** — `Row.value` is `(name, scalar, step)` via `latest(Topic.VALUE, name=env.objective)`; if no objective is configured, `value` is `None`. (spec §7, §14, H1)
- **Freshness never lies bright** — age is `max(0, now - last_activity)`; a negative raw age (future `t`) never yields `live` and raises `SkewSuspected`. (spec §4)
- **Two-tier defense** — this plan implements the *per-observable* guard (a torn record degrades one factor to `None` + a `Torn` issue, the row keeps its verdict). The *open* guard (`missing`/`unreadable`) lives in Stage 1's open-wrapper and is **out of scope here**. (spec §3.1)
- **Freshness-only core** — the fold never calls `live_episode`/`os.kill`; liveness comes only from `FreshnessSignal` registered in `Env.liveness`. (spec §2.1)
- **Out of scope for Stage 0 (documented, not dropped):** `missing`/`unreadable` (Stage-1 open-wrapper); `conflicted` semantic-anomaly detection (a follow-up — the enum member exists, the fold does not yet emit it); `UnsafeStop` (Stage 2).

---

## File Structure

- `pyproject.toml` — package metadata, `requires-python`, deps, pytest config.
- `runstate_tui/__init__.py` — re-exports the public types + `status_fold`.
- `runstate_tui/types.py` — `Severity`, `IssueKind`, `Issue`, `StatusKind`, `Status`, `Row`.
- `runstate_tui/env.py` — `Liveness`, `LivenessSignal`, `FreshnessSignal`, `Env`.
- `runstate_tui/fold.py` — `status_fold` + guarded sub-folds + the torn-seq locator.
- `tests/conftest.py` — the `build_log` memory-log builder + `torn_sqlite_channel` fixture.
- `tests/test_types.py`, `tests/test_env.py`, `tests/test_fold.py`.

---

### Task 1: Project scaffold + runstate dependency

**Files:**
- Create: `pyproject.toml`, `runstate_tui/__init__.py`, `tests/test_smoke.py`

**Interfaces:**
- Produces: an installed, importable `runstate_tui` package; `runstate` importable in the same env.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[project]
name = "runstate-tui"
version = "0.0.0"
requires-python = ">=3.11"
dependencies = ["runstate"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
packages = ["runstate_tui"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Create the package init**

`runstate_tui/__init__.py`:

```python
"""runstate-tui: a control-plane cockpit for runstate runs."""
```

- [ ] **Step 3: Write the smoke test**

`tests/test_smoke.py`:

```python
def test_runstate_and_package_import():
    import runstate
    import runstate_tui
    assert hasattr(runstate, "open_channel")
```

- [ ] **Step 4: Install both packages editable and run the smoke test**

Run:
```bash
pip install -e ../runstate && pip install -e '.[dev]'
pytest tests/test_smoke.py -v
```
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml runstate_tui/__init__.py tests/test_smoke.py
git commit -m "chore: scaffold runstate_tui package + runstate dep"
```

---

### Task 2: `Severity`, `IssueKind`, `Issue`

**Files:**
- Create: `runstate_tui/types.py`, `tests/test_types.py`

**Interfaces:**
- Produces:
  - `class Severity(IntEnum)`: members `OK=0`, `INFO=1`, `MEDIUM=2`, `HIGH=3`.
  - `class IssueKind(Enum)`: members `TORN`, `SKEW_SUSPECTED`, `UNSAFE_STOP`.
  - `@dataclass(frozen=True) class Issue`: `kind: IssueKind`, `severity: Severity`, `message: str`, `seq: int | None = None`, `detail: str | None = None`.

- [ ] **Step 1: Write the failing test**

`tests/test_types.py`:

```python
from runstate_tui.types import Severity, IssueKind, Issue


def test_severity_orders_and_maxes():
    assert Severity.OK < Severity.INFO < Severity.MEDIUM < Severity.HIGH
    assert max(Severity.INFO, Severity.HIGH) is Severity.HIGH


def test_issue_is_a_frozen_value():
    a = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    b = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="log torn at seq 4012", seq=4012)
    assert a == b
    assert a.detail is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_types.py -v`
Expected: FAIL with `ModuleNotFoundError`/`ImportError` for `runstate_tui.types`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/types.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


class Severity(IntEnum):
    """Row/issue severity — int-valued so a row's badge is `max(...)`."""
    OK = 0
    INFO = 1
    MEDIUM = 2
    HIGH = 3


class IssueKind(Enum):
    TORN = "torn"
    SKEW_SUSPECTED = "skew_suspected"
    UNSAFE_STOP = "unsafe_stop"


@dataclass(frozen=True)
class Issue:
    kind: IssueKind
    severity: Severity
    message: str
    seq: int | None = None
    detail: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_types.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/types.py tests/test_types.py
git commit -m "feat(types): Severity, IssueKind, Issue"
```

---

### Task 3: `Status` (the open coproduct wrapping `Outcome`)

**Files:**
- Modify: `runstate_tui/types.py`
- Modify: `tests/test_types.py`

**Interfaces:**
- Consumes: `runstate.observables.Outcome` (a `StrEnum`: `COMPLETED/PREEMPTED/ERRORED/KILLED/PRESUMED_DEAD`).
- Produces:
  - `class StatusKind(Enum)`: `PENDING`, `LIVE`, `STALE`, `TERMINAL`, `MISSING`, `UNREADABLE`, `CONFLICTED`.
  - `@dataclass(frozen=True) class Status`: `kind: StatusKind`, `outcome: Outcome | None = None`; classmethods `Status.pending()/live()/stale()/terminal(outcome)/missing()/unreadable()/conflicted()`; properties `.label -> str` and `.severity -> Severity`.
  - Rule: `.label` for `TERMINAL` maps via a display table (`COMPLETED -> "done"`), falling back to `outcome.value` for an unrecognized member (never a silent default); `.severity`: `UNREADABLE -> HIGH`, `CONFLICTED -> MEDIUM`, `PENDING/MISSING -> INFO`, else `OK`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_types.py`:

```python
from runstate.observables import Outcome
from runstate_tui.types import Status, StatusKind, Severity


def test_terminal_wraps_outcome_and_labels_completed_as_done():
    s = Status.terminal(Outcome.COMPLETED)
    assert s.kind is StatusKind.TERMINAL
    assert s.outcome is Outcome.COMPLETED
    assert s.label == "done"
    assert Status.terminal(Outcome.KILLED).label == "killed"


def test_unknown_outcome_renders_honestly_not_a_default():
    # a future/unknown Outcome member must render via its own wire string
    class FakeOutcome:
        value = "suspended"
    s = Status.terminal(FakeOutcome())
    assert s.label == "suspended"


def test_status_severity_map():
    assert Status.unreadable().severity is Severity.HIGH
    assert Status.conflicted().severity is Severity.MEDIUM
    assert Status.pending().severity is Severity.INFO
    assert Status.live().severity is Severity.OK
    assert Status.terminal(Outcome.COMPLETED).severity is Severity.OK
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_types.py -k "terminal or unknown or severity_map" -v`
Expected: FAIL with `ImportError` for `Status`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/types.py`:

```python
from typing import Any

# display labels are cockpit policy (spec §4/§8); the internal `outcome` stays the
# raw runstate Outcome so there is no drift-prone translation of the protocol's logic.
_TERMINAL_LABELS = {
    "completed": "done",
    "preempted": "preempted",
    "errored": "errored",
    "killed": "killed",
    "presumed_dead": "dead",
}


class StatusKind(Enum):
    PENDING = "pending"
    LIVE = "live"
    STALE = "stale"
    TERMINAL = "terminal"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    CONFLICTED = "conflicted"


_STATUS_SEVERITY = {
    StatusKind.UNREADABLE: Severity.HIGH,
    StatusKind.CONFLICTED: Severity.MEDIUM,
    StatusKind.PENDING: Severity.INFO,
    StatusKind.MISSING: Severity.INFO,
}


@dataclass(frozen=True)
class Status:
    kind: StatusKind
    outcome: Any | None = None  # set iff kind is TERMINAL; a runstate Outcome

    @classmethod
    def pending(cls) -> "Status": return cls(StatusKind.PENDING)
    @classmethod
    def live(cls) -> "Status": return cls(StatusKind.LIVE)
    @classmethod
    def stale(cls) -> "Status": return cls(StatusKind.STALE)
    @classmethod
    def missing(cls) -> "Status": return cls(StatusKind.MISSING)
    @classmethod
    def unreadable(cls) -> "Status": return cls(StatusKind.UNREADABLE)
    @classmethod
    def conflicted(cls) -> "Status": return cls(StatusKind.CONFLICTED)
    @classmethod
    def terminal(cls, outcome: Any) -> "Status": return cls(StatusKind.TERMINAL, outcome)

    @property
    def label(self) -> str:
        if self.kind is StatusKind.TERMINAL:
            # render honestly: an unrecognized outcome falls back to its own wire string
            return _TERMINAL_LABELS.get(str(self.outcome.value), str(self.outcome.value))
        return self.kind.value

    @property
    def severity(self) -> Severity:
        return _STATUS_SEVERITY.get(self.kind, Severity.OK)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_types.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/types.py tests/test_types.py
git commit -m "feat(types): Status open coproduct wrapping Outcome"
```

---

### Task 4: `Row` (frozen, value-equality) + `Row.severity`

**Files:**
- Modify: `runstate_tui/types.py`
- Modify: `tests/test_types.py`

**Interfaces:**
- Consumes: `Status`, `Issue`, `Severity`.
- Produces: `@dataclass(frozen=True) class Row` with fields `status: Status`, `frontier: int | None`, `freshness: float | None`, `value: tuple[str, object, int | None] | None`, `elapsed: float | None`, `episode: str | None`, `issues: tuple[Issue, ...]`; property `.severity -> Severity = max(status.severity, *(i.severity for issues))`.
  - Note `issues` is a **tuple** (hashable/immutable), not a list, so `Row` stays a frozen value.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_types.py`:

```python
from runstate_tui.types import Row, Issue, IssueKind


def _bare_row(**kw):
    base = dict(status=Status.live(), frontier=10, freshness=1.0, value=None,
                elapsed=5.0, episode=None, issues=())
    base.update(kw)
    return Row(**base)


def test_row_is_a_frozen_value_for_the_singleton_test():
    assert _bare_row() == _bare_row()


def test_row_severity_is_max_of_status_and_issues():
    torn = Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message="torn")
    assert _bare_row().severity is Severity.OK
    assert _bare_row(issues=(torn,)).severity is Severity.MEDIUM
    assert _bare_row(status=Status.unreadable()).severity is Severity.HIGH
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_types.py -k row -v`
Expected: FAIL with `ImportError` for `Row`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/types.py`:

```python
@dataclass(frozen=True)
class Row:
    status: Status
    frontier: int | None
    freshness: float | None                      # age = max(0, now - last_activity)
    value: tuple[str, object, int | None] | None  # (name, scalar, step)
    elapsed: float | None                        # now - first started.t; None if no started
    episode: str | None                          # latest_episode handle (PURE); None in Stage 0
    issues: tuple[Issue, ...]

    @property
    def severity(self) -> Severity:
        return max([self.status.severity, *(i.severity for i in self.issues)], key=int)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_types.py -v`
Expected: PASS.

- [ ] **Step 5: Re-export from the package and commit**

Set `runstate_tui/__init__.py`:

```python
"""runstate-tui: a control-plane cockpit for runstate runs."""
from .types import Severity, IssueKind, Issue, StatusKind, Status, Row  # noqa: F401
```

```bash
git add runstate_tui/types.py runstate_tui/__init__.py tests/test_types.py
git commit -m "feat(types): Row frozen value + severity"
```

---

### Task 5: `Env` + the committed liveness seam

**Files:**
- Create: `runstate_tui/env.py`, `tests/test_env.py`

**Interfaces:**
- Consumes: `runstate.channel.Channel`, `runstate.observables.last_activity`.
- Produces:
  - `class Liveness(Enum)`: `LIVE`, `STALE`, `DEAD`.
  - `class LivenessSignal(Protocol)`: `liveness(self, channel, env, now: float, last_activity: float | None) -> Liveness | None` — `last_activity` is read once by the fold and passed in (so signals are pure and can't double-read).
  - `@dataclass(frozen=True) class FreshnessSignal`: returns `LIVE`/`STALE` from the passed `last_activity`, or `None` when it is `None` (no opinion).
  - `@dataclass(frozen=True) class Env`: `clock: Callable[[], float]`, `objective: str | None = None`, `stuck_threshold: float = 60.0`, `liveness: tuple[LivenessSignal, ...] = (FreshnessSignal(),)`.
  - `resolve_liveness(channel, env, now, last_activity) -> Liveness | None`: first signal (in order = precedence) with a non-`None` opinion wins; overlays register ahead of `FreshnessSignal`.

- [ ] **Step 1: Write the failing test**

`tests/test_env.py`:

```python
from runstate_tui.env import Env, Liveness, FreshnessSignal, resolve_liveness


def test_freshness_signal_live_stale_and_no_opinion(build_log):
    ch = build_log([])
    env = Env(clock=lambda: 100.0, stuck_threshold=60.0)
    assert resolve_liveness(ch, env, now=100.0, last_activity=100.0) is Liveness.LIVE
    assert resolve_liveness(ch, env, now=161.0, last_activity=100.0) is Liveness.STALE
    assert resolve_liveness(ch, env, now=200.0, last_activity=None) is None  # no dated activity


def test_signals_are_consulted_in_order_first_opinion_wins(build_log):
    ch = build_log([])
    class AlwaysDead:
        def liveness(self, channel, env, now, last_activity): return Liveness.DEAD
    env = Env(clock=lambda: 0.0, liveness=(AlwaysDead(), FreshnessSignal()))
    assert resolve_liveness(ch, env, now=0.0, last_activity=None) is Liveness.DEAD
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_env.py -v`
Expected: FAIL — `ImportError` for `runstate_tui.env` (and `build_log` fixture missing; Task 6 adds it — for now expect a collection/import error).

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/env.py`:

```python
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from runstate.channel import Channel


class Liveness(Enum):
    LIVE = "live"
    STALE = "stale"
    DEAD = "dead"


class LivenessSignal(Protocol):
    # last_activity is read once by the fold and passed in, so the signal is pure and
    # can't double-read; a channel-reading overlay (probe) still gets the channel.
    def liveness(self, channel: Channel, env: "Env", now: float,
                 last_activity: float | None) -> Liveness | None: ...


@dataclass(frozen=True)
class FreshnessSignal:
    """The core's only liveness signal: a pure verdict from the log's last-activity clock."""

    def liveness(self, channel: Channel, env: "Env", now: float,
                 last_activity: float | None) -> Liveness | None:
        if last_activity is None:
            return None  # no dated activity -> no opinion (the fold decides pending)
        age = max(0.0, now - last_activity)
        return Liveness.LIVE if age <= env.stuck_threshold else Liveness.STALE


@dataclass(frozen=True)
class Env:
    clock: Callable[[], float]
    objective: str | None = None
    stuck_threshold: float = 60.0
    liveness: tuple[LivenessSignal, ...] = field(default_factory=lambda: (FreshnessSignal(),))


def resolve_liveness(channel: Channel, env: Env, now: float,
                     last_activity: float | None) -> Liveness | None:
    for signal in env.liveness:  # order == precedence; overlays register ahead of freshness
        verdict = signal.liveness(channel, env, now, last_activity)
        if verdict is not None:
            return verdict
    return None
```

- [ ] **Step 4: Run (after Task 6 provides `build_log`)**

Deferred to Task 6 Step 4 (the fixture is needed to run this). For now just verify the import:

Run: `python -c "import runstate_tui.env"`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/env.py tests/test_env.py
git commit -m "feat(env): Env + committed liveness seam (freshness-only)"
```

---

### Task 6: Test fixtures — `build_log` + `torn_sqlite_channel`

**Files:**
- Create: `tests/conftest.py`

**Interfaces:**
- Produces:
  - `build_log(records)`: opens a fresh **memory** channel, sends each `(body, topic, name)` record, returns a *new* channel on the same log to fold over. Signature: `records: list[tuple[dict, str, str | None]]`.
  - `torn_sqlite_channel(records, torn_seq)`: builds a **sqlite** channel from `records`, then corrupts the body at `torn_seq` via raw sqlite (test tooling only), returns a channel whose `read`/`latest` of that record raises `json.JSONDecodeError`.

- [ ] **Step 1: Write the fixtures**

`tests/conftest.py`:

```python
import sqlite3
from itertools import count

import pytest
from runstate import open_channel

_ids = count()


@pytest.fixture
def build_log():
    def _build(records):
        run_id = f"run-{next(_ids)}"
        writer = open_channel(run_id, backend="memory")
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        return open_channel(run_id, backend="memory")  # a fresh reader on the same log
    return _build


@pytest.fixture
def torn_sqlite_channel(tmp_path):
    def _build(records, torn_seq):
        run_id = "torn"
        writer = open_channel(run_id, root=tmp_path, backend="sqlite")
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        writer.close()
        # test tooling only (not runtime): plant an un-decodable body at torn_seq
        conn = sqlite3.connect(str(tmp_path / f"{run_id}.db"))
        conn.execute("UPDATE log SET body = ? WHERE seq = ?", ("{not json", torn_seq))
        conn.commit()
        conn.close()
        return open_channel(run_id, root=tmp_path, backend="sqlite")
    return _build
```

- [ ] **Step 2: Write a fixture self-test**

Append to `tests/conftest.py` a sibling test file `tests/test_fixtures.py`:

```python
import json
import pytest


def test_build_log_roundtrips(build_log):
    ch = build_log([({"handle": "local://h/1", "t": 10.0}, "lifecycle.started", None)])
    assert ch.latest("lifecycle.started").body["t"] == 10.0


def test_torn_channel_raises_jsondecodeerror(torn_sqlite_channel):
    ch = torn_sqlite_channel(
        [({"step": 0, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)],
        torn_seq=1,
    )
    with pytest.raises(json.JSONDecodeError):
        ch.latest("lifecycle.heartbeat")
```

- [ ] **Step 3: Run the fixture self-test and the deferred env test**

Run: `pytest tests/test_fixtures.py tests/test_env.py -v`
Expected: PASS (all).

- [ ] **Step 4: Commit**

```bash
git add tests/conftest.py tests/test_fixtures.py
git commit -m "test: build_log + torn_sqlite_channel fixtures"
```

---

### Task 7: The guarded sub-fold + torn-seq locator

**Files:**
- Create: `runstate_tui/fold.py`
- Create: `tests/test_fold.py`

**Interfaces:**
- Consumes: `runstate.channel.Channel`, `runstate.observables.MalformedRecordError`.
- Produces:
  - `guarded(fn, channel) -> tuple[object | None, Issue | None]`: calls `fn(channel)`; on `(json.JSONDecodeError, sqlite3.DatabaseError, MalformedRecordError)` returns `(None, Issue(TORN, MEDIUM, "log torn at seq N"|"log torn", seq))`; else `(value, None)`.
  - `locate_torn_seq(channel) -> int | None`: bounded `read(after=k, limit=1)` walk that returns the seq of the first record whose decode raises, or `None`.

- [ ] **Step 1: Write the failing test**

`tests/test_fold.py`:

```python
from runstate.observables import progress
from runstate_tui.fold import guarded
from runstate_tui.types import IssueKind, Severity


def test_guarded_passes_through_a_clean_observable(build_log):
    ch = build_log([({"step": 5, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    value, issue = guarded(progress, ch)
    assert value == 5 and issue is None


def test_guarded_degrades_a_torn_read_to_a_torn_issue_with_seq(torn_sqlite_channel):
    ch = torn_sqlite_channel(
        [({"step": 5, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)],
        torn_seq=1,
    )
    value, issue = guarded(progress, ch)
    assert value is None
    assert issue.kind is IssueKind.TORN and issue.severity is Severity.MEDIUM
    assert issue.seq == 1  # located in-tree, no upstream ask
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fold.py -v`
Expected: FAIL — `ImportError` for `runstate_tui.fold`.

- [ ] **Step 3: Write minimal implementation**

`runstate_tui/fold.py`:

```python
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from runstate.channel import Channel
from runstate.observables import MalformedRecordError

from .types import Issue, IssueKind, Severity

_DECODE_ERRORS = (json.JSONDecodeError, sqlite3.DatabaseError, MalformedRecordError)


def locate_torn_seq(channel: Channel) -> int | None:
    """Find the seq of the first record whose decode raises (append-only contiguity):
    walk read(after=k, limit=1); a raising probe localizes the tear at k+1."""
    k = 0
    last = channel.last_seq()
    while k < last:
        try:
            got = channel.read(after=k, limit=1)
        except _DECODE_ERRORS:
            return k + 1
        if not got:
            return None
        k = got[0].seq
    return None


def guarded(fn: Callable[[Channel], object], channel: Channel) -> tuple[object | None, Issue | None]:
    try:
        return fn(channel), None
    except _DECODE_ERRORS as exc:
        seq = getattr(exc, "seq", None)
        if seq is None:
            seq = locate_torn_seq(channel)
        message = f"log torn at seq {seq}" if seq is not None else "log torn"
        return None, Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message=message, seq=seq)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/fold.py tests/test_fold.py
git commit -m "feat(fold): guarded sub-fold + torn-seq locator"
```

---

### Task 8: Status reconciliation (pending / live / stale / terminal)

**Files:**
- Modify: `runstate_tui/fold.py`
- Modify: `tests/test_fold.py`

**Interfaces:**
- Consumes: `runstate.observables.peek_terminal`, `last_activity`; `Env`, `resolve_liveness`; `guarded`.
- Produces: `reconcile_status(channel, env, now) -> tuple[Status, float | None, list[Issue]]` — reads `last_activity` **once** (guarded), computes `freshness = None | max(0, now-la)` and a `SkewSuspected` issue on `la > now`, and implements spec §4.1 for the readable freshness-only core: terminal(T) if `peek_terminal`; else `pending` if no dated activity; else `live`/`stale` via `resolve_liveness(..., la)`. Returns the freshness float so `status_fold` need not re-read.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fold.py`:

```python
from runstate.observables import Outcome
from runstate_tui.env import Env
from runstate_tui.fold import reconcile_status
from runstate_tui.types import StatusKind


def _env(now, **kw):
    return Env(clock=lambda: now, stuck_threshold=60.0, **kw)


def test_terminal_wins(build_log):
    ch = build_log([
        ({"handle": "local://h/1", "t": 1.0}, "lifecycle.started", None),
        ({"completed": True, "error": None, "final_step": 3, "t": 2.0}, "lifecycle.stopped", None),
    ])
    status, freshness, issues = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.TERMINAL and status.outcome is Outcome.COMPLETED
    assert issues == []


def test_pending_when_no_dated_activity(build_log):
    ch = build_log([])
    status, freshness, _ = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.PENDING and freshness is None


def test_live_then_stale_by_freshness(build_log):
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 100.0}, "lifecycle.heartbeat", None)])
    status, freshness, _ = reconcile_status(ch, _env(100.0), now=100.0)
    assert status.kind is StatusKind.LIVE and freshness == 0.0
    assert reconcile_status(ch, _env(1000.0), now=1000.0)[0].kind is StatusKind.STALE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fold.py -k "terminal_wins or pending_when or live_then" -v`
Expected: FAIL — `ImportError` for `reconcile_status`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/fold.py`:

```python
from runstate.observables import last_activity, peek_terminal

from .env import Env, Liveness, resolve_liveness
from .types import Status


def reconcile_status(channel: Channel, env: Env, now: float) -> tuple[Status, float | None, list[Issue]]:
    issues: list[Issue] = []
    result, term_issue = guarded(peek_terminal, channel)
    if term_issue is not None:
        issues.append(term_issue)

    la, la_issue = guarded(last_activity, channel)   # the ONE last_activity read
    if la_issue is not None:
        issues.append(la_issue)
    freshness = None if la is None else max(0.0, now - la)
    if isinstance(la, (int, float)) and not isinstance(la, bool) and la > now:
        issues.append(Issue(kind=IssueKind.SKEW_SUSPECTED, severity=Severity.MEDIUM,
                            message="last activity is in the future (clock skew)"))

    if result is not None:
        return Status.terminal(result.outcome), freshness, issues  # terminal wins
    if la is None:
        return Status.pending(), freshness, issues  # no dated activity at all
    verdict = resolve_liveness(channel, env, now, la)
    status = Status.live() if verdict is Liveness.LIVE else Status.stale()
    return status, freshness, issues
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fold.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/fold.py tests/test_fold.py
git commit -m "feat(fold): status reconciliation (pending/live/stale/terminal)"
```

---

### Task 9: `value` sub-fold (named objective, never nameless)

**Files:**
- Modify: `runstate_tui/fold.py`
- Modify: `tests/test_fold.py`

**Interfaces:**
- Consumes: `Channel.latest`, `runstate.vocabulary.payloads.Topic`.
- Produces: `read_value(channel, objective) -> tuple[str, object, int | None] | None`: `None` if `objective is None` or no such record; else `(name, value, step)` from `latest(Topic.VALUE, name=objective)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fold.py`:

```python
from runstate_tui.fold import read_value


def test_value_is_named_and_none_without_an_objective(build_log):
    ch = build_log([
        ({"value": 0.5, "step": 4, "t": 1.0}, "value", "loss"),
        ({"value": 0.9, "step": 4, "t": 1.0}, "value", "acc"),
    ])
    assert read_value(ch, objective=None) is None            # never nameless
    assert read_value(ch, objective="loss") == ("loss", 0.5, 4)
    assert read_value(ch, objective="missing") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fold.py -k value -v`
Expected: FAIL — `ImportError` for `read_value`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/fold.py`:

```python
from runstate.vocabulary.payloads import Topic


def read_value(channel: Channel, objective: str | None) -> tuple[str, object, int | None] | None:
    if objective is None:
        return None
    e = channel.latest(Topic.VALUE, name=objective)
    if e is None:
        return None
    return (objective, e.body.get("value"), e.body.get("step"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fold.py -k value -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/fold.py tests/test_fold.py
git commit -m "feat(fold): value sub-fold (named objective, O(1))"
```

---

### Task 10: `elapsed` sub-fold + skew guard (`SkewSuspected`)

**Files:**
- Modify: `runstate_tui/fold.py`
- Modify: `tests/test_fold.py`

**Interfaces:**
- Consumes: `Channel.read`, `Topic`.
- Produces:
  - `read_elapsed(channel, now) -> tuple[float | None, Issue | None]`: first `started.t` via `read(topics=[LIFECYCLE_STARTED], limit=1)`; `elapsed = max(0.0, now - t)`; `None` if no `started`; on `t > now` return a `SkewSuspected` issue (and clamped elapsed).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_fold.py`:

```python
from runstate_tui.fold import read_elapsed
from runstate_tui.types import IssueKind


def test_elapsed_is_wall_age_from_first_started(build_log):
    ch = build_log([
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"handle": "local://h/2", "t": 200.0}, "lifecycle.started", None),  # a later episode
    ])
    elapsed, issue = read_elapsed(ch, now=250.0)
    assert elapsed == 150.0 and issue is None  # from the FIRST started (100.0), not the latest


def test_elapsed_none_without_a_started(build_log):
    ch = build_log([({"step": 0, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    assert read_elapsed(ch, now=9.0) == (None, None)


def test_elapsed_never_negative_and_flags_skew(build_log):
    ch = build_log([({"handle": "local://h/1", "t": 500.0}, "lifecycle.started", None)])
    elapsed, issue = read_elapsed(ch, now=100.0)  # started stamped in the future
    assert elapsed == 0.0
    assert issue.kind is IssueKind.SKEW_SUSPECTED
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fold.py -k elapsed -v`
Expected: FAIL — `ImportError` for `read_elapsed`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/fold.py`:

```python
def read_elapsed(channel: Channel, now: float) -> tuple[float | None, Issue | None]:
    started = channel.read(topics=[Topic.LIFECYCLE_STARTED], limit=1)
    if not started:
        return None, None
    t = started[0].body.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool):
        return None, None
    if t > now:
        return 0.0, Issue(
            kind=IssueKind.SKEW_SUSPECTED, severity=Severity.MEDIUM,
            message="run epoch is in the future (clock skew)", detail=f"started.t={t} > now={now}",
        )
    return now - float(t), None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_fold.py -k elapsed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add runstate_tui/fold.py tests/test_fold.py
git commit -m "feat(fold): elapsed wall-age + skew guard"
```

---

### Task 11: Assemble `status_fold` → `Row` + integration tests

**Files:**
- Modify: `runstate_tui/fold.py`
- Modify: `runstate_tui/__init__.py`
- Modify: `tests/test_fold.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `status_fold(channel: Channel, env: Env) -> Row` — captures `now = env.clock()` once, threads it to freshness/elapsed, assembles the truth-quintet + all issues. `frontier` via `guarded(progress)`. `episode` is `None` in Stage 0 (drill-down, Stage 3). Re-exported from the package.

- [ ] **Step 1: Write the failing integration test**

Append to `tests/test_fold.py`:

```python
from runstate_tui import status_fold
from runstate_tui.types import Row, StatusKind


def test_status_fold_on_a_healthy_live_run(build_log):
    ch = build_log([
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
        ({"value": 0.03, "step": 7, "t": 140.0}, "value", "loss"),
    ])
    row = status_fold(ch, _env(150.0, objective="loss"))
    assert isinstance(row, Row)
    assert row.status.kind is StatusKind.LIVE
    assert row.frontier == 7
    assert row.value == ("loss", 0.03, 7)
    assert row.elapsed == 50.0
    assert row.freshness == 10.0
    assert row.issues == ()


def test_status_fold_degrades_one_torn_factor_not_the_whole_row(torn_sqlite_channel):
    # a torn heartbeat: frontier is lost + a Torn issue, but the run's verdict survives
    ch = torn_sqlite_channel([
        ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
        ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
    ], torn_seq=2)
    row = status_fold(ch, _env(150.0))
    assert row.status.kind is not StatusKind.UNREADABLE      # NOT collapsed to unreadable
    assert any(i.kind is IssueKind.TORN for i in row.issues)  # the torn factor is surfaced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_fold.py -k status_fold -v`
Expected: FAIL — `ImportError` for `status_fold`.

- [ ] **Step 3: Write minimal implementation**

Append to `runstate_tui/fold.py`:

```python
from runstate.observables import progress

from .types import Row


def status_fold(channel: Channel, env: Env) -> Row:
    now = env.clock()  # captured once per frame, threaded below
    issues: list[Issue] = []

    status, freshness, status_issues = reconcile_status(channel, env, now)
    issues.extend(status_issues)

    frontier, frontier_issue = guarded(progress, channel)
    if frontier_issue is not None:
        issues.append(frontier_issue)

    value = read_value(channel, env.objective)
    elapsed, elapsed_issue = read_elapsed(channel, now)
    if elapsed_issue is not None:
        issues.append(elapsed_issue)

    return Row(status=status, frontier=frontier, freshness=freshness, value=value,
               elapsed=elapsed, episode=None, issues=tuple(issues))
```

- [ ] **Step 4: Run the full suite**

Run: `pytest -v`
Expected: PASS (all tasks green).

- [ ] **Step 5: Re-export and commit**

Update `runstate_tui/__init__.py` to add:

```python
from .env import Env, Liveness, LivenessSignal, FreshnessSignal  # noqa: F401
from .fold import status_fold  # noqa: F401
```

```bash
git add runstate_tui/fold.py runstate_tui/__init__.py tests/test_fold.py
git commit -m "feat(fold): status_fold assembles the truth-quintet Row"
```

---

## Self-Review

- **Spec coverage (Stage 0 scope):** truth-quintet ✓ (Tasks 8–11); two-tier *per-observable* defense ✓ (Task 7, 11); freshness-only liveness via the committed seam ✓ (Task 5); `value` named-objective ✓ (Task 9); `elapsed` wall-age + skew ✓ (Task 10); `Status` wraps `Outcome`, honest unknowns ✓ (Task 3); `Row` frozen value ✓ (Task 4); Torn/SkewSuspected issues ✓. **Deferred per Global Constraints:** `missing`/`unreadable` (Stage-1 open-wrapper), `conflicted` detection (follow-up), `UnsafeStop`/stop (Stage 2), the atomic verdict read + pure-`[Envelope]→RunResult` fold (H3 — Stage 0 calls `peek_terminal` and accepts the cosmetic tear, per spec §3.2 caveat).
- **Placeholder scan:** two implementer notes flag intentionally-explicit code (Task 4 `.severity`, Task 11 import) with the clean form given — not placeholders. No TBDs.
- **Type consistency:** `Severity`/`Issue`/`Status`/`Row`/`Env`/`Liveness` names and `status_fold`/`guarded`/`read_value`/`read_elapsed`/`reconcile_status`/`resolve_liveness` signatures are consistent across tasks and match spec §14.
