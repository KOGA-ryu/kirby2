# Kirby2 Sequential Execution Roadmap

**Status:** Canonical execution queue  
**Scope:** Review repairs R01-R05, then Work Orders 21-30  
**Source:** User-supplied Work Orders 21-30, preserved verbatim below  
**Expansion boundary:** Stop after Work Order 30

## Goal objective

Complete every slice in the sequence below. Work on exactly one slice at a time. A
slice is complete only after its acceptance evidence is produced, relevant
determinism and runtime-invariant gates pass, the exact files and limitations are
reported, and the slice is committed. Retrieve and begin the next slice only after
the preceding slice's commit is verified and the worktree is clean.

The five repair gates come first because Work Order 21 creates an immutable evidence
store. Known-wrong stability, time, configuration, feature, or lesson behavior must
not be canonized in that store.

## Sequence

| Order | Slice | Dependency | Initial state |
|---|---|---|---|
| R01 | Certified Hawkes stability | Review finding | PENDING |
| R02 | Deterministic strategy-time scheduler | R01 | PENDING |
| R03 | Distribution configuration truth | R02 | PENDING |
| R04 | Historical feature replay and provenance | R03 | PENDING |
| R05 | Interactive historical lesson runtime | R04 | PENDING |
| 21 | Immutable Run Ledger + Research Store | R05 | PENDING |
| 22 | Market Data Normalization + Ingestion | 21 | PENDING |
| 23 | Latency + Asynchronous Order Lifecycle | 22 | PENDING |
| 24 | Advanced Exchange + Session Mechanics | 23 | PENDING |
| 25 | Hidden Liquidity + Observability Boundary | 24 | PENDING |
| 26 | Fragmented Multi-Venue Market | 25 | PENDING |
| 27 | Execution Algorithm Bench | 26 | PENDING |
| 28 | Counterfactual Execution Debugger | 27 | PENDING |
| 29 | Synthetic Market Agent Ecology | 28 | PENDING |
| 30 | Model-Risk Laboratory + Generative Audit | 29 | PENDING |

## Per-slice execution protocol

1. Confirm the current HEAD and inspect the worktree before touching the slice.
2. Read only the current slice specification and its already-completed dependencies.
3. Implement the smallest complete design that satisfies the current acceptance
   criteria. Do not prebuild later work-order architecture unless the current slice
   genuinely requires it.
4. Preserve integer-tick exchange prices, explicit seeded RNG ownership, simulation
   time, canonical event sequencing, replayability, observable-versus-hidden
   boundaries, and evidence labels.
5. Run the slice's acceptance demonstration plus relevant existing regression,
   determinism, replay, and runtime-invariant checks.
6. Report the exact files created or modified, actual commands and output, accepted
   digest changes, invariant failures, unavailable evidence, and remaining
   limitations.
7. Commit only the completed slice. Verify the commit and a clean worktree.
8. Retrieve the next slice only after step 7. Do not combine two numbered slices in
   one implementation commit.
9. Stop rather than inventing data, silently weakening an invariant, claiming
   unsupported historical capability, or broadening into live trading.
10. After Work Order 30 is accepted and committed, stop. The expansion batch
    described after it is not part of this goal.

Before Work Order 21 exists, evidence is the actual command output and committed
artifact set reported at the slice boundary. From Work Order 21 onward, every
eligible acceptance run must also be stored in the immutable ledger and identified
by run ID and digest.

## Cross-slice gates

These properties remain active through every slice:

- Same version, canonical configuration, seed, and incoming commands produce the
  same canonical event and result digests.
- One large simulation-time advance and equivalent smaller advances produce the
  same outcome unless an explicitly recorded input differs.
- Replay and fork restoration preserve all declared deterministic state.
- Every accepted behavioral configuration field has a documented runtime consumer;
  unsupported fields are rejected rather than silently ignored.
- Runtime invariants fail closed and preserve a compact reproducer.
- Historical values distinguish observed, derived, reconstructed, unavailable, and
  counterfactual information. Missing information is never encoded as measured zero.
- Player, strategy, router, and algorithm decisions consume only information
  observable at their decision time.
- Any accepted replay-digest change is explained by the current slice and explicitly
  re-accepted; hashes are never updated merely to make an audit green.
