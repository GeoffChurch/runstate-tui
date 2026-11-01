# Value trend / trajectory — a lightweight in-terminal plot

The cockpit is control-plane-focused, but the separation from visualization is a *stance, not a law*:
a lightweight in-terminal trend — a value or the progress rate over commits, or one value against
another as an arbitrary trajectory — is a natural fit. What it must respect is not the philosophy but
the **scale seam**.

## The two forms — split by cost, not by taste

The original ban braided two reasons together; only the identity one is relaxed. The **scale** reason
stands, and it cleanly partitions the feature:

- **Cheap trend (free, per-frame OK).** A zero-replay ring buffer over the already-sampled frontier /
  reading gives a sparkline-grade trend at ~no cost — explicitly *not* a data-plane replay (core spec
  §10 names it a Stage-3 candidate). Fits in a table cell or the drill-down card, fleet-wide. A
  reading carries the commit count at its node, so the buffer has a real x axis, not tick order.
- **Full trajectory (O(lineage), on-demand, one run).** A true value-over-commits curve is runstate's
  `series(ch, stream, head=)`, a one-shot lineage walk the fold budget bars from the per-frame path.
  Its home is a **drill-down action** on a single run — you pay the cost deliberately, once, when
  you ask, never across the fleet each tick.

## Committed seams it drops onto

- The **ring buffer** seam for the cheap trend already exists in the fold's clock-triggered factors
  (the frontier is sampled every tick regardless).
- The **drill-down** is already a codomain refinement of the same fold with its own view state — the
  natural host for an on-demand trajectory, alongside the raw envelope tail.
- `series` and `aligned` are public lineage reads the cockpit leaves deliberately **unused**; the full
  trajectory is their first (bounded, opt-in) consumer. A run that rewound has abandoned branches;
  `series` reads the head's lineage only, which is the curve to draw, and `AtNode` reads a branch.

## Open when built

- Unicode rendering: braille/block sparklines vs. a small braille-canvas plot; axis/scale policy.
- Which value(s): the group objective by default; a picker ties into the deferred
  [`metric-discovery`](metric-discovery.md).
- The value-vs-value case needs two series aligned on a common index. runstate's
  `aligned(ch, names, head=, axis=Commits())` is exactly that join, one row per node, so no
  cockpit-side reader is needed.

**Build it on a concrete need** (a real run whose trajectory you want in the terminal), with a fixture
to test against — otherwise defer (YAGNI). The cheap trend is the low-risk first slice.
