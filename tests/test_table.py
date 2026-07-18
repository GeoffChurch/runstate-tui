import os
import sqlite3
from pathlib import Path

import pytest
from runstate import open_channel

from runstate_tui.env import Env
from runstate_tui.resolver import const_resolver
from runstate_tui.table import open_and_fold, render_single, render_table
from runstate_tui.types import IssueKind, Severity, StatusKind


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


def test_render_table_maps_over_the_resolver_in_order(tmp_path):
    _sqlite_run(tmp_path, "a", [({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None)])

    # "b" is a missing pointer
    def resolver(now):
        return [("a", str(tmp_path), "sqlite"), ("b", str(tmp_path), "sqlite")]

    rows = render_table(resolver, _env())
    assert len(rows) == 2
    assert rows[0].status.kind is StatusKind.LIVE
    assert rows[1].status.kind is StatusKind.MISSING


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unreadable_parent_dir_is_unreadable(tmp_path):
    d = tmp_path / "locked"
    d.mkdir()
    (d / "r.db").write_bytes(b"")  # a file exists, but we'll make the dir unsearchable
    d.chmod(0o000)
    try:
        row = open_and_fold(("r", str(d), "sqlite"), _env())
        assert row.status.kind is StatusKind.UNREADABLE
    finally:
        d.chmod(0o755)  # restore so tmp_path cleanup works


def test_open_and_fold_memory_backend_skips_stat():
    run_id = "r"
    ch = open_channel(run_id, backend="memory")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    row = open_and_fold((run_id, "", "memory"), _env())
    assert row.status.kind is not StatusKind.MISSING


def test_singleton_test_single_run_is_the_table_at_one(tmp_path):
    _sqlite_run(
        tmp_path,
        "r",
        [
            ({"handle": "local://h/1", "t": 100.0}, "lifecycle.started", None),
            ({"step": 3, "consumed_seq": 0, "t": 140.0}, "lifecycle.heartbeat", None),
        ],
    )
    ref = ("r", str(tmp_path), "sqlite")
    env = _env()  # fixed clock -> both fold passes see the same `now`
    assert render_single(ref, env) == render_table(const_resolver(ref), env)[0]


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


def test_open_and_fold_maps_byte_torn_to_corrupt(torn_sqlite_channel, tmp_path):
    # byte-torn is NOT unreadable and NOT a crash — it's a distinct, loud `corrupt`
    # status carrying the torn seq.
    torn_sqlite_channel([({"handle": "h", "t": 1.0}, "lifecycle.started", None)], torn_seq=1)
    # torn_sqlite_channel writes run_id "torn" under tmp_path
    row = open_and_fold(("torn", str(tmp_path), "sqlite"), Env(clock=lambda: 1.0))
    assert row.status.kind is StatusKind.CORRUPT
    assert row.status.severity is Severity.HIGH
    assert any(i.kind is IssueKind.CORRUPT and i.seq == 1 for i in row.issues)


def test_read_log_delta_is_incremental(tmp_path):
    from runstate_tui.table import read_log_delta

    ch = open_channel("d", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")  # seq 1
    ch.send({"step": 1, "consumed_seq": 0, "t": 2.0}, topic="lifecycle.heartbeat")  # seq 2
    ch.close()
    ref = ("d", str(tmp_path), "sqlite")

    all_ = read_log_delta(ref, after=0)
    assert [e.seq for e in all_] == [1, 2]
    assert read_log_delta(ref, after=1)[0].seq == 2  # only the delta
    assert read_log_delta(ref, after=2) == []  # nothing new


def test_read_log_delta_missing_run_is_empty(tmp_path):
    from runstate_tui.table import read_log_delta

    assert read_log_delta(("ghost", str(tmp_path), "sqlite"), after=0) == []
    assert not (tmp_path / "ghost.db").exists()  # no phantom fabricated


def test_read_log_delta_corrupt_db_is_empty(tmp_path):
    from runstate_tui.table import read_log_delta

    (Path(tmp_path) / "corrupt.db").write_bytes(b"this is not a sqlite database")
    assert read_log_delta(("corrupt", str(tmp_path), "sqlite"), after=0) == []


def test_read_log_delta_maps_a_substrate_read_fault_to_empty(tmp_path, monkeypatch):
    from runstate.channel.sqlite import SqliteChannel

    from runstate_tui.table import read_log_delta

    ch = open_channel("sub", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    ch.close()

    def boom(self, *a, **kw):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(SqliteChannel, "read", boom)
    assert read_log_delta(("sub", str(tmp_path), "sqlite"), after=0) == []


def test_read_log_delta_byte_torn_is_empty(torn_sqlite_channel, tmp_path):
    # byte-torn is no longer a crash; the drill-down header surfaces `corrupt` via
    # render_single, so the raw tail degrades to empty (like the other read faults).
    torn_sqlite_channel([({"handle": "h", "t": 1.0}, "lifecycle.started", None)], torn_seq=1)
    from runstate_tui.table import read_log_delta

    assert read_log_delta(("torn", str(tmp_path), "sqlite"), after=0) == []
