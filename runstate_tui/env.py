from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from runstate.channel import Channel


class Liveness(Enum):
    LIVE = "live"
    STALE = "stale"
    DEAD = "dead"


class LivenessSignal(Protocol):
    # last_activity is read once by the fold and passed in, so the signal is pure and
    # can't double-read; a channel-reading overlay (probe) still gets the channel.
    def liveness(self, channel: Channel, env: "Env", now: float,
                 last_activity: float | None) -> Liveness | None: ...


@dataclass(frozen=True)
class FreshnessSignal:
    """The core's only liveness signal: a pure verdict from the log's last-activity clock."""

    def liveness(self, channel: Channel, env: "Env", now: float,
                 last_activity: float | None) -> Liveness | None:
        if last_activity is None:
            return None  # no dated activity -> no opinion (the fold decides pending)
        age = max(0.0, now - last_activity)
        return Liveness.LIVE if age <= env.stuck_threshold else Liveness.STALE


@dataclass(frozen=True)
class Env:
    clock: Callable[[], float]
    objective: str | None = None
    stuck_threshold: float = 60.0
    liveness: tuple[LivenessSignal, ...] = field(default_factory=lambda: (FreshnessSignal(),))


def resolve_liveness(channel: Channel, env: Env, now: float,
                     last_activity: float | None) -> Liveness | None:
    for signal in env.liveness:  # order == precedence; overlays register ahead of freshness
        verdict = signal.liveness(channel, env, now, last_activity)
        if verdict is not None:
            return verdict
    return None
