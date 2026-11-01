import os
import sqlite3
from pathlib import Path

import pytest
from runstate import create_channel

from runstate_tui.env import Env
from runstate_tui.fold import RunFolds, status_fold
from runstate_tui.resolver import const_resolver
from runstate_tui.table import open_and_fold, render_single, render_table
from runstate_tui.types import IssueKind, Severity, StatusKind
from tests.helpers import corrupt_seq, log_path, move_log
from tests.records import Script, write


def _env(now=150.0, **kw):
    return Env(clock=lambda: now, stuck_threshold=60.0, **kw)


def _folds():
    return RunFolds.start(objective=None)


def _sqlite_run(tmp_path, run_id, script):
    ch = create_channel(run_id, root=tmp_path, backend="sqlite")
    write(ch, script.records)
    ch.close()


def _claimed(t=100.0, commits=0):
    s = Script()
    s.started(t)
    for i in range(commits):
        s.commit(t + 40.0 + i)
    return s


def test_open_and_fold_healthy_run(tmp_path):
    _sqlite_run(tmp_path, "r", _claimed(commits=7))
    row = open_and_fold(("r", str(tmp_path), "sqlite"), _env(), _folds())
    assert row.status.kind is StatusKind.LIVE
    assert row.frontier == 7


def test_missing_pointer_is_missing_and_creates_no_phantom(tmp_path):
    ref = ("ghost", str(tmp_path), "sqlite")
    row = open_and_fold(ref, _env(), _folds())
    assert row.status.kind is StatusKind.MISSING  # attach_channel -> RunNotFound
    assert row.frontier is None and row.issues == ()
    assert not log_path(tmp_path, "ghost").exists()  # attach_channel never creates a file
    assert not (Path(tmp_path) / "v0.4.0").exists()  # ... nor the format's directory


def test_corrupt_db_is_unreadable(tmp_path):
    path = log_path(tmp_path, "corrupt")
    path.parent.mkdir()
    path.write_bytes(b"this is not a sqlite database")
    row = open_and_fold(("corrupt", str(tmp_path), "sqlite"), _env(), _folds())
    assert row.status.kind is StatusKind.UNREADABLE
    assert row.frontier is None and row.issues == ()


def _older(tmp_path, run_id, version):
    """A run whose log sits at format `version`'s address (0.4.0's sqlite table is
    0.3.0's, so the moved file is a log of that format)."""
    _sqlite_run(tmp_path, run_id, _claimed(commits=2))
    return move_log(tmp_path, run_id, to=log_path(tmp_path, run_id, version=version))


def test_an_older_format_log_is_a_loud_format_row_naming_the_migration(tmp_path):
    _older(tmp_path, "old", "0.3.0")
    row = open_and_fold(("old", str(tmp_path), "sqlite"), _env(), _folds())
    assert row.status.kind is StatusKind.LOG_FORMAT and row.severity is Severity.HIGH
    assert row.status.label == "format 0.3.0"
    assert row.status.detail == "migrate it (runstate migrate)"
    (issue,) = row.issues
    assert issue.kind is IssueKind.LOG_FORMAT and issue.severity is Severity.HIGH
    # runstate's own message: the log, the format, and the exact command for this root
    assert f"runstate migrate {tmp_path}" in issue.message and "format 0.3.0" in issue.message
    assert row.frontier is None and row.freshness is None


def test_a_newer_format_root_is_a_format_row_asking_for_an_upgrade(tmp_path):
    _older(tmp_path, "new", "9.0.0")  # a root holding a format newer than this runstate's
    row = open_and_fold(("new", str(tmp_path), "sqlite"), _env(), _folds())
    assert row.status.label == "format 9.0.0"
    assert row.status.detail == "upgrade runstate to read it"


def test_an_unversioned_legacy_log_is_a_format_row_naming_onboarding(tmp_path):
    _sqlite_run(tmp_path, "legacy", _claimed())
    move_log(tmp_path, "legacy", to=Path(tmp_path) / "legacy.db")
    row = open_and_fold(("legacy", str(tmp_path), "sqlite"), _env(), _folds())
    assert row.status.kind is StatusKind.LOG_FORMAT and row.status.label == "unversioned"
    assert row.status.detail == "onboard it, then runstate migrate"
    (issue,) = row.issues
    assert "predates versioned log formats" in issue.message
    assert "\n" not in issue.message  # one line: runstate's onboarding script rides `detail`
    assert issue.detail is not None and "tombstone" in issue.detail


