# KIRBY2-EXECUTION-004 — direct implementation authority

The user authorized Chapter 4 directly on 2026-09-07. Backend baseline is
7165a77d4e66237e053501a98fc52ae033b86fca. UI baseline is ca07faf plus the
existing uncommitted Chapter 2/3 implementation; preserve it. No commit, push,
Chapter 5, release qualification, worker contact, or visible Qt is authorized.
The user's absolute no-images instruction overrides all visual proof requests:
no image capture, rendering, generation, inspection, or image-producing tests.

Canonical order: revision-3 chapters/04-execution-under-uncertainty.md and
C4-01 through C4-08 in that design's ACCEPTANCE.md. This document records
authority and design decisions, not passing evidence.

## A — integration gate, before desktop wiring

Use a separately advertised execution practice capability backed by
FullDayRuntime's SINGLE_VENUE_AGENT_FLOW_DELIVERY_V1 composition. The existing
LiveMarketSession and standalone AsynchronousExecutionSession are unchanged.
One full-day runtime owns clock, scheduler and matching engine; its passive
DeliveryOwnerV1 owns only latency draws and delivery queues. No synchronized
second book, private clock mutation, forced fill or imposed price move.

Recipes use an explicit short synthetic calendar, real FIFO submissions and
seeded background flow. Versioned curriculum counterparties submit ordinary
orders. Their future schedule is not included in live client projections.
Start prepares a nonzero cut; hold does not advance any component. Stepping
uses simulation microseconds, not measured human or desktop reaction time.

Live account knowledge is derived only from delivered economic receipts, never
from the venue order snapshots bundled with them. Those snapshots can include
other consequences of the same matching transaction. Deduplicate by economic
event identity, reserve before routing, and release only explicit terminal
quantity. Keep an unresolved interval [q-S, q+B], including fills not yet
reported when a cancel acknowledgement arrives first.

A restore proof must include generated flow, actual partial fills, in-flight
cancel, an intervening fill, pending reports, exact nonzero checkpoint restore
and identical continuation. Freeze/hold, equal-time order and truthful resource
release are part of this gate. If this selected seam requires a broad runtime
rewrite, stop with the precise blocker and keep the executable evidence.

## B–D — contingent on A passing

Backend owns bounded targets, reservations, explicit disarm/expiry, refusal
categories, immutable recording and verified review. UI owns presentation and
extends the existing application lifecycle authority: one mutable practice at
a time, stale callbacks refused, recovery available, Review fenced while live.
Unknown execution state must never be displayed as flat or cancelled. Existing
V1 records retain their meaning. New durable pending-state evidence needs an
explicit schema and deep reconstruction across a fresh process.

Stop before Chapter 5. Report non-image behavioral acceptance separately from
visual and human acceptance (both NOT RUN).

## Selected V1 implementation and frozen policy

The integration gate passed before desktop wiring. The new optional public
surface is `kirby2.ui.execution_practice`; it does not add to or reinterpret the
existing `kirby2.ui` export inventory or simulation profiles. FullDayRuntime,
matching, delivery, checkpoint and restoration production sources are unchanged.

Two recipe IDs are versioned independently of the old catalog:

- `execution.cancel-race.v1`: bid 9998 × 500, ask 10000 × 300; a scheduled
  counterparty sells 200 shares. Passive entry at 9998 queues behind 500 shares,
  so a trade at that price need not fill the learner. A limit at 10000 can take
  displayed liquidity and then rest. Both use the same ordinary FIFO engine.
- `execution.queue-pressure.v1`: the same initial book; counterparties sell
  1300 and then 900 shares. Whether and where orders fill is determined by the
  actual queue. Later executions through lower bids can follow a passive fill.

Background buy/sell limit additions each arrive at 5 events/second, with seeded
1–5-share quantities and placement depths 20–40 ticks. Other background
channels are zero in these focused drills. This is a small curated execution
curriculum, not a realistic population of traders or a calibration claim.
Changing any of these recipes requires a new version; do not retune V1 outcomes.

Preparation ends at 1,000,000 simulated microseconds. Practice stops at
1,020,000 microseconds, before calendar-close mechanics. The built-in NORMAL
latency profile supplies the routed delays; default seed is 11. Future recipe
orders never enter live knowledge. The desktop offers explicit microsecond
steps, not a 1× human-reaction exercise or a real-time network emulation.

All late economic reports are retained and deduplicated by economic identity;
fills use trade ID, independent of transport sequence. Market publication uses
the existing monotone source-cut policy: a later-arriving older market snapshot
does not replace newer known market state. Market age above 500,000 µs, missing
market data, or unknown exposure refuses new orders. Cancels and cleanup remain
available. Reported account quantities use availability time, never venue order
snapshots. Review records both client-known and venue positions at each decision,
with source-event IDs and a digest of the complete decision knowledge.

Training-account interval is [q-S,q+B], bounded to 0–1000 shares. Every submitted
limit order and one-shot target goes through the same reservation function.
Opposite sides cannot net unresolved commitments. Controlled reductions reserve
sell quantity against confirmed, unreserved exposure. Supported price bounds
are 9900–10100 ticks; share/price constraints are not a monetary-loss guarantee.
Arming does not submit. Fire consumes the arm once; expiry/disarm invalidates it.
No automatic target replenishment, replace flow, corrections, shorts, or scripts
are advertised. Unsupported corrections explicitly lock new exposure.

Freeze PARTIAL captures a marker-complete cut and stops model advancement without
draining or pretending to cancel pending work. COMPLETE additionally requires all
learner commitments and cancellation requests to be accounted for. A confirmed
nonzero position may remain; ACCOUNTED is not FLAT. Simulator release returns
execution_settlement=NOT_CLAIMED. Guided hold advances no component.

Durable evidence lives alongside the Chapter 2 library under
`evidence/execution-practice-v1`, with explicit KIRBY2_EXECUTION_BUNDLE_V1 records.
It includes unique attempt identity, recipe/seed, each instruction and refusal,
decision knowledge, pending runtime checkpoint and revealed review. Opening
verifies exact checkpoint restore and independently reconstructs the instruction
prefix. A read-only audit continuation compares restored and reconstructed
suffixes; it never resumes an armed learner intent. Exact repetitions remain
separate attempts even if their deterministic states match. Existing C2 bundle
formats and Repeat semantics are unchanged. An unexpected backend-integrity
failure may prevent reconstruction and saving; retain cleanup authority and
report the failure rather than certify unsupported evidence.

The optional Execution page has its own strict controller because V1 simulation
frames cannot represent this runtime. It shares the existing application's
exclusive practice gate: controller authority is retained before projection,
the application fences Start/Library/Markets/Review/old timer and hotkey paths,
and source-generation checks reject abandoned button callbacks. Refresh can
recover presentation for cancellation/cleanup without re-enabling risk after
an integrity lock. Close and global New Practice/Exit preserve Cancel/Discard;
unconfirmed exit requires the explicit existing-style escape decision. Resource
release never claims cancellation. No second window or image proof is involved.

Non-image verification commands:

```
.venv/bin/python -m unittest kirby2.audit.execution_practice -v
```

In the UI checkout, use only the existing protected launcher:

```
./scripts/test_replay_ui.sh test_execution_no_images.py
./scripts/test_market_no_images.sh
```
