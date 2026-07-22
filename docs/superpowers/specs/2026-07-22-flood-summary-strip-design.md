# Fleet summary strip (issue-flood aggregation): design (2026-07-22)

An **always-on, one-line summary strip** pinned above the multi-run table. It is a **legend for the
table's own glyphs, with live counts** — rebuilt every frame from the folded rows. It does triple
duty: a **legend** (what the colored dots / `⚠` marks mean), a **fleet roll-up** (how many runs in
each state), and — the motivating case — a **flood digest**: when a shared-FS hiccup paints dozens of
identical `unreadable` rows, the strip shows `● unreadable 30` so the operator reads "one condition,
30 runs" instead of parsing a wall of red (spec §3.3 / §7, ISA-18.2 flood suppression).

This is the shipped multi-run table's remaining §7 deferral ("issue-flood aggregation"). The design
reframes "collapse N badges" into an **additive** summary: the main table is never collapsed — every
run keeps its own row (the §11 one-row-per-run invariant) — and the strip is a pure view-layer
roll-up on top.

## Goal

During a storm the table stays readable, and any single anomaly is still visible:

1. **Digest a flood** — N runs failing the same way read as one line (`● unreadable 30`), not N alarms.
2. **Never bury the singleton** — a lone `malformed` run gets its own chip (`⚠ malformed 1`) instead
   of drowning in the red; the strip doubles as a **triage list**.
3. **Legend + orientation** — the colored dots and `⚠` marks in the table are explained, always, by
   the same strip.

Non-goals (v1): collapsing/hiding table rows; any threshold to tune; click-to-filter/jump;
zero-count entries. All additive-later (see Deferred).

## The model: two tallies over the current frame's rows

The strip lists every **condition present** in the frame as `<glyph> <label> <count>`, from two
tallies (this is the "both flavors" decision — status-level *and* issue-level abnormalities):

**1. Status partition** — every run counted **once** by `row.status`.
- Bucket key: `Status.label` (already collapses terminal `Outcome`s to `done`/`errored`/… and maps
  `StatusKind.ERROR → "fold-error"`; label→color is 1:1, so a bucket has one `status_color`).
- Glyph: `●` colored by `status_color(row.status)` (the dot legend). Sums to the fleet size.

**2. Issue tags** — problems counted by **# rows carrying the kind** (a row with two `malformed`
records counts once).
- **Name = passthrough** — `IssueKind.value` verbatim (`malformed`, `skew_suspected`, `unsafe_stop`);
  no per-kind label map.
- **Skip status-twin kinds** — `CORRUPT` and `INTERNAL_ERROR` are the *footnote* of a status verdict,
  not a separate problem: a byte-torn run is `Status.corrupt()` **plus** a `CORRUPT` issue carrying the
  torn seq; a fold crash is `Status.error()` **plus** an `INTERNAL_ERROR` issue carrying the exception
  (`table.py` `_corrupt`/`_fold_error` — deliberate dual-surfacing, feeding the status column *and* the
  drill-down list). A per-event headcount counts them **once, by their status**, so the issue tally
  skips them. Encoded as a named set `_STATUS_TWIN_ISSUES = {CORRUPT, INTERNAL_ERROR}` in **`types.py`**
  (documenting the twin relationship at its source, not a magic exclusion in the strip). A `malformed`
  run, by contrast, has **no** status twin — it rides on some other status (e.g. `live`) — so it
  correctly appears under `live` *and* as a `malformed` tag: two genuinely-different facts.
- Glyph: `⚠` colored by the kind's max `Issue.severity` (`#d29922` MEDIUM / `#f85149` HIGH), mirroring
  `_marker`.

## Rendering rules

- **Order: `(severity desc, name)`** — worst conditions first, name as a stable tiebreak. Both keys
  come from properties Status/Issue already carry (`Status.severity` / `Issue.severity`; `Status.label`
  / `IssueKind.value`), so there is **no order table**, and it never reorders on count, so the legend
  stays steady. Consequence: it **interleaves** the two axes — a MEDIUM `⚠ malformed` sorts between the
  HIGH `●`-failures and the lower `●`-healthy chips — worst-first regardless of axis, the triage read.
  (Uses the existing severity model as-is: terminal outcomes like `errored` carry `Severity.OK`, so
  they sort among the calm chips though their dot is still red — consistent with the rest of the UI.)
- **Present-only** — a condition appears iff its count ≥ 1 (keeps it to one line; no zero clutter).
- **No threshold** — it just counts; a flood is simply a big number, a singleton a small one.
- **Dot-only color** — the `●`/`⚠` glyph carries the color; the `label count` text is neutral
  (`style="default"`), matching `status_color`'s "redundant with the text, never the sole signal"
  convention. This must use the explicit-`"default"` append guard (the `Text.append` base-style
  inheritance footgun already documented in `_marker` / `format_summary_card`).
- **Empty fleet** (0 rows) → empty; the strip hides (the `#empty` placeholder owns that state).

Calm day (`pending` is INFO, above the OK `done`/`live`):
```
● pending 1   ● done 3   ● live 12
```
Storage storm (worst-first; both flavors; flood digest):
```
● corrupt 2   ● unreadable 30   ⚠ malformed 1   ● done 3   ● live 94
```
(each `●` colored by `status_color`, each `⚠` by severity; the `label count` text is neutral.)

## The builder — a pure function

`format_fleet_summary(rows: Sequence[Row]) -> Text` in `runstate_tui/format.py`, beside
`format_row` / `format_summary_card`. Pure over the frame's rows; returns a single Rich `Text`
one-liner (empty `Text()` for no rows). Sketch:

```python
# types.py, beside IssueKind / StatusKind — the footnote-twins of a status verdict:
_STATUS_TWIN_ISSUES = {IssueKind.CORRUPT, IssueKind.INTERNAL_ERROR}

# format.py:
def format_fleet_summary(rows: Sequence[Row]) -> Text:
    status_count: dict[str, int] = {}
    status_repr: dict[str, Status] = {}
    for row in rows:                                    # status partition (each run once)
        lbl = row.status.label
        status_count[lbl] = status_count.get(lbl, 0) + 1
        status_repr.setdefault(lbl, row.status)         # a bucket rep -> its color & severity
    issue_count: dict[IssueKind, int] = {}
    issue_sev: dict[IssueKind, Severity] = {}
    for row in rows:                                    # issue tags (skip status-twins)
        for kind in {i.kind for i in row.issues} - _STATUS_TWIN_ISSUES:
            issue_count[kind] = issue_count.get(kind, 0) + 1
            issue_sev[kind] = max(issue_sev.get(kind, Severity.OK),
                                  max(i.severity for i in row.issues if i.kind == kind))
    chips = [  # (severity, name, glyph, color, count) — one per present condition
        (status_repr[l].severity, l, "●", status_color(status_repr[l]), n)
        for l, n in status_count.items()
    ] + [
        (issue_sev[k], k.value, "⚠", _sev_color(issue_sev[k]), n)
        for k, n in issue_count.items()
    ]
    out = Text()
    for _sev, name, glyph, color, n in sorted(chips, key=lambda c: (-c[0], c[1])):
        _chip(out, glyph, color, f"{name} {n}")         # severity desc, then name
    return out
```
`_chip(out, glyph, color, text)` appends `f"{glyph} "` with `style=color`, then `f"{text}   "` with
`style="default"` (the footgun guard). `_sev_color`: `MEDIUM → #d29922`, `HIGH → #f85149` (plan detail).
Names are pure passthrough — `Status.label` / `IssueKind.value`, no label map.

## Layout & wiring

A `Static(id="summary")` joins the existing top regions — order top-to-bottom: `#stall` (watchdog) ·
`#summary` (this strip) · `#empty` (placeholder) · `#runs` (table). Built on the **main thread** in
`MultiRunApp.on_table_ready`, from the same `msg.table` the reconcile already iterates:

```python
        summary = self.query_one("#summary", Static)
        if want:                                            # >=1 run this frame
            summary.update(format_fleet_summary([row for _, row in msg.table]))
            summary.display = True
        else:
            summary.display = False                         # 0 runs -> #empty owns the screen
```
(Folded into the existing empty/table `display` toggle; `want` is already computed there.)

## What does NOT change

Purely additive, same posture as the glob resolver: **no change to the fold, `ChannelPool`,
`fold_frame`, the keyed reconcile, the watchdog, or the concurrency/teardown model.** The strip is a
pure function of the rows already delivered to the main thread; it reads no channels and touches no
worker state. The only touch outside `format.py` / `multirun.py` is the additive `_STATUS_TWIN_ISSUES`
constant in `types.py` — a documented 2-entry set that changes no existing behavior.

## Model vs view (daemon / multi-frontend readiness)

The roll-up is **view/policy**, and stays DOWN (the discipline from the daemon/Emacs direction —
`runstate/docs/backlog/cockpit.md`, the drill-down spec's model/view section): a future daemon serves
each frontend the raw per-run `Row`s (with their statuses/issues); **each frontend rolls up its own
strip** with its own glyphs/order/labels. An Emacs frontend would tally the same served Rows into its
own header line. Nothing here needs a daemon or a new upstream runstate feature — the strip is
derivable entirely from data the cockpit already has (confirmed in the 2026-07-22 daemon/Emacs
review).

## Testing

- **Pure builder (unit, no app):** a mixed fleet → the expected chips in `(severity desc, name)` order
  with correct counts; a run counted once by status; a flood (`30 × unreadable`) → `unreadable 30`; a
  lone `malformed` on an otherwise-`live` run → both a `live` chip **and** a `⚠ malformed 1` tag (the
  wanted double-count); a `corrupt` run → a `corrupt` **status** chip only, **no** `⚠ corrupt` tag
  (the `_STATUS_TWIN_ISSUES` skip); an issue name is `IssueKind.value` verbatim (`skew_suspected`, not
  a remapped label); empty rows → empty `Text`.
- **Color (dot-only):** the `●`/`⚠` glyph carries `status_color`/severity color; the `label count`
  text is neutral (`style="default"`) — assert the spans, guarding the `Text.append` base-style
  footgun (as `test_summary_card_colors_only_the_dot_not_the_whole_line` does).
- **Order stability:** two frames whose counts differ but whose conditions/severities match produce
  the **same** chip order (the sort is on severity + name, never count).
- **Wiring (in-app, `run_test`/`Pilot`):** with ≥1 run the `#summary` strip is visible and its content
  matches the fold; a `glob`/empty-dir frame hides `#summary` and shows `#empty`; a run appearing
  swaps them. Reuse the `_seed` helper + fixture basis.
- No `@pytest.mark.asyncio` (use the `asyncio.run` wrapper). Regenerate a showcase scene if the strip
  should appear in the README table screenshot (optional).

## Deferred (additive, not built)

- **Navigable summary** — select/click a chip → filter or move the cursor to those runs (turns the
  strip into a real control). Needs a filter/selection seam.
- **Full always-legend** — show common conditions at count 0 too (a fixed legend rather than
  present-only). Costs width; only if wanted.
- **Threshold styling** — visually flag a chip as a *flood* past K (bold/inverse) rather than relying
  on the number alone.
- **Data-plane / multi-condition rows** — a run in more than one abnormal state is already covered
  (status chip + issue tags); no per-run multi-bucket beyond that.
