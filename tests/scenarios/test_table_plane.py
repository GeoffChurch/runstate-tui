"""Layout snapshot for the multi-run table (.superpowers/sdd/task-3-brief.md): two
seeded runs rendered through `MultiRunApp`'s keyed `DataTable`, headless SVG,
following the snapshot-test convention established in `tests/test_detail.py`.

`MultiRunApp` is itself the top-level `App` (unlike `DrillDownScreen`, which needs a
`Host` app to push it onto) -- so `snap_compare` takes the app instance directly,
same as `test_detail.py`'s `Host()` case, no extra wrapping needed."""

from __future__ import annotations

from runstate import Outcome, create_channel
from textual.widgets import DataTable

from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import explicit_resolver
from tests.helpers import log_path, move_log
from tests.records import Script, write


def _seed(tmp_path, run_id, t=100.0):
    """A claimed run (log format 0.4.0): its `started` at t, no commits yet."""
    ch = create_channel(run_id, root=tmp_path, backend="sqlite")
    s = Script()
    s.started(t, "h")
    write(ch, s.records)
    ch.close()
    return (run_id, str(tmp_path), "sqlite")


def test_table_plane_snapshot(snap_compare, tmp_path):
    refs = [_seed(tmp_path, "a"), _seed(tmp_path, "b", t=40.0)]
    app = MultiRunApp(explicit_resolver(refs), Env(clock=lambda: 150.0), tick_interval=999.0)

    async def _settle(pilot):
        # poll until the keyed reconcile has landed both rows -- the same
        # convergence-loop idiom as test_detail.py's snapshot `run_before`, so the
        # baseline is captured settled, not mid-fold.
        for _ in range(60):
            await pilot.pause(0.02)
            t = pilot.app.query_one("#runs", DataTable)
            if t.row_count == 2:
                break

    assert snap_compare(app, run_before=_settle)


def test_log_format_snapshot(snap_compare, tmp_path):
    """The cockpit on runstate 0.4.0 beside logs it cannot read: a run still in log format
    0.3.0 (refused with LogFormatMismatch, naming `runstate migrate`) and one from before
    versioned addresses (LogFormatMissing) are each a loud HIGH row with a red dot and
    ⚠⚠ -- never a crash, never `missing` -- beside a migrated run that reads normally,
    including the death report that ended it."""
    migrated = _seed(tmp_path, "migrated")
    w = Script(records=[], claim=1)
    w.commit(120.0)
    w.died(Outcome.KILLED, "slurm:TIMEOUT", 140.0)
    ch = create_channel("migrated", root=tmp_path, backend="sqlite")
    write(ch, w.records)
    ch.close()
    old = _seed(tmp_path, "still-030")
    move_log(tmp_path, "still-030", to=log_path(tmp_path, "still-030", version="0.3.0"))
    legacy = _seed(tmp_path, "unversioned")
    move_log(tmp_path, "unversioned", to=tmp_path / "unversioned.db")
    app = MultiRunApp(
        explicit_resolver([migrated, old, legacy]),
        Env(clock=lambda: 150.0),
        tick_interval=999.0,
    )

    async def _settle(pilot):
        for _ in range(60):
            await pilot.pause(0.02)
            t = pilot.app.query_one("#runs", DataTable)
            if t.row_count == 3:
                break

    assert snap_compare(app, run_before=_settle, terminal_size=(110, 12))
