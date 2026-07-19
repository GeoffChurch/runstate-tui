from __future__ import annotations

import asyncio
import threading

from textual import work
from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import DataTable, Static

from .detail import _TEARDOWN_ERRORS, DrillDownScreen
from .env import Env
from .pool import ChannelPool, Table, fold_frame
from .resolver import Resolver, RunRef, ref_key
from .types import Row, Severity

_COLUMNS = ("run", "status", "step", "age", "value", "elapsed", "!")

# How long on_unmount waits for the owner thread to finish its in-flight fold before
# giving up and LEAKING the pool (see on_unmount) rather than hanging quit forever on a
# wedged thread.
_DRAIN_TIMEOUT = 5.0


def _marker(row: Row) -> str:
    """A compact per-row severity glyph (keeps the table below the ISA-18.2 flood line).
    row.severity already folds status + issues (CORRUPT/UNREADABLE -> HIGH)."""
    stops = f"⏹{len(row.undischarged_stops)}" if row.undischarged_stops else ""
    if row.severity >= Severity.HIGH:
        return f"⚠⚠{stops}"
    if row.severity >= Severity.MEDIUM:
        return f"⚠{stops}"
    return stops


def _cells(ref: RunRef, row: Row) -> tuple[str, str, str, str, str, str, str]:
    """The 7 column cells — same field semantics as format_row, one field per column."""
    run_id = ref[0]
    status = row.status.label + (f": {row.status.detail}" if row.status.detail else "")
    step = "" if row.frontier is None else str(row.frontier)
    age = "" if row.freshness is None else f"{row.freshness:.0f}s"
    if row.value is None:
        value = ""
    else:
        name, val, vstep = row.value
        value = f"{name}={val}" + (f"@{vstep}" if vstep is not None else "")
    elapsed = "" if row.elapsed is None else f"{row.elapsed:.0f}s"
    return (run_id, status, step, age, value, elapsed, _marker(row))


class TableReady(Message):
    def __init__(self, table: Table) -> None:
        self.table = table
        super().__init__()


