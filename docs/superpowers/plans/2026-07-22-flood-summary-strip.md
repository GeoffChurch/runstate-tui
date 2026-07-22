# Fleet Summary Strip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-on, one-line summary strip above the multi-run table that tallies the frame's rows into per-condition chips (a legend + fleet roll-up + flood digest).

**Architecture:** A pure `format_fleet_summary(rows) -> Text` builds the strip from two tallies — a status partition (every run once) and issue tags (skipping the two `_STATUS_TWIN_ISSUES` that merely restate a status), sorted `(severity desc, name)`. It's wired into `MultiRunApp.on_table_ready` on the main thread. Purely additive: no change to the fold, pool, reconcile, or concurrency model.

**Tech Stack:** Python 3.11, Rich (`Text`), Textual 8.2.8 (`Static`, `run_test`/`Pilot`), uv, ruff, mypy `--strict`, pytest.

## Global Constraints

- **Python floor `>=3.11`.**
- **Purely additive** — do NOT change the fold, `ChannelPool`, `fold_frame`, the keyed reconcile, the watchdog, teardown, or the concurrency model. The only files touched are `types.py`, `format.py`, `multirun.py`, and their tests.
- **Names are passthrough** — status name is `Status.label`, issue name is `IssueKind.value`. No per-kind label maps.
- **Dot-only color** — the `●`/`⚠` glyph carries the color; the `label count` text is neutral via an explicit `style="default"` append (the `Text.append` base-style inheritance footgun; `_marker` / `format_summary_card` guard it the same way).
- **Tests use `asyncio.run(...)` wrappers, never `@pytest.mark.asyncio`.** Reuse `_row` in `tests/test_format.py` and `_seed` in `tests/test_multirun.py`.
- **Gates (all must pass before every commit):** during implementation run `uv run ruff format .` then `uv run ruff check --fix .` (auto-applies formatting *and* import order), then `uv run mypy`, `uv run pytest`. CI verifies with `ruff format --check .` and `ruff check .`.

## File Structure

- `runstate_tui/types.py` (modify) — add the `_STATUS_TWIN_ISSUES` constant beside `IssueKind`.
- `runstate_tui/format.py` (modify) — add `format_fleet_summary`, `_chip`, `_sev_color`.
- `runstate_tui/multirun.py` (modify) — a `#summary` `Static` in `compose`; build + display-toggle it in `on_table_ready`.
- `tests/test_format.py`, `tests/test_multirun.py` (modify) — append tests.

---

### Task 1: the pure builder + the `_STATUS_TWIN_ISSUES` constant

**Files:**
- Modify: `runstate_tui/types.py`, `runstate_tui/format.py`
- Test: `tests/test_format.py`

**Interfaces:**
- Consumes: `Row`, `Status`, `IssueKind`, `Severity` (types); `status_color` (format); `collections.abc.Sequence`.
- Produces: `_STATUS_TWIN_ISSUES: set[IssueKind]` in `types.py`; `format_fleet_summary(rows: Sequence[Row]) -> Text` in `format.py`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_format.py` (it already has `_row(**kw)` and imports `Issue, IssueKind, Row, Severity, Status`, `Outcome`):

```python
def test_fleet_summary_orders_worst_first_and_counts():
    from runstate_tui.format import format_fleet_summary

    rows = (
        [_row(status=Status.unreadable()) for _ in range(30)]
        + [
            _row(
                status=Status.corrupt(),
                issues=(Issue(IssueKind.CORRUPT, Severity.HIGH, "log corrupt", seq=1),),
            )
            for _ in range(2)
        ]
        + [_row(status=Status.live(), issues=(Issue(IssueKind.MALFORMED, Severity.MEDIUM, "bad"),))]
        + [_row(status=Status.live()) for _ in range(93)]
        + [_row(status=Status.terminal(Outcome.COMPLETED)) for _ in range(3)]
    )
    plain = format_fleet_summary(rows).plain
    assert "unreadable 30" in plain
    assert "corrupt 2" in plain
    assert "malformed 1" in plain
    assert "live 94" in plain  # 93 pure-live + the 1 live-with-malformed
    assert "done 3" in plain
    # worst-first: HIGH (corrupt < unreadable) before MEDIUM (malformed) before OK (done < live)
    i = plain.index
    assert i("corrupt") < i("unreadable") < i("malformed") < i("done") < i("live")