- Audit commands and runtime probes are preferred to conventional test-suite
  bureaucracy, consistent with the project work orders.

## Repair Gate R01: Certified Hawkes stability

### Goal

Replace the oscillation-prone Hawkes spectral-radius estimate with a deterministic,
fail-closed stability certification for the nonnegative branching matrix.

### Required behavior

- Validate finite nonnegative branching entries before estimation.
- Compute lower and upper spectral-radius bounds robustly for reducible and periodic
  matrices. A suitable deterministic design is strongly connected component
  decomposition followed by shifted Collatz-Wielandt bounds on each irreducible
  block.
- Accept only when the certified upper bound is safely below one.
- Reject a certified supercritical matrix.
- Reject an unconverged or numerically ambiguous matrix as stability unverified.
- Expose method, lower bound, upper bound, iteration count, safety margin, and
  classification in diagnostics.
- Retain runtime intensity caps as containment, not as proof of stability.
- Do not otherwise change accepted Hawkes arrival semantics.

### Acceptance

- The two-channel cycle with branching weights 4.0 and 0.4 is rejected; its true
  spectral radius is approximately 1.264911.
- Known stable, near-critical, reducible, zero, and accepted packaged profiles are
  classified correctly.
- Same accepted configuration and seed reproduce identical diagnostics and replay.
- Existing accepted scenario and flow-comparison invariant gates pass, with any
  digest change explained.
- Commit R01 before starting R02.

## Repair Gate R02: Deterministic strategy-time scheduler

### Goal

Make simulation time, rather than exchange activity, authoritative for feature
expiry, timed strategy conditions, cooldowns, and state transitions.

### Required behavior

- Schedule the next boundary from synthetic or historical arrivals, feature expiry,
  TRUE_FOR deadlines, cooldown expiry, and the caller's requested target.
- Establish and document one total ordering for simultaneous clock, exchange,
  feature, and strategy activity.
- Evaluate outgoing predicates at reset and seed TRUE_FOR timing when already true.
- Transition at the exact qualifying simulation time, including during a quiet book.
- Prune time-window event history even when no new exchange event arrives.
- Prevent zero-time transition cycles with a deterministic transition bound and an
  explicit invariant failure.
- Emit replay-suitable strategy evaluation and transition events without
  double-consuming exchange events.

### Acceptance

- With zero flow, a condition true for 500 ms transitions at 500 ms.
- One 1-second advance and ten 100-ms advances produce identical canonical results.
- Cooldown and occurred-within/event-window behavior expires at the correct simulated
  time during quiet periods.
- Same-time flow and strategy deadlines follow the documented ordering.
- Existing live-session, feature, state-machine, experiment, replay, and invariant
  demonstrations pass, with any digest changes explained.
- Commit R02 before starting R03.

## Repair Gate R03: Distribution configuration truth

### Goal

Ensure every required distribution purpose has documented units and an actual
deterministic runtime consumer.

### Required behavior

- Preserve existing order-size, trade-size, queue-depth, and placement behavior.
- Define CANCEL_SIZE as a target cancellation-volume budget. Cancel only actual
  active simulated liquidity using whole-order exchange cancellation; record
  requested, actual, overshoot or unfulfilled quantity, and affected order IDs.
- Define INTER_EVENT_TIMING_MODIFIER in an explicit scale such as basis points where
  10,000 means 1x, and apply it to sampled simulated interarrival duration.
- Define SPREAD_STATE_DURATION in simulation-time units. Use it to govern a latent
  quote-placement or liquidity state; it may influence ordinary order flow but may
  not directly command exchange prices.
- Keep every draw under explicitly owned seeded RNG state.
- Provide an inspectable diagnostic draw record containing draw sequence, profile,
  purpose, sampled value, simulation time, and consumer.
- Reject unsupported or dimensionally invalid profile fields rather than accepting
  no-ops.

### Acceptance

- Extreme fixed variants of every distribution purpose alter their declared
  behavioral output over a deterministic seed matrix while preserving invariants.
- Cancellation never targets nonexistent liquidity.
- Timing and spread-duration changes do not create impossible crossed resting orders
  or direct scripted price changes.
- Same profile and seed produce identical draw traces and replay; different seeds
  produce different valid behavior.