def test_a_migrated_run_opens_at_the_current_format_past_its_sealed_copies(tmp_path):
    # after `runstate migrate`, a run has its current log plus a sealed older copy and a
    # legacy tombstone; attach opens the current one, so the run reads normally
    _sqlite_run(tmp_path, "m", _claimed(commits=3))
    (Path(tmp_path) / "v0.3.0").mkdir()
    (Path(tmp_path) / "v0.3.0" / "m.db").write_bytes(b"")
    (Path(tmp_path) / "m.db").write_bytes(b"")  # the tombstone
    row = open_and_fold(("m", str(tmp_path), "sqlite"), _env(), _folds())
    assert row.status.kind is StatusKind.LIVE and row.frontier == 3


def test_render_table_maps_over_the_resolver_in_order(tmp_path):
    _sqlite_run(tmp_path, "a", _claimed())

    # "b" is a missing pointer
    def resolver(now):
        return [(("a", str(tmp_path), "sqlite"), {}), (("b", str(tmp_path), "sqlite"), {})]

    rows = render_table(resolver, _env())
    assert len(rows) == 2
    assert rows[0].status.kind is StatusKind.LIVE
    assert rows[1].status.kind is StatusKind.MISSING


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_unreadable_parent_dir_is_unreadable(tmp_path):
    # Log format 0.4.0's opening checks list the root first (is a newer format present?),
    # so an unsearchable root raises PermissionError at the open -> `unreadable`, not the
    # `missing` an unsearchable dir read as when the open was a bare `mode=rw` connect
    # (RunNotFound). The faithful answer: the cockpit cannot read the run, which is not
    # the same as there being no run.
    d = tmp_path / "locked"
    log_path(d, "r").parent.mkdir(parents=True)
    log_path(d, "r").write_bytes(b"")  # a file exists, but we'll make the dir unsearchable
    d.chmod(0o000)
    try:
        row = open_and_fold(("r", str(d), "sqlite"), _env(), _folds())
        assert row.status.kind is StatusKind.UNREADABLE
    finally:
        d.chmod(0o755)  # restore so tmp_path cleanup works


def test_open_and_fold_memory_backend_attaches_a_run_with_records():
    # the memory backend has no file to stat; open_and_fold attaches by registry lookup.
    # A run WITH records attaches (not RunNotFound) and folds to a real status. The
    # registry key is (abspath(root), run_id), so the birth root must match the ref's
    # root ("" here) for attach to find the same log.
    run_id = "r"
    ch = create_channel(run_id, root="", backend="memory")
    write(ch, _claimed().records)
    row = open_and_fold((run_id, "", "memory"), _env(), _folds())
    assert row.status.kind is StatusKind.LIVE  # attached + folded to a real status


def test_singleton_test_single_run_is_the_table_at_one(tmp_path):
    _sqlite_run(tmp_path, "r", _claimed(commits=3))
    ref = ("r", str(tmp_path), "sqlite")
    env = _env()  # fixed clock -> both fold passes see the same `now`
    assert render_single(ref, env) == render_table(const_resolver(ref), env)[0]


def test_open_and_fold_maps_a_substrate_read_fault_to_unreadable(tmp_path, monkeypatch):
    # a real, openable run; status_fold raises a substrate error mid-read (injected)
    _sqlite_run(tmp_path, "sub", _claimed(1.0))

    def boom(channel, env, folds):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr("runstate_tui.table.status_fold", boom)
    row = open_and_fold(("sub", str(tmp_path), "sqlite"), Env(clock=lambda: 1.0), _folds())
    assert row.status.kind is StatusKind.UNREADABLE


def test_open_and_fold_maps_byte_torn_to_corrupt(torn_sqlite_channel, tmp_path):
    # byte-torn is NOT unreadable and NOT a crash — it's a distinct, loud `corrupt`
    # status carrying the torn seq.
    torn_sqlite_channel(_claimed(1.0).records, torn_seq=1)
    # torn_sqlite_channel writes run_id "torn" under tmp_path
    row = open_and_fold(("torn", str(tmp_path), "sqlite"), Env(clock=lambda: 1.0), _folds())
    assert row.status.kind is StatusKind.CORRUPT
    assert row.status.severity is Severity.HIGH
    assert any(i.kind is IssueKind.CORRUPT and i.seq == 1 for i in row.issues)


def test_read_log_delta_is_incremental(tmp_path):
    from runstate_tui.table import read_log_delta

    _sqlite_run(tmp_path, "d", _claimed(1.0, commits=1))  # seq 1 started, seq 2 commit
    ref = ("d", str(tmp_path), "sqlite")

    all_ = read_log_delta(ref, after=0)
    assert [e.seq for e in all_] == [1, 2]
    assert read_log_delta(ref, after=1)[0].seq == 2  # only the delta
    assert read_log_delta(ref, after=2) == []  # nothing new


