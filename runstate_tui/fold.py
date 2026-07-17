from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from runstate.channel import Channel
from runstate.observables import MalformedRecordError

from .types import Issue, IssueKind, Severity

_DECODE_ERRORS = (json.JSONDecodeError, sqlite3.DatabaseError, MalformedRecordError)


def locate_torn_seq(channel: Channel) -> int | None:
    """Find the seq of the first record whose decode raises (append-only contiguity):
    walk read(after=k, limit=1); a raising probe localizes the tear at k+1."""
    k = 0
    last = channel.last_seq()
    while k < last:
        try:
            got = channel.read(after=k, limit=1)
        except _DECODE_ERRORS:
            return k + 1
        if not got:
            return None
        k = got[0].seq
    return None


def guarded(fn: Callable[[Channel], object], channel: Channel) -> tuple[object | None, Issue | None]:
    try:
        return fn(channel), None
    except _DECODE_ERRORS as exc:
        seq = getattr(exc, "seq", None)
        if seq is None:
            seq = locate_torn_seq(channel)
        message = f"log torn at seq {seq}" if seq is not None else "log torn"
        return None, Issue(kind=IssueKind.TORN, severity=Severity.MEDIUM, message=message, seq=seq)