def test_fleet_summary_corrupt_counts_once_as_status_not_issue():
    from runstate_tui.format import format_fleet_summary

    torn = Issue(IssueKind.CORRUPT, Severity.HIGH, "log corrupt at seq 5", seq=5)
    plain = format_fleet_summary([_row(status=Status.corrupt(), issues=(torn,)) for _ in range(2)]).plain
    assert "corrupt 2" in plain
    assert plain.count("corrupt") == 1  # ONLY the status chip -- the CORRUPT issue-twin is skipped
    assert "⚠" not in plain  # no issue chip at all


def test_fleet_summary_malformed_shows_under_status_and_as_a_tag():
    from runstate_tui.format import format_fleet_summary

    m = Issue(IssueKind.MALFORMED, Severity.MEDIUM, "bad record")
    plain = format_fleet_summary([_row(status=Status.live(), issues=(m,))]).plain
    assert "live 1" in plain  # counted under its status...
    assert "malformed 1" in plain  # ...AND tagged -- two genuinely-different facts


def test_fleet_summary_issue_name_is_kind_value_verbatim():
    from runstate_tui.format import format_fleet_summary

    s = Issue(IssueKind.SKEW_SUSPECTED, Severity.MEDIUM, "clock skew")
    assert "skew_suspected 1" in format_fleet_summary([_row(status=Status.live(), issues=(s,))]).plain


def test_fleet_summary_empty_is_empty_text():
    from runstate_tui.format import format_fleet_summary

    assert format_fleet_summary([]).plain == ""


def test_fleet_summary_colors_the_glyph_only_not_the_label():
    from runstate_tui.format import format_fleet_summary, status_color

    text = format_fleet_summary([_row(status=Status.live())])
    green = [s for s in text.spans if s.style == status_color(Status.live())]
    assert green  # the ● glyph is colored
    assert all(s.end <= len("● ") for s in green)  # color covers only the glyph, not the label
    assert any(s.style == "default" for s in text.spans)  # the label text is neutral


def test_fleet_summary_order_is_stable_regardless_of_counts():
    from runstate_tui.format import format_fleet_summary

    few = [_row(status=Status.unreadable())] + [_row(status=Status.live()) for _ in range(2)]
    many = [_row(status=Status.unreadable()) for _ in range(50)] + [_row(status=Status.live())]
    for rows in (few, many):
        plain = format_fleet_summary(rows).plain
        assert plain.index("unreadable") < plain.index("live")  # severity order, never count
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_format.py -k fleet_summary -v`
Expected: FAIL — `ImportError: cannot import name 'format_fleet_summary'`.

- [ ] **Step 3: Add the `_STATUS_TWIN_ISSUES` constant** — in `runstate_tui/types.py`, immediately after the `IssueKind` enum (before the `Issue` dataclass):

```python
# Issue kinds that are the *footnote* of a status verdict, not a separate problem: a
# byte-torn run is Status.corrupt() + a CORRUPT issue carrying the seq; a fold crash is
# Status.error() + an INTERNAL_ERROR issue carrying the exception (table.py _corrupt /
# _fold_error -- deliberate dual-surfacing, feeding the status column AND the drill-down
# list). A per-event headcount (the fleet summary strip) counts these once, by their
# status, so it skips them as issue tags.
_STATUS_TWIN_ISSUES = {IssueKind.CORRUPT, IssueKind.INTERNAL_ERROR}
```

- [ ] **Step 4: Add the builder** — in `runstate_tui/format.py`. First extend the imports at the top:

```python
from collections.abc import Sequence

from rich.text import Text
from runstate.channel import Envelope
from runstate.observables import Outcome

from .types import IssueKind, Row, Severity, Status, StatusKind, _STATUS_TWIN_ISSUES
```

Then add these three functions (below `format_summary_card`, above `format_envelope`):

```python
def _sev_color(severity: Severity) -> str:
    """The ⚠-tag hue by severity (mirrors _marker): HIGH red, else amber."""
    return "#f85149" if severity >= Severity.HIGH else "#d29922"


