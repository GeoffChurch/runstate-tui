# Drill-down redesign: design (2026-07-19)

A redesign of the Stage-3 `DrillDownScreen` (`runstate_tui/detail.py`) from a flat text blob into a
**two-region, tabular** detail view: a compact summary card over a colored, zebra-striped, newest-at-top
log table with copy, filter, and expand. Mockup-verified (`docs/img` showcase machinery). This spec is
**v1**; search and whole-log scrollback are designed-for but deferred.

## Goal

Make the run's log legible and actionable: clear region boundaries, an obviously *tabular* log,
per-event copy, and live filtering — while keeping the query logic frontend-agnostic (so a future
headless daemon / native Emacs frontend reuses it) and behind the `read(after, filter, limit)` seam
that upstream `GeoffChurch/runstate#15` will serve.

## Layout — two regions

```
┌ train-mnist ───────────────────────────────────────────┐
│ ● live   step 1450   8s ago   loss=0.0123   ran 20s     │   ← summary card (compact)
│ episode local://h/1     ■ 1 stop pending   ◆ 1 demand   │
└─────────────────────────────────────────────────────────┘
┌ log · live · newest ↑ ──────────────────────────────────┐
│ / filter…   topic · request · step>N · text             │   ← filter bar (focus with /)
│ seq  topic               request      body              │
│  9   control.stop         webui:stop1  {}               │ ← selected (yank/expand target)
│  8   control.subscribe    webui:sub1   {'names':['loss']}│ ░ zebra
│  7   value                              loss=0.0123 @1450│
│  …                                                       │
│ ● lifecycle 4   ● value 3   ● control 2   · key toggles │   ← topic-family toggles (counts)
└─────────────────────────────────────────────────────────┘
 y yank   / filter   n search   enter expand   esc back        ← footer
```

- **Summary card** (top, bordered, titled `run_id`): a *compact* projection of the `Row` — the
  `format_row` one-line summary (with the `●` status dot, `status_color`) on line 1; `episode` +
  **counts** (`■ N stop pending`, `◆ N demand`, `⚠ N issues` when present) on line 2. The full
  stop/demand/issue *lists* (today's verbose `format_detail` body) move to the `enter`-expand. `■`
  is `#d29922`; the `●` uses `status_color`; the status word stays uncolored (the dot carries it).
- **Log panel** (bottom, bordered, titled `log · live · newest ↑`): a filter bar, the log
  `DataTable`, and the topic-toggle chips.

## The log table

- **`DataTable`** — columns `seq · topic · request · body`; `cursor_type="row"` (the cursor is the
  yank/expand target); `zebra_stripes=True`.
- **Newest-at-top** (reverse-chron, seq descending). Rationale: for a live monitor the newest event
  is the point, and it removes the auto-scroll-fights-the-user problem — new events land at the edge
  you're watching, and scrolling *down* into history (to read/copy) is undisturbed by arrivals.
- **Topic color** (a new `topic_color(topic) -> str`, hex, mirroring `status_color`): `lifecycle.*`
  `#539bf5`, `control.*` `#d29922`, `value` `#3fb950`; other families a neutral default. The `topic`
  cell is a colored `Text`; color is redundant with the (always-present) topic text.
- **Body** stays the last column (can be wide; horizontal-scrolls). v1 renders it via the existing
  `format_envelope`-style body text; light prettifying of common bodies (e.g. `value` →
  `loss=0.0123 @ 1450`) is allowed but the raw dict is the faithful default.

## The log is a bounded, filtered WINDOW (the load-bearing model choice)

The log pane holds a **bounded window** (last *N* matching envelopes, `N` a generous cap e.g. 500) —
NOT a growing tail. This is the `log_view = window ∘ filter ∘ read` model, and it is what lets search
and scrollback slot in later without a rewrite:

- **Live tail** = the window pinned to the end, following (newest-first).
- Each tick/notify: read only the **delta** (`read_log_delta(ref, after=cursor, filter=…, limit=…)`),
  merge into the window newest-first, trim to `N`. Incremental read (cursor watermark) preserved —
  never re-read the whole log. The exact `DataTable` mutation (prepend, or `sort("seq", reverse=True)`
  on the bounded window) is a plan detail; the invariant is **read the delta, render a bounded
  newest-first window, never rebuild from seq 0**.
- Off-thread + teardown-guarded exactly as Stage 3 (`self.app.call_from_thread`, `_TEARDOWN_ERRORS`).

## Filter — into the read seam

- **`/`** focuses the **filter bar**; typing narrows the window live. Grammar (v1): topic /
  topic-family (`control.*`), `request_id`, a `step>N`-style numeric bound, and a plain-text substring
  over the body.
- **Topic-family quick toggles** — chips (lifecycle / value / control) with live counts; a key/click
  toggles a family on/off. Sugar over the same predicate.
- **The filter is a predicate in the read pipeline, not a display filter.** `read_log_delta` gains a
  `filter=` parameter: `read_log_delta(ref, after, *, filter=None, limit=None)`. **v1** may evaluate
  the predicate client-side inside the guarded read (over the bounded window) — but the *seam is the
  read*, so when `GeoffChurch/runstate#15` (`read(filter=…)`) lands, the cockpit delegates to the
  substrate with **no call-site change**. This is the "design-for-defer" line: v1 owns a small
  predicate; the interface is already the one the daemon/Emacs frontends will share.

## Copy — `y` yanks the full envelope (OSC 52)

- **`y`** copies the **selected row's full envelope** (`seq · topic · request_id · body`, via
  `format_envelope`) to the system clipboard through **Textual's OSC 52 clipboard** — which works
  over **SSH and tmux** in supporting terminals (the cockpit runs on login nodes). Labeled **yank**
  (not "copy") to avoid the "duplicate this row" reading; `y` = the vim mnemonic.
