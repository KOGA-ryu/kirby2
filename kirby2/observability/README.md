# Hidden liquidity and observability boundary

`HiddenLiquidityVenue` owns a private mutable ground-truth ledger and returns only
fresh immutable `ObservableMarketFeed` values to player and strategy consumers.
The feed has no venue backlink, maker identity, reserve quantity, hidden quantity,
or ground-truth priority. A full `GroundTruthExchangeState` is gated until
`complete_session()` and carries the explicit label
`SIMULATOR_GROUND_TRUTH_POST_SESSION`.

This is generic simulator behavior. It does not claim to reproduce a named live
venue.

## Liquidity mechanisms

- Displayed limit orders expose their full remainder.
- Icebergs specify initial display, reserve, refresh quantity, automatic or manual
  refresh, and quote-only or explicit replenishment visibility.
- The venue rule `iceberg_refresh_priority` explicitly chooses whether refreshed
  slices preserve or lose priority.
- Fully hidden priced orders are allowed only when the venue rule permits them.
- Midpoint-hidden orders are allowed only when configured and execute at the
  displayed midpoint represented as integer half-ticks (`price_x2`), so no binary
  floating-point price enters matching.
- `hidden_priority` declares whether equally priced hidden quantity is before or
  after displayed quantity.

## Public uncertainty

Public depth is aggregate only. A decrease emits
`DISPLAY_QUANTITY_CHANGED` with attribution
`UNRESOLVED_FROM_PUBLIC_FEED` and retains all plausible causes: execution,
cancellation, refresh, feed delay, and snapshot replacement. Public trades and
explicit replenishment events supply evidence, but the feed does not silently turn
that evidence into exact opponent identity or reserve truth.

Feed latency uses simulation microseconds. Each public event records source and
receive time, and the published snapshot remains stale until its event is delivered.

## Queue and strategy boundary

`QueuePositionEstimator` returns an estimate, bounds, confidence, update time, and
assumptions for aggregate-depth data. It can return exact quantity ahead only when
the caller declares genuine market-by-order evidence and supplies that evidence.
The player's acknowledged order lifecycle and position remain exact without making
the surrounding opponent queue exact.

Traffic-light code receives `ObservableStrategyBook`, which contains displayed
aggregate depth, public strategy events, exact own working orders, and player
position. The canonical feature engine now accepts this minimal aggregate-depth
protocol; it never needs the private venue object.

## Scoring and replay

`ObservabilityScore` measures missed liquidity only from quantity observable at
decision time. Post-session revealed hidden liquidity is reported, but its penalty
is fixed to zero with status `NOT_SCORED_UNOBSERVABLE`.

Recordings contain the venue rules, ordered commands, final observable feed,
reveal-only ground truth, and full state digests. Replay compares all three
structurally and by digest.

```bash
python3 -m kirby2 hidden-liquidity-demo --scenario all
python3 -m kirby2 audit-hidden-liquidity
```