- Existing accepted scenario hashes are changed only with an explicit behavioral
  explanation and re-acceptance.
- Commit R03 before starting R04.

## Repair Gate R04: Historical feature replay and provenance

### Goal

Drive the canonical feature engine from ordered historical activity and represent
missing source capability as unavailable rather than measured zero.

### Required behavior

- Reset at the historical start, consume supported ordered events, advance feature
  windows through simulation time, and return requested frames or the terminal frame.
- Attach explicit availability and provenance to historical feature values.
- Preserve distinctions among observed, derived from source, synthetic
  reconstruction, counterfactual, and unavailable values.
- Gate each feature on source capability and required fields.
- Refuse strategy evaluation that requires unavailable evidence unless the strategy
  explicitly defines an unavailable-value policy.
- Retain the same feature definitions for live and historical paths wherever source
  capability genuinely supports them.

### Acceptance

- An exact fixture with trades produces nonempty supported rolling trade features.
- A source without order-level evidence reports queue features as unavailable, not
  zero.
- Reconstructed queue features are labelled synthetic or derived from reconstruction,
  never observed.
- Timestamp gaps, identical timestamps, unsupported aggressor side, and final quiet
  window expiry produce deterministic declared results.
- Historical replay, feature invariants, lessons, and provenance output pass.
- Commit R04 before starting R05.

## Repair Gate R05: Interactive historical lesson runtime

### Goal

Turn packaged historical lessons into real blind training sessions rather than
already-complete presentations.

### Required behavior

- Implement explicit READY, BLIND_RUNNING, QUESTIONS, COMPLETE, REVEALED, and
  DEBRIEFED phases or equivalent states with the same separation.
- Advance or step the source through simulation time while withholding protected
  identity and outcome information until completion.
- Record player actions and question responses with observable decision-time context.
- Freeze responses before reveal and compare them with the evidence in the debrief.
- Preserve the source replay as authoritative. Counterfactual player execution must
  live in a separate labelled overlay and must not mutate recorded history.
- Permit genuine queue or fill claims only when source capability supports them.
  Otherwise provide an observation/decision lesson or an explicitly synthetic
  counterfactual.
- Prevent presentation models used by the blind phase from accessing reveal-only
  fields.

### Acceptance

- A newly started lesson is incomplete and does not reveal protected identity.
- The player can advance, pause or step, answer the configured questions, and reach
  completion deterministically.
- Reveal and debrief become available only after responses are frozen.
- Exact, partial-observation, and reconstruction lessons expose only supported claims.
- Replaying the same lesson inputs produces identical session events, responses,
  presentation states, and result digest.
- Commit R05 before starting Work Order 21.

---

# User-supplied Work Orders 21-30

The following roadmap text is preserved verbatim. Its per-work-order stop boundaries
remain authoritative inside the sequential goal.

Absolutely. The next batch makes Kirby2 less of a simulator and more of an execution laboratory: real-data plumbing, latency races, hidden liquidity, fragmented venues, algorithm benchmarking, counterfactual replay, and hostile synthetic markets.

Do these in order. Work Order 21 provides the evidence spine needed by everything after it.

### WORK ORDER 21: Immutable run ledger and research store

