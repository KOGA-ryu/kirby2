# Execution algorithm bench

The benchmark algorithms are simulator-only comparison policies. They accept one
immutable `AlgorithmObservation` containing the objective, remaining quantity,
deadline, observable features, acknowledged working orders, received fills, client
latency state, visible venue state, and risk limits. They return exactly one of
`SUBMIT`, `CANCEL`, `REPLACE`, `WAIT`, or `FINISH`.

No algorithm receives a coordinator, venue matching engine, hidden-liquidity
ledger, scenario regime label, background-event schedule, future price, or future
historical observation. The benchmark runtime translates an accepted action into
the same routed order and cancellation interfaces used by the fragmented-market
simulator. Aggressive price limits are enforced at venue matching time as integer
ticks; they are not merely pre-trade quote checks.

The supplied baselines are `IMMEDIATE_MARKET`, `JOIN_BEST`, `IMPROVE_ONE_TICK`,
`PASSIVE_PEG`, `TWAP`, `VWAP_PROFILE`, `POV`, `SWEEP`, and
`IMPLEMENTATION_SHORTFALL_ADAPTIVE`. `MANUAL_REPLAY` allows a recorded player
action sequence to occupy another policy slot. Pass `--manual-replay PATH` with a
Kirby2 session recording to use a real player trace. The source recording must
first pass exact replay. Accepted one-way order actions are projected onto the
benchmark decision grid; source fills are never imported. The manifest records
each mapping, ignored control input, rejected input, source state/timeline digest,
and strategy-source digest. Two actions that land in one decision interval, reset,
flatten, and objective-side changes fail closed instead of being silently changed.
These are descriptive baselines, not live-trading executors or recommendations.

Each scenario/seed/policy run starts from the same reconstructed deterministic fork
state. Parameter manifests are recursively immutable after validation. Their full
inventory is strict, so misspelled or unsupported no-op fields fail closed. Each
run's parameter manifest, full observable decision evidence, routed command
recording, metrics, and digests are written to a content-addressed immutable run
directory. Verification checks artifact hashes and exact multi-venue replay; an
existing invalid run is never overwritten.

Metric units are explicit: average fill is an integer `price_x2` numerator over
filled shares; implementation shortfall, spread paid versus the observable
midpoint at each route decision, and adverse selection are signed integer
half-tick-shares; market impact is signed integer half-ticks versus
the no-action fork; fees and rebates are integer micros; completion is basis
points; elapsed time is simulation microseconds. `deadline_failure` uses fills
visible to the client by the deadline. Aggregate averages retain integer
numerators and denominators rather than introducing binary floating-point values.
`cancel_count` counts venue child orders actually cancelled, including the cancel
component of replacements, rather than merely counting requested cancel actions.
Because the current fragmented coordinator processes cancel-all venue responses
synchronously, a benchmark containing cancel-capable policies refuses decision
intervals shorter than the summed bounded cancel latency. That prevents latency
from pushing exogenous events past their committed timestamps and silently making
comparison paths unequal.

```console
python -m kirby2 benchmark-execution \
  --scenario opening_momentum \
  --algorithms twap,pov,adaptive,manual_replay \
  --seeds 100:103 \
  --manual-replay player-session.json
python -m kirby2 audit-execution-algorithms
```
