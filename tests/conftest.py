import sqlite3
from itertools import count

import pytest
from runstate import open_channel

_ids = count()


@pytest.fixture
def build_log():
    opened = []  # every channel handle this fixture opens, closed in teardown below

    def _build(records):
        run_id = f"run-{next(_ids)}"
        writer = open_channel(run_id, backend="memory")
        opened.append(writer)
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        reader = open_channel(run_id, backend="memory")  # a fresh reader on the same log
        opened.append(reader)
        return reader

    yield _build
    for channel in opened:
        channel.close()


@pytest.fixture
def rich_run():
    """A live run with an episode, a heartbeat, a value, an undischarged stop, and
    live demand. Built directly (not via build_log) because control.subscribe /
    control.stop correlate on request_id, which build_log's (body, topic, name)
    triples have no slot for."""
    opened = []  # every channel handle this fixture opens, closed in teardown below

    def _build():
        run_id = f"rich-{next(_ids)}"
        writer = open_channel(run_id, backend="memory")
        opened.append(writer)
        writer.send({"handle": "local://h/1", "t": 100.0}, topic="lifecycle.started")
        writer.send({"step": 7, "consumed_seq": 0, "t": 140.0}, topic="lifecycle.heartbeat")
        writer.send({"value": 0.03, "step": 7, "t": 140.0}, topic="value", name="loss")
        writer.send(
            {"schedule": {}, "names": ["loss"]},
            topic="control.subscribe",
            request_id="webui:sub1",
        )
        writer.send({}, topic="control.stop", request_id="webui:stop1")
        reader = open_channel(run_id, backend="memory")  # a fresh reader on the same log
        opened.append(reader)
        return reader

    yield _build
    for channel in opened:
        channel.close()


@pytest.fixture
def torn_sqlite_channel(tmp_path):
    opened = []  # every channel handle this fixture opens, closed in teardown below

    def _build(records, torn_seq):
        run_id = "torn"
        writer = open_channel(run_id, root=tmp_path, backend="sqlite")
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        writer.close()  # closed before the file is corrupted below; nothing to track
        # test tooling only (not runtime): plant an un-decodable body at torn_seq
        conn = sqlite3.connect(str(tmp_path / f"{run_id}.db"))
        conn.execute("UPDATE log SET body = ? WHERE seq = ?", ("{not json", torn_seq))
        conn.commit()
        conn.close()
        reader = open_channel(run_id, root=tmp_path, backend="sqlite")
        opened.append(reader)
        return reader

    yield _build
    for channel in opened:
        channel.close()
