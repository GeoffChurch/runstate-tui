# `postgres` resolver — runs whose logs live on a server

The design names a `postgres` resolver beside `explicit`, `glob` and `manifest` (`design.md`, "The
three units"). Today a Postgres-backed run is reachable only through a `manifest` row naming
`backend: postgres` and the DSN as its root; nothing discovers one, since a directory walk cannot
see a table.

## Why it's gated

No consumer's run logs are on Postgres yet. mycooc's coordination server holds its scheduler's job
tables (its Postgres plan's stage 2), but its runs still write sqlite channels
(`RUNSTATE_CHANNEL_BACKEND=sqlite`). Stage 3 of that plan, live channels on runstate's
`PostgresChannel`, is what moves them ([mycooc#2](https://github.com/GeoffChurch/mycooc/issues/2)),
and from then on `glob` cannot find a live mycooc run. **Build it when mycooc's stage 3 lands**, against a real server: runstate's test suite starts throwaway
ones, and so can this repo's.

## Open

- **Discovery.** Listing the runs in a format's schema (`runstate_v0_4_0`) is a query the address
  scheme does not export, the same gap as a sqlite root's inverse
  ([runstate#64](https://github.com/GeoffChurch/runstate/issues/64)). A run's absence needs no
  query: a place's `reader` raises `RunNotFound`, and a pooled Postgres handle reads the live table.
- **Attributes.** A run's labels and groups come from the resolver, never from its id; on a server
  they would come from a consumer's own table or a manifest beside it.
- **Liveness.** On Postgres the session probe is evidence of a death (runstate's `SessionProbe`,
  `report_if_dead`); whether the cockpit asks it belongs to `liveness-overlay.md`.
