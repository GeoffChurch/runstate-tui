# Fixture basis — synthesis of the 6-lens brainstorm (2026-07-18)

Six parallel subagents (lifecycle / integrity / time-clocks / episodes-ordering / control-plane / incremental-log) each proposed ~12–16 controllable scenarios grounded in the real fold. ~85 scenarios total. This synthesizes them into (1) the reusable **infra basis** to build, (2) the **coverage matrix**, and (3) **real code findings** the brainstorm surfaced (not fixtures — decisions).

## 1. Reusable infra basis (build these once; every scenario composes from them)

| Helper | Why | Requested by lenses |
|---|---|---|
| `build_log` gains a 4th tuple slot `(body, topic, name, request_id=None)` | today's triples can't set `request_id`, blocking all control/launcher correlation (stops, demand, nak discharge, launcher reap) | control, episodes, lifecycle |
| `advance_tick(pilot, screen)` — construct with `tick_interval=999`, call `screen._tick()`, `await workers.wait_for_complete(); pause()` | the current poll-converge loop proves "eventually", not "this delta on this tick"; needed for append-then-tick vs tick-then-append ordering | incremental |
| `log_text(richlog) -> [Strip.text]` | every current assert is `len(lines) >= N`; can't see *which* lines survived eviction / what an unobserved-topic line contains | incremental |
| `counting_env(base, step)` / a clock that records call-count | prove `env.clock()` is called exactly once/frame (the fixed `lambda: now` can't) | time |
| `fake_clock(start, step)` — tick-iterator + no-op sleep crossing a deadline | standardize the `await_consumed` timeout pattern (currently ad hoc `iter([...])`) | control, time |
| `answer_on_sleep(channel, on_call={1:…, 2:…})` — seed answers on `await_consumed`'s sleep seam, multi-call | the watermark-climb (not-yet → covering) case needs a 2-phase seed no current test has | control |
| `corrupt_seq(tmp_path, run_id, seq)` — mutate a row mid-test (writer still open) | `torn_sqlite_channel` only corrupts *before* any reader opens; can't do "drain clean, then a later append tears" | incremental, integrity |
| `alien_body_sqlite_channel(records, seq, literal)` — plant valid-JSON-non-dict (`42`, `[1,2,3]`) | a 3rd corruption class (finding #3) nothing currently plants | integrity |
| `foreign_db(tmp_path, run_id)` — a valid sqlite file with an alien schema, pre-existing | the §8 create=False gap (finding #4b); no fixture plants it | integrity |
| `held_writer_sqlite_run(tmp_path)` — writer kept open across the test | every sqlite fixture closes the writer first; the live-WAL cross-connection tail path is unexercised | incremental |
| `real_worker_channel` (optional, later) — an in-memory channel + a driven `runstate.Worker` | hand-written naks/stops don't prove a *real* worker produces that exact record for that body | control |

**These ~11 helpers ARE the basis.** With them, the ~85 scenarios are mostly data, not bespoke closures.

## 2. Coverage matrix (scenario families per lens; ★ = sharp adversarial)

- **Lifecycle/verdict (14):** pending(empty / value-only-orphan) · live(heartbeat-only / stepless / started-only) · stale-boundary(≤ inclusive, 3 clocks)★ · terminal×5 outcomes · done-label · terminal-wins-over-stray-heartbeat★ · terminal-superseded-by-new-episode★ · schema-invalid-stopped-degrades-not-collapses★. Unreachable-as-log: presumed_dead, unknown-Outcome (types-level only).
- **Integrity/corruption (17):** missing(no-phantom) · unreadable(open / mid-read) · corrupt(seq1 / deep-5000★ / value-matches-objective) · **corrupt-invisible**(wrong-objective-name★ / discharged-stop★) · corrupt-in-delta · corrupt+malformed-together★ · malformed-stopped-doesn't-mask-live★ · malformed-terminated-missing-reqid · malformed-value-silently-tolerated★ · foreign-valid-db★ · empty-db · **alien-non-dict-body★**.
- **Time/clocks (16):** freshness-boundary(≤)★ · skew(heartbeat / started / both-fire-2-issues)★ · split-verdict(elapsed-skew-vs-freshness-clean, both directions)★ · **NaN-invisible-LIVE★★** · ±inf-asymmetric★ · unstamped/typeless-t(incl bool-not-int) · elapsed-from-first · clock-moves-backward★ · huge-elapsed · t==0-not-falsy · captured-once-per-frame★.
- **Episodes/ordering (14):** single · clean-relaunch · two-terminals · **two-live-no-stop★(conflicted gap)** · **activity-after-terminal★(conflicted gap)** · post-terminal-earlier-t(seq-vs-t)★ · orphan-stopped · out-of-order-t-seq-latest-wins★ · nak'd-episode · launcher-reap · stale-reap-after-relaunch★ · undischarged-stop-spans-episode(ghost badge)★ · completed/preempted/errored-projection(error="" not-None)★ · two-starts-then-terminal(conflicted gap).
- **Control plane (16):** undischarged(pending / discharged / naked-over-reports★ / multi / cond-vs-immediate★) · live_demand(present / unsubscribed-vs-nullid-nak★ / no-episode / resubscribe★ / time-lease-voided★) · await_consumed(accepted-watermark-climb★ / refused / died / reqid-absent★) · dispatch_stop(missing-no-phantom / unserved / sent-after-terminal-UNSAFE★).
- **Incremental log (16):** cold-full-drain · append-before-tick / tick-before-append★ · idle-no-growth · batch-in-gap · ring-eviction★ · long-line-no-wrap★ · embedded-newline-splits★ · unobserved-topics-only-in-tail★ · empty-log · byte-torn-in-delta★ · both-header-and-tail-identical★ · status-flips-mid-view★ · pop-mid-tick-race★ · re-entry-resets-cursor★ · sqlite-WAL-held-writer★.

## 3. Real CODE findings (decisions, not fixtures)

1. **`conflicted` is dead code.** `Status.conflicted()` is never called by `status_fold`; §4/§4.1's "two live episodes" and "activity strictly after a terminal, no re-start" triggers are UNIMPLEMENTED. Blocker: `last_activity`/`peek_terminal` return `float`/`RunResult` with **no seq**, so the fold can't compare orderings without a new seq-aware read (the atomic read §3.2 gestures at but the fold doesn't do). Also a spec self-tension: §4.1 row 4 (terminal-wins-over-stray-heartbeat) vs row 3 (activity-after-terminal→conflicted) describe the same shape two ways. Axis question: judge by **seq** (recommended — `t` is non-monotone) not `t`.
2. **NaN `t` → falsely-fresh `LIVE`, silent.** `last_activity` has no `math.isfinite` guard (unlike `read_elapsed`). A `NaN` timestamp → `freshness=0.0`, `LIVE`, zero issues — indistinguishable from a just-beaconed healthy run. `+inf`→LIVE+skew-flag; `-inf`→STALE+silent. The single most dangerous time-axis hole. (Fix: guard `last_activity` — upstream in runstate, or surface a `corrupt`/`malformed` issue here.)
3. **Alien non-dict body → uncaught `AttributeError`.** A committed body that's valid JSON but not a dict (`42`, `[1,2,3]`) on a tolerant topic (heartbeat/value/started) crashes `progress()`'s `.get(...)`. A *third* failure class distinct from JSONDecodeError and MalformedRecordError — the `corrupt` reshape (`except json.JSONDecodeError`) does NOT catch it. On verdict topics it's accidentally safe (`cls(**body)`→TypeError→MalformedRecordError). Decision: widen the corrupt catch to `(JSONDecodeError, AttributeError, TypeError)`, or a principled shape-check.
4. **Corruption invisibility is structural.** A torn `value` whose `name` ≠ `objective`, or a torn *discharged* `control.stop`, is filtered out at the SQL level → never decoded → the fold reads clean, zero signal. Corruption can sit on a log indefinitely if nothing reads its topic. (Inherent to name/cursor-filtered reads; a full-scan integrity check would be a deferred drill-down/Stage-4 surface.) Related UX trap: `undischarged_stops` is **not episode-scoped** → a ghost stop badge on a fresh LIVE episode.

## Also noted
- Several current byte-torn tests (`test_*_lets_byte_torn_propagate`, `test_byte_torn_crashes_the_cockpit`) encode the *old crash contract* → replaced (not kept) by the corrupt reshape, per no-legacy-compat.
- Fixture landmine: `lifecycle.stopped`/`launcher.terminated` are verdict-plane topics (need valid schema/request_id) — a bare body trips an incidental MALFORMED issue. Use `launcher.launched` as the "safe extra dated topic" for time-only fixtures.

## Coverage delivered (2026-07-18)

Built: 11 helpers + ~50 curated scenarios across fold/control/log planes (143→149 tests). Every deferred-finding is locked-at-current-behavior with a `# FINDING:` comment. The final review confirmed **no bug-locking/vacuous scenarios** (one vacuous helper self-test was repaired). Sharp ★ scenarios the first pass over-narrowed were added back: reqid-absent, both-future-skew, clock-moves-backward, malformed-value-tolerated, error-empty-errored, pop-mid-tick (covers detail.py's teardown guard).

**Still deferred (marginal ★, documented not dropped):** `long-line-no-wrap`, `both-header-and-tail-identical` (header/tail share `format_envelope` — guaranteed today, unpinned), `corrupt+malformed-together` (ordering: peek_terminal malformed then a torn read → corrupt; the more-severe wins). Add on demand.
