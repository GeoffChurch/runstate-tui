# Value trend / trajectory — a lightweight in-terminal plot

**Status: deferred, seam committed.** The core has always been control-plane-focused ("no scientific
plots"), but that separation is a *stance, not a law* (owner, 2026-07-24): a lightweight in-terminal
trend — a value or the progress rate over steps, or one value against another as an arbitrary
trajectory — is a natural fit. What it must respect is not the philosophy but the **scale seam**.

## The two forms — split by cost, not by taste

The original ban braided two reasons together; only the identity one is relaxed. The **scale** reason
stands and cleanly partitions the feature:

- **Cheap trend (free, per-frame OK).** A zero-replay ring buffer over the already-sampled frontier /
  value gives a sparkline-grade trend at ~no cost — explicitly *not* a data-plane replay (core spec
  §10 names it a Stage-3 candidate). Fits in a table cell or the drill-down card, fleet-wide.
- **Full trajectory (O(N), on-demand, one run).** A true value-over-steps or value-vs-value curve is
  the `value_series` replay (~1.9 s at 10⁶ envelopes) the fold budget bars from the per-frame path.
  Its home is a **drill-down action** on a single run — you pay the O(N) cost deliberately, once, when
  you ask, never across the fleet each tick.

## Committed seams it drops onto

- The **ring buffer** seam for the cheap trend already exists in the fold's clock-triggered factors
  (the frontier is sampled every tick regardless).
- The **drill-down** is already a codomain refinement of the same fold with its own view state — the
  natural host for an on-demand trajectory, alongside the raw envelope tail.
- `value_series` is a public observable the cockpit currently leaves deliberately **unused**; the full
  trajectory is its first (bounded, opt-in) consumer.

## Open when built

- Unicode rendering: braille/block sparklines vs. a small braille-canvas plot; axis/scale policy.
- Which value(s): the group objective by default; a picker ties into the deferred
  [`metric-discovery`](metric-discovery.md).
- The value-vs-value case needs two series aligned on a common step index — a join, not a fold; decide
  whether that lives in the drill-down query or a small dedicated reader.

**Build it on a concrete need** (a real run whose trajectory you want in the terminal), with a fixture
to test against — otherwise defer (YAGNI). The cheap trend is the low-risk first slice.
