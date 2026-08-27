# Deterministic latency and asynchronous order lifecycle

`AsynchronousExecutionSession` is a discrete-event transport layer around the
same FIFO `OrderBook` used by synchronous Kirby2 sessions. Simulation time is the
only clock. No wall-clock sleeping, networking, or global random state is used.

The existing `LiveMarketSession` remains the explicit zero-latency compatibility
path for recordings created before Work Order 23. This package owns the new
asynchronous path so transport semantics do not silently alter old replays.

## Model boundary

Every market-data update travels through publication, downlink, and render
stages. Every player order records key press, client creation, client departure,
gateway receipt, venue receipt, and venue acknowledgement or rejection. Exchange
fills are authoritative; fill reports then travel independently to the client and
UI. Client position is a delayed view, while `player_position` is always derived
from exchange fills.

The nine configurable components use an owned seeded sampler. Fixed, bounded
uniform, bounded lognormal, and empirical-sample distributions are available.
The named `ZERO_LATENCY`, `LOW_LATENCY`, `NORMAL`, `STRESSED`, and `UNSTABLE`
profiles are simulator labels only and are serialized into recordings.

Orders transition through `CREATED`, `PENDING_NEW`, `WORKING`,
`PARTIALLY_FILLED`, `PENDING_CANCEL`, `CANCELLED`, `FILLED`, `REJECTED`, or
`EXPIRED`. A cancellation can lose to a fill. A pre-ack replace is explicitly
rejected. If a fast cancellation overtakes its pending new order at the gateway,
the gateway holds it until the venue resolves the new order, preserving causal
new-before-cancel ordering. Out-of-order market-data renders are version-checked
and stale updates are discarded.

`LatencyRecording` stores the seed, full profile, initial book, time-ordered
commands, completion time, and expected event/state digests. Exact replay rebuilds
the session and must match both digests. The state digest includes pending
messages, gateway-held cancels, the displayed state, counters, RNG draw trace,
orders, fills, events, and book state.

## Inspection and acceptance

```bash
python3 -m kirby2 latency-demo --race cancel-wins --seed 42
python3 -m kirby2 latency-demo --race fill-wins --seed 42
python3 -m kirby2 audit-latency
```

The demo prints exact intention/venue metrics, event and recording digests, replay
status, and a microsecond market-time timeline. The audit also exercises all four
distribution families, all five profiles, moved-liquidity stale-quote slippage,
replace-before-ack rejection, the fast-cancel gateway hold, partial-fill
cancellation, market-order expiration, and venue rejection.
