# Metric-name discovery — a picker sourced by `streams()`

The metric-**picker** is deferred; the core value display uses a hand-configured **objective** and
needs none of this. Captured so the shape and its limits aren't re-derived later.

## The source exists upstream

runstate 0.4.0's `streams(ch, *, head)` returns `Streams(eager, sources)`: the names with a
committed value on the head's lineage, each by its committing claim's declaration. That is the
complete list the picker needs, from the public API. It is a one-shot read that walks the lineage,
O(lineage), so it belongs to a user action on one run (opening the picker), never to the per-frame
path. The lazy "seen so far" discovery this entry used to design existed only to route around a
missing enumerator, and is deleted rather than built.

## What the list still cannot say

- **A name not yet emitted.** No enumeration can name it, so free text stays beside the picker
  ([`interactive-objective`](interactive-objective.md) is the sink both feed).
- **Names off the head's lineage.** A value committed on an abandoned branch (a rewind, a resume
  from an older checkpoint) is not on the head's lineage, and `streams` does not list it. That is
  the right list for a value column that reads the head, and worth saying in the picker.
- **Sources versus eager streams.** The value column reads an eager stream (`LastElement` counts a
  name its claim declared a source as nothing). A source's samples are answers to a subscription,
  read by request id (`answers`), so the picker offers `eager` only, until the column has a reading
  for a source. Raw-sent registers outside every lineage (a consumer's `status` value) appear in
  neither set. Whether runstate reads either publicly is
  [runstate#67](https://github.com/GeoffChurch/runstate/issues/67).

## Across a group

`streams` is per run. A fleet-wide picker would union it over the group, at O(lineage) per run, a
cold cost to measure before it is offered for a large group. An index-served `SELECT DISTINCT
name` (runstate#19 landed the `(topic, name, seq)` index) would be cheaper but is not public, and
would also list names off the lineage.
