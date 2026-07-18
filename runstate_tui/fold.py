from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable

from runstate.channel import Channel
from runstate.observables import MalformedRecordError, last_activity, peek_terminal, progress
from runstate.vocabulary.payloads import Topic

from .env import Env, Liveness, resolve_liveness
from .types import Issue, IssueKind, Row, Severity, Status

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


def reconcile_status(channel: Channel, env: Env, now: float) -> tuple[Status, float | None, list[Issue]]:
    issues: list[Issue] = []
    result, term_issue = guarded(peek_terminal, channel)
    if term_issue is not None:
        issues.append(term_issue)

    la, la_issue = guarded(last_activity, channel)   # the ONE last_activity read
    if la_issue is not None:
        issues.append(la_issue)
    freshness = None if la is None else max(0.0, now - la)
    if isinstance(la, (int, float)) and not isinstance(la, bool) and la > now:
        issues.append(Issue(kind=IssueKind.SKEW_SUSPECTED, severity=Severity.MEDIUM,
                            message="last activity is in the future (clock skew)"))

    if result is not None:
        return Status.terminal(result.outcome), freshness, issues  # terminal wins
    if la is None:
        return Status.pending(), freshness, issues  # no dated activity at all
    verdict = resolve_liveness(channel, env, now, la)
    status = Status.live() if verdict is Liveness.LIVE else Status.stale()
    return status, freshness, issues


def read_value(channel: Channel, objective: str | None) -> tuple[str, object, int | None] | None:
    if objective is None:
        return None
    e = channel.latest(Topic.VALUE, name=objective)
    if e is None:
        return None
    return (objective, e.body.get("value"), e.body.get("step"))


def read_elapsed(channel: Channel, now: float) -> tuple[float | None, Issue | None]:
    started, torn_issue = guarded(lambda ch: ch.read(topics=[Topic.LIFECYCLE_STARTED], limit=1), channel)
    if torn_issue is not None:
        return None, torn_issue  # a torn `started` never masquerades as "no started at all"
    if not started:
        return None, None
    t = started[0].body.get("t")
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t):
        return None, None
    if t > now:
        return 0.0, Issue(
            kind=IssueKind.SKEW_SUSPECTED, severity=Severity.MEDIUM,
            message="run epoch is in the future (clock skew)", detail=f"started.t={t} > now={now}",
        )
    return now - float(t), None


def status_fold(channel: Channel, env: Env) -> Row:
    now = env.clock()  # captured once per frame, threaded below
    issues: list[Issue] = []

    status, freshness, status_issues = reconcile_status(channel, env, now)
    issues.extend(status_issues)

    frontier, frontier_issue = guarded(progress, channel)
    if frontier_issue is not None:
        issues.append(frontier_issue)

    value, value_issue = guarded(lambda ch: read_value(ch, env.objective), channel)
    if value_issue is not None:
        issues.append(value_issue)

    elapsed, elapsed_issue = read_elapsed(channel, now)
    if elapsed_issue is not None:
        issues.append(elapsed_issue)

    return Row(status=status, frontier=frontier, freshness=freshness, value=value,
               elapsed=elapsed, episode=None, issues=tuple(issues))