def _chip(out: Text, glyph: str, color: str, text: str) -> None:
    """Append one `<glyph> text   ` chip: the glyph carries `color`; the label stays neutral
    (explicit style="default" -- Text.append without a style inherits the base and would paint
    the label, the footgun _marker / format_summary_card also guard)."""
    out.append(f"{glyph} ", style=color)
    out.append(f"{text}   ", style="default")


def format_fleet_summary(rows: Sequence[Row]) -> Text:
    """The always-on fleet legend / roll-up strip: one `<glyph> <name> <count>` chip per
    condition present in `rows`, worst-first `(severity desc, name)`. Statuses partition the
    fleet (each run once; ● + status_color); issues that are NOT a status-twin are tagged
    (⚠, colored by Issue.severity). Names are passthrough (Status.label / IssueKind.value).
    Empty Text for no rows. Pure -- rebuilt each frame in on_table_ready."""
    status_count: dict[str, int] = {}
    status_repr: dict[str, Status] = {}
    for row in rows:
        lbl = row.status.label
        status_count[lbl] = status_count.get(lbl, 0) + 1
        status_repr.setdefault(lbl, row.status)  # a bucket rep -> its color & severity
    issue_count: dict[IssueKind, int] = {}
    issue_sev: dict[IssueKind, Severity] = {}
    for row in rows:
        for kind in {i.kind for i in row.issues} - _STATUS_TWIN_ISSUES:
            issue_count[kind] = issue_count.get(kind, 0) + 1
            issue_sev[kind] = max(
                issue_sev.get(kind, Severity.OK),
                max(i.severity for i in row.issues if i.kind == kind),
            )
    chips: list[tuple[Severity, str, str, str, int]] = [
        (status_repr[lbl].severity, lbl, "●", status_color(status_repr[lbl]), n)
        for lbl, n in status_count.items()
    ] + [
        (issue_sev[k], k.value, "⚠", _sev_color(issue_sev[k]), n) for k, n in issue_count.items()
    ]
    out = Text()
    for _sev, name, glyph, color, n in sorted(chips, key=lambda c: (-c[0], c[1])):
        _chip(out, glyph, color, f"{name} {n}")  # severity desc, then name
    return out
```

(All of `Envelope`, `Outcome`, `Status`, `StatusKind` remain used elsewhere in `format.py` — keep them; `ruff check --fix` will settle the import member order.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_format.py -k fleet_summary -v`
Expected: PASS (7 passed).

- [ ] **Step 6: Run the gates**

Run: `uv run ruff format . && uv run ruff check --fix . && uv run mypy && uv run pytest`
Expected: all green (the existing `test_format` suite stays green).

- [ ] **Step 7: Commit**

```bash
git add runstate_tui/types.py runstate_tui/format.py tests/test_format.py
git commit -m "feat(format): format_fleet_summary — per-condition roll-up chips"
```

---

### Task 2: wire the strip into the multi-run table

**Files:**
- Modify: `runstate_tui/multirun.py`
- Test: `tests/test_multirun.py`

**Interfaces:**
- Consumes: `format_fleet_summary` (Task 1); the existing `compose`, `on_table_ready`, `#stall`/`#empty` regions, and `want`.
- Produces: a `#summary` `Static` populated each frame from `msg.table`, shown iff the frame has ≥1 run.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_multirun.py` (it already imports `asyncio`, `Static`, `DataTable`, `Env`, `MultiRunApp`, `explicit_resolver`, and defines `_seed`):

```python
def test_summary_strip_shows_fleet_rollup(tmp_path):
    asyncio.run(_summary_strip_shows_fleet_rollup(tmp_path))


async def _summary_strip_shows_fleet_rollup(tmp_path):
    a = _seed(tmp_path, "a")
    b = _seed(tmp_path, "b")
    app = MultiRunApp(explicit_resolver([a, b]), Env(clock=lambda: 150.0), tick_interval=999)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        summary = app.query_one("#summary", Static)
        assert summary.display
        assert "live 2" in str(summary.content)  # both seeded runs are live -> one chip, count 2


def test_summary_hidden_when_no_runs_then_swaps_in(tmp_path):
    asyncio.run(_summary_hidden_when_no_runs_then_swaps_in(tmp_path))


