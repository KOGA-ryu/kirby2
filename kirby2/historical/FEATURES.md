# Historical feature evidence contract

Historical features use the same `FeatureKey` definitions and rolling-window
calculations as live simulation when the source provides the fields those
definitions require. Replay resets at historical time zero, consumes commands in
timestamp and source-sequence order, advances quiet windows through simulation
time, and emits either requested frames or the terminal frame.

Each value carries two independent labels:

- Availability is `AVAILABLE`, `UNDEFINED`, or `UNAVAILABLE`. `UNDEFINED` means
  the required evidence exists but the numeric expression is not currently
  defined, such as a midpoint on a one-sided book. `UNAVAILABLE` means the source
  does not support the claim and never carries a numeric zero.
- Provenance is `OBSERVED`, `DERIVED_FROM_SOURCE`,
  `SYNTHETIC_RECONSTRUCTION`, `COUNTERFACTUAL`, or `UNAVAILABLE`.

`SOURCE_ONLY` is the default evidence scope. A reconstruction source without
historical order or book messages therefore has unavailable queue features.
`INCLUDE_RECONSTRUCTION` is an explicit opt-in that exposes deterministic modeled
queue values labelled `SYNTHETIC_RECONSTRUCTION`; it never upgrades them to
observed evidence. Direct spread observations remain observed only at their
declared timestamps.

Trade-event counts require an ordered complete trade stream. Directional trade
volume and depletion also require aggressor side. Order-add and cancellation rates
require ordered order messages. Book, queue, midpoint, and depth features require
ordered order messages or source book states. Relative volume requires an explicit
historical baseline.

Historical strategy evaluation refuses `UNAVAILABLE` and `UNDEFINED` required
evidence by default. A strategy can deliberately choose a fallback directly after
its optional `window` line:

```text
unavailable AS_FALSE
```

The supported policies are `REFUSE`, `AS_FALSE`, and `AS_ZERO`. The selected
policy and every substituted condition remain visible in evaluation evidence.
