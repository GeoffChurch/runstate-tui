from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import NamedTuple

from runstate import Envelope, Outcome, Tier


class Severity(IntEnum):
    """Row/issue severity — int-valued so a row's badge is `max(...)`."""

    OK = 0
    INFO = 1
    MEDIUM = 2
    HIGH = 3


class IssueKind(Enum):
    MALFORMED = "malformed"
    SKEW_SUSPECTED = "skew_suspected"
    UNSAFE_STOP = "unsafe_stop"
    CORRUPT = "corrupt"
    INTERNAL_ERROR = "internal_error"
    LOG_FORMAT = "log_format"


# Issue kinds that are the *footnote* of a status verdict, not a separate problem: a
# byte-torn run is Status.corrupt() + a CORRUPT issue carrying the seq; a fold crash is
# Status.error() + an INTERNAL_ERROR issue carrying the exception; a log in another format
# is Status.log_format() + a LOG_FORMAT issue carrying runstate's instructions (table.py
# _corrupt / _fold_error / _wrong_format -- deliberate dual-surfacing, feeding the status
# column AND the drill-down list). A per-event headcount (the fleet summary strip) counts
# these once, by their status, so it skips them as issue tags.
_STATUS_TWIN_ISSUES = {IssueKind.CORRUPT, IssueKind.INTERNAL_ERROR, IssueKind.LOG_FORMAT}


@dataclass(frozen=True)
class Issue:
    kind: IssueKind
    severity: Severity
    message: str
    seq: int | None = None
    detail: str | None = None


# display labels are cockpit policy (spec §4/§8); the internal `outcomes` stay the raw
# runstate Outcomes so there is no drift-prone translation of the protocol's logic.
_TERMINAL_LABELS = {
    "completed": "done",
    "preempted": "preempted",
    "errored": "errored",
    "killed": "killed",
    "lost": "lost",
    "presumed_dead": "dead",
}

# The order a verdict's outcomes are listed in, worst LAST (status_color takes the last):
# a set has no order of its own, and runstate chooses none among death reports, so the
# cockpit fixes one for display only. An outcome missing here (a future member) sorts
# after these by its wire string, so it still renders by its own label.
_OUTCOME_ORDER = (
    Outcome.COMPLETED,
    Outcome.PREEMPTED,
    Outcome.LOST,
    Outcome.PRESUMED_DEAD,
    Outcome.ERRORED,
    Outcome.KILLED,
)


def ordered_outcomes(outcomes: frozenset[Outcome]) -> tuple[Outcome, ...]:
    """A verdict's outcomes in display order (`_OUTCOME_ORDER`, then any unknown member)."""
    rank = {o: i for i, o in enumerate(_OUTCOME_ORDER)}
    return tuple(sorted(outcomes, key=lambda o: (rank.get(o, len(rank)), str(o.value))))


class StatusKind(Enum):
    PENDING = "pending"
    LIVE = "live"
    STALE = "stale"
    TERMINAL = "terminal"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    CONFLICTED = "conflicted"
    CORRUPT = "corrupt"
    ERROR = "error"  # an UNEXPECTED exception escaped the fold — a genuine internal bug on one run
    # the log is in a format this runstate does not read (LogFormatMismatch: older or newer)
    # or predates versioned formats (LogFormatMissing) -- an operator action, not a fault
    LOG_FORMAT = "log_format"


_STATUS_SEVERITY = {
    StatusKind.UNREADABLE: Severity.HIGH,
    StatusKind.CORRUPT: Severity.HIGH,
    StatusKind.ERROR: Severity.HIGH,
    StatusKind.LOG_FORMAT: Severity.HIGH,  # the cockpit cannot read the run at all
    StatusKind.CONFLICTED: Severity.MEDIUM,
    StatusKind.PENDING: Severity.INFO,
    StatusKind.MISSING: Severity.INFO,
}