```text
KIRBY2 WORK ORDER 21
Immutable Run Ledger + Research Store

Assume Work Orders 01 through 20 are complete.

Goal:
Create a canonical persistent record for every simulation, replay, lesson, calibration run and experiment.

Kirby2 now has enough subsystems that console output and scattered files are no longer acceptable evidence.

Use:

TOML for manifests and human-authored configuration
Parquet for event and measurement tables
DuckDB for querying and derived views

Avoid JSON unless an external format genuinely requires it.

1. Define an immutable RunManifest containing at least:

run_id
parent_run_id where applicable
run_type
scenario_id
lesson_id where applicable
seed
flow model
market profile
strategy ID
hotkey layout ID
session objective
simulation start/end
software version
git commit
schema versions
input dataset references
configuration digest
result digest
creation timestamp

2. Generate run IDs from canonical run identity rather than filename order.

The same completed run identity must resolve predictably.

Do not silently overwrite an existing run with different contents.

3. Persist canonical tables for:

events
book snapshots
orders
fills
trades
player actions
traffic-light transitions
strategy states
features
latency messages
scores
calibration metrics
experiment membership
data provenance

Do not duplicate derived values unnecessarily.

4. Preserve the complete replay chain.

A stored run must contain enough information to identify:

starting state
scenario configuration
seed
player actions
flow model state
latency configuration
historical source where applicable

5. Add content digests for immutable artifacts.

6. Add schema versioning.

Do not build a giant migration framework, but make old schema versions detectable rather than silently misread.

7. Build DuckDB views for:

run summary
execution summary
strategy summary
scenario summary
historical lesson summary
experiment comparison
invariant violations

8. Add commands conceptually equivalent to:

kirby2 record-run ...
kirby2 inspect-run RUN_ID
kirby2 query-runs --scenario absorption
kirby2 verify-run RUN_ID

9. `verify-run` must check:

manifest references exist
artifact digests match
event sequence is complete
replay configuration is available
result digest matches
schema versions are supported

10. Do not build a new GUI in this work order.

Acceptance:

Run one synthetic training session, persist it, close the process, load it again, verify all digests, reproduce its summary and replay it deterministically.

When complete:

1. List exact files created or modified.
2. Show the stored directory/table layout.
3. Show actual commands and output.
4. Report any information that still prevents exact replay.
5. Stop.

Do not begin Work Order 22.
```

### WORK ORDER 22: Real market-data normalization

```text
KIRBY2 WORK ORDER 22
Market Data Normalization + Ingestion

Assume the immutable run ledger exists.

Goal:
Create a strict adapter boundary for importing real market data without pretending the source contains information it does not contain.

Do not acquire paid datasets or scrape websites in this work order.

Start from small local fixtures.

1. Define explicit source capability levels:

BARS_ONLY
TRADES
TRADES_AND_QUOTES
LEVEL2_SNAPSHOTS
LEVEL2_DELTAS
MARKET_BY_ORDER

2. Every imported dataset must declare its capability level.

Examples:

Bars do not provide actual trades.
Market-by-price does not provide exact individual queue ordering.
Snapshots do not reveal every event between snapshots.
Historical reconstruction is not exact replay.

Kirby2 must preserve those distinctions structurally.

3. Define normalized records for:

bar
trade
quote
book snapshot
book delta
order event where genuinely available
auction event
halt/resume event
symbol metadata
session metadata

4. Normalize timestamps into one canonical internal representation.

Preserve:

source timestamp
normalized timestamp
source timezone
timestamp precision
sequence number where supplied

Do not create fake nanosecond precision from second-resolution data.

5. Build adapters for at least:

normalized CSV fixture
normalized Parquet fixture
one representative vendor-like fixture format already available locally

6. Detect and report:

duplicate records
out-of-order records
missing sequences
crossed quotes
negative quantities
invalid prices
timestamp reversals
session-boundary problems
unknown aggressor side
snapshot gaps

Do not silently repair questionable source records.

Repairs must be explicit and recorded.

7. Create a DataQualityReport containing:

input rows
accepted rows
rejected rows
warnings
capability level
time range
symbols
session count
gaps
repairs
source digest

8. Exact replay may only be offered when supported by the source capability.

Otherwise route the dataset into reconstruction or partial-observation mode.

9. Store provenance in the Work Order 21 ledger.

10. Add commands conceptually equivalent to:

kirby2 ingest-market-data --adapter csv SOURCE
kirby2 inspect-dataset DATASET_ID
kirby2 validate-dataset DATASET_ID
kirby2 replay-capability DATASET_ID

Acceptance:

Import all fixtures, generate quality reports and demonstrate that Kirby2 refuses to call bar-only or quote-only information exact Level 2 replay.

Show actual validation output and stop.
```

### WORK ORDER 23: Latency and asynchronous order lifecycle

