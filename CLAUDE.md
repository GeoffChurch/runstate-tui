# runstate-tui

A Textual cockpit that observes runs from the sibling `runstate` library. What it does:
`README.md`. Why it is shaped this way: `docs/design.md`.

## Orienting a fresh session

Three lookups, not one:

- **`docs/backlog/index.md`** — what is deferred, and what each item is gated on. This is the
  entry point for "what's next", but it only covers work *in this repo*.
- **`gh pr list`** — what is in flight. In-flight work is git state and is deliberately not
  mirrored into the backlog, where it would drift.
- **Memory** — why things are gated, and what lives in sibling repos (`../runstate`,
  `../mycooc`) that this repo's backlog will never surface.

## The one rule

Use only `runstate`'s public API. No raw `sqlite3`, no `?mode=ro` side-doors, no private
`_`-prefixed functions. When the public API cannot answer, that is a **finding**: file it
against runstate and reduce scope to route around it. Findings are a development-time output,
never a runtime feature — API gaps must not reach the UI.

## Conventions

- **Backlog entries are living docs.** They describe what is still open — no `SHIPPED`, no PR
  numbers, no dates; git history carries provenance. Rename a file when its name stops fitting.
  Dated `docs/superpowers/specs/` and `plans/` are the opposite: point-in-time records, left
  alone even when they reference renamed files.
- **Verify by looking.** Anything rendered — screenshots, snapshots, table cells, colors — gets
  generated and inspected, never assumed. This has caught real bugs repeatedly (tofu glyphs, an
  ANSI blue rendering as purple, a Rich style-inheritance footgun).
- **Measure before asserting a cost.** "It's an index seek" was wrong once already and shipped
  into two design docs before anyone checked (runstate#19). Run `EXPLAIN QUERY PLAN`; do not
  reason from the shape of the call.
- **A bad run is a loud row, never a crash.** Failures become typed, visible issues on the
  relevant row. Surfacing uncertainty is the feature.

## Gates

```
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run mypy --strict runstate_tui
```