# non-terminal kinds whose displayed label differs from the raw kind value. Mirrors
# _TERMINAL_LABELS: ERROR renders "fold-error" so an internal fold bug is never confused
# with a run's terminal `errored` outcome (which labels "errored" via _TERMINAL_LABELS).
_KIND_LABELS = {
    StatusKind.ERROR: "fold-error",
}


@dataclass(frozen=True)
class Status:
    kind: StatusKind
    # the verdict's outcomes and the viewpoint it rests on: non-empty / set iff TERMINAL
    outcomes: frozenset[Outcome] = frozenset()
    tier: Tier | None = None
    detail: str | None = None  # optional diagnostic text (an exit's error, death evidence)
    format: str | None = None  # LOG_FORMAT only: the format found, None for an unversioned log

    @classmethod
    def pending(cls) -> Status:
        return cls(StatusKind.PENDING)

    @classmethod
    def live(cls) -> Status:
        return cls(StatusKind.LIVE)

    @classmethod
    def stale(cls) -> Status:
        return cls(StatusKind.STALE)

    @classmethod
    def missing(cls) -> Status:
        return cls(StatusKind.MISSING)

    @classmethod
    def unreadable(cls) -> Status:
        return cls(StatusKind.UNREADABLE)

    @classmethod
    def conflicted(cls) -> Status:
        return cls(StatusKind.CONFLICTED)

    @classmethod
    def corrupt(cls) -> Status:
        return cls(StatusKind.CORRUPT)

    @classmethod
    def error(cls, detail: str | None = None) -> Status:
        # an unexpected fold exception, contained to a loud per-run row (fold_frame stays
        # total); `detail` carries the exception text. Distinct from a terminal `errored`.
        return cls(StatusKind.ERROR, detail=detail)

    @classmethod
    def log_format(cls, found: str | None, detail: str) -> Status:
        # `found` is the format the log is in (older or newer than the cockpit's runstate
        # reads), or None for a log at the unversioned legacy address; `detail` the action.
        return cls(StatusKind.LOG_FORMAT, detail=detail, format=found)

    @classmethod
    def terminal(cls, outcomes: frozenset[Outcome], tier: Tier, detail: str | None) -> Status:
        if not outcomes:
            raise ValueError("a terminal status has at least one outcome")
        return cls(StatusKind.TERMINAL, outcomes, tier, detail=detail)

    @property
    def label(self) -> str:
        if self.kind is StatusKind.TERMINAL:
            # render honestly: an unrecognized outcome falls back to its own wire string;
            # several outcomes (death reports that disagree) are all shown, never one chosen
            return "/".join(
                _TERMINAL_LABELS.get(str(o.value), str(o.value))
                for o in ordered_outcomes(self.outcomes)
            )
        if self.kind is StatusKind.LOG_FORMAT:
            return "unversioned" if self.format is None else f"format {self.format}"
        return _KIND_LABELS.get(self.kind, str(self.kind.value))

    @property
    def severity(self) -> Severity:
        return _STATUS_SEVERITY.get(self.kind, Severity.OK)


class Reading(NamedTuple):
    """The objective's newest element on the head's lineage: its name, its value, and the
    commit count at the node that committed it (the run-absolute position a 0.3.0 value
    carried as `step`; a count, so it agrees with the frontier column)."""

    name: str
    value: object
    commits: int


@dataclass(frozen=True)
class Row:
    status: Status
    frontier: int | None  # commits on the latest claim's lineage; None when unreadable
    freshness: float | None  # age = max(0, now - last_activity)
    value: Reading | None  # the objective's last element on the lineage
    elapsed: float | None  # now - first started.t; None if no started
    episode: str | None  # latest_episode handle (PURE)
    undischarged_stops: tuple[Envelope, ...]
    live_demand: tuple[Envelope, ...]
    issues: tuple[Issue, ...]

    @property
    def severity(self) -> Severity:
        return max([self.status.severity, *(i.severity for i in self.issues)], key=int)
