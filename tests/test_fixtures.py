import json

import pytest
import runstate as rs

from tests.records import Script


def test_build_log_roundtrips(build_log):
    s = Script()
    s.started(10.0)
    ch = build_log(s.records)
    assert ch.latest("lifecycle.started").body["t"] == 10.0


def test_torn_channel_raises_jsondecodeerror(torn_sqlite_channel):
    s = Script()
    s.started(1.0)
    s.heartbeat(1.0)
    ch = torn_sqlite_channel(s.records, torn_seq=2)
    with pytest.raises(json.JSONDecodeError):
        ch.latest("lifecycle.heartbeat")


def test_a_script_is_a_well_formed_0_4_0_log_runstate_reads(build_log):
    # The hand-built records are runstate's typed bodies, so its strict reads accept
    # them: the head fold's structural checks (a node's parent is its claim's position,
    # a values seq names a value below it), the verdict, the answer folds.
    s = Script()
    s.launched("L1", 1.0)
    s.started(2.0, launch="L1")
    s.value("loss", 0.5, 3.0)
    s.commit(3.0)
    s.heartbeat(3.5, consumed=0)
    s.value("loss", 0.25, 4.0)
    s.commit(4.0)
    stop = s.stop("webui:s1")
    s.subscribe("dash", None, {"until": {"commits": 9}})
    s.withdraw("dash", 4.5)
    s.value("loss", 0.125, 5.0)
    s.stopped(5.0, completed=False, honored=("webui:s1",))
    s.terminated("L1", 6.0)
    ch = build_log(s.records)
    head = rs.LatestClaimHead()
    assert rs.prefix_len(ch, rs.Commits(), head=head) == 2
    assert [e.value for e in rs.series(ch, "loss", head=head)] == [0.5, 0.25, 0.125]
    result = rs.peek_terminal(ch)
    assert result.outcomes == {rs.Outcome.PREEMPTED} and result.tier is rs.Tier.EXIT
    assert rs.undischarged_stops(ch) == [] and rs.live_demand(ch) == []
    assert rs.await_consumed(ch, stop) is None  # the exit's `honored` names the stop