class MultiRunApp(App[None]):
    CSS = "#stall { color: $warning; height: auto; }"

    def __init__(
        self,
        resolver: Resolver,
        env: Env,
        *,
        tick_interval: float = 1.0,
        pool_cap: int = 128,
        stall_ticks: int = 3,
    ) -> None:
        super().__init__()
        self._resolver = resolver
        self._env = env
        self._tick_interval = tick_interval
        self._pool = ChannelPool(cap=pool_cap)
        self._stall_after = stall_ticks * tick_interval
        self._last_ready: float | None = None
        self._closing = False
        # Set whenever the owner thread is NOT inside _fold_frame's real work; on_unmount
        # drains this (not Worker/WorkerManager) before touching the pool -- verified
        # empirically that `await self.workers.wait_for_complete()` does NOT wait for the
        # OS thread: Textual's shutdown calls `workers.cancel_all()` *before* dispatching
        # Unmount, which cancels the asyncio wrapper Task; `worker.wait()` then raises
        # WorkerCancelled within the same event-loop tick, while the real OS thread (which
        # cancellation cannot touch) is still mid-fold -- an uncaught exception AND a
        # use-after-close race, not the drain the brief's rationale required.
        self._idle = threading.Event()
        self._idle.set()

    def compose(self) -> ComposeResult:
        yield Static("", id="stall")  # the watchdog banner (hidden via display, see on_mount)
        yield DataTable(id="runs")

    def on_mount(self) -> None:
        t = self.query_one("#runs", DataTable)
        # explicit (label, key) tuples; an unkeyed add_columns yields anonymous
        # ColumnKey(None) and every later update_cell/sort fails deterministically.
        t.add_columns(*[(c, c) for c in _COLUMNS])
        t.cursor_type = "row"
        # Static("").update("") still reserves a 1-line height in Textual 8.2.8 (verified
        # empirically) -- an empty renderable is NOT the same as no line, so the banner
        # must be display-toggled, not just text-cleared, to truly disappear.
        self.query_one("#stall", Static).display = False
        # MAIN-thread, independent of the owner thread — a wedged owner thread
        # can't also freeze the watchdog.
        self.set_interval(self._tick_interval, self._on_watchdog)
        # Baseline BEFORE the first tick: if the owner thread wedges on its very FIRST
        # fold (e.g. all runs on a hung mount at launch), TableReady never fires and
        # _last_ready would otherwise stay None forever -- _is_stalled() treats None as
        # "not stalled" (see below), so the banner would never trip and a permanently
        # blank table would give no signal at all (the exact §10 failure the watchdog
        # exists to prevent, just at t=0 instead of mid-session). Seeding it here makes
        # _is_stalled() measure from mount time; a healthy fast first frame overwrites it
        # in on_table_ready before the stall window elapses, so this never fires falsely.
        self._last_ready = self._env.clock()
        self._tick()

    def _tick(self) -> None:
        # ONLY on_mount and _fold_frame's own tail may call this. That self-reschedule
        # chain is the real serialization — exclusive=True does NOT serialize thread workers.
        if not self._closing:
            self._fold_frame()

    @work(thread=True, exclusive=True)
    def _fold_frame(self) -> None:  # the single owner thread — owns the whole pool
        # clear _idle as the FIRST statement -- before even the _closing check -- so
        # on_unmount's drain can never observe "idle" while this invocation still might
        # touch the pool below.
        self._idle.clear()
        try:
            if self._closing:
                return
            now = self._env.clock()
            table = fold_frame(self._pool, self._resolver(now), self._env, now)
            try:
                self.post_message(TableReady(table))  # post_message is thread-safe on its own
            except _TEARDOWN_ERRORS:
                pass  # a quit landed mid-frame; drop the marshal
            # Reschedule on the SUCCESS path (after post_message), NOT the finally. fold_frame
            # is total -- a per-run fold bug is contained to a loud fold-error row -- so the
            # only way to skip this reschedule is a CATASTROPHIC non-fold bug (e.g. a broken
            # resolver): that exception propagates, the loop stops, and exit_on_error (default
            # True, matching SingleRunApp) crashes the cockpit loudly -- fail-fast, §10 "a
            # crash is not a freeze". Guarded on _closing so teardown never re-arms the loop;
            # wrapped in _TEARDOWN_ERRORS so a quit landing mid-marshal is dropped, not raised.
            if not self._closing:
                try:
                    self.call_from_thread(self.set_timer, self._tick_interval, self._tick)
                except _TEARDOWN_ERRORS:
                    pass
        finally:
            self._idle.set()  # always -- even on an early return or an uncaught raise

    def on_table_ready(self, msg: TableReady) -> None:  # MAIN thread: keyed reconcile
        self._last_ready = self._env.clock()
        t = self.query_one("#runs", DataTable)
        want = {ref_key(ref) for ref, _ in msg.table}
        sel = None
        if t.row_count:
            sel = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        with self.batch_update():  # App.batch_update — DataTable has none
            # every row we ever add carries an explicit str key (ref_key(ref)), so
            # k.value is never None in practice; the filter satisfies mypy --strict
            # (RowKey.value is typed str | None) without changing behavior.
            present = {k.value for k in list(t.rows.keys()) if k.value is not None}
            for key in present:
                if key not in want:
                    t.remove_row(key)
            present &= want
            for ref, row in msg.table:
                key = ref_key(ref)
                cells = _cells(ref, row)
                if key in present:
                    for col, val in zip(_COLUMNS, cells, strict=True):
                        t.update_cell(key, col, val)
                else:
                    t.add_row(*cells, key=key)
                    # a resolver that yields the same ref twice this frame must UPDATE the
                    # row it just added, never re-add it (add_row on a live key raises
                    # DuplicateKey) -- so mark it present the instant it is added.
                    present.add(key)
            t.sort("run")
            if sel is not None and sel in want:
                # sort() doesn't track the selected row key; restore it explicitly.
                t.move_cursor(row=t.get_row_index(sel))

    def _is_stalled(self) -> bool:
        if self._last_ready is None:
            return False
        return self._env.clock() - self._last_ready > self._stall_after

    def _on_watchdog(self) -> None:
        banner = self.query_one("#stall", Static)
        if self._is_stalled():
            banner.update("⚠ I/O stalled")
            banner.display = True
        else:
            banner.display = False

    def on_data_table_row_selected(self, message: DataTable.RowSelected) -> None:
        # `enter` opens the drill-down for the selected row -- but DataTable itself
        # binds `enter` -> action_select_cursor (which posts this RowSelected message)
        # when cursor_type="row", and the focused DataTable intercepts the key BEFORE
        # it can bubble to an App-level BINDINGS entry (confirmed empirically: an
        # App-level ("enter", "detail", ...) binding never fires while the table is
        # focused). So drill-down hooks the table's own selection message instead of
        # trying to rebind `enter` at the App level -- the idiomatic Textual pattern
        # for row-cursor tables, and the SingleRunApp precedent (no DataTable there,
        # so its App-level `enter` binding works) doesn't apply here.
        self.action_detail()

    def action_detail(self) -> None:
        t = self.query_one("#runs", DataTable)
        if t.row_count == 0:
            return
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        if key is None:  # every row's key is ref_key(ref) (str); defensive only
            return
        # reconstruct from the resolver rather than touching the owner-thread pool.
        by_key = {ref_key(r): r for r in self._resolver(self._env.clock())}
        ref = by_key.get(key)
        if ref is not None:
            self.push_screen(DrillDownScreen(ref, self._env, self._tick_interval))

    async def on_unmount(self) -> None:
        self._closing = True
        # drain the in-flight fold via the threading.Event, NOT workers.wait_for_complete()
        # -- see _idle's docstring in __init__ for why the latter doesn't actually wait for
        # the OS thread. run_in_executor awaits the blocking Event.wait() off the event loop,
        # BOUNDED by _DRAIN_TIMEOUT so a wedged owner thread can't hang quit forever.
        drained = await asyncio.get_running_loop().run_in_executor(
            None, self._idle.wait, _DRAIN_TIMEOUT
        )
        if drained:
            self._pool.close_all()  # owner thread idle -> safe to close the handles
        # else: the owner thread is STILL mid-fold past _DRAIN_TIMEOUT. Closing a sqlite
        # connection out from under an in-flight read is undefined behavior, so we LEAK the
        # pooled handles rather than close_all() into a live reader; the OS reclaims the fds
        # at process exit. A bounded quit that leaks beats a use-after-close race on a
        # wedged thread.
