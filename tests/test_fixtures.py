import json

import pytest


def test_build_log_roundtrips(build_log):
    ch = build_log([({"handle": "local://h/1", "t": 10.0}, "lifecycle.started", None)])
    assert ch.latest("lifecycle.started").body["t"] == 10.0


def test_torn_channel_raises_jsondecodeerror(torn_sqlite_channel):
    ch = torn_sqlite_channel(
        [({"step": 0, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)],
        torn_seq=1,
    )
    with pytest.raises(json.JSONDecodeError):
        ch.latest("lifecycle.heartbeat")
