from __future__ import annotations

import sqlite3
from pathlib import Path

from runstate import open_channel
from runstate.channel import Envelope

from .env import Env
from .fold import status_fold
from .resolver import Resolver, RunRef, const_resolver
from .types import Row, Status

_OPEN_ERRORS = (sqlite3.DatabaseError, sqlite3.OperationalError, PermissionError, OSError)


def _bare(status: Status) -> Row:
    """A row with a verdict but no derivable observables (missing / unreadable)."""
    return Row(
        status=status,
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        undischarged_stops=(),
        live_demand=(),
        issues=(),
    )


def open_and_fold(ref: RunRef, env: Env) -> Row:
    run_id, root, backend = ref
    # stat-before-open (sqlite): a missing pointer must NOT open_channel (that would
    # fabricate a phantom <run_id>.db into a content-addressed store) — spec §4/§11.
    # stat() (not Path.exists(), which swallows EACCES into a wrong `missing`) so an
    # unreadable parent dir (PermissionError) is distinguished from a missing pointer.
    if backend == "sqlite":
        try:
            (Path(root) / f"{run_id}.db").stat()
        except FileNotFoundError:
            return _bare(Status.missing())
        except OSError:
            return _bare(Status.unreadable())
    try:
        channel = open_channel(run_id, root=root, backend=backend)
    except _OPEN_ERRORS:
        return _bare(Status.unreadable())  # corrupt/foreign/unopenable db
    try:
        return status_fold(channel, env)
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return _bare(Status.unreadable())  # substrate fault mid-read (a corrupt db
        # fails every read; byte-torn's json.JSONDecodeError is NOT caught -> it crashes)
    finally:
        channel.close()


def read_log_delta(ref: RunRef, after: int, *, limit: int | None = None) -> list[Envelope]:
    """The raw log tail as a query: envelopes with seq > `after`. Filter-shaped for
    later (topics/name/request_ids). Missing/unreadable/substrate-fault -> [] (the
    header carries the run's status); a byte-torn record -> json.JSONDecodeError -> crash."""
    run_id, root, backend = ref
    if backend == "sqlite":
        try:
            (Path(root) / f"{run_id}.db").stat()
        except OSError:
            return []  # missing pointer / unreadable dir — never fabricate a phantom db
    try:
        channel = open_channel(run_id, root=root, backend=backend)
    except _OPEN_ERRORS:
        return []
    try:
        return channel.read(after=after, limit=limit)
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return []  # substrate fault mid-read (byte-torn's JSONDecodeError is NOT caught -> crash)
    finally:
        channel.close()


def render_table(resolver: Resolver, env: Env) -> list[Row]:
    # `now` is re-sampled per row (once for the resolver, then again in each row's
    # status_fold) — Stage 4 should capture `now` once per frame for frame-consistent
    # freshness across the whole table.
    return [open_and_fold(ref, env) for ref in resolver(env.clock())]


def render_single(ref: RunRef, env: Env) -> Row:
    # the single-run view IS the table at |I|=1 — one code path, no bespoke screen (spec §11)
    return render_table(const_resolver(ref), env)[0]
