from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from runstate import RunResult, await_consumed, open_channel
from runstate.channel import Channel
from runstate.observables import MalformedRecordError, peek_terminal
from runstate.vocabulary.payloads import Nak, Topic

from .resolver import RunRef
from .types import Severity

# the same open-error taxonomy the fold uses (table.py); sqlite3 for its exceptions only
_OPEN_ERRORS = (sqlite3.DatabaseError, sqlite3.OperationalError, PermissionError, OSError)


class StopResult(Enum):
    ACCEPTED = "accepted"  # consumed by a live worker (await_consumed -> None)
    REFUSED = "refused"  # naked, with a reason (await_consumed -> Nak)
    DIED = "died"  # run ended under the request (await_consumed -> RunResult)
    UNSAFE = "unsafe"  # sent, not consumed within the bound — may never be served
    UNDELIVERED = "undelivered"  # never appended (missing/unreadable run, or a lost claim)
    MOOT = "moot"  # run already ended BEFORE the stop was sent — never sent at all


_STOP_SEVERITY = {
    StopResult.ACCEPTED: Severity.OK,
    StopResult.DIED: Severity.MEDIUM,
    StopResult.REFUSED: Severity.MEDIUM,
    StopResult.UNSAFE: Severity.HIGH,
    StopResult.UNDELIVERED: Severity.HIGH,
    StopResult.MOOT: Severity.MEDIUM,
}

_STOP_LABEL = {
    StopResult.ACCEPTED: "✓ stop accepted",
    StopResult.DIED: "◼ run ended under stop",
    StopResult.REFUSED: "✗ stop refused",
    StopResult.UNSAFE: "⚠ unsafe stop",
    StopResult.UNDELIVERED: "⚠ stop not delivered",
    StopResult.MOOT: "◼ run already ended",
}


@dataclass(frozen=True)
class StopOutcome:
    result: StopResult
    request_id: str
    detail: str | None = None

    @property
    def severity(self) -> Severity:
        return _STOP_SEVERITY[self.result]

    @property
    def label(self) -> str:
        base = _STOP_LABEL[self.result]
        return f"{base}: {self.detail}" if self.detail else base


def _already_ended(channel: Channel) -> str | None:
    """Peek (never send) for an existing terminal, so a stop against a run that
    already ended can be recognized as MOOT before it ever touches the log.

    Returns the terminal outcome's value (str) if the run has already ended,
    else None. A `MalformedRecordError` (a decodable-but-wrong-shape terminal
    record) can't confirm the run ended — degrade to None and let the caller
    proceed to send (consistent with `fold.py`'s `guarded` degradation). A
    byte-torn body (`json.JSONDecodeError`) is NOT caught here — it propagates,
    consistent with the corrupt taxonomy elsewhere (an undecodable substrate is
    not this function's call to make)."""
    try:
        result = peek_terminal(channel)
    except MalformedRecordError:
        return None
    return None if result is None else str(result.outcome)


def stop_run(
    channel: Channel,
    *,
    request_id: str,
    timeout: float,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> StopOutcome:
    """Send one unconditional `control.stop` and await its answer (bounded).

    An empty StopTrigger body (`{}`) is a `from`-less one-shot: the worker fires
    it at the next safe point. `await_consumed`'s codomain is the whole answer
    space — None (accepted) | Nak (refused) | RunResult (died under the
    request) | TimeoutError (not drained in time).

    BEFORE sending, peek for an existing terminal: a stop against a run that
    already ended is MOOT, not UNSAFE — UNSAFE means "sent to a live run, not
    answered"; a moot stop is never sent at all, so it never writes a pointless
    `control.stop` into a finished run's log."""
    existing = _already_ended(channel)
    if existing is not None:
        return StopOutcome(StopResult.MOOT, request_id, f"run already ended ({existing})")
    seq = channel.send({}, topic=Topic.CONTROL_STOP, request_id=request_id)
    if seq is None:  # provably-lost claim — only reachable with expected_seq; defensive
        return StopOutcome(StopResult.UNDELIVERED, request_id, "stop was not appended (lost claim)")
    try:
        answer = await_consumed(
            channel, seq, request_id=request_id, timeout=timeout, now=now, sleep=sleep
        )
    except TimeoutError:
        return StopOutcome(
            StopResult.UNSAFE, request_id, f"not consumed within {timeout:g}s — no live worker?"
        )
    if answer is None:
        return StopOutcome(StopResult.ACCEPTED, request_id)
    if isinstance(answer, Nak):
        return StopOutcome(StopResult.REFUSED, request_id, answer.reason)
    # RunResult — the run died under the request (refused-by-death)
    assert isinstance(answer, RunResult)
    return StopOutcome(StopResult.DIED, request_id, str(answer.outcome.value))


def dispatch_stop(
    ref: RunRef,
    *,
    request_id: str,
    timeout: float,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
) -> StopOutcome:
    """Open the run by ref, run the handshake, close the channel after.

    stat-before-open (sqlite): a missing pointer must NOT open_channel — that
    would fabricate a phantom `<run_id>.db` AND write a `control.stop` into a
    file we do not own (spec §8). Missing/unreadable/unopenable ⇒ UNDELIVERED."""
    run_id, root, backend = ref
    if backend == "sqlite":
        try:
            (Path(root) / f"{run_id}.db").stat()
        except FileNotFoundError:
            return StopOutcome(StopResult.UNDELIVERED, request_id, "no such run (missing)")
        except OSError:
            return StopOutcome(StopResult.UNDELIVERED, request_id, "run is unreadable")
    try:
        channel = open_channel(run_id, root=root, backend=backend)
    except _OPEN_ERRORS:
        return StopOutcome(StopResult.UNDELIVERED, request_id, "run could not be opened")
    try:
        return stop_run(channel, request_id=request_id, timeout=timeout, now=now, sleep=sleep)
    finally:
        channel.close()
