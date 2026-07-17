import sqlite3
from itertools import count

import pytest
from runstate import open_channel

_ids = count()


@pytest.fixture
def build_log():
    def _build(records):
        run_id = f"run-{next(_ids)}"
        writer = open_channel(run_id, backend="memory")
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        return open_channel(run_id, backend="memory")  # a fresh reader on the same log
    return _build


@pytest.fixture
def torn_sqlite_channel(tmp_path):
    def _build(records, torn_seq):
        run_id = "torn"
        writer = open_channel(run_id, root=tmp_path, backend="sqlite")
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        writer.close()
        # test tooling only (not runtime): plant an un-decodable body at torn_seq
        conn = sqlite3.connect(str(tmp_path / f"{run_id}.db"))
        conn.execute("UPDATE log SET body = ? WHERE seq = ?", ("{not json", torn_seq))
        conn.commit()
        conn.close()
        return open_channel(run_id, root=tmp_path, backend="sqlite")
    return _build
