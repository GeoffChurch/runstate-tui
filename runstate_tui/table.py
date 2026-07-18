from __future__ import annotations

import sqlite3
from pathlib import Path

from runstate import open_channel

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
        issues=(),
    )


def open_and_fold(ref: RunRef, env: Env) -> Row:
    run_id, root, backend = ref
    # stat-before-open (sqlite): a missing pointer must NOT open_channel (that would
    # fabricate a phantom <run_id>.db into a content-addressed store) — spec §4/§11.
    if backend == "sqlite" and not (Path(root) / f"{run_id}.db").exists():
        return _bare(Status.missing())
    try:
        channel = open_channel(run_id, root=root, backend=backend)
    except _OPEN_ERRORS:
        return _bare(Status.unreadable())  # corrupt/foreign/unopenable db
    try:
        return status_fold(channel, env)
    finally:
        channel.close()


def render_table(resolver: Resolver, env: Env) -> list[Row]:
    return [open_and_fold(ref, env) for ref in resolver(env.clock())]


def render_single(ref: RunRef, env: Env) -> Row:
    # the single-run view IS the table at |I|=1 — one code path, no bespoke screen (spec §11)
    return render_table(const_resolver(ref), env)[0]
