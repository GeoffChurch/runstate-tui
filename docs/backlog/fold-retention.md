# Fold retention — groups larger than the pool

Under log format 0.4.0 a run's row is read through held folds (runstate's `HeadFold`, `PrefixLen`,
`LastElement`, `LiveDemand`, `PendingStops`) that live beside its channel in the LRU pool. Warm, a
row costs ~0.1 ms whatever the log's size, because each poll reads only the records since the last.
Cold, the first poll reads the log once: ~10 µs per record, ~35–45 ms for a 3.6k-record run
(mycooc's median log; measured, with an objective set). Held, the folds keep ~140 B per record
(`PrefixLen` and `LastElement` hold a per-node map), ~0.5 MB for that run.

So a group within the pool cap (128) pays the cold cost once, on its first frame, and is cheap from
then on. A group **larger** than the cap evicts and cold-opens runs every frame, and each cold open
now re-folds that run's log. That was cheap under 0.3.0, whose reads were point lookups, and is not
now: a glob over mycooc's whole `outputs/` (6,121 runs) would cost minutes per frame.

## What is open

- **Retain folds past their channel.** The pool's cap is set by file descriptors (3 per sqlite
  channel), but a fold's cursor is a seq, not a handle, so folds could outlive their channel in a
  second, memory-bounded LRU. At ~0.5 MB per median run that is a memory budget, not an fd one,
  and it makes the cap two numbers. Decide against a real group above 128 runs.
- **Spread the first frame.** A group's first frame folds every run cold on the owner thread, so a
  100-run group of median runs takes ~4 s and trips the stall watchdog for a few ticks. Folding at
  most k cold runs per frame, with the rest shown as an honest "loading" row, would keep frames
  inside the budget; it adds a status.
- **Upstream.** The count is cold O(lineage) because `count(Commits(), head)` needs the lineage, and
  `PrefixLen` keeps the count at every node, not only the head's. A head-only variant, or a commit
  that carries its own count, would make the frontier's cold read O(1). This is a runstate
  question, recorded as a finding of the 0.4.0 upgrade.
