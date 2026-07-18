# runstate-tui backlog

Deferred features and their *committed seams*. An entry here means the core design left a place
for the feature to slot in additively — the implementation waits for its first real need.

- [liveness-overlay](liveness-overlay.md) — external liveness probes (`os.kill` same-host;
  `squeue`/`kubectl` cross-host). Seam committed in the core spec §2.1/§14.2; core is
  freshness-only.
- [metric-discovery](metric-discovery.md) — lazy metric-name discovery (default, labeled
  *partial*) + an explicit expensive full-log scan for completeness; upstream TODO to investigate a
  runstate name-enumeration API, filed only on demand.
