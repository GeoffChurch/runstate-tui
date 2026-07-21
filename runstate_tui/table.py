from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable

from runstate import RunNotFound, attach_channel
from runstate.channel import Channel, Envelope

from .env import Env
from .fold import locate_torn_seq, status_fold
from .resolver import Resolver, RunRef, const_resolver
from .types import Issue, IssueKind, Row, Severity, Status

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


def _corrupt(seq: int | None) -> Row:
    """A row for a byte-torn log: a distinct, loud `corrupt` status (HIGH) carrying
    the torn seq — NOT a crash, and NOT reused `unreadable` (that would be lossy)."""
    msg = f"log corrupt at seq {seq}" if seq is not None else "log corrupt"
    issue = Issue(kind=IssueKind.CORRUPT, severity=Severity.HIGH, message=msg, seq=seq)
    return Row(
        status=Status.corrupt(),
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        undischarged_stops=(),
        live_demand=(),
        issues=(issue,),
    )


def _fold_error(exc: Exception) -> Row:
    """A row for an UNEXPECTED exception escaping the fold — i.e. a genuine internal bug on
    ONE run. Every EXPECTED fold failure is already its own loud row: missing/unreadable
    (`_bare`), byte-torn (`_corrupt`), malformed record (a per-factor Issue). Containing the
    escaped exception to a distinct HIGH `error` row (NOT reused `unreadable`/`corrupt`,
    which would be lossy) keeps the table alive while the worker stays fail-fast for
    catastrophic non-fold bugs. The exception rides both the status detail and the Issue
    message so it surfaces in the table's status column AND the drill-down's issue list."""
    detail = f"{type(exc).__name__}: {exc}"
    # IssueKind.INTERNAL_ERROR: our CODE threw an unexpected exception -- distinct from
    # MALFORMED (a decodable-but-wrong-shape DATA record) and CORRUPT (a byte-torn log at
    # a known seq). The kind is an internal tag (never displayed — only `message` renders),
    # but it is consumed programmatically, so it must stay faithful to its own category.
    issue = Issue(
        kind=IssueKind.INTERNAL_ERROR,
        severity=Severity.HIGH,
        message=f"unexpected fold error: {detail}",
    )
    return Row(
        status=Status.error(detail=detail),
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        undischarged_stops=(),
        live_demand=(),
        issues=(issue,),
    )


def fold_open_channel(channel: Channel, env: Env) -> Row:
    """Fold an ALREADY-OPEN channel with the integrity guards, WITHOUT closing it.
    A byte-torn (json.JSONDecodeError) -> loud `corrupt` carrying the located seq; a
    substrate fault mid-read -> `unreadable`. open_and_fold closes in its own finally;
    the pool keeps the handle and re-uses it next tick (folding fresh)."""
    try:
        return status_fold(channel, env)
    except json.JSONDecodeError:
        return _corrupt(locate_torn_seq(channel))
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return _bare(Status.unreadable())


def open_and_fold(ref: RunRef, env: Env) -> Row:
    run_id, root, backend = ref
    # attach_channel (never create_channel): observing a run must NOT fabricate a
    # phantom <run_id>.db, and must NOT schema-mutate a foreign valid sqlite db under a
    # stale pointer — both, plus a genuinely-empty run, are `RunNotFound` -> `missing`
    # (a run exists iff it has records; spec channel-locators.md). Only corrupt/non-sqlite
    # bytes still raise `sqlite3.DatabaseError` -> `unreadable`. This is strictly SAFER
    # than the old stat-before-open, which could not catch the foreign-db mutation.
    try:
        channel = attach_channel(run_id, root=root, backend=backend)
    except RunNotFound:
        return _bare(Status.missing())
    except _OPEN_ERRORS:
        return _bare(Status.unreadable())  # corrupt/non-sqlite/unopenable db
    try:
        return fold_open_channel(channel, env)
    finally:
        channel.close()


def read_log_delta(
    ref: RunRef,
    after: int,
    *,
    filter: Callable[[Envelope], bool] | None = None,
    limit: int | None = None,
) -> list[Envelope]:
    """The raw log tail as a query: envelopes with seq > `after`, optionally narrowed
    by `filter`. Missing/unreadable/substrate-fault/byte-torn -> [] (the header carries
    the run's status, including the loud `corrupt` verdict, via render_single/
    open_and_fold)."""
    run_id, root, backend = ref
    # attach_channel (never create_channel): never fabricate a phantom db nor mutate a
    # foreign one. A run with no records (missing/empty/foreign) raises `RunNotFound`;
    # corrupt/non-sqlite raises a `_OPEN_ERRORS` member — both degrade the tail to [].
    try:
        channel = attach_channel(run_id, root=root, backend=backend)
    except (RunNotFound, *_OPEN_ERRORS):
        return []
    try:
        got = channel.read(after=after, limit=limit)
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError, json.JSONDecodeError):
        # substrate fault or byte-torn mid-read -> []. TODO(follow-up): a byte-torn
        # tail could instead raw-passthrough everything up to the tear rather than
        # dropping the whole delta — deferred; the header's `corrupt` status is the
        # loud signal for now.
        return []
    finally:
        channel.close()
    # UPSTREAM(runstate#15): v1 applies the predicate here in Python. When runstate's
    # read() gains filter= (+ before=/max_seq= for backward reads), push `filter` into
    # channel.read so the SUBSTRATE filters and history is retroactively filterable.
    # Discover all revisit sites with: grep -rn "UPSTREAM(runstate#15)"
    return [e for e in got if filter is None or filter(e)]


def envelope_filter(text: str, hidden_families: set[str]) -> Callable[[Envelope], bool]:
    """Build a v1 log-filter predicate from the filter-bar text + the toggled-off
    (hidden) topic families. text: a plain substring matched against topic +
    request_id (+ 'step>N' numeric bound over the body's 'step'). hidden_families:
    topic families to HIDE (the toggled-off KNOWN families) — a topic whose family
    is NOT in this set always shows, so launcher.* / any unclassified topic is never
    silently dropped (the log streams every record). The daemon/upstream #15 will
    serve this as read(filter=…)."""
    text = text.strip()
    stepbound: int | None = None
    if text.startswith("step>") and text[5:].strip().isdigit():
        stepbound = int(text[5:].strip())

    def pred(e: Envelope) -> bool:
        if e.topic.split(".")[0] in hidden_families:  # subtractive: hide toggled-off known families
            return False
        if stepbound is not None:
            step = e.body.get("step") if isinstance(e.body, dict) else None
            return isinstance(step, int) and step > stepbound
        if text and text not in e.topic and text not in (e.request_id or ""):
            return False
        return True

    return pred


def render_table(resolver: Resolver, env: Env) -> list[Row]:
    # `now` is re-sampled per row (once for the resolver, then again in each row's
    # status_fold) — Stage 4 should capture `now` once per frame for frame-consistent
    # freshness across the whole table.
    return [open_and_fold(ref, env) for ref in resolver(env.clock())]


def render_single(ref: RunRef, env: Env) -> Row:
    # the single-run view IS the table at |I|=1 — one code path, no bespoke screen (spec §11)
    return render_table(const_resolver(ref), env)[0]
