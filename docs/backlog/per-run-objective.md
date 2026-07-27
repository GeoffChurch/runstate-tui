# Per-run objective — let the manifest declare which metric each run reports

`--objective NAME` sets one metric for the whole table. That is right when every run reports the
same thing and wrong when they don't: a sweep can legitimately contain run classes that track
*different* metrics — UBLI runs with ground-truth labels report `P@1`, runs without report a
GT-free proxy, and neither is the other's substitute.

## The invariant it changes

Not "the value column is comparable" — **comparable within a section, not necessarily across
sections.** The grouped table already makes a section the unit of comparison (each gets its own
fleet roll-up), so run classes that track different metrics land in different sections and each
column stays internally meaningful. Heterogeneity across sections becomes a feature rather than a
defect; heterogeneity *within* one is still a smell, and grouping on the attr that distinguishes
the classes is what prevents it.

## The shape — a sibling field, not an attr

```json
{"run_id": "a3f…", "root": "outputs/runs/a3/a3f…", "backend": "sqlite",
 "attrs": {"scenario": "en-ru-16M", "labels": "with-gt"},
 "objective": "mean_p1"}
```

`Attrs` is **grouping and labeling only, never fold input** — the resolver may say which runs to
show and what to call them, never what is true about them. If attrs could reach the fold, whoever
writes the manifest could change what a run's status *is*: a crashed run made to display `done` by
editing a JSON file, never touching the run.

Choosing a metric to display is a gray case — it selects among facts the run itself reported rather
than falsifying one — but it *is* fold input (`read_value(channel, objective)`). Putting it in
`attrs` would be the first exception to a rule that is currently exception-free, and would leave
every future reader to work out which attrs are labels and which secretly steer the fold. A sibling
field costs one line of schema and keeps the rule literally true.

## Precedence

`--objective` (global override) > manifest `objective` (per-run) > `None` (blank).

One override, three sources. The deferred interactive setter
([`interactive-objective`](interactive-objective.md)) is the *same* override arriving from a
keystroke, so it stacks on this without a second mechanism.

## Rejected: auto-detect

`latest(VALUE, name=None)` is genuinely index-served (0.01 ms at 500k value records — it is the
query `(topic, seq)` was built for), so "show whatever this run reported last" is cheap and
zero-config. It is still wrong: it produces heterogeneity you did not *choose* — whichever metric
happened to be written last — which is strictly worse than heterogeneity you declared.

The tempting compromise, auto-detect in the single-run view only (where comparability is vacuous
with one row), is worse still: **single-run is the table at the singleton resolver**, so differing
defaults between the two views would break that identity for a convenience.

## Gate

The manifest producer that would emit this field doesn't exist yet — building the consumer first is
guessing at the producer. Land it alongside the first real emitter (mycooc's is spec'd in that
repo's backlog), which is also what will show whether per-run objectives are wanted in practice or
whether grouping plus a global override covers every case that actually arises.
