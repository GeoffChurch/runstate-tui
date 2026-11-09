from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from typing import NamedTuple

from runstate import (
    Envelope,
    LogFormatError,
    LogFormatMismatch,
    LogFormatMissing,
    Reader,
    RunNotFound,
)

from .env import Env
from .fold import RunFolds, locate_torn_seq, status_fold
from .resolver import Resolver, RunRef, place_of
from .types import Issue, IssueKind, Row, Severity, Status, StatusKind

_OPEN_ERRORS = (sqlite3.DatabaseError, sqlite3.OperationalError, PermissionError, OSError)

# integrity verdicts that mean an open handle -- and the folds held beside it -- is no
# good: the holder drops both, so the next tick cold-opens and folds from scratch
# (self-healing detection; a holder keeps only healthy handles and folds).
EVICT_KINDS = (StatusKind.CORRUPT, StatusKind.UNREADABLE)


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


def _version(v: str) -> tuple[int, ...]:
    """An X.Y.Z format version as a comparable tuple (runstate's `formats.parse` is not
    public; the grammar is log-formats.md §3's)."""
    return tuple(int(part) for part in v.split("."))


class FormatError(NamedTuple):
    """What a `LogFormatError` means to an operator: the format found (None for a log from
    before versioned addresses), the short action the status cell shows, and the message
    the issue carries -- runstate's own, which names the log and the exact command."""

    found: str | None
    action: str
    message: str


def describe_format_error(exc: LogFormatError) -> FormatError:
    """runstate refuses a log in another format at the open, by design, and names the
    action: `runstate migrate` for an older format, a runstate upgrade for a newer one,
    onboarding then migrating for a log at the unversioned legacy address. A
    `LogFormatMissing` message is runstate's multi-line onboarding script, so its issue
    carries a one-line summary instead (the script stays in `Issue.detail`)."""
    if isinstance(exc, LogFormatMismatch):
        newer = _version(exc.found) > _version(exc.expected)
        action = "upgrade runstate to read it" if newer else "migrate it (runstate migrate)"
        return FormatError(exc.found, action, str(exc))
    if isinstance(exc, LogFormatMissing):
        action = "onboard it, then runstate migrate"
        return FormatError(
            None,
            action,
            f"{exc.where} predates versioned log formats: onboard it as format 0.2.0 "
            "(runstate log-formats.md §7), then run `runstate migrate`",
        )
    # a LogFormatError subclass runstate adds later: render it by its own message
    return FormatError(None, str(exc), str(exc))


def _wrong_format(exc: LogFormatError) -> Row:
    """A row for a run whose log is in a format this runstate does not read. The cockpit
    shows it as a loud HIGH row (it cannot read the run at all), never as `missing` and
    never as a crash. runstate's full instructions (the root, the exact command, the
    onboarding loop) ride the issue's detail for the drill-down."""
    found, action, message = describe_format_error(exc)
    issue = Issue(
        kind=IssueKind.LOG_FORMAT, severity=Severity.HIGH, message=message, detail=str(exc)
    )
    return Row(
        status=Status.log_format(found, action),
        frontier=None,
        freshness=None,
        value=None,
        elapsed=None,
        episode=None,
        undischarged_stops=(),
        live_demand=(),
        issues=(issue,),
    )


def open_row_for(exc: Exception) -> Row:
    """The row for a run whose open was refused: no records -> `missing`, another
    format -> the loud format row, corrupt/non-sqlite/unopenable -> `unreadable`. Raises
    anything else, which is a bug the caller's fold-error containment reports."""
    if isinstance(exc, RunNotFound):
        return _bare(Status.missing())
    if isinstance(exc, LogFormatError):
        return _wrong_format(exc)
    if isinstance(exc, _OPEN_ERRORS):
        return _bare(Status.unreadable())
    raise exc


def fold_open_channel(channel: Reader, env: Env, folds: RunFolds) -> Row:
    """Fold an ALREADY-OPEN channel with the integrity guards, WITHOUT closing it.
    A byte-torn (json.JSONDecodeError) -> loud `corrupt` carrying the located seq; a
    substrate fault mid-read -> `unreadable`. open_and_fold closes in its own finally;
    the pool keeps the handle and its folds and re-uses them next tick (folding only the
    new records)."""
    try:
        return status_fold(channel, env, folds)
    except json.JSONDecodeError:
        return _corrupt(locate_torn_seq(channel))
    except (sqlite3.DatabaseError, sqlite3.OperationalError, OSError):
        return _bare(Status.unreadable())


