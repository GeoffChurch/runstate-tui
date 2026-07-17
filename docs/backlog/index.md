# runstate-tui backlog

Deferred features and their *committed seams*. An entry here means the core design left a place
for the feature to slot in additively — the implementation waits for its first real need.

- [liveness-overlay](liveness-overlay.md) — external liveness probes (`os.kill` same-host;
  `squeue`/`kubectl` cross-host). Seam committed in the core spec §2.1/§14.2; core is
  freshness-only.
