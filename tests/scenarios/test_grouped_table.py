"""Layout snapshot for the GROUPED multi-run table: three runs across two scenarios rendered as
single-table sections (``── <scenario> ──`` header rows, groups ascending, rows by label within a
group), headless SVG. Mirrors `test_table_plane.py`'s `snap_compare` harness -- `MultiRunApp` is the
top-level `App`, so `snap_compare` takes the instance directly."""

from __future__ import annotations

from runstate import create_channel
from textual.widgets import DataTable

from runstate_tui.env import Env
from runstate_tui.multirun import MultiRunApp


def _seed(tmp_path, run_id, t=100.0):
    ch = create_channel(run_id, root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": t}, topic="lifecycle.started")
    ch.close()
    return (run_id, str(tmp_path), "sqlite")


def test_grouped_table_snapshot(snap_compare, tmp_path):
    a = _seed(tmp_path, "a", t=100.0)
    b = _seed(tmp_path, "b", t=40.0)
    c = _seed(tmp_path, "c", t=120.0)
    items = [
        (a, {"scenario": "en-ru-16M", "variant": "fb_5k"}),
        (b, {"scenario": "en-ru-16M", "variant": "fb_10k"}),
        (c, {"scenario": "fr-en-4M", "variant": "fb_5k"}),
    ]
    app = MultiRunApp(
        lambda now: items, Env(clock=lambda: 150.0), tick_interval=999.0, group_by="scenario"
    )

    async def _settle(pilot):
        # poll until the reconcile has landed all rows (3 data + 2 group headers), same
        # convergence-loop idiom as test_table_plane.py so the baseline is settled.
        for _ in range(60):
            await pilot.pause(0.02)
            t = pilot.app.query_one("#runs", DataTable)
            if t.row_count == 5:
                break

    assert snap_compare(app, run_before=_settle)
