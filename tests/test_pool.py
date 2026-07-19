import sqlite3

import pytest
from runstate import open_channel

from runstate_tui.env import Env
from runstate_tui.pool import ChannelPool, fold_frame
from runstate_tui.table import render_single
from runstate_tui.types import StatusKind
from tests.helpers import corrupt_seq


def _seed(tmp_path, run_id, t=100.0):
    ch = open_channel(run_id, root=tmp_path, backend="sqlite")
    ch.send({"handle": "h", "t": t}, topic="lifecycle.started")
    ch.close()
    return (run_id, str(tmp_path), "sqlite")


def test_fold_frame_row_equals_render_single(tmp_path):
    ref = _seed(tmp_path, "r")
    env = Env(clock=lambda: 150.0)
    pool = ChannelPool(cap=8)
    table = fold_frame(pool, [ref], env, 150.0)
    assert dict(table)[ref] == render_single(ref, env)  # the §11 singleton, through the pool
    pool.close_all()


def test_fold_frame_distinguishes_same_basename_across_roots(tmp_path):
    import os

    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    os.mkdir(a_dir)
    os.mkdir(b_dir)
    ra = _seed(a_dir, "run1", t=100.0)
    rb = _seed(b_dir, "run1", t=200.0)
    pool = ChannelPool(cap=8)
    table = fold_frame(pool, [ra, rb], Env(clock=lambda: 300.0), 300.0)
    assert len(table) == 2 and len(pool) == 2  # two distinct runs, two channels
    assert dict(table)[ra].elapsed == 200.0 and dict(table)[rb].elapsed == 100.0
    pool.close_all()


def test_fold_frame_one_corrupt_run_does_not_sink_the_others(tmp_path):
    good = _seed(tmp_path, "good")
    _seed(tmp_path, "bad")
    corrupt_seq(tmp_path, "bad", 1, literal="{not json")
    bad = ("bad", str(tmp_path), "sqlite")
    pool = ChannelPool(cap=8)
    table = dict(fold_frame(pool, [good, bad], Env(clock=lambda: 150.0), 150.0))
    assert table[good].status.kind is not StatusKind.CORRUPT
    assert table[bad].status.kind is StatusKind.CORRUPT  # contained
    assert bad not in [r for r in pool._open]  # bad handle evicted; re-detected next tick
    pool.close_all()


def test_pool_lru_evicts_beyond_cap(tmp_path):
    refs = [_seed(tmp_path, f"r{i}") for i in range(3)]
    env = Env(clock=lambda: 150.0)
    pool = ChannelPool(cap=2)
    fold_frame(pool, [refs[0]], env, 150.0)  # open r0 alone
    ch0 = pool._open[refs[0]]  # capture its handle
    fold_frame(pool, refs, env, 151.0)  # r0 is the LRU -> evicted + closed
    assert len(pool) <= 2  # LRU kept the pool bounded
    with pytest.raises(sqlite3.ProgrammingError):
        ch0.read(after=0)  # evicted handle was actually closed
    pool.close_all()


def test_reconcile_closes_runs_that_left_the_resolver(tmp_path):
    a = _seed(tmp_path, "a")
    b = _seed(tmp_path, "b")
    env = Env(clock=lambda: 150.0)
    pool = ChannelPool(cap=8)
    fold_frame(pool, [a, b], env, 150.0)
    assert len(pool) == 2
    ch_b = pool._open[b]  # capture b's handle
    fold_frame(pool, [a], env, 151.0)
    assert len(pool) == 1  # b dropped + closed
    with pytest.raises(sqlite3.ProgrammingError):
        ch_b.read(after=0)  # b's handle was actually closed
    pool.close_all()
