# README showcase — scenario-backed screenshots: design (2026-07-19)

Refines and supersedes the scoping in `docs/backlog/readme-showcase.md` for the first cut. Adds
visual showcases of the cockpit's screens to the README, **generated deterministically from the
fixture basis** (not hand-captured), led by the multi-run table (the flagship as of Stage 4).

## Goal

A committed, CI-runnable generator that drives the *real* app headlessly into precise seeded states
and emits static PNGs, embedded in the README under a "Screens" section. Regenerable by one command
— so a future rename (deferred; proceeding under `runstate-tui`) is a re-run, not a re-shoot.

## Scope (first cut)

- **Static PNGs only.** Deterministic, embed inline on GitHub, cover every screen, CI-safe.
- **GIFs / animated usage DEFERRED** to a follow-up (the live tick, drill-down streaming, the
  stop→confirm→outcome motion) — higher-value-but-fiddlier and non-deterministic; the backlog doc
  keeps the approach notes.
- **No pixel-diff CI gate.** `pytest-textual-snapshot` already guards layout; the generator running
  green in CI (every screen renders without error) is the smoke-test value, not image equality.

## The generator — `scripts/showcase.py`

A `python -m` / `uv run`-invokable module. One function per scene; a `main()` runs all and writes
PNGs to `docs/img/`. Each scene:

1. Seed a `runstate` log (or several) on a temp `sqlite` backend at controlled `t` values, reusing
   `tests/helpers.py` planters (`build_log`, `corrupt_seq`, `foreign_db`, alien-body, and the
   multi-run seeding) — the same machinery the scenario suite uses, so visuals can't drift from
   behavior.
2. Build the app with an **injected fixed clock** (`Env(clock=lambda: NOW)`) and a **fixed console
   size** (`run_test(size=(W, H))`) so dimensions are stable.
3. Drive `Pilot` to the target state (`pause()` to settle a tick; `press("enter")` for drill-down;
   `press("s")` for the stop gate).
4. `app.save_screenshot(<scene>.svg)` → SVG, then `cairosvg.svg2png(...)` → `docs/img/<scene>.png`.

`NOW` is chosen per scene *relative to* the seeded `t`s to produce the intended live/stale spread
(e.g. `stuck_threshold=60`: a heartbeat at `NOW-20` renders `live`, one at `NOW-120` renders
`stale`). Determinism = seeded logs + fixed clock + fixed size.

## Scenes (led by the flagship)

1. **Multi-run table** — the hero. ~5 runs spanning the taxonomy in one shot: a **live** run (recent
   heartbeat + `loss=…` value), a **stale** run (old heartbeat), a **done** run (terminal
   `completed`), an **errored** run (terminal with a `RunResult.error` detail), and a loud
   **corrupt** row (`corrupt_seq`). The whole control-plane-at-a-glance.
2. **Single-run view** — one healthy **live** run: `status · step · age · value · elapsed`.
3. **Integrity taxonomy** — a 4-row table: `corrupt` / `unreadable` (`foreign_db`) / `missing`
   (resolver points at an absent file) / a `malformed`-issue row (alien body). Shows the "one bad
   run is a loud row, never a crash" property side by side.
4. **Drill-down detail** — `enter` into a rich run: the live header (episode + undischarged stops +
   full issues) above the raw-envelope log tail.
5. **Stop flow** — `s` → the `ConfirmStopScreen` gate (the distinctive control-plane moment).

## README integration

A **Screens** section embedding the five PNGs with one-line captions, in scene order (table first).
PNGs committed under `docs/img/`. No other README restructuring in this cut.

## Dependencies & determinism caveats

- **`cairosvg`** added as a dev dependency (its own group / extra), not a runtime dep of the cockpit.
- Prefer PNG in the README (SVG carries minor id/font nondeterminism).
- **Glyph rendering risk:** the status markers (`⚠`, `⚠⚠`, `⏹`) and box-drawing must survive
  SVG→PNG through `cairosvg`'s available fonts. **The first plan task spikes the full pipeline on one
  scene** (export → `cairosvg` → a legible PNG with the glyphs intact) before building the rest; if a
  glyph renders as tofu, resolve the font (bundle/point `cairosvg` at one) in that spike.

## Testing / CI

- The generator is invoked in CI (renders every scene → fails if any screen errors). It is the smoke
  test; no image-equality assertion.
- A tiny unit check that `main()` writes the expected PNG files to a temp dir (files exist,
  non-empty) — cheap regression that the pipeline stays wired.

## Deferred (out of this cut)

GIFs / animated usage; any README restructuring beyond the Screens section; branding/favicon;
sequencing against a rename (proceeding under `runstate-tui` — regeneration makes a later rename
cheap).
