import os
import sqlite3
from pathlib import Path

import pytest
from runstate import create_channel

from runstate_tui.env import Env
from runstate_tui.fold import status_fold
from runstate_tui.resolver import const_resolver
from runstate_tui.table import open_and_fold, render_single, render_table
from runstate_tui.types import IssueKind, Severity, StatusKind
from tests.helpers import corrupt_seq


def _env(now=150.0, **kw):
    return Env(clock=lambda: now, stuck_threshold=60.0, **kw)


def _sqlite_run(tmp_path, run_id, records):
    ch = create_channel(run_id, root=tmp_path, backend="sqlite")
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
    assert row.status.kind is StatusKind.MISSING  # attach_channel -> RunNotFound
    assert row.frontier is None and row.issues == ()
    assert not (Path(tmp_path) / "ghost.db").exists()  # attach_channel never creates a file


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
def test_unreadable_parent_dir_is_missing(tmp_path):
    # Semantic shift under attach_channel: an unsearchable parent dir makes the
    # `mode=rw` connect fail with sqlite3.OperationalError ("unable to open database
    # file"), which attach_channel maps to RunNotFound (indistinguishable from an
    # absent run) -> `missing`. The old stat-before-open surfaced this as `unreadable`
    # via its `except OSError`; that distinction is gone (permission-denied -> missing).
    d = tmp_path / "locked"
    d.mkdir()
    (d / "r.db").write_bytes(b"")  # a file exists, but we'll make the dir unsearchable
    d.chmod(0o000)
    try:
        row = open_and_fold(("r", str(d), "sqlite"), _env())
        assert row.status.kind is StatusKind.MISSING
    finally:
        d.chmod(0o755)  # restore so tmp_path cleanup works


def test_open_and_fold_memory_backend_attaches_a_run_with_records():
    # the memory backend has no file to stat; open_and_fold attaches by registry lookup.
    # A run WITH records attaches (not RunNotFound) and folds to a real status. The
    # registry key is (abspath(root), run_id), so the birth root must match the ref's
    # root ("" here) for attach to find the same log.
    run_id = "r"
    ch = create_channel(run_id, root="", backend="memory")
    ch.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
    row = open_and_fold((run_id, "", "memory"), _env())
    assert row.status.kind is StatusKind.LIVE  # attached + folded to a real status


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
    ch = create_channel("sub", root=tmp_path, backend="sqlite")
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

    ch = create_channel("d", root=tmp_path, backend="sqlite")
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

    ch = create_channel("sub", root=tmp_path, backend="sqlite")
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


def test_fold_open_channel_matches_status_fold_on_a_healthy_run(build_log):
    from runstate_tui.table import fold_open_channel

    ch = build_log([({"handle": "h", "t": 100.0}, "lifecycle.started", None)])
    env = _env(150.0)  # the module's Env helper
    assert fold_open_channel(ch, env) == status_fold(ch, env)


def test_fold_open_channel_maps_byte_torn_to_corrupt(tmp_path):
    from runstate_tui.table import fold_open_channel

    ch = create_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 100.0}, topic="lifecycle.started")
    ch.close()
    corrupt_seq(tmp_path, "r", 1, literal="{not json")
    ch = create_channel("r", root=tmp_path, backend="sqlite")
    assert fold_open_channel(ch, _env(150.0)).status.kind is StatusKind.CORRUPT
    ch.close()


def test_read_log_delta_applies_filter(tmp_path):
    from runstate import create_channel

    from runstate_tui.table import read_log_delta

    ch = create_channel("r", root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    ch.send({}, topic="control.stop", request_id="webui:x")
    ch.close()
    ref = ("r", str(tmp_path), "sqlite")
    only_control = read_log_delta(ref, after=0, filter=lambda e: e.topic.startswith("control."))
    assert [e.topic for e in only_control] == ["control.stop"]
    all_e = read_log_delta(ref, after=0)  # filter=None -> unchanged behavior
    assert len(all_e) == 2


def test_envelope_filter_text_and_families():
    from types import SimpleNamespace as NS

    from runstate_tui.table import envelope_filter

    started = NS(topic="lifecycle.started", request_id=None, body={"t": 1.0})
    stop = NS(topic="control.stop", request_id="webui:x", body={})
    f_text = envelope_filter("control", set())
    assert f_text(stop) and not f_text(started)  # substring over topic/request
    f_req = envelope_filter("webui:x", set())
    assert f_req(stop) and not f_req(started)  # matches request_id
    f_hide = envelope_filter("", {"control"})  # subtractive: hide "control"
    assert f_hide(started) and not f_hide(stop)
    f_none = envelope_filter("", set())
    assert f_none(started) and f_none(stop)  # nothing hidden -> everything


def test_unknown_family_topics_always_shown():
    # Finding #1: envelope_filter's family param is subtractive (HIDE the toggled-off
    # KNOWN families), never restrict-to -- a topic outside the known families (e.g.
    # launcher.* written onto the same channel by runstate's Launcher) is never in
    # `hidden_families`, so it is never silently dropped, no matter which known
    # families are toggled off.
    from types import SimpleNamespace as NS

    from runstate_tui.table import envelope_filter

    launcher = NS(topic="launcher.terminated", request_id=None, body={})
    # hide every known family the app knows about -- launcher.* still shows
    f = envelope_filter("", {"lifecycle", "value", "control"})
    assert f(launcher)
    # even with nothing hidden, unknown-family topics show too (sanity)
    assert envelope_filter("", set())(launcher)