```text
KIRBY2 WORK ORDER 23
Latency + Asynchronous Order Lifecycle

Goal:
Replace instantaneous player actions with a deterministic discrete-event communications model.

Do not use wall-clock sleep calls to simulate latency.

Simulation time remains authoritative.

1. Model separate timestamps for:

market event occurs
market data published
client receives market data
UI renders state
player presses key
client creates order
order leaves client
broker or gateway receives order
venue receives order
venue acknowledges or rejects order
fill occurs
fill report leaves venue
client receives fill
UI displays fill

2. Add configurable latency components:

market-data publication latency
downlink latency
render latency
input processing latency
client routing latency
uplink latency
gateway latency
venue processing latency
fill-report latency

3. Support deterministic latency distributions.

At minimum:

fixed
uniform bounded
lognormal bounded
empirical samples

All latency RNG must be owned and seeded.

4. Implement an asynchronous order state machine:

CREATED
PENDING_NEW
WORKING
PARTIALLY_FILLED
PENDING_CANCEL
CANCELLED
FILLED
REJECTED
EXPIRED

5. Correctly model races.

Examples:

A fill occurs while cancellation is travelling.
A replace is requested before the original acknowledgement arrives.
The player acts on a stale displayed quote.
A market order reaches the venue after liquidity has moved.
A cancel acknowledgement arrives after a fill report.

6. Player position must be derived from exchange fills, not client assumptions.

7. Record intention time separately from venue execution time.

8. Create configurable profiles such as:

ZERO_LATENCY
LOW_LATENCY
NORMAL
STRESSED
UNSTABLE

These names are simulator profiles, not claims about real firms or networks.

9. Extend scoring with:

decision-to-send latency
send-to-ack latency
send-to-fill latency
observed quote age
execution against stale quote
cancel race outcome
latency-induced slippage

10. Build a latency inspection timeline.

Example:

09:31:15.442000  key pressed
09:31:15.443200  order sent
09:31:15.455700  venue received
09:31:15.456000  order accepted
09:31:15.460400  fill occurred
09:31:15.474900  client received fill

Acceptance:

Create one deterministic scenario where a cancellation wins the race and another where the order fills before cancellation reaches the venue.

Replay both exactly and stop.
```

### WORK ORDER 24: Advanced exchange mechanics

```text
KIRBY2 WORK ORDER 24
Advanced Exchange + Session Mechanics

Goal:
Expand the exchange from a continuous FIFO book into a configurable market-mechanics engine.

Do not tie the lowest-level implementation to one real exchange or jurisdiction.

1. Add configurable instrument rules:

tick size
lot size
minimum quantity
maximum quantity
price bands
supported order instructions
session schedule

2. Add order instructions:

LIMIT
MARKET
MARKETABLE_LIMIT
IOC
FOK
POST_ONLY
DAY
SESSION
GOOD_UNTIL_TIME

Preserve existing simple order behavior.

3. Define exact semantics for every instruction.

Examples:

POST_ONLY rejects rather than crossing.
IOC fills immediately available quantity and cancels the remainder.
FOK either fills the entire requested quantity immediately or fills none.

4. Implement cancel/replace priority rules explicitly.

Quantity increase should normally lose priority.

Quantity reduction may preserve priority only when the configured venue rules permit it.

5. Add configurable self-trade prevention modes for simulator agents and player accounts.

6. Add session states:

CLOSED
PREOPEN
OPENING_AUCTION
CONTINUOUS
HALTED
REOPENING_AUCTION
CLOSING_AUCTION
POSTCLOSE

7. Implement a basic auction book with:

auction-only orders
indicative clearing price
matched quantity
imbalance quantity
deterministic uncrossing
allocation rules documented in code

8. Add generic configurable protections:

price collars
volatility interruption
order-price rejection
maximum order size
fat-finger protection

Do not claim these replicate any specific live venue unless explicitly calibrated later.

9. Ensure replay and event logging cover:

rejections
expirations
auction indications
halts
resumes
uncrossing fills
protection triggers

10. Add demonstration scenarios:

opening auction
closing auction
halt during momentum
reopening gap
IOC partial fill
FOK rejection
post-only rejection

Acceptance:

Each scenario must pass exchange invariants and produce a concise event timeline explaining the result.

Stop after completion.
```

### WORK ORDER 25: Hidden liquidity and queue uncertainty

