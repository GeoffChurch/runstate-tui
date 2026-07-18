# Fold polish batch (red-team-cleared findings) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]`.

**Goal:** Four small, independent, low-risk fixes surfaced by the fixture brainstorm and cleared by a 3-lens adversarial review (which rejected the larger "seq-aware fold rewrite" — those are NOT in scope): **#3** episode-scope `undischarged_stops`; **#4** retain the terminal error diagnostic; **#8** distinguish a moot stop (run already ended) from an unsafe one; **#6** single-line the raw-envelope log rendering.

**Architecture:** All additive to the existing composed, independently-guarded fold; the verdict path (`peek_terminal`) is unchanged. No fold rewrite. Each fix is a faithful-representation improvement (retain/faithfully-scope a signal), matching the owner's principle. `conflicted` is explicitly OUT (deferred to the liveness overlay).

**Tech Stack:** Python 3.11, runstate (locked), Textual 8.2.8, uv, ruff, mypy --strict, pytest.

## Global Constraints
- Additive only; keep `peek_terminal` as the verdict source (no in-tree Outcome re-derivation — §3.2/F7).
- `guarded()` discipline preserved: a `.body` access that can hit an alien non-dict body stays inside a guarded call.
- Public-API-only; frozen value types; no back-compat shims; ruff/format/mypy/pytest green before each commit.

## File Structure
- `runstate_tui/fold.py` — #3 (episode-scope), #4 (thread the terminal error).
- `runstate_tui/types.py` — #4 (`Status.detail`).
- `runstate_tui/control.py` — #8 (`StopResult.MOOT` + the pre-send terminal check).
- `runstate_tui/format.py` — #4 (render the terminal detail), #6 (single-line envelope).
- Tests: `test_fold.py`/`test_types.py`/`test_control.py`/`test_format.py` + the fixture scenarios that lock current behavior (`test_fold_plane.py`, `test_control_plane.py`) — some `# FINDING:` tests flip to positive assertions.

---

### Task 1: #3 episode-scope `undischarged_stops` + #4 retain terminal error (`fold.py`, `types.py`, `format.py`)

**#3 — episode-scope the stops** (mirror runstate's own `boundary_voided` fencing; verified: `undischarged_stops` anchors only to `latest(STOPPED)`, no started-boundary). In `status_fold`, read the current episode's **seq** (not just its handle) once, and drop stops that predate it (a stop aimed at a prior episode is a ghost on the fresh one).

Replace the current episode read + the stops handling so the fold has the started envelope's `.seq`:
```python
    def _episode(ch: Channel) -> object:
        env = latest_episode(ch)
        if env is None:
            return None
        return (env.seq, env.body.get("handle"))  # .body.get can hit an alien body -> guarded -> malformed

    episode_info, episode_issue = guarded(_episode, channel)
    if episode_issue is not None:
        issues.append(episode_issue)
    episode_seq = episode_info[0] if episode_info is not None else None
    episode = episode_info[1] if episode_info is not None else None

    stops, stops_issue = guarded(undischarged_stops, channel)
    if stops_issue is not None:
        issues.append(stops_issue)
    if stops is not None and episode_seq is not None:
        stops = [s for s in stops if s.seq > episode_seq]  # episode-scope: drop prior-episode ghosts
```
(Type `episode_info` carefully for mypy — a `tuple[int, object] | None`; narrow before indexing, or use a small dataclass/helper. The existing `_episode_handle` is replaced by `_episode`.)

**#4 — retain the terminal diagnostic** (faithful-representation: `RunResult.error` is currently dropped — `Status.terminal` only threads `.outcome`).
- `types.py`: add `detail: str | None = None` to `Status`; `Status.terminal(cls, outcome, detail=None)` → `cls(StatusKind.TERMINAL, outcome, detail=detail)`.
- `fold.py` `reconcile_status`: the terminal return becomes `Status.terminal(result.outcome, detail=result.error)` (`RunResult` has `.error`, None for non-errored).
- `format.py`: `format_row`/`format_detail` append the terminal detail when present (e.g. status label `"errored"` followed by `: <detail>` in the detail view, or a compact suffix in the row). Keep the row line concise.

