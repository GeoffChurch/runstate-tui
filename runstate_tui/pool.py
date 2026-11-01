from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace

from runstate import Channel, LogFormatError, RunNotFound, attach_channel

from .env import Env
from .fold import RunFolds
from .resolver import Attrs, RunRef, sqlite_log_path
from .table import _OPEN_ERRORS, EVICT_KINDS, _bare, _fold_error, fold_open_channel, open_row_for
from .types import Row, Status

Table = tuple[tuple[RunRef, Attrs, Row], ...]


@dataclass(frozen=True)
class _Pooled:
    """An open channel and the folds held beside it: evicted together, so a re-opened
    run is folded from scratch, never resumed from another handle's partial state."""

    channel: Channel
    folds: RunFolds


class ChannelPool:
    """Owner-thread-ONLY LRU pool of open channels and their held folds, keyed by the
    full RunRef. reader == evictor: a mid-fold channel is never closed under it. NOT
    thread-safe by design — one owner thread touches it (opens, reads, evicts, closes)."""

    def __init__(self, cap: int = 128) -> None:
        self._cap = cap
        self._open: OrderedDict[RunRef, _Pooled] = OrderedDict()

    def __len__(self) -> int:
        return len(self._open)

    def _evict(self, ref: RunRef) -> None:
        pooled = self._open.pop(ref, None)
        if pooled is not None:
            pooled.channel.close()

    def _evict_oldest(self) -> None:
        _ref, pooled = self._open.popitem(last=False)
        pooled.channel.close()

    def reconcile(self, live: set[RunRef]) -> None:
        """Close + drop any pooled run no longer resolved this frame."""
        for ref in [r for r in self._open if r not in live]:
            self._evict(ref)

    def row_for(self, ref: RunRef, frame_env: Env) -> Row:
        run_id, root, backend = ref
        pooled = self._open.get(ref)
        if pooled is None:
            # cold open via attach_channel: never fabricate a phantom db nor mutate a
            # foreign one. A run with no records (missing/empty/foreign valid db) raises
            # `RunNotFound` -> honest `missing`; a log in another format a `LogFormatError`
            # -> the loud format row; corrupt/non-sqlite `_OPEN_ERRORS` -> `unreadable`.
            # None is cached, so each is retried next tick (a run migrated meanwhile opens).
            try:
                ch = attach_channel(run_id, root=root, backend=backend)
            except (RunNotFound, LogFormatError, *_OPEN_ERRORS) as exc:
                return open_row_for(exc)
            if len(self._open) >= self._cap:
                self._evict_oldest()
            pooled = _Pooled(ch, RunFolds.start(objective=frame_env.objective))
            self._open[ref] = pooled
        elif backend == "sqlite":
            # A CACHED sqlite handle keeps reading off its still-open inode even after the
            # .db is unlinked out from under a live cockpit (WAL: the fd persists), so the
            # fold would silently serve a phantom read that never self-heals. attach_channel
            # cannot re-check an already-open handle — a per-tick stat is the ONLY detector
            # for the vanished-cached-file case (a finding: no public "does this handle's
            # log still exist"). Missing -> evict + honest `missing`; an unreadable dir ->
            # evict + `unreadable`. (Cold opens above skip this: attach covers them.)
            try:
                sqlite_log_path(ref).stat()
            except FileNotFoundError:
                self._evict(ref)
                return _bare(Status.missing())
            except OSError:
                self._evict(ref)
                return _bare(Status.unreadable())
        self._open.move_to_end(ref)  # LRU: most-recently-used last
        row = fold_open_channel(pooled.channel, frame_env, pooled.folds)
        if row.status.kind in EVICT_KINDS:
            self._evict(ref)  # the handle and its folds go together
        return row

    def close_all(self) -> None:
        for pooled in self._open.values():
            pooled.channel.close()
        self._open.clear()


def fold_frame(pool: ChannelPool, items: list[tuple[RunRef, Attrs]], env: Env, now: float) -> Table:
    """One owner-thread frame. Reconcile the pool to the resolved refs, then fold every
    DISTINCT run fresh under a single per-frame `now` (via a frozen-clock Env so objective/
    threshold/liveness carry through). A run named by several items (a shared cell -> one
    RunRef) folds ONCE and its Row is reused, so shared runs cost one open channel and one
    read. The row for `r` == render_single(r) at this `now`."""
    frame_env = replace(env, clock=lambda: now)
    refs = [ref for ref, _ in items]
    pool.reconcile(set(refs))
    # fold_frame is TOTAL: an EXPECTED fold failure is already a loud row (missing/
    # unreadable/corrupt/malformed), so any exception escaping row_for is a genuine
    # internal bug on ONE run. Contain it to a loud per-run `error` row rather than let
    # it sink the whole frame — the table survives; the worker stays fail-fast for
    # catastrophic non-fold bugs (a broken resolver/reconcile, which are outside this loop).
    folded: dict[RunRef, Row] = {}
    out: list[tuple[RunRef, Attrs, Row]] = []
    for ref, attrs in items:
        if ref not in folded:
            try:
                folded[ref] = pool.row_for(ref, frame_env)
            except Exception as exc:  # noqa: BLE001 — internal fold bug -> loud per-run row, table survives
                folded[ref] = _fold_error(exc)
        out.append((ref, attrs, folded[ref]))
    return tuple(out)