```text
KIRBY2 WORK ORDER 25
Hidden Liquidity + Observability Boundary

Goal:
Separate the true exchange state from what the player and strategy can legitimately observe.

This boundary must be architectural, not merely a UI toggle.

1. Maintain two distinct representations:

GROUND_TRUTH_EXCHANGE_STATE
OBSERVABLE_MARKET_FEED

Player code and strategy scripts may only consume the observable feed.

2. Implement configurable undisplayed liquidity mechanisms:

reserve or iceberg orders
fully hidden resting orders where the venue permits
midpoint-style hidden matching where configured

3. Iceberg orders must define:

display quantity
reserve quantity
refresh behavior
priority behavior after refresh
event visibility

Do not choose ambiguous refresh priority accidentally. Make it a venue rule.

4. A displayed quantity disappearing must not automatically prove cancellation.

It may represent:

execution
cancellation
refresh
feed delay
snapshot replacement

Preserve this uncertainty.

5. Replace exact opponent queue position with an estimator unless the data mode genuinely supports market-by-order information.

The estimator may expose:

estimated quantity ahead
lower bound
upper bound
confidence
last update time
assumptions

6. The player's own acknowledged order state may be known more precisely than the surrounding queue.

Model that distinction.

7. Add hidden-liquidity scenario types:

iceberg absorption
hidden midpoint fill
displayed queue repeatedly refreshing
apparent wall with little executable quantity
small displayed book with deep hidden liquidity

8. Traffic-light scripts must not gain access to reserve quantity or ground-truth queue state.

9. Post-session analysis may reveal hidden state, clearly labelled as simulator ground truth.

10. Adjust scoring so the player is not penalized for failing to know information that was not observable.

Acceptance:

Create a blind exercise in which identical displayed books contain materially different hidden liquidity.

The trainee should need to infer the difference from fills, replenishment and tape behavior.

Stop after completion.
```

### WORK ORDER 26: Multi-venue market and routing

```text
KIRBY2 WORK ORDER 26
Fragmented Multi-Venue Market

Goal:
Allow multiple independent venues to trade the same instrument with different liquidity, latency, fees and order behavior.

This remains entirely synthetic and offline.

1. Create a MarketCoordinator that owns multiple Venue instances.

Each venue keeps its own:

order book
matching rules
latency profile
fees and rebates
supported order instructions
displayed depth
hidden liquidity rules
session state

2. Add a consolidated observable feed containing:

best displayed bid
best displayed ask
venue attribution
consolidated trades
per-venue depth where subscribed
quote age

Do not assume all feeds arrive simultaneously.

3. Allow temporary locked or crossed composite quotes caused by latency or independent venue changes.

Do not create crossed orders inside one venue unless its rules permit them.

4. Support player routing choices:

specific venue
best displayed price
passive best venue
sweep available venues
router-selected venue

5. Add a SmartOrderRouter interface with initial implementations:

DIRECT
BEST_DISPLAYED_PRICE
LOWEST_EXPECTED_COST
PASSIVE_QUEUE
SWEEP
LATENCY_AWARE

These are simulator baselines, not production routing recommendations.

6. Router decisions may use only information available at decision time.

No hidden venue state or future path.

7. Model:

partial fills across venues
venue rejection
venue-specific queue position
routing latency
fees
rebates
stale consolidated quotes
cancel-all across multiple venues

8. Reconcile one global player position from all venue fills.

9. Extend execution scoring with:

gross execution price
fees
rebates
net execution cost
routing delay
venue selection quality
missed better displayed price
stale-quote exposure

10. Build drills for:

one venue shows better price but poor fill probability
deep slow venue versus shallow fast venue
sweep during momentum
passive routing across two venues
stale composite quote
partial multi-venue completion

Acceptance:

A routed order must be explainable from the information available when it was routed.

No route may silently use hidden future knowledge.

Stop after completion.
```

### WORK ORDER 27: Execution algorithm benchmark bench

