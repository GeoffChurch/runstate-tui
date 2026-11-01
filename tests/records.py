"""Hand-built log format 0.4.0 records, for scenarios that need an exact shape: fixed
clocks and handles (so a snapshot does not depend on the host or the pid), or a record
the reference writers never produce (a death report, a torn or junk body).

Every body is built from runstate's own typed bodies (`Started`, `Commit`, `Stopped`,
`Died`, ...) and serialized by `dataclasses.asdict`, the vocabulary's declared
serialization, so each conforms to the 0.4.0 schemas by construction. Behavior tests
that need no exact shape drive runstate's real writers instead (`Worker`,
`report_death`, the launchers).

A `Script` tracks seqs the way the substrate assigns them (the n-th record is seq n), and
the claim's position, so `commit()` names its parent and the values it commits the way
the Worker does: `started` claims, `value`s are pending, `commit` commits them.
`records` is `build_log`'s input: `(body, topic, name, request_id)` tuples."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import runstate as rs

Rec = tuple[Any, str, str | None, str | None]  # (body, topic, name, request_id)

H1 = "local://h/1"  # the default worker handle: a fixed host and pid


@dataclass
class Script:
    records: list[Rec] = field(default_factory=list)
    claim: int | None = None  # the newest claim's seq
    position: int | None = None  # the claim's position: its newest node, None at the root
    pending: list[int] = field(default_factory=list)  # value seqs the next node commits

    def _add(self, body: Any, topic: str, name: str | None = None, rid: str | None = None) -> int:
        self.records.append((body, topic, name, rid))
        return len(self.records)

    def raw(self, body: Any, topic: str, name: str | None = None, rid: str | None = None) -> int:
        """Any record, verbatim: an exact or deliberately non-conformant shape."""
        return self._add(body, topic, name, rid)

    # ----- supervisor-v0.5 --------------------------------------------------------
    def launched(self, launch: str, t: float, handle: str = H1) -> int:
        body = asdict(rs.Launched(handle=handle, status="running", t=t))
        return self._add(body, rs.Topic.SUPERVISOR_LAUNCHED, None, launch)

    def terminated(
        self,
        launch: str,
        t: float,
        *,
        reason: rs.Reap = rs.Reap.EXITED,
        exit_code: int | None = 0,
        signal: int | None = None,
    ) -> int:
        body = asdict(rs.Terminated(reason=reason, exit_code=exit_code, signal=signal, t=t))
        return self._add(body, rs.Topic.SUPERVISOR_TERMINATED, None, launch)

    def died(
        self, outcome: rs.Outcome, evidence: str, t: float, *, claim: int | None = None
    ) -> int:
        """A death report naming `claim` (the newest claim by default)."""
        named = self.claim if claim is None else claim
        assert named is not None, "a death report names a claim"
        body = asdict(rs.Died(claim_seq=named, outcome=outcome, evidence=evidence, t=t))
        return self._add(body, rs.Topic.SUPERVISOR_DIED)

    # ----- lifecycle-v0.6 ---------------------------------------------------------
    def started(
        self,
        t: float,
        handle: str = H1,
        *,
        launch: str | None = None,
        resume_from: int | None = None,
        sources: tuple[str, ...] = (),
    ) -> int:
        body = asdict(
            rs.Started(handle=handle, t=t, resume_from=resume_from, sources=list(sources))
        )
        seq = self._add(body, rs.Topic.LIFECYCLE_STARTED, None, launch)
        self.claim, self.position, self.pending = seq, resume_from, []
        return seq

    def value(self, name: str, v: Any, t: float | None) -> int:
        seq = self._add(asdict(rs.Value(value=v, t=t)), rs.Topic.VALUE, name)
        self.pending.append(seq)
        return seq

    def commit(
        self,
        t: float,
        *,
        consumed: int = 0,
        fired: tuple[str, ...] = (),
        expired: tuple[str, ...] = (),
    ) -> int:
        assert self.claim is not None, "a commit names its claim"
        body = asdict(
            rs.Commit(
                claim_seq=self.claim,
                consumed_seq=consumed,
                parent=self.position,
                values=list(self.pending),
                fired=list(fired),
                expired=list(expired),
                t=t,
            )
        )
        seq = self._add(body, rs.Topic.LIFECYCLE_COMMIT)
        self.position, self.pending = seq, []
        return seq

    def heartbeat(self, t: float, *, consumed: int = 0) -> int:
        assert self.claim is not None, "a heartbeat names its claim"
        body = asdict(rs.Heartbeat(claim_seq=self.claim, consumed_seq=consumed, t=t))
        return self._add(body, rs.Topic.LIFECYCLE_HEARTBEAT)

    def stopped(
        self,
        t: float,
        *,
        completed: bool,
        error: str | None = None,
        honored: tuple[str, ...] = (),
    ) -> int:
        """The claim's exit node. An exit with an error is torn (`values: null`), as
        lifecycle-v0.6's "`error` non-null => `values` null" requires."""
        assert self.claim is not None, "an exit names its claim"
        torn = error is not None
        body = asdict(
            rs.Stopped(
                completed=completed,
                error=error,
                claim_seq=self.claim,
                honored=list(honored),
                parent=self.position,
                values=None if torn else list(self.pending),
                fired=[],
                expired=[],
                t=t,
            )
        )
        seq = self._add(body, rs.Topic.LIFECYCLE_STOPPED)
        self.pending = []
        return seq

    # ----- subscription-v0.4 ------------------------------------------------------
    def stop(self, request_id: str, body: dict[str, Any] | None = None) -> int:
        return self._add(body or {}, rs.Topic.CONTROL_STOP, None, request_id)

    def subscribe(self, request_id: str, name: str | None, body: dict[str, Any]) -> int:
        return self._add(body, rs.Topic.CONTROL_SUBSCRIBE, name, request_id)

    def withdraw(self, request_id: str, t: float) -> int:
        return self._add(asdict(rs.Withdraw(t=t)), rs.Topic.CONTROL_WITHDRAW, None, request_id)


def write(channel: rs.Channel, records: list[Rec]) -> None:
    """Append `records` to `channel` in order (the n-th lands at seq n on a fresh log)."""
    for body, topic, name, rid in records:
        channel.send(body, topic=topic, name=name, request_id=rid)


def worker(
    channel: rs.Channel, *, clock: float = 100.0, sources: tuple[str, ...] = ()
) -> rs.Worker:
    """runstate's reference Worker on `channel`, fresh, with no checkpoints, under a frozen
    clock: the real writer, for tests whose records' shape is runstate's business."""
    return rs.Worker(
        channel, resume_from=None, sources=list(sources), save=rs.NoSave(), now=lambda: clock
    )
