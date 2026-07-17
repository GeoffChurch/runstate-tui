from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, IntEnum


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