```text
KIRBY2 WORK ORDER 27
Execution Algorithm Bench

Goal:
Create benchmark execution agents that can be compared against the player's manual execution and traffic-light strategy.

These algorithms operate only inside Kirby2.

Do not produce live brokerage connectors or exportable live-trading executors.

1. Define a common ExecutionAlgorithm interface.

Inputs:

objective
remaining quantity
deadline
observable market features
working orders
fills
latency state
venue state available to the client
risk limits

Outputs:

submit
cancel
replace
wait
finish

2. Implement initial benchmark algorithms:

IMMEDIATE_MARKET
JOIN_BEST
IMPROVE_ONE_TICK
PASSIVE_PEG
TWAP
VWAP_PROFILE
POV
SWEEP
IMPLEMENTATION_SHORTFALL_ADAPTIVE

3. Preserve parameter manifests for each algorithm.

Examples:

participation rate
urgency
minimum slice
maximum slice
price limit
passive timeout
deadline curve
maximum spread
venue preference

4. Algorithms must obey the same latency, observable-data and order-state restrictions as the player.

5. No algorithm may inspect:

hidden regime label
future events
ground-truth hidden liquidity
future historical prices

6. Use forked deterministic runs so algorithms can be compared from identical starting states.

7. Benchmark metrics:

completion
average fill
implementation shortfall
spread paid
fees
adverse selection
market impact
time
cancel count
fill uncertainty
deadline failure

8. Add experiment commands conceptually equivalent to:

kirby2 benchmark-execution \
    --scenario opening_momentum \
    --algorithms twap,pov,adaptive,manual_replay \
    --seeds 100:199

9. Report per-seed and aggregate results.

Do not declare one universally best algorithm.

10. Allow the player replay to be included as another execution policy.

Acceptance:

Run at least four algorithms over a multi-seed scenario set and produce an auditable comparison linked to immutable run records.

Stop afterward.
```

### WORK ORDER 28: Counterfactual execution debugger

```text
KIRBY2 WORK ORDER 28
Counterfactual Execution Debugger

Goal:
Answer questions such as:

What if I waited 250 ms?
What if I joined the bid instead of crossing?
What if I used 200 shares rather than 1,000?
What if I cancelled one event earlier?
What if I routed to the other venue?

1. Support branching from a stored simulation point.

A branch snapshot must preserve:

exchange state
all venue states
flow-model state
Hawkes decay state
RNG state
simulation clock
pending latency messages
working orders
player state
strategy state
agent state
historical replay cursor
feature windows

2. Implement two explicit counterfactual modes.

EXOGENOUS_REPLAY:

The external market path remains fixed.
Player orders do not alter the reference path.

Use this for decision comparison when player impact is intentionally ignored.

ENDOGENOUS_FORK:

The full simulator branches.
Changed orders may alter queues, agents and subsequent market events.

Use this when market interaction matters.

Never confuse the two.

3. Allow one or more action mutations:

change command
change order type
change price
change size
change venue
change timing
remove action
insert action
change hotkey mapping outcome

4. Produce paired timelines showing where the original and branch first diverged.

5. Compare:

fill
completion
slippage
fees
adverse selection
position
risk
traffic-light state
deadline
P&L where applicable

6. Add batch sensitivity sweeps.

Example:

original timing
minus 500 ms
minus 250 ms
plus 250 ms
plus 500 ms

7. Prevent hindsight leakage.

A counterfactual report may use later information for analysis, but the alternative decision policy may only use information available at its decision time.

8. Record every branch with a parent run ID and mutation manifest.

9. Use cautious language.

A simulated counterfactual is evidence about Kirby2's model, not proof of what the real market would have done.

10. Add command conceptually equivalent to:

kirby2 counterfactual RUN_ID \
    --at action:37 \
    --replace "BUY_ASK" \
    --with "JOIN_BID" \
    --mode endogenous

Acceptance:

Demonstrate one exogenous and one endogenous branch where the outcomes differ for understandable reasons.

Stop after completion.
```

### WORK ORDER 29: Agent ecology and adversarial drills

