from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from runstate import open_channel
from runstate.channel import Channel

from .env import Env
from .resolver import RunRef
from .table import _OPEN_ERRORS, _bare, fold_open_channel
from .types import Row, Status, StatusKind

Table = tuple[tuple[RunRef, Row], ...]

# integrity verdicts that mean the pooled handle is no good — evict + close so the
# next tick cold-opens fresh (self-healing detection; the pool holds only healthy handles).
_EVICT_KINDS = (StatusKind.CORRUPT, StatusKind.UNREADABLE)


class ChannelPool:
    """Owner-thread-ONLY LRU pool of open channels, keyed by the full RunRef.
    reader == evictor: a mid-fold channel is never closed under it. NOT thread-safe
    by design — one owner thread touches it (opens, reads, evicts, closes)."""

    def __init__(self, cap: int = 128) -> None:
        self._cap = cap
        self._open: OrderedDict[RunRef, Channel] = OrderedDict()

    def __len__(self) -> int:
        return len(self._open)

    def _evict(self, ref: RunRef) -> None:
        ch = self._open.pop(ref, None)
        if ch is not None:
            ch.close()

    def _evict_oldest(self) -> None:
        _ref, ch = self._open.popitem(last=False)
        ch.close()

    def reconcile(self, live: set[RunRef]) -> None:
        """Close + drop any pooled run no longer resolved this frame."""
        for ref in [r for r in self._open if r not in live]:
            self._evict(ref)

    def row_for(self, ref: RunRef, frame_env: Env) -> Row:
        run_id, root, backend = ref
        # stat-before-open EVERY tick (never fabricate a phantom db; catch a run whose
        # file vanished mid-session -> honest `missing`, matching open_and_fold).
        if backend == "sqlite":
            try:
                (Path(root) / f"{run_id}.db").stat()
            except FileNotFoundError:
                self._evict(ref)
                return _bare(Status.missing())
            except OSError:
                self._evict(ref)
                return _bare(Status.unreadable())
        ch = self._open.get(ref)
        if ch is None:
            try:
                ch = open_channel(run_id, root=root, backend=backend)
            except _OPEN_ERRORS:
                return _bare(Status.unreadable())  # not cached — retried next tick
            if len(self._open) >= self._cap:
                self._evict_oldest()
            self._open[ref] = ch
        self._open.move_to_end(ref)  # LRU: most-recently-used last
        row = fold_open_channel(ch, frame_env)
        if row.status.kind in _EVICT_KINDS:
            self._evict(ref)
        return row

    def close_all(self) -> None:
        for ch in self._open.values():
            ch.close()
        self._open.clear()


def fold_frame(pool: ChannelPool, refs: list[RunRef], env: Env, now: float) -> Table:
    """One owner-thread frame. Reconcile the pool to `refs`, then fold EVERY run fresh
    under a single per-frame `now` (via a frozen-clock Env so objective/threshold/
    liveness carry through). The row for `r` == render_single(r) at this `now`."""
    frame_env = replace(env, clock=lambda: now)
    pool.reconcile(set(refs))
    return tuple((ref, pool.row_for(ref, frame_env)) for ref in refs)
