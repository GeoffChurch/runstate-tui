from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Any


class Severity(IntEnum):
    """Row/issue severity — int-valued so a row's badge is `max(...)`."""
    OK = 0
    INFO = 1
    MEDIUM = 2
    HIGH = 3


class IssueKind(Enum):
    TORN = "torn"
    SKEW_SUSPECTED = "skew_suspected"
    UNSAFE_STOP = "unsafe_stop"


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


_STATUS_SEVERITY = {
    StatusKind.UNREADABLE: Severity.HIGH,
    StatusKind.CONFLICTED: Severity.MEDIUM,
    StatusKind.PENDING: Severity.INFO,
    StatusKind.MISSING: Severity.INFO,
}


@dataclass(frozen=True)
class Status:
    kind: StatusKind
    outcome: Any | None = None  # set iff kind is TERMINAL; a runstate Outcome

    @classmethod
    def pending(cls) -> "Status": return cls(StatusKind.PENDING)
    @classmethod
    def live(cls) -> "Status": return cls(StatusKind.LIVE)
    @classmethod
    def stale(cls) -> "Status": return cls(StatusKind.STALE)
    @classmethod
    def missing(cls) -> "Status": return cls(StatusKind.MISSING)
    @classmethod
    def unreadable(cls) -> "Status": return cls(StatusKind.UNREADABLE)
    @classmethod
    def conflicted(cls) -> "Status": return cls(StatusKind.CONFLICTED)
    @classmethod
    def terminal(cls, outcome: Any) -> "Status": return cls(StatusKind.TERMINAL, outcome)

    @property
    def label(self) -> str:
        if self.kind is StatusKind.TERMINAL:
            # render honestly: an unrecognized outcome falls back to its own wire string
            return _TERMINAL_LABELS.get(str(self.outcome.value), str(self.outcome.value))
        return self.kind.value

    @property
    def severity(self) -> Severity:
        return _STATUS_SEVERITY.get(self.kind, Severity.OK)
