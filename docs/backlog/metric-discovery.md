# Metric-name discovery — lazy default, explicit complete scan, upstream TODO

**Status:** deferred. The metric-**picker** is a Stage-3+ feature; the core value display uses a
hand-configured **objective** and needs none of this. Captured so the honest-UX shape and the
upstream question aren't re-derived later.

## The problem

The cockpit shows one scalar per run — the configured objective, `latest(Topic.VALUE,
name=objective)`, O(1). To let a user *pick* a different metric, the picker needs the list of
metric names a run reports. There is **no cheap public way to enumerate names**: `latest(VALUE,
name=…)` needs a name you already have, and the only enumerator is `value_series(channel).keys()`
— the full-log O(N) `VALUE` scan the core bans on the hot path.

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

## Upstream TODO (investigate; do not file yet)

runstate has **no index-served name enumeration**. A public `channel.names(topic)` (or a
distinct-`name` index over the `VALUE` topic) would make the complete list cheap — collapsing
step 3 from O(N) to an index seek and letting the picker be complete-by-default. **Investigate**,
and file against runstate **only when the picker exists and the O(N) scan actually proves painful**
— demand as evidence, not speculation (runstate's own `third-party-observer` discipline; cf. its
`cockpit.md`).
