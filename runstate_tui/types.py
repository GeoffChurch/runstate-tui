from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum

from runstate.channel import Envelope
from runstate.observables import Outcome


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


@dataclass(frozen=True)
class Issue:
    kind: IssueKind
    severity: Severity
    message: str
    seq: int | None = None
    detail: str | None = None


# display labels are cockpit policy (spec §4/§8); the internal `outcome` stays the
# raw runstate Outcome so there is no drift-prone translation of the protocol's logic.
_TERMINAL_LABELS = {
    "completed": "done",
    "preempted": "preempted",
    "errored": "errored",
    "killed": "killed",
    "presumed_dead": "dead",
}


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


_STATUS_SEVERITY = {
    StatusKind.UNREADABLE: Severity.HIGH,
    StatusKind.CORRUPT: Severity.HIGH,
    StatusKind.ERROR: Severity.HIGH,
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
    outcome: Outcome | None = None  # set iff kind is TERMINAL
    detail: str | None = None  # optional diagnostic text (e.g. a terminal RunResult.error)

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
    def terminal(cls, outcome: Outcome, detail: str | None = None) -> Status:
        return cls(StatusKind.TERMINAL, outcome, detail=detail)

    @property
    def label(self) -> str:
        if self.kind is StatusKind.TERMINAL:
            assert self.outcome is not None  # invariant: set iff kind is TERMINAL
            # render honestly: an unrecognized outcome falls back to its own wire string
            return _TERMINAL_LABELS.get(str(self.outcome.value), str(self.outcome.value))
        return _KIND_LABELS.get(self.kind, str(self.kind.value))

    @property
    def severity(self) -> Severity:
        return _STATUS_SEVERITY.get(self.kind, Severity.OK)


@dataclass(frozen=True)
class Row:
    status: Status
    frontier: int | None
    freshness: float | None  # age = max(0, now - last_activity)
    value: tuple[str, object, int | None] | None  # (name, scalar, step)
    elapsed: float | None  # now - first started.t; None if no started
    episode: str | None  # latest_episode handle (PURE); None in Stage 0
    undischarged_stops: tuple[Envelope, ...]
    live_demand: tuple[Envelope, ...]
    issues: tuple[Issue, ...]

    @property
    def severity(self) -> Severity:
        return max([self.status.severity, *(i.severity for i in self.issues)], key=int)