def test_read_log_delta_missing_run_is_empty(tmp_path):
    from runstate_tui.table import read_log_delta

    assert read_log_delta(("ghost", str(tmp_path), "sqlite"), after=0) == []
    assert not log_path(tmp_path, "ghost").exists()  # no phantom fabricated


def test_read_log_delta_of_an_older_format_log_is_empty(tmp_path):
    from runstate_tui.table import read_log_delta

    _older(tmp_path, "old", "0.3.0")
    assert read_log_delta(("old", str(tmp_path), "sqlite"), after=0) == []


def test_read_log_delta_corrupt_db_is_empty(tmp_path):
    from runstate_tui.table import read_log_delta

    log_path(tmp_path, "corrupt").parent.mkdir()
    log_path(tmp_path, "corrupt").write_bytes(b"this is not a sqlite database")
    assert read_log_delta(("corrupt", str(tmp_path), "sqlite"), after=0) == []


def test_read_log_delta_maps_a_substrate_read_fault_to_empty(tmp_path, monkeypatch):
    from runstate.channel.sqlite import SqliteChannel

    from runstate_tui.table import read_log_delta

    _sqlite_run(tmp_path, "sub", _claimed(1.0))

    def boom(self, *a, **kw):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(SqliteChannel, "read", boom)
    assert read_log_delta(("sub", str(tmp_path), "sqlite"), after=0) == []


def test_read_log_delta_byte_torn_is_empty(torn_sqlite_channel, tmp_path):
    # byte-torn is no longer a crash; the drill-down header surfaces `corrupt` via
    # render_single, so the raw tail degrades to empty (like the other read faults).
    torn_sqlite_channel(_claimed(1.0).records, torn_seq=1)
    from runstate_tui.table import read_log_delta

    assert read_log_delta(("torn", str(tmp_path), "sqlite"), after=0) == []


def test_fold_open_channel_matches_status_fold_on_a_healthy_run(build_log):
    from runstate_tui.table import fold_open_channel

    ch = build_log(_claimed(commits=2).records)
    env = _env(150.0)  # the module's Env helper
    assert fold_open_channel(ch, env, _folds()) == status_fold(ch, env, _folds())


def test_fold_open_channel_maps_byte_torn_to_corrupt(tmp_path):
    from runstate_tui.table import fold_open_channel

    _sqlite_run(tmp_path, "r", _claimed())
    corrupt_seq(tmp_path, "r", 1, literal="{not json")
    ch = create_channel("r", root=tmp_path, backend="sqlite")
    assert fold_open_channel(ch, _env(150.0), _folds()).status.kind is StatusKind.CORRUPT
    ch.close()


def test_read_log_delta_applies_filter(tmp_path):

    from runstate_tui.table import read_log_delta

    s = _claimed(1.0)
    s.stop("webui:x")
    _sqlite_run(tmp_path, "r", s)
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


def test_envelope_filter_seq_bound():
    # `seq>N` bounds the one position every record carries (0.4.0 records carry no step)
    from types import SimpleNamespace as NS

    from runstate_tui.table import envelope_filter

    early = NS(seq=3, topic="lifecycle.commit", request_id=None, body={})
    late = NS(seq=9, topic="value", request_id=None, body={"value": 1, "t": 1.0})
    f = envelope_filter("seq>5", set())
    assert f(late) and not f(early)
    f_hidden = envelope_filter("seq>5", {"value"})  # a hidden family stays hidden
    assert not f_hidden(late)


def test_unknown_family_topics_always_shown():
    # Finding #1: envelope_filter's family param is subtractive (HIDE the toggled-off
    # KNOWN families), never restrict-to -- a topic outside the known families (e.g.
    # supervisor.* written onto the same channel by runstate's launchers, the death
    # report `supervisor.died` among them) is never in `hidden_families`, so it is never
    # silently dropped, no matter which known families are toggled off.
    from types import SimpleNamespace as NS

    from runstate_tui.table import envelope_filter

    died = NS(topic="supervisor.died", request_id=None, body={})
    # hide every known family the app knows about -- supervisor.* still shows
    f = envelope_filter("", {"lifecycle", "value", "control"})
    assert f(died)
    # even with nothing hidden, unknown-family topics show too (sanity)
    assert envelope_filter("", set())(died)
