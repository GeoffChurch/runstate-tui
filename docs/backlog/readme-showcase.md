# README showcase — scenario-backed screenshots & GIFs

**Status:** static screenshots **SHIPPED** (PR #12 `4b662a7`) — `scripts/showcase.py` drives the
real app headlessly over seeded logs into 5 deterministic scenes (`docs/img/`, embedded in the
README `## Screens`), with a `tests/test_showcase.py` CI smoke test. **GIFs (animated usage) remain
deferred** — the scenes/approach below are kept as the reference for that follow-up. Everything is
generated **from the fixture basis** so it's deterministic and regenerable, not hand-captured.

## Why scenario-backed

The curated fixture basis (`tests/scenarios/`, `tests/helpers.py`) already drives the *real* app
headlessly into precise states via `App.run_test()` + `Pilot`, over seeded `runstate` logs with an
injected clock. Reuse that machinery to render showcase frames — no manual terminal, fully
reproducible, and the visuals can't drift from the actual behavior (a broken screen fails the
render). This session already proved the two capture primitives:
- `app.export_screenshot()` → SVG string, and `app.save_screenshot(name, path=…)` → file, both
  headless inside `run_test()` (no display).
- SVG → PNG via `cairosvg` (`uv run --with cairosvg python -c "import cairosvg; cairosvg.svg2png(...)"`).

## Scenes to capture (each backed by a scenario/fixture)

- **Single-run view — healthy live run**: `status live · step N · Ns ago · loss=… · ran Ns` (a
  `rich_run`-style seeded log, fixed clock).
- **Integrity taxonomy**: side-by-side or sequential `corrupt` / `unreadable` / `missing` /
  `malformed`-issue rows (the `corrupt_seq`/`foreign_db`/`alien_body` planters produce these).
- **Drill-down detail**: the live header (episode + undischarged stops + full issues) above the
  incremental raw-envelope log tail — use `held_writer_sqlite_run` so the tail visibly streams.
- **Control — the stop flow**: the `ConfirmStopScreen` gate, then a stop outcome line
  (`✓ accepted` / `⚠ unsafe` / `◼ run already ended`).

## Approach

- **Screenshots (static):** in a small `scripts/showcase.py` (or a `-m` entry), for each scene push
  the app via `run_test()`, drive `Pilot` to the moment, `save_screenshot(scene.svg)`, then convert
  to PNG. Commit the PNGs under `docs/img/` and reference them in the README. Keep clocks/logs
  seeded so re-running is byte-stable (mostly — SVG carries a little nondeterminism; prefer PNG in
  the README).
- **GIFs (animated usage):** capture a *sequence* of frames across a scripted Pilot interaction
  (e.g. `enter` → drill-down streams a few log lines → `escape`; or `s` → confirm → `y` → outcome),
  one `save_screenshot` per step, then assemble with `ffmpeg`/`imagemagick`. Alternatively a
  terminal-native recorder (charmbracelet **vhs**, or asciinema → **agg**) driving the real
  `runstate-tui <run.db>` CLI over a seeded db — richer motion (cursor, live ticks) but less
  deterministic; pick per scene.

## Notes

- Keep the generator in-repo and CI-runnable (it exercises the real screens — doubles as a smoke
  test), but don't gate CI on pixel-diffing the committed images (the snapshot suite already guards
  layout via `pytest-textual-snapshot`).
- Favicon/branding and any rename (`spool`/`parley`/keep-`runstate` — see the public-readiness
  memory) are separate, and a rename would churn these assets, so sequence this *after* any rename
  decision.
