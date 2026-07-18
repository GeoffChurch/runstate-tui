# Fixture basis — reusable helpers + curated scenarios Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Build a shared, reusable test-fixture basis for runstate-tui: ~11 controllable infra helpers, then a **curated** set of adversarial + finding-locking scenarios composed from them (NOT all ~85 — the sharp/discriminating ones), across the fold / control / incremental-log planes.

**Architecture:** All helpers live in `tests/conftest.py` (pytest fixtures) or `tests/helpers.py` (plain functions). Scenarios are tests that use them and assert the fold's **actual** behavior (locking it as a regression). Where a scenario reveals a *deferred* finding (`conflicted`, corruption-invisibility, ghost-stop-badge), it asserts the CURRENT behavior and carries a `# FINDING:` comment, so nothing is silently dropped.

**Source of truth for scenarios:** `docs/superpowers/notes/2026-07-18-fixture-basis.md` (the synthesized coverage matrix, ★ = the curated adversarial cases). Ground every recipe in the real fold and **verify expected values empirically** (run the code), exactly as the brainstorm did — do not guess an expected value; assert what the fold actually produces.

**Tech Stack:** Python 3.11, runstate (locked), Textual 8.2.8, uv, ruff, mypy --strict, pytest, pytest-textual-snapshot.

## Global Constraints

- **Curated, not exhaustive:** implement every ★ adversarial case and every finding-lock / just-shipped-behavior case from the notes doc; SKIP pure baseline variants (the "control" rows). Aim ~35–45 scenarios total.
- **Assert actual behavior:** a regression test's "expected" is what the code does — verify by running, not by guessing. For a scenario whose "correct" answer is an open design question (a deferred finding), assert the CURRENT behavior + a `# FINDING:` comment naming the gap.
- **Helpers are backward-compatible:** extending `build_log` must not break its existing 3-tuple callers.
- **Fixture landmine (from the brainstorm):** `lifecycle.stopped` / `launcher.terminated` are verdict-plane topics (need valid schema / `request_id`); a bare body trips an incidental MALFORMED issue. Use `launcher.launched` as the "safe extra dated topic" for time-only fixtures.
- **Public-API-only; no back-compat shims; ruff/format/mypy/pytest green before each commit.**

## File Structure

- **Modify `tests/conftest.py`** — the fixture-shaped helpers (`build_log` extension, `counting_env`, `answer_on_sleep`, planters, `foreign_db`, `held_writer_sqlite_run`).
- **Create `tests/helpers.py`** — plain-function helpers (`advance_tick`, `log_text`, `fake_clock`, `corrupt_seq`) imported by tests.
- **Create `tests/scenarios/`** — `test_fold_plane.py`, `test_control_plane.py`, `test_log_plane.py` (the curated scenarios), + a `test_helpers.py` self-test.

---

### Task 1: The reusable infra basis

**Files:** Modify `tests/conftest.py`; Create `tests/helpers.py`, `tests/scenarios/test_helpers.py`.

**Produces (exact interfaces):**
- `build_log` fixture — `_build(records)` where each record is `(body, topic, name)` OR `(body, topic, name, request_id)` (backward-compatible via `*rest` unpacking → `writer.send(..., request_id=rid)`).
- `counting_env(base=100.0, threshold=60.0) -> tuple[Env, dict]` fixture-or-func — clock returns `base + n` on the n-th call; the returned dict exposes `["n"]` (call count). For "clock captured once/frame."
- `fake_clock(*times) -> tuple[Callable[[], float], Callable[[float], None]]` (helpers.py) — a `now` that yields `times` in order + a no-op `sleep`, for bounded `await_consumed` timeout tests.
- `answer_on_sleep(channel, on_call: dict[int, Callable]) -> Callable[[float], None]` (conftest) — a `sleep` callback that runs `on_call[k](channel)` on its k-th call; seeds `await_consumed` answers on the poll seam (supports the multi-call watermark-climb case).
- `advance_tick(pilot, screen) -> Awaitable` (helpers.py) — `screen._tick(); await pilot.app.workers.wait_for_complete(); await pilot.pause()`. Use with a screen built at `tick_interval=999` so only manual ticks fire.
- `log_text(richlog) -> list[str]` (helpers.py) — `[strip.text for strip in richlog.lines]`.
- `corrupt_seq(tmp_path, run_id, seq, *, literal="{not json") -> None` (helpers.py) — raw `UPDATE log SET body=? WHERE seq=?` on the sqlite db (writer may still be open); `literal="{not json"` plants a byte-torn (JSONDecodeError) body, `literal="42"` an alien non-dict body.
- `foreign_db(tmp_path, run_id="ghost") -> RunRef` (conftest) — writes a VALID sqlite file with an alien schema (`CREATE TABLE unrelated(id, note)` + a row) at `<tmp_path>/<run_id>.db` BEFORE any `open_channel`, returns the ref.
- `held_writer_sqlite_run(tmp_path)` (conftest) — yields `(ref, send)` where the writer channel stays OPEN across the test; `send(body, topic, **kw)` appends live; closed in teardown.

