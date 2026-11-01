from __future__ import annotations

import json
import math
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TypeVar

from runstate import (
    Channel,
    Commits,
    Envelope,
    HeadFold,
    LastElement,
    LiveDemand,
    MalformedRecordError,
    PendingStops,
    PrefixLen,
    RunResult,
    Tier,
    Topic,
    last_activity,
    latest_episode,
    peek_terminal,
)

from .env import Env, Liveness, resolve_liveness
from .types import Issue, IssueKind, Reading, Row, Severity, Status

T = TypeVar("T")

_TORN_DECODE_ERRORS = (json.JSONDecodeError, sqlite3.DatabaseError)


@dataclass(frozen=True)
class Objective:
    """The value column's stream and its held `LastElement` fold."""

    name: str
    last: LastElement


@dataclass(frozen=True)
class RunFolds:
    """One run's held folds (runstate's `HeadFold`, `PrefixLen`, `LastElement`,
    `LiveDemand`, `PendingStops`): each poll reads only the records since the last, so a
    polled row costs O(new records), never O(log) -- "poll a cheap watermark, apply the
    delta, never rebuild". It is state that outlives a tick, so it lives with whoever holds
    the run across ticks: the pool beside its channel, a screen beside its ref. A fresh one
    gives the pure reads' answers at a cold O(log) cost, which a one-shot caller pays.

    A fold's cursor is a seq, not a handle, so a holder that re-opens the run each tick
    keeps its folds. A holder drops them with the channel when the run reads corrupt or
    unreadable (the pool's eviction), so a healed run is folded from scratch."""

    head: HeadFold
    commits: PrefixLen
    objective: Objective | None  # None: no objective named, no value column
    demand: LiveDemand
    stops: PendingStops

    @classmethod
    def start(cls, *, objective: str | None) -> RunFolds:
        return cls(
            head=HeadFold(),
            commits=PrefixLen(Commits()),
            objective=None if objective is None else Objective(objective, LastElement(objective)),
            demand=LiveDemand(),
            stops=PendingStops(),
        )


def locate_torn_seq(channel: Channel) -> int | None:
    """Find the seq of the first record whose decode raises (append-only contiguity):
    walk read(after=k, limit=1); a raising probe localizes the tear at k+1. Returns
    None if no tear is found (the caller degrades to an unlocalized message)."""
    k = 0
    last = channel.last_seq()
    while k < last:
        try:
            got = channel.read(after=k, limit=1)
        except _TORN_DECODE_ERRORS:
            return k + 1
        if not got:
            return None
        k = got[0].seq
    return None


def guarded(fn: Callable[[Channel], T], channel: Channel) -> tuple[T | None, Issue | None]:
    """A read that degrades a decodable-but-wrong-shape record to a MALFORMED issue so
    the run's other factors survive: runstate's MalformedRecordError (schema-invalid), or
    an AttributeError/TypeError from a non-dict body (`42`, `[1,2,3]`). The exact exception
    is retained in `detail`. A byte-torn body (json.JSONDecodeError) is NOT caught here — it
    propagates to open_and_fold as whole-run `corrupt` (undecodable substrate).

    A held fold that raises keeps raising on every poll (runstate: "a check that fails
    holds for the claim, and every poll raises it, until a newer claim"), so the issue is
    stable across ticks rather than flickering."""
    try:
        return fn(channel), None
    except (MalformedRecordError, AttributeError, TypeError) as exc:
        seq = exc.seq if isinstance(exc, MalformedRecordError) else None
        loc = f" at seq {seq}" if seq is not None else ""
        return None, Issue(
            kind=IssueKind.MALFORMED,
            severity=Severity.MEDIUM,
            message=f"record malformed{loc}",
            seq=seq,
            detail=f"{type(exc).__name__}: {exc}",
        )


def _verdict_detail(result: RunResult) -> str | None:
    """The operator-facing diagnostic a verdict carries, by the viewpoint it rests on:
    the exit's own error (None for a clean exit); every death report's evidence, which no
    fold parses (`reap: signal 9`, `slurm:TIMEOUT`), in seq order; and for an unclaimed
    run, that the verdict is its launch's -- a fact about a process, never the worker's
    word (an `sbatch` wrapper exits 0 at submit time)."""
    if result.tier is Tier.DIED:
        return "; ".join(r.evidence for r in result.reports) or None
    if result.tier is Tier.TERMINATED:
        return "unclaimed launch"
    return result.error


def _skipped_issue(result: RunResult) -> Issue | None:
    """The unreadable records the verdict skipped (`RunResult.malformed`): runstate reports
    them rather than raising, so the cockpit surfaces them as one MEDIUM issue -- the
    verdict stands, a signal is flagged."""
    if not result.malformed:
        return None
    seqs = ", ".join(str(s) for s in result.malformed)
    return Issue(
        kind=IssueKind.MALFORMED,
        severity=Severity.MEDIUM,
        message=f"verdict skipped unreadable record at seq {seqs}",
        seq=result.malformed[0],
        detail=f"RunResult.malformed={result.malformed!r}",
    )


