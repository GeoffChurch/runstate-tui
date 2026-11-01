# Interactive objective — set the value metric from inside the cockpit

`--objective NAME` fixes the value column's metric at launch. Changing it without restarting is the
obvious next want, and the mechanism is nearly free. It was gated on runstate#19, whose premise is
gone; what gates it now is a measured cold cost.

## The gate: one cold fold per run per name

The value column is no longer a `latest(VALUE, name=…)` peek. Under log format 0.4.0 it is the
objective's **last element on the head's lineage**, read through runstate's held `LastElement(name)`
fold, which lives beside each run's channel and reads only the records since the last tick. So a
name that is absent or not yet emitted costs O(new records) per tick, like any other: the 0.3.0
miss cliff (~85 ms per run per tick at 500k value records) is gone. (runstate#19 also landed the
name-aware index, but the cockpit no longer reads the value by `latest`.)

What a new name costs instead is a **cold** fold: a fresh `LastElement` reads every value record
on the log once. Measured: ~17 ms for a 3.6k-record run (mycooc's median log), and it holds
~100 B per record for as long as it is pooled. So a typed name costs one cold pass per pooled run,
on the owner thread, once, not per tick. Across a 100-run group of median runs that is ~2 s of one
frame, which the stall watchdog shows as `⚠ I/O stalled` for a tick. That is acceptable for an
action the user took on `Submitted`, and it argues for:

- building the new folds **beside** the old ones and swapping when they are warm, so the column
  never shows a frame folded with half the runs on the new name; and
- dropping the old name's folds at the swap, since each holds memory proportional to its log.

## The invariant it must establish

Today `Env` is effectively a session constant. Making the objective interactive makes it vary, and
the rule that keeps that honest is **not** "sample Env per tick" — that is too weak. It permits the
summary strip to label a frame with an objective the frame was never folded with, since the worker
samples at fold time and `on_table_ready` paints later off `self._env`. Under a stall the strip
claims a metric the visible rows don't carry: the "quietly shows a false state" failure the
integrity keystone exists to prevent.

The rule that holds:

> **The Env a frame was folded with travels on the frame.**

`TableReady` carries `(table, env)`; nothing re-reads an Env at paint time. This subsumes the
narrower fixes — `_fold_frame`'s two separate `self._env` reads, and the strip's paint-time read —
rather than patching them individually.

It must also cover state *derived* from Env, not just Env references. The ring buffer planned in
[`value-trend`](value-trend.md) accumulates `Row.value` across ticks; change the objective mid-run
and it silently splices two metrics into one curve. `Row.value` carries its own name, so the buffer
can key on it — but only if the rule says derived state must be keyed by the Env that produced it.

Note the tempting precedent is **backwards**: `fold_frame`'s `replace(env, clock=lambda: now)`
exists to *remove* time-variation (freeze the clock so a frame is internally consistent), and
`Env.clock` is a test-injection seam — every test passes a fake, production is always `time.time`.
Read correctly it argues for carrying a snapshot with the frame, which is the invariant above.

## The real bug it must fix

`DrillDownScreen.__init__` stores an `Env` and re-folds its summary card from the stored copy; both
apps construct it with a snapshot. Harmless while Env is constant — a latent capture-a-mutable-value
bug the moment it isn't, and a drill-down held open across a change would show the old objective
forever. Either the screen takes the Env source rather than a value, or (better, and worth deciding
then) it stops folding independently altogether: it holds its own folds (`HeldRun`) and re-opens the
channel every tick **outside the pool**, while the owner thread already folds that run's `Row` with
a warm pooled handle and folds of its own. So the same run is folded twice, and its folds' memory is
held twice. If the card consumed the frame instead, this bug could not exist.

## Verified mechanics (Textual 8.2.8) — do not re-derive

- A `display: none` `Input` is still **focusable** (`focusable` gates on `visible`, not `display`),
  so `AUTO_FOCUS` grabs it at mount. `SingleRunApp` composes no other focusable widget, so adding a
  hidden Input **silently kills its `s` and `enter` bindings**. Needs `AUTO_FOCUS = ""`.
- `Input` messages **bubble past the handling screen to the App**. The drill-down's log filter would
  therefore drive a naive app-level `on_input_submitted` — pressing `enter` in the filter would set
  the fleet objective to the filter text. Discriminate on `msg.input.id`.
- App-level bindings fire **while a screen is pushed**. Pressing the key inside a drill-down does
  nothing visible but arms the prompt on the base screen, which is revealed and focused on `escape`.
  Guard on the screen stack, or make the prompt a `ModalScreen` (verified to block app bindings).
- `MultiRunApp` never calls `focus()`; the `DataTable` wins only by being the sole focusable widget.
  Compose order becomes load-bearing — the Input must come **after** the table.
- `v` is free: not in `DataTable.BINDINGS`, and a subclass `BINDINGS` merges with the App defaults
  rather than replacing them, so `ctrl+q` survives.
- Commit on `Input.Submitted`, never `Changed`: on `Changed`, every keystroke prefix is a new name —
  i.e. a cold fold per run per keystroke.

## Blast radius

13 `DrillDownScreen` construction sites (2 production, 11 test) if its signature changes; 8
`format_fleet_summary` call sites if the strip gains an objective chip, plus `test_format.py`'s
assertion that the colored span ends within the first two characters — which constrains the chip to
**append**, not prepend. Two stored snapshots and five showcase images would need regenerating.

## Why the setter, not a picker, is the shape

A picker is a name *source*; `set_objective(name)` is a sink with no opinion about where the name
came from, so building the sink first costs the picker nothing — it becomes a second caller. And
free text stays permanently, for a structural reason rather than a contingent one: **no enumeration
can name a metric that has not been emitted yet**, and naming one before it appears is a real want.

Neither half now waits on runstate: the setter's cost is the cold fold above, and the picker's
source is runstate's `streams()` (see [`metric-discovery`](metric-discovery.md)).

## Out of scope

- **Per-group or per-attribute objectives.** Grouped sections could plausibly want different
  metrics; nothing has asked.
- **Persisting the objective** across restarts. No config file exists and none is warranted.
- **A runtime `--stuck-threshold`.** Not the same mechanism: its positivity invariant lives in
  `CliArgs`, so an interactive setter would bypass it — a typed `0` makes the whole fleet read
  `stale` with no error. It would first have to move into `Env`.
