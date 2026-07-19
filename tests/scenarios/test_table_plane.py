"""Layout snapshot for the multi-run table (.superpowers/sdd/task-3-brief.md): two
seeded runs rendered through `MultiRunApp`'s keyed `DataTable`, headless SVG,
following the snapshot-test convention established in `tests/test_detail.py`.

`MultiRunApp` is itself the top-level `App` (unlike `DrillDownScreen`, which needs a
`Host` app to push it onto) -- so `snap_compare` takes the app instance directly,
same as `test_detail.py`'s `Host()` case, no extra wrapping needed."""

from __future__ import annotations

from runstate import open_channel
from textual.widgets import DataTable

from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp
from runstate_tui.resolver import explicit_resolver


def _seed(tmp_path, run_id, t=100.0):
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": t}, topic="lifecycle.started")
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