def open_and_fold(ref: RunRef, env: Env, folds: RunFolds) -> Row:
    run_id = ref[0]
    # a reader (never a create): observing a run must NOT fabricate a
    # phantom <run_id>.db, and must NOT schema-mutate a foreign valid sqlite db under a
    # stale pointer — both, plus a genuinely-empty run, are `RunNotFound` -> `missing`
    # (a run exists iff it has records; spec channel-locators.md). A log in another format
    # raises a `LogFormatError` -> the loud format row. Only corrupt/non-sqlite bytes still
    # raise `sqlite3.DatabaseError` -> `unreadable`.
    try:
        channel = place_of(ref).reader(run_id)
    except (RunNotFound, LogFormatError, *_OPEN_ERRORS) as exc:
        return open_row_for(exc)
    try:
        return fold_open_channel(channel, env, folds)
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
    by `filter`. Missing/other-format/unreadable/substrate-fault/byte-torn -> [] (the
    header carries the run's status, including the loud `corrupt` and format verdicts,
    via render_single/open_and_fold)."""
    run_id = ref[0]
    # a reader (never a create): never fabricate a phantom db nor mutate a
    # foreign one. A run with no records (missing/empty/foreign) raises `RunNotFound`; a
    # log in another format a `LogFormatError`; corrupt/non-sqlite a `_OPEN_ERRORS` member
    # — each degrades the tail to [] (the header carries the loud verdict).
    try:
        channel = place_of(ref).reader(run_id)
    except (RunNotFound, LogFormatError, *_OPEN_ERRORS):
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
    # UPSTREAM(runstate filtered-reads): v1 applies the predicate here in Python. If runstate's
    # read() gains a filtered backward read (GeoffChurch/runstate's
    # docs/backlog/filtered-reads.md, ask 1), push the index-served part of `filter` into
    # channel.read so history is retroactively filterable; a substring match stays here.
    # Discover all revisit sites with: grep -rn "UPSTREAM(runstate filtered-reads)"
    return [e for e in got if filter is None or filter(e)]


def envelope_filter(text: str, hidden_families: set[str]) -> Callable[[Envelope], bool]:
    """Build a v1 log-filter predicate from the filter-bar text + the toggled-off
    (hidden) topic families. text: a plain substring matched against topic +
    request_id (+ 'seq>N', a numeric bound over the envelope's seq — the one position
    every record carries; log format 0.4.0 records carry no `step`). hidden_families:
    topic families to HIDE (the toggled-off KNOWN families) — a topic whose family
    is NOT in this set always shows, so supervisor.* / any unclassified topic is never
    silently dropped (the log streams every record). Substring and negation are not in
    runstate's read() vocabulary (GeoffChurch/runstate's docs/backlog/filtered-reads.md,
    ask 2), so this predicate stays client-side."""
    text = text.strip()
    seqbound: int | None = None
    if text.startswith("seq>") and text[4:].strip().isdigit():
        seqbound = int(text[4:].strip())

    def pred(e: Envelope) -> bool:
        if e.topic.split(".")[0] in hidden_families:  # subtractive: hide toggled-off known families
            return False
        if seqbound is not None:
            return e.seq > seqbound
        if text and text not in e.topic and text not in (e.request_id or ""):
            return False
        return True

    return pred


def render_table(resolver: Resolver, env: Env) -> list[Row]:
    """A one-shot table: every row folded cold, with fresh folds (the pure reads'
    answers at O(log) each). The live table is `pool.fold_frame`, which holds the folds.
    `now` is re-sampled per row; `fold_frame` captures it once per frame."""
    return [
        open_and_fold(ref, env, RunFolds.start(objective=env.objective))
        for ref, _attrs in resolver(env.clock())
    ]


class HeldRun:
    """One run observed across ticks by a screen that re-opens it each tick (the
    single-run app, the drill-down card): it holds the run's folds, so a tick folds only
    the records since the last, and drops them whenever the run reads corrupt or
    unreadable, as the pool drops a handle (EVICT_KINDS). The single-run view is the table
    at |I|=1 (spec §11): the same open_and_fold, the same folds."""

    def __init__(self, ref: RunRef, env: Env) -> None:
        self.ref = ref
        self._env = env
        self._folds = RunFolds.start(objective=env.objective)

    def row(self) -> Row:
        row = open_and_fold(self.ref, self._env, self._folds)
        if row.status.kind in EVICT_KINDS:
            self._folds = RunFolds.start(objective=self._env.objective)
        return row


def render_single(ref: RunRef, env: Env) -> Row:
    # the single-run view IS the table at |I|=1 — one code path, no bespoke screen (spec §11)
    return HeldRun(ref, env).row()
