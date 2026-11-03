# Fold retention — groups larger than the pool

Under log format 0.4.0 a run's row is read through held folds (runstate's `HeadFold`, `PrefixLen`,
`LastElement`, `LiveDemand`, `PendingStops`) that live beside its channel in the LRU pool. Warm, a
row costs ~0.35 ms whatever the log's size, because each poll reads only the records since the last.
Cold, the first poll reads the log once: ~54 ms median, ~100 ms p90 (measured 2026-11-02 on 120 runs
sampled from mycooc's 6,125 format-0.4.0 logs, median 3.6k records, with an objective set). Held, the
folds keep ~0.7 MB for a median run.

So a group within the pool cap (128) pays the cold cost once, on its first frame, and is cheap from
then on. A group **larger** than the cap evicts and cold-opens runs every frame, and each cold open
re-folds that run's log. That was cheap under 0.3.0, whose reads were point lookups, and is not now.
Such groups are real: 9 of mycooc's experiments exceed the cap, the largest (`h2h_s3_v5k`) with 904
runs, and `outputs/runs` holds 6,041. A frame that visits 904 runs in order misses the LRU on every
one, so it re-folds them all, ~49 s a frame.

## Where a cold row goes

| Fold, cold | Median |
|---|---|
| `LastElement` (the objective) | ~38 ms |
| `LiveDemand` | ~7 ms |
| `HeadFold` | ~5 ms |
| `PrefixLen(Commits())` | ~5 ms |
| the unheld reads | <1 ms |

`LastElement` reads every value record (~3,300 under 27 names in a median mycooc run, ~150 of them the
objective's) to learn names it takes from envelope fields, and `read()` decodes every body.

## The plan: [#36](https://github.com/GeoffChurch/runstate-tui/issues/36)

- **Cache an ended run's row by its `last_seq`.** 119 of the 120 sampled runs had ended. A terminal
  verdict is a pure function of an append-only log, so the row cannot change until the log grows. The
  cache holds the row's log-derived inputs (a few hundred bytes) and drops the run's folds and channel.
  A frame re-checks each cached run (the re-attach, ~0.36 ms) and recomputes only the `now`-dependent
  fields. A non-terminal run keeps its held folds in the pool.
- **Spread the first frame.** A group's first frame folds every run cold on the owner thread, so a
  100-run group of median runs takes ~5 s and trips the stall watchdog for a few ticks. Folding at
  most k cold runs per frame, with the rest shown as an honest "loading" row, keeps frames inside the
  budget; it adds a status.

## The alternatives

- **Retain folds past their channel**, in a second, memory-bounded LRU: a fold's cursor is a seq, not a
  handle. It keeps every run warm at ~0.7 MB each, ~630 MB for the 904-run group and ~4.2 GB for all of
  `outputs/`, and most of those runs will not change. The ended-row cache gets the same frames for a few
  hundred bytes a run.
- **Upstream.** [runstate#66](https://github.com/GeoffChurch/runstate/issues/66) asked for an O(1) cold
  commit count, and was closed on the table above: the count is ~5 ms of ~54. The lever on the cold read
  itself is reading envelope fields without decoding bodies, from ~54 ms to ~30 ms, a constant factor
  (GeoffChurch/runstate's `docs/backlog/header-reads.md`). Each run's first read would remain, once per
  session.