- The full envelope (not just the body) is the single copy target — the metadata (`seq`/`topic`/
  `request_id`) is a few characters and makes every yank self-describing and citable.
- **Fallbacks:** terminal-native drag-select (modifier key — Textual captures the mouse) remains the
  universal escape hatch; if OSC 52 is unsupported, the `enter`-expand panel is natively selectable.

## Expand — `enter`

- **`enter`** on the selected row → an expand panel/modal with the **pretty-printed full envelope**
  (and, from the summary card's counts, this is also where the full undischarged-stops / live-demand /
  issues lists live). Natively selectable + yankable. `esc` closes it back to the log. For long bodies
  that the table truncates, this is the readable/copyable home.

## Keybindings (footer)

`y` yank · `/` filter · `n` search *(deferred — shown disabled/absent in v1)* · `enter` expand ·
`esc` back. Selection: ↑/↓ move the row cursor. The footer renders the live bindings.

## Components / files

- **`runstate_tui/detail.py`** — the `DrillDownScreen` redesign: the two-region compose (summary
  `Static` card + a log `Vertical` containing the filter bar, `DataTable`, and toggle chips + the
  footer), the windowed live-tail worker feeding the `DataTable`, and the `y`/`enter`/`/`/toggle
  actions.
- **`runstate_tui/format.py`** — `topic_color(topic) -> str`; a compact `format_summary_card(row)`
  (the two-line card projection); `format_envelope` reused for the yank + expand.
- **`runstate_tui/table.py`** — `read_log_delta` gains `filter=` (+ the bounded-window `limit`), the
  v1 predicate applied in the guarded read; the seam ready to delegate to upstream `read(filter=)`.
- **Clipboard** — Textual's `App.copy_to_clipboard` (OSC 52); degrade gracefully if unsupported.
- **Expand** — a small `ModalScreen`/panel showing the pretty-printed envelope + the full lists.

## Model vs view (daemon / multi-frontend readiness)

- **Model (frontend-agnostic, daemon-servable):** the windowed/filtered log query
  (`read_log_delta(after, filter, limit)`), the `Row`/`Status`/`Envelope` semantics. These are what a
  future daemon serves to N frontends over RPC.
- **View (cockpit-only):** the layout, widgets, keybindings, `topic_color`/`status_color`/`format_*`,
  OSC 52, the `DataTable` reconcile. An Emacs frontend renders the same model with its own faces.
- Keep `format_*`/colors on the view side; `Row`/`Status`/`Envelope` is the semantic boundary. This is
  the discipline that keeps a daemon extraction cheap (upstream `GeoffChurch/runstate#15`/`#16`/`#17`).

## Testing

- **Layout/render:** summary card renders the compact two-line projection; the log table renders
  newest-at-top, zebra, topic-colored (`snap_compare` for layout; content asserts for order/color).
- **Windowed live-tail:** appending envelopes to a `held_writer_sqlite_run` makes them appear at the
  **top** incrementally; the window stays bounded at `N`; the read is a delta (cursor), not a rebuild.
- **Filter:** a filter predicate (topic-family / request / substring) narrows the window to matching
  rows; a topic toggle hides/shows a family + updates the count; the predicate is applied via
  `read_log_delta(filter=)`, not post-render.
- **Yank:** `y` on the selected row calls the clipboard with the full `format_envelope` text (assert
  the copied string; a spy on `copy_to_clipboard`).
- **Expand:** `enter` opens the modal with the full pretty-printed envelope; `esc` returns.
- Teardown: quitting mid-tail is guarded (`_TEARDOWN_ERRORS`), as Stage 3.
- Reuse the fixture basis + the showcase seed style; no `@pytest.mark.asyncio` (use the `asyncio.run`
  wrapper).

## Deferred (designed-for, not built)

- **Search** (`n`/`N` highlight-and-jump) — reuses the filter bar as a shared component (filter =
  hide, search = highlight + move the window's anchor to a match). Needs anchored (non-tail) reads.
- **Whole-log scrollback + search** — backward reads (upstream `#15` `before=`/`max_seq=`) and, for
  very large logs, a backend index (postgres full-text). runstate-side, genuinely later.
- **Richer filter grammar** (boolean combinations, saved filters), and pushing the predicate fully
  into `read` once `#15` lands (v1's client-side predicate is the drop-in).
