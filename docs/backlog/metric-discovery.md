# Metric-name discovery — lazy default, explicit complete scan, upstream TODO

The metric-**picker** is deferred; the core value display uses a hand-configured **objective** and
needs none of this. Captured so the honest-UX shape and the upstream question aren't re-derived later.

## The problem

The cockpit shows one scalar per run — the configured objective, `latest(Topic.VALUE,
name=objective)`. To let a user *pick* a different metric, the picker needs the list of metric
names a run reports. There is **no cheap public way to enumerate names**: `latest(VALUE, name=…)`
needs a name you already have, and the only enumerator is `value_series(channel).keys()` — the
full-log O(N) `VALUE` scan the core bans on the hot path.

**And the peek itself is not O(1) on a miss** (measured; this doc previously claimed otherwise).
Both backends index `(topic, seq)` with no `name`, so `latest` post-filters the name while walking
the topic partition: 0.01 ms for a name at the tail, **~85 ms at 500k value records** for one that
is absent, rare, or emitted early. That is the same scan the picker's enumeration problem is made
of, which means **the two halves share one upstream fix** — see below.

## The shape (when the picker ships)

1. **Lazy discovery (default, cheap).** Accumulate names from the value records the cockpit reads
   over time — a bounded, opt-in incremental cursor over *unfiltered* value records
   (`read(topics=[VALUE], after=last_seen)`), filling the menu as metrics appear. Bounded per tick;
   runs only while a picker is open. **Caveat (the reason this is a design note):** it only works if
   the read is *not* narrowed to the requested name — the core's own value read is the filtered
   `latest(VALUE, name=objective)`, which never sees the other names, so discovery is a *separate*
   read.
2. **The lazy set is a lower bound — say so.** A metric logged before the cockpit attached, or one
   simply not yet emitted this session, is absent. The picker must **label the list "seen so far
   (partial)"** and never present it as complete — the keystone (surface uncertainty, never
   mislead) applied to discovery.
3. **Explicit complete scan (opt-in, marked expensive).** For the definitive list, offer a
   user-initiated **full-log scan** (`read(topics=[VALUE])` collecting distinct `.name`), clearly
   marked expensive (O(N), ~seconds on a 10⁶-record log). This is **not** the banned per-frame
   `value_series` replay — it is a deliberate, one-shot, off-the-hot-path action the user asked for
   and was warned about.

## Upstream — filed as runstate#19

runstate has **no name-aware index**, which is one root under two symptoms: the peek can't seek a
name, and there is no index-served way to enumerate names. An index on `(topic, name, seq)` serves
both — the seek becomes real, and `SELECT DISTINCT name` makes the complete list an index scan
rather than a full-log replay, letting the picker be **complete-by-default** and retiring the
lazy/partial machinery above before it is ever built.

Filed as **runstate#19** (with the peek cost as the motivating measurement) rather than held back
under the usual demand-as-evidence discipline, because the evidence arrived without the picker: the
peek's miss case is already a per-frame cliff in shipped code. When #19 lands, revisit steps 1–3
above — most of the lazy-discovery design exists only to route around the missing index, and per
the no-legacy directive it should be deleted rather than kept alongside.