def reconcile_status(
    channel: Channel, env: Env, now: float
) -> tuple[Status, float | None, list[Issue]]:
    issues: list[Issue] = []
    result, term_issue = guarded(peek_terminal, channel)
    if term_issue is not None:
        issues.append(term_issue)

    la, la_issue = guarded(last_activity, channel)  # the ONE last_activity read
    if la_issue is not None:
        issues.append(la_issue)
    if la is not None and not math.isfinite(la):
        issues.append(
            Issue(
                kind=IssueKind.MALFORMED,
                severity=Severity.HIGH,
                message="activity timestamp is not finite (garbage clock)",
                detail=f"last_activity={la!r}",
            )
        )
        la = None  # unusable — never let a non-finite time read as fresh LIVE
    freshness = None if la is None else max(0.0, now - la)
    if isinstance(la, (int, float)) and not isinstance(la, bool) and la > now:
        issues.append(
            Issue(
                kind=IssueKind.SKEW_SUSPECTED,
                severity=Severity.MEDIUM,
                message="last activity is in the future (clock skew)",
            )
        )

    if result is not None:
        # terminal wins: the claim's exit, else the death reports naming it, else an
        # unclaimed run's launch. The verdict is a set of outcomes; none is chosen.
        skipped = _skipped_issue(result)
        if skipped is not None:
            issues.append(skipped)
        status = Status.terminal(result.outcomes, result.tier, detail=_verdict_detail(result))
        return status, freshness, issues
    if la is None:
        return Status.pending(), freshness, issues  # no dated activity at all
    # A held claim. No read probes a process, so a crash nobody reported reads held until
    # a party with evidence writes a death report; freshness ages it to `stale` past
    # `stuck_threshold`, which is inference (slow, or dead), never a verdict.
    verdict = resolve_liveness(channel, env, now, la)
    status = Status.live() if verdict is Liveness.LIVE else Status.stale()
    return status, freshness, issues


def read_elapsed(channel: Channel, now: float) -> tuple[float | None, Issue | None]:
    def _started_t(ch: Channel) -> object:
        started = ch.read(topics=[Topic.LIFECYCLE_STARTED], limit=1)
        # alien non-dict body -> AttributeError here -> caught by guarded() below
        return started[0].body.get("t") if started else None

    t, malformed_issue = guarded(_started_t, channel)
    if malformed_issue is not None:
        return None, malformed_issue  # a malformed `started` never masquerades as "no started"
    if t is None:
        return None, None
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t):
        return None, None
    if t > now:
        return 0.0, Issue(
            kind=IssueKind.SKEW_SUSPECTED,
            severity=Severity.MEDIUM,
            message="run epoch is in the future (clock skew)",
            detail=f"started.t={t} > now={now}",
        )
    return now - float(t), None


def read_lineage(
    channel: Channel, folds: RunFolds
) -> tuple[int | None, Reading | None, Issue | None]:
    """The frontier and the objective's reading, both at ONE head resolved this poll, so
    they agree. The frontier is `count(Commits(), head)`, None for the empty lineage; the
    reading is the objective's last element in lineage order, with the commit count at the
    node that committed it. A head that raises (a malformed node on the newest claim)
    degrades both to None under one MALFORMED issue."""

    def _read(ch: Channel) -> tuple[int | None, Reading | None]:
        head = folds.head.poll(ch)
        count = folds.commits.poll(ch, head)
        frontier = None if head.node is None else count
        if folds.objective is None:
            return frontier, None
        element = folds.objective.last.poll(ch, head)
        if element is None:
            return frontier, None
        at = folds.commits.at(ch, element.node, w=head.w)
        return frontier, Reading(name=folds.objective.name, value=element.value, commits=at)

    got, issue = guarded(_read, channel)
    if got is None:
        return None, None, issue
    return got[0], got[1], issue


def status_fold(channel: Channel, env: Env, folds: RunFolds) -> Row:
    now = env.clock()  # captured once per frame, threaded below
    issues: list[Issue] = []

    status, freshness, status_issues = reconcile_status(channel, env, now)
    issues.extend(status_issues)

    frontier, value, lineage_issue = read_lineage(channel, folds)
    if lineage_issue is not None:
        issues.append(lineage_issue)

    elapsed, elapsed_issue = read_elapsed(channel, now)
    if elapsed_issue is not None:
        issues.append(elapsed_issue)

    def _episode(ch: Channel) -> str | None:
        e: Envelope | None = latest_episode(ch)
        if e is None:
            return None
        # .body.get can hit an alien non-dict body -> AttributeError -> caught by guarded() below
        handle = e.body.get("handle")
        return handle if isinstance(handle, str) else None

    episode, episode_issue = guarded(_episode, channel)
    if episode_issue is not None:
        issues.append(episode_issue)

    # Pending stops by name, for the whole log (runstate's PendingStops): a stop is a
    # request about the process, so one a crashed claim never honored is still pending,
    # and the next claim's first drain honors it -- live, not a ghost of a prior episode.
    stops, stops_issue = guarded(folds.stops.poll, channel)
    if stops_issue is not None:
        issues.append(stops_issue)

    demand, demand_issue = guarded(folds.demand.poll, channel)
    if demand_issue is not None:
        issues.append(demand_issue)

    return Row(
        status=status,
        frontier=frontier,
        freshness=freshness,
        value=value,
        elapsed=elapsed,
        episode=episode,
        undischarged_stops=tuple(stops or ()),
        live_demand=tuple(demand or ()),
        issues=tuple(issues),
    )