- [ ] **Step 1: Write the helper self-tests**

Create `tests/scenarios/test_helpers.py` asserting each helper does what it claims, e.g.:
```python
def test_build_log_accepts_request_id(build_log):
    from runstate.observables import undischarged_stops
    ch = build_log([({}, "control.stop", None, "webui:s1")])
    stops = undischarged_stops(ch)
    assert len(stops) == 1 and stops[0].request_id == "webui:s1"


def test_build_log_still_accepts_three_tuples(build_log):
    ch = build_log([({"step": 1, "consumed_seq": 0, "t": 1.0}, "lifecycle.heartbeat", None)])
    from runstate.observables import progress
    assert progress(ch) == 1


def test_counting_env_counts_calls(counting_env):
    env, calls = counting_env(base=100.0)
    assert env.clock() == 101.0 and env.clock() == 102.0 and calls["n"] == 2


def test_fake_clock_yields_then_noop_sleep():
    from tests.helpers import fake_clock
    now, sleep = fake_clock(10.0, 11.0, 12.0)
    assert now() == 10.0
    sleep(999)  # no-op, returns immediately
    assert now() == 11.0


def test_corrupt_seq_plants_torn_and_alien(tmp_path, build_log):
    import json, sqlite3
    from runstate import open_channel
    from tests.helpers import corrupt_seq
    w = open_channel("r", root=tmp_path, backend="sqlite")
    w.send({"handle": "h", "t": 1.0}, topic="lifecycle.started")
    w.close()
    corrupt_seq(tmp_path, "r", 1, literal="42")
    r = open_channel("r", root=tmp_path, backend="sqlite")
    got = r.read(after=0)
    assert got[0].body == 42  # alien body decoded as a bare int
    r.close()


def test_foreign_db_is_valid_sqlite_with_alien_schema(foreign_db):
    import sqlite3
    ref = foreign_db  # (run_id, root, backend)
    from pathlib import Path
    assert (Path(ref[1]) / f"{ref[0]}.db").exists()
```
(Add self-tests for `answer_on_sleep`, `log_text`, `held_writer_sqlite_run`, `advance_tick` too — the last two need `run_test()` harnesses; keep them minimal.)

- [ ] **Step 2: Run → fail** (`uv run pytest tests/scenarios/test_helpers.py -q` — imports/fixtures missing).

- [ ] **Step 3: Implement the helpers.** Extend `build_log` in `conftest.py` (unpack `body, topic, name, *rest`), and add the conftest fixtures + `tests/helpers.py` functions per the interfaces above. Ensure `tests/scenarios/__init__.py` exists if needed for the `from tests.helpers import …` path (or configure via `conftest`/`pytest` rootdir — the repo already imports `tests.*`? verify; if not, put helpers where importable, e.g. top-level `tests/helpers.py` with `tests/__init__.py`, or use a `conftest`-exported fixture instead of a module import).

- [ ] **Step 4: Run → pass; full gates; commit** `test: reusable fixture-basis infra helpers`

---

### Task 2: Curated fold-plane scenarios

**Files:** Create `tests/scenarios/test_fold_plane.py`. Uses `build_log`, `corrupt_seq`, `foreign_db`, `counting_env`.

