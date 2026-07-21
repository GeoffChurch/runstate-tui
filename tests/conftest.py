import sqlite3
from itertools import count

import pytest
from runstate import create_channel

from runstate_tui.env import Env

_ids = count()


@pytest.fixture
def build_log():
    opened = []  # every channel handle this fixture opens, closed in teardown below

    def _build(records):
        run_id = f"run-{next(_ids)}"
        writer = create_channel(run_id, backend="memory")
        opened.append(writer)
        for record in records:
            # 3-tuple (body, topic, name) OR 4-tuple (body, topic, name, request_id);
            # *rest keeps every existing 3-tuple caller working unchanged.
            body, topic, name, *rest = record
            writer.send(body, topic=topic, name=name, request_id=rest[0] if rest else None)
        reader = create_channel(run_id, backend="memory")  # a fresh reader on the same log
        opened.append(reader)
        return reader

    yield _build
    for channel in opened:
        channel.close()


@pytest.fixture
def counting_env():
    """A factory: `counting_env(base=100.0, threshold=60.0) -> (Env, calls)`.
    `Env.clock` returns `base + n` on its n-th call; `calls["n"]` exposes the
    running call count — for pinning "the clock is captured once per frame,
    not re-read mid-fold" tests."""

    def _make(base: float = 100.0, threshold: float = 60.0):
        calls = {"n": 0}

        def clock() -> float:
            calls["n"] += 1
            return base + calls["n"]

        return Env(clock=clock, stuck_threshold=threshold), calls

    return _make


@pytest.fixture
def answer_on_sleep():
    """A factory: `answer_on_sleep(channel, on_call) -> sleep_fn`. `sleep_fn` is
    an `await_consumed`-shaped `sleep(interval)` callback that, on its k-th
    call, runs `on_call[k](channel)` if present (else no-op) — seeds answers on
    the poll seam, including a multi-call watermark-climb sequence."""

    def _make(channel, on_call):
        state = {"k": 0}

        def sleep(_interval: float) -> None:
            state["k"] += 1
            action = on_call.get(state["k"])
            if action is not None:
                action(channel)

        return sleep

    return _make


@pytest.fixture
def foreign_db(tmp_path):
    """A VALID sqlite file at `<tmp_path>/ghost.db` with an alien (non-runstate)
    schema, written directly (never through a runstate locator) — the "real sqlite
    file, wrong shape" case distinct from a corrupt/non-sqlite file. Under
    `attach_channel` this now reads as `missing` (no `log` table -> RunNotFound)
    and is left byte-identical (the old open-mutates-foreign-db bug is fixed)."""
    run_id = "ghost"
    conn = sqlite3.connect(str(tmp_path / f"{run_id}.db"))
    try:
        conn.execute("CREATE TABLE unrelated(id, note)")
        conn.execute("INSERT INTO unrelated VALUES (1, 'not a runstate log')")
        conn.commit()
    finally:
        conn.close()
    return (run_id, str(tmp_path), "sqlite")


@pytest.fixture
def held_writer_sqlite_run(tmp_path):
    """Yields `(ref, send)` with the sqlite writer channel kept OPEN across the
    test — `send(body, topic, **kw)` appends live to the run while it's being
    observed. Closed in teardown."""
    run_id = "held"
    writer = create_channel(run_id, root=tmp_path, backend="sqlite")
    ref = (run_id, str(tmp_path), "sqlite")

    def send(body, topic, **kw):
        return writer.send(body, topic=topic, **kw)

    yield ref, send
    writer.close()


@pytest.fixture
def rich_run():
    """A live run with an episode, a heartbeat, a value, an undischarged stop, and
    live demand. Built directly (not via build_log) because control.subscribe /
    control.stop correlate on request_id, which build_log's (body, topic, name)
    triples have no slot for."""
    opened = []  # every channel handle this fixture opens, closed in teardown below

    def _build():
        run_id = f"rich-{next(_ids)}"
        writer = create_channel(run_id, backend="memory")
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
        reader = create_channel(run_id, backend="memory")  # a fresh reader on the same log
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
        writer = create_channel(run_id, root=tmp_path, backend="sqlite")
        for body, topic, name in records:
            writer.send(body, topic=topic, name=name)
        writer.close()  # closed before the file is corrupted below; nothing to track
        # test tooling only (not runtime): plant an un-decodable body at torn_seq
        conn = sqlite3.connect(str(tmp_path / f"{run_id}.db"))
        conn.execute("UPDATE log SET body = ? WHERE seq = ?", ("{not json", torn_seq))
        conn.commit()
        conn.close()
        reader = create_channel(run_id, root=tmp_path, backend="sqlite")
        opened.append(reader)
        return reader

    yield _build
    for channel in opened:
        channel.close()