```text
KIRBY2 WORK ORDER 29
Synthetic Market Agent Ecology

Goal:
Generate richer market behavior from interacting participants rather than one centralized regime generator.

This work is for synthetic training and defensive pattern recognition only.

Do not add live exchange connectivity, real-market message emission or exportable manipulation tooling.

1. Define a MarketAgent interface.

Agents observe permitted market information, maintain private state and submit ordinary exchange orders.

Agents must not directly modify prices or queues.

2. Implement initial synthetic agent families:

NOISE_TRADER
PASSIVE_MARKET_MAKER
INVENTORY_SENSITIVE_MARKET_MAKER
MOMENTUM_TRADER
MEAN_REVERSION_TRADER
SCHEDULED_METAORDER
DISTRESSED_LIQUIDATOR
LIQUIDITY_WITHDRAWER
LATENT_VALUE_TRADER
AUCTION_PARTICIPANT

3. Add a DECEPTIVE_DISPLAY training agent only for simulator recognition drills.

Strict constraints:

It operates only inside Kirby2.
It has no live-market adapter.
Its parameters are not exported as real-market tactics.
The curriculum focuses on identifying unreliable displayed liquidity and managing execution risk.

4. Every agent should have bounded:

capital or quantity budget
inventory
order rate
risk
information set
latency
lifetime

5. Agents must not be omniscient unless a scenario explicitly marks one as a controlled latent-information actor.

6. Allow scenarios to compose populations.

Example:

8 noise agents
3 market makers
1 scheduled buyer
1 liquidity withdrawer

7. Regime labels may describe the resulting scenario, but price changes still emerge through submitted orders and matching.

8. Add adversarial drills:

liquidity mirage
repeated wall withdrawal
absorption with hidden reserve
momentum ignition followed by exhaustion
distressed liquidation
stop-like cascade
auction imbalance reversal
halt and disorderly reopen

9. Record agent actions in ground-truth logs but hide identity and intent from the player.

10. Post-session analysis may explain which actors generated the observed behavior.

Acceptance:

Run several populations from identical starting books and show that changing participant composition produces materially different order-flow ecology.

Stop after completion.
```

### WORK ORDER 30: Model-risk laboratory and generative audit

```text
KIRBY2 WORK ORDER 30
Model-Risk Laboratory + Generative Audit

Goal:
Aggressively attack Kirby2's correctness without creating a conventional unit-test bureaucracy.

Use:

runtime invariants
deterministic replay
generative scenario sweeps
accepted scenario evidence
statistical holdouts
failure minimization

1. Build a generative configuration runner that can vary:

seed
flow model
regime
volume
liquidity
latency
session phase
order types
hidden liquidity
venue count
auction state
agent population
strategy
objective

2. Enforce structural invariants including:

quantity conservation
cash and position reconciliation
no negative resting quantity
valid order-state transitions
valid event ordering
venue book ordering
auction allocation consistency
global position equals venue fills
no fill after terminal cancellation unless explained by a race
replay digest parity
branch parent consistency
observable layer contains no hidden fields

3. Add determinism audits.

Run the same configuration multiple times in fresh processes and compare canonical event/result digests.

4. Add replay parity audits.

Original run and loaded replay must produce the same declared outputs.

5. Add fault injection for:

duplicate messages
dropped market-data message
delayed acknowledgement
out-of-order delivery
snapshot gap
corrupted dataset row
venue rejection
halt during pending order
cancel/fill race
schema mismatch

Faults must be explicit and recorded.

6. Build failure minimization.

When a generated run fails, automatically attempt to reduce:

duration
event count
agent count
venue count
configuration complexity

while preserving the violation.

Output the smallest practical reproducer.

7. Add statistical model-risk checks:

calibration train versus holdout
distribution drift
scenario overfitting
seed sensitivity
unstable Hawkes configuration
unrealistic event explosion
degenerate no-trade simulation
price runaway
permanent crossed composite quote

8. Preserve manual acceptance records for scenarios requiring human behavioral judgment.

An acceptance record should contain:

scenario version
seed
reviewer decision
observed characteristics
known defects
artifact digests
superseding record where applicable

9. Add a command conceptually equivalent to:

kirby2 audit-lab \
    --budget 10000 \
    --seed 771 \
    --save-failures

10. Produce a machine-readable audit packet and concise human report.

Example:

10,000 generated runs
9,996 passed
4 violations
2 unique minimized defects
0 replay mismatches
1 unstable parameter family
1 observability leak

Acceptance:

Run a substantial audit budget, show actual failures found or honestly report none, minimize every discovered failure and preserve the evidence in the immutable ledger.

Do not weaken invariants merely to make the audit green.

Stop after completion.
```

After Work Order 30, Kirby2 should be capable of replaying constrained historical sessions, producing calibrated synthetic markets, training manual execution under latency and uncertainty, benchmarking execution algorithms, and explaining alternative decisions through deterministic branches.

The next batch would move into synthetic full-day generation, adaptive curriculum, script mutation and discovery, instructor tooling, market-data visualization, distributed simulation, and packaged releases. That is expansion territory. This batch is the steel frame.