- [ ] **Step 1: Tests first.**
  - `test_fold_plane.py::test_undischarged_stop_spans_episode` currently `# FINDING:`-locks the ghost (asserts the stale stop IS present). **Flip it**: after the fix, a stop from a prior episode (before the current `started.seq`) is EXCLUDED → assert `row.undischarged_stops == ()`; add a control that a stop AFTER the current `started` IS present. Remove/adjust the `# FINDING:` comment.
  - Add a fold test: a stopped `{completed:False, error:"OOM killed", …}` → `row.status.detail == "OOM killed"`.
  - `test_fold_plane.py::test_terminal_errored_via_stopped` currently asserts `"OOM" not in repr(row)` (`# FINDING:` for the drop). **Flip it**: assert the error string now IS retained on the Row (via `status.detail`).
- [ ] **Step 2:** run → fail. **Step 3:** implement #3 + #4. **Step 4:** run → pass; full gates. **Step 5:** commit `feat(fold): episode-scope undischarged_stops + retain terminal error diagnostic`.

---

### Task 2: #8 moot-vs-unsafe stop + #6 single-line envelope (`control.py`, `format.py`)

**#8 — distinguish a moot stop** (faithful-representation: sending a stop to an already-terminal run currently times out → `UNSAFE`, conflating "run already over, stop is moot" with "sent to a live run, not answered"; it also writes a pointless `control.stop` into a finished run's log). In `stop_run`, check for an existing terminal BEFORE sending:
```python
    existing = _already_ended(channel)  # a small guarded peek_terminal
    if existing is not None:
        return StopOutcome(StopResult.MOOT, request_id, f"run already ended ({existing})")
    seq = channel.send({}, topic=Topic.CONTROL_STOP, request_id=request_id)
    ...
```
where `_already_ended` returns the terminal outcome value (str) or None, catching `MalformedRecordError` → None (a malformed terminal can't confirm ended → proceed to send); a byte-torn (`json.JSONDecodeError`) still propagates (consistent). Add `StopResult.MOOT` with `_STOP_SEVERITY[MOOT]=Severity.MEDIUM` and `_STOP_LABEL[MOOT]="◼ run already ended"`. Import `peek_terminal` from `runstate.observables`.

**#6 — single-line the raw-envelope render** (a body with an embedded `"\n"` splits one envelope across multiple `RichLog` lines). In `format_envelope`, escape control chars in the rendered body so one envelope is always one line, e.g. `body = str(env.body).replace("\\n", "\\\\n").replace("\\r", "\\\\r").replace("\\t", "\\\\t")` (verify the exact escaping renders literally — the intent is: no raw newline reaches `RichLog.write`).

- [ ] **Step 1: Tests first.**
  - `test_control_plane.py::test_sent_after_terminal_is_unsafe_not_died` currently `# FINDING:`-locks UNSAFE. **Flip it**: a stop dispatched against an already-terminal run → `StopResult.MOOT` (not UNSAFE), and assert **no `control.stop` is appended** to the log (the moot check short-circuits the send). A live run still reaches UNSAFE on timeout.
  - `test_log_plane.py::test_embedded_newline_splits` currently `# FINDING:`-locks the split (`log_text` shows >1 line for one envelope). **Flip it**: after #6, one envelope with an embedded newline renders as exactly ONE `log_text` line (the newline escaped). Remove the `# FINDING:` comment.
- [ ] **Step 2:** run → fail. **Step 3:** implement #8 + #6. **Step 4:** run → pass (rerun `test_log_plane.py` 3×); full gates. **Step 5:** commit `feat(control/format): moot stop for ended runs + single-line log envelopes`.

---

## Self-Review
- **Scope:** exactly the 4 red-team-cleared fixes; NO fold rewrite, NO `conflicted` (deferred to liveness overlay), NO #2 (deferred opt-in scan).
- **Faithful-representation:** #4 retains a dropped signal; #8 distinguishes two conflated conditions; #3 faithfully scopes to the current episode; #6 preserves the raw line.
- **Flipped `# FINDING:` tests:** `test_undischarged_stop_spans_episode`, `test_terminal_errored_via_stopped`, `test_sent_after_terminal_is_unsafe_not_died`, `test_embedded_newline_splits` — each moves from locking-the-gap to asserting-the-fix; verify none is left asserting the old (now-wrong) behavior.
- **Guard discipline:** the `_episode` and `_already_ended` reads keep alien-body/`MalformedRecordError` handling; byte-torn still propagates.