Implement these curated scenarios (★ from the notes doc's lifecycle / integrity / time / episodes lenses). For each: build the recipe, run `status_fold`/`reconcile_status`/`read_elapsed`, and **assert the actual `Row`/values** (verify empirically). Group with clear names.

- [ ] **Step 1: Write the scenario tests** (curated list — recipes condensed; expand + verify against the fold):

Lifecycle/verdict: `stale_threshold_boundary` (t=0, three clocks now=59.999/60.0/60.001 → LIVE/LIVE/STALE, locks `<=`); `terminal_wins_over_stray_heartbeat` (started→stopped(completed)→heartbeat far later → TERMINAL, freshness large but status terminal); `terminal_superseded_by_new_episode` (started h1→stopped→started h2 → LIVE, episode=h2, elapsed from FIRST started); `terminal_errored_via_stopped` (stopped completed=False error="OOM" → ERRORED, and assert the error string is NOWHERE on the Row); `schema_invalid_stopped_completed_with_error` (stopped completed=True+error set → MalformedRecordError → MALFORMED issue, status falls through to LIVE via a later heartbeat, frontier from the heartbeat).

Integrity: `corrupt_at_seq_1` (torn_seq=1 → CORRUPT, issue.seq==1); `corrupt_deep` (torn deep in ~2000 records → CORRUPT with the right seq — assert it terminates, no full re-decode blowup); `corrupt_value_wrong_name_invisible` (torn `value` name="acc", objective="loss" → a fully CLEAN Row, no corrupt/issue — `# FINDING: corruption-invisibility`); `corrupt_discharged_stop_invisible` (torn control.stop discharged by a later stopped → clean terminal Row — `# FINDING: corruption-invisibility`); `alien_started_body_malformed` (alien `lifecycle.started` body `42` via corrupt_seq → MALFORMED issue with detail startswith "AttributeError", NOT corrupt, run survives); `foreign_valid_db_reads_pending` (via `foreign_db` → PENDING, bare — `# FINDING: §8 foreign-db gap`, and assert the file is mutated i.e. now has a `log` table).

Time: `nan_heartbeat_not_falsely_live` (t=NaN → PENDING + HIGH MALFORMED "not finite", NOT live); `inf_asymmetric` (+inf and -inf sub-cases → both PENDING + issue now, not live/stale — locks #2); `elapsed_skew_isolated_from_freshness` (first started future, latest started past → freshness clean, elapsed=0 + skew issue); `captured_once_per_frame` (via `counting_env` → assert `calls["n"]==1` and freshness/elapsed imply the SAME now); `t_exactly_zero_not_falsy` (started t=0 → elapsed computed, not dropped).

Episodes: `two_live_episodes_no_stop` (started h1→heartbeat→started h2→heartbeat → CURRENT: LIVE episode=h2, no flag — `# FINDING: conflicted unimplemented (§4.1 row 3)`); `activity_after_terminal_no_restart` (started→stopped→heartbeat → CURRENT: TERMINAL — `# FINDING: conflicted / §4.1 row3-vs-row4 tension`); `out_of_order_t_seq_latest_wins` (heartbeat t=300 seq2, heartbeat t=200 seq3 → frontier/freshness from seq3 (t=200), locks seq-latest-not-max-t).

- [ ] **Step 2–4:** run → verify each assertion reflects real fold output (fix the assertion to match actual behavior, NOT the code) → gates → commit `test(scenarios): curated fold-plane basis (lifecycle/integrity/time/episodes)`.

---

### Task 3: Curated control-plane scenarios

**Files:** Create `tests/scenarios/test_control_plane.py`. Uses `build_log` (with request_id), `answer_on_sleep`, `fake_clock`.

- [ ] **Step 1: Write the scenario tests** (★ from the control lens):

`undischarged_stops`: `naked_stop_still_undischarged` (malformed stop `{"until":…}` + a nak → still listed — `# over-reports: no nak discharges a stop`); `multiple_stops_one_discharge_clears_all` (2 stops + reuse-id + a stopped → all listed pre, [] post); `conditional_vs_immediate_pending_not_due` (`{}` and `{"from":{"step":100}}` both listed indistinguishably — `# pending ≠ due`); `undischarged_stop_spans_episode` (stop then a new started, no discharge → the ghost stop still listed on the fresh live episode — `# FINDING: ghost-stop-badge, not episode-scoped`).

`live_demand`: `unsubscribed_vs_nullid_nak` (subscribe sub-a, unsubscribe sub-a, subscribe sub-b, a null-id nak → live_demand==[sub-b], the null-id nak clears nothing); `resubscribe_after_answer_is_live` (subscribe→nak→resubscribe same id → live again); `time_lease_voided_by_episode_boundary` (started, time-referencing subscribe, two more starteds → live_demand==[] via boundary voiding; contrast a non-time subscribe stays live).

`await_consumed`/`stop_run`: `accepted_watermark_climb` (via `answer_on_sleep({1: seed hb consumed_seq=S-1, 2: seed hb consumed_seq=S})` → ACCEPTED, locks the `>=` boundary + not-fooled-by-early-poll); `refused_nak`, `died_terminal_follows`, `unsafe_timeout` (via `fake_clock`); `sent_after_terminal_is_unsafe_not_died` (started→stopped(seq2)→stop(seq3) then handshake → UNSAFE not DIED, since the terminal PRECEDES the request — `# UX gap: UNSAFE conflates "no worker yet" with "run already over"`); `dispatch_stop_missing_no_phantom` (UNDELIVERED + assert no db fabricated).

- [ ] **Step 2–4:** run → verify → gates → commit `test(scenarios): curated control-plane basis (stops/demand/handshake)`.

---

### Task 4: Curated incremental-log-plane scenarios

**Files:** Create `tests/scenarios/test_log_plane.py`. Uses `advance_tick`, `log_text`, `corrupt_seq`, `held_writer_sqlite_run`, a `run_test()` harness pushing `DrillDownScreen`.

- [ ] **Step 1: Write the scenario tests** (★ from the incremental-log lens — all use manual `advance_tick`):

`cold_open_full_drain` (pre-populate 3 records → first tick drains all 3, cursor==3 — not a tail-seek); `append_before_tick_included` / `tick_before_append_deferred` (the two-directional delta boundary — assert the state BETWEEN append and next tick); `batch_append_in_one_gap` (5 records in one gap → one batched delta, seq order); `ring_eviction` (`log_cap=5`, 8 records → `log_text` shows seq 4–8, cursor==8 — cursor/pane diverge); `embedded_newline_splits` (a body with `"\n"` → `log_text` shows >1 physical line for one envelope — `# format_envelope should defensively single-line`); `unobserved_topics_only_in_tail` (nak/launcher/unsubscribe → header (format_detail) excludes them, `log_text` includes them); `byte_torn_in_delta` (via `corrupt_seq` on a fold-invisible topic after a clean drain → `read_log_delta` returns [] for that tick, header stays clean, NO crash — post-reshape contract); `header_status_flips_live_to_terminal` (append a stopped mid-view → next tick header flips to "done" AND the stopped appears in the tail); `re_entry_resets_cursor` (pop, new screen, append one, tick → fresh full drain, not a resume); `sqlite_wal_held_writer` (via `held_writer_sqlite_run` → each tick's fresh reader connection sees the live writer's committed appends).

- [ ] **Step 2–4:** run 3× (non-flaky) → verify → gates → commit `test(scenarios): curated incremental-log-plane basis`.

---

## Self-Review

- **Coverage:** every ★ adversarial + finding-lock from the notes doc maps to a scenario; deferred findings (`conflicted`, corruption-invisibility, ghost-stop-badge, §8 foreign-db, embedded-newline, UNSAFE-conflation) are each locked-at-current-behavior with a `# FINDING:` comment (nothing silently dropped).
- **Helpers:** all 11 in Task 1, each self-tested; scenarios compose from them (no bespoke per-test closures).
- **No placeholders:** Task 1's helper interfaces are exact; Tasks 2–4 give the curated recipe per scenario — the implementer verifies each expected value empirically against the real fold (the "expected" of a regression test is the actual behavior).
- **Scope:** curated (~35–45), not all ~85; pure baseline variants intentionally skipped.
```
