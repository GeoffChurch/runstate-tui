# Interactive objective — set the value metric from inside the cockpit

`--objective NAME` fixes the value column's metric at launch. Changing it without restarting is the
obvious next want, and the mechanism is nearly free — but it is **gated on runstate#19**, and the
gate is the whole story.

## Why it's gated

`latest(VALUE, name=objective)` is an index seek only when the name sits near the tail of the value
partition. Neither backend indexes `name`, so a name that is absent, rare, or not yet emitted costs
a full partition walk: **0.01 ms vs ~85 ms at 500k value records**, per run, per tick, on the owner
thread. Across a 100-run fleet that is ~8.5 s/frame against a ~5 ms budget — the watchdog trips and
the teardown drain leaks the pool.

Choosing a name once at launch survives this: you notice and restart. **Typing names interactively
makes the miss case routine**, and the miss is precisely the interesting case — "show me the metric
that hasn't appeared yet" is a use, not a mistake. No consumer-side mitigation works: a negative
cache can't know when a metric first appears, and a `last_seq()` gate doesn't help live runs, which
are the ones being watched.

So: build this when **runstate#19** lands. Not before, and not scoped-down to route around it —
that scoping would be the superseded path the no-legacy directive exists to prevent.

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
then) it stops folding independently altogether: it currently re-folds the same run **outside the
pool**, cold-opening the channel every tick, while the owner thread already folds that run's `Row`
with a warm pooled handle. If the card consumed the frame instead, this bug could not exist.

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
- Commit on `Input.Submitted`, never `Changed`: on `Changed`, every keystroke prefix is a miss —
  i.e. a full scan per run per keystroke.

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

What is *not* true — and was the original argument for doing this before
[`metric-discovery`](metric-discovery.md) — is that the setter is upstream-independent. Both halves
want the same `(topic, name, seq)` index, so runstate#19 is the shared prerequisite and neither
strictly precedes the other.

## Out of scope

- **Per-group or per-attribute objectives.** Grouped sections could plausibly want different
  metrics; nothing has asked.
- **Persisting the objective** across restarts. No config file exists and none is warranted.
- **A runtime `--stuck-threshold`.** Not the same mechanism: its positivity invariant lives in
  `CliArgs`, so an interactive setter would bypass it — a typed `0` makes the whole fleet read
  `stale` with no error. It would first have to move into `Env`.
