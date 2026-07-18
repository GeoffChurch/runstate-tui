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
    _sqlite_run(
        tmp_path,
        "r",
        [
            ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
            ({"step": 7, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
        ],
    )
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
