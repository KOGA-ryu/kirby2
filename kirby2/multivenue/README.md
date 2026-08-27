# Fragmented synthetic market

`MarketCoordinator` owns independent `Venue` objects. Each venue wraps its own
hidden-liquidity matching engine, session state, fee schedule, supported order
instructions, seeded latency sampler, and asynchronous public feed. Venue books
must remain internally uncrossed; the consolidated public quote may temporarily
lock or cross because venue publications arrive independently.

Routers receive an immutable `ConsolidatedFeed`, never a venue or hidden ledger.
Every `RouteDecision` embeds the exact public snapshot and its SHA-256 digest, so a
post-session explanation can be judged using only evidence that existed at decision
time. `DIRECT`, `BEST_DISPLAYED_PRICE`, `LOWEST_EXPECTED_COST`, `PASSIVE_QUEUE`,
`SWEEP`, and `LATENCY_AWARE` are deterministic simulator baselines, not production
routing advice.

Prices remain integer ticks internally. Midpoint trades inherited from the hidden
venue use integer half-ticks (`price_x2`). Fees and rebates use integer micro-units
per share. Venue truth is gated until completion and is explicitly labelled
`SIMULATOR_GROUND_TRUTH_POST_SESSION`.

Run all drills and the runtime acceptance audit with:

```console
python -m kirby2 multivenue-demo --scenario all
python -m kirby2 audit-multivenue
```