async def _summary_hidden_when_no_runs_then_swaps_in(tmp_path):
    # a glob-empty frame: #empty owns the screen, #summary is hidden; a run appearing swaps.
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
        summary = app.query_one("#summary", Static)
        assert not summary.display  # 0 runs -> hidden
        assert app.query_one("#empty", Static).display
        live["refs"] = [_seed(tmp_path, "a")]
        app._tick()
        await pilot.pause()
        await pilot.pause()
        assert summary.display  # a run appeared -> shown
        assert "live 1" in str(summary.content)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_multirun.py -k summary -v`
Expected: FAIL — `NoMatches` (there is no `#summary` widget yet).

- [ ] **Step 3: Implement the wiring** — three edits in `runstate_tui/multirun.py`:

Extend the format import:

```python
from .format import format_fleet_summary, status_color
```

Add the `#summary` `Static` in `compose` (between `#stall` and `#empty`, per the spec's top-to-bottom order):

```python
    def compose(self) -> ComposeResult:
        yield Static("", id="stall")  # the watchdog banner (hidden via display, see on_mount)
        yield Static("", id="summary")  # the always-on fleet legend / roll-up strip
        yield Static("", id="empty")  # the zero-match placeholder (glob mode; toggled in reconcile)
        yield DataTable(id="runs")
```

Populate + toggle it at the end of `on_table_ready`. Replace the existing empty/table toggle block:

```python
        empty = self.query_one("#empty", Static)
        if self._empty_hint is not None and not want:
            empty.display = True
            t.display = False
        else:
            empty.display = False
            t.display = True
```

with:

```python
        empty = self.query_one("#empty", Static)
        summary = self.query_one("#summary", Static)
        if self._empty_hint is not None and not want:
            empty.display = True
            t.display = False
            summary.display = False
        else:
            empty.display = False
            t.display = True
            if want:
                summary.update(format_fleet_summary([row for _, row in msg.table]))
                summary.display = True
            else:
                summary.display = False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_multirun.py -v`
Expected: PASS — the two new tests pass and every existing `test_multirun` test stays green (adding a widget doesn't disturb the reconcile/cursor/watchdog/drain tests).

- [ ] **Step 5: Run the gates**

Run: `uv run ruff format . && uv run ruff check --fix . && uv run mypy && uv run pytest`
Expected: all green (including the showcase smoke test — a new strip renders, it doesn't break the render).

- [ ] **Step 6: Commit**

```bash
git add runstate_tui/multirun.py tests/test_multirun.py
git commit -m "feat(multirun): always-on fleet summary strip above the table"
```

---

## Self-Review (spec coverage)

| Spec section | Task |
|---|---|
| Two tallies: status partition + issue tags; both flavors | Task 1 |
| Passthrough names (`Status.label` / `IssueKind.value`) | Task 1 (`test_..._kind_value_verbatim`) |
| `_STATUS_TWIN_ISSUES` skip in `types.py`; count corrupt/error once by status | Task 1 (`test_..._corrupt_counts_once`) + the constant |
| Wanted double-count kept (`malformed` under status + as tag) | Task 1 (`test_..._malformed_shows_under_status_and_as_a_tag`) |
| Order `(severity desc, name)`, no order table, stable | Task 1 (`test_..._orders_worst_first`, `test_..._order_is_stable`) |
| Dot-only color + footgun guard | Task 1 (`test_..._colors_the_glyph_only`) + `_chip` |
| Present-only, no threshold, empty → empty | Task 1 (empty test; present-only falls out of counting) |
| Always-on strip above the table, main-thread, shown iff ≥1 run | Task 2 |
| Purely additive (no fold/pool/reconcile/concurrency change) | Both — only `types.py`/`format.py`/`multirun.py` touched |

**Placeholder scan:** none — every step has complete code and exact commands.

**Type consistency:** `format_fleet_summary(rows: Sequence[Row]) -> Text` (Task 1) is imported and called with `[row for _, row in msg.table]` in Task 2; `_STATUS_TWIN_ISSUES: set[IssueKind]` (types.py) is consumed via set-difference in the builder; `Static(id="summary")` (compose) matches the `query_one("#summary", Static)` in `on_table_ready` and both tests.

**Deferred (not built):** navigable chips, full always-legend (zero-count), threshold styling — see the spec's Deferred section. Optionally regenerate the `docs/img` multi-run scene to show the strip (cosmetic; not required).
