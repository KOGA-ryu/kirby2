# Generic advanced exchange and session mechanics

`MarketMechanicsEngine` is a deterministic policy and session layer around the
original FIFO `OrderBook`. Existing simple `OrderBook.process()` behavior is
unchanged. This module is generic simulator infrastructure; its rule names and
defaults do not claim to reproduce any particular exchange, broker, country, or
jurisdiction.

Prices remain integer ticks. `tick_size` is decimal metadata for translating a
tick to currency units and never introduces floating-point exchange prices.
Instrument rules validate lot size, minimum and maximum quantity, absolute price
bands, supported instructions, the session schedule, and optional protection and
self-trade settings.

## Exact continuous-order semantics

- `LIMIT` may execute against compatible prices, then rests its remainder with
  normal FIFO priority.
- `MARKET` consumes immediately available compatible depth and expires any
  unfilled remainder.
- `MARKETABLE_LIMIT` must be marketable when received. It rejects if no quantity
  is immediately executable; otherwise it behaves as a priced limit.
- `IOC` executes immediately and expires any remainder without leaving it live.
- `FOK` preflights all compatible FIFO depth and either fills the complete order
  immediately or rejects it without any fill.
- `POST_ONLY` is a limit-order modifier. It rejects if the order would cross and
  rests normally otherwise. It cannot be combined with `MARKETABLE_LIMIT`, `IOC`,
  or `FOK`.
- `DAY` remains eligible through continuous trading and halts, then expires on the
  postclose/day boundary.
- `SESSION` expires when its current continuous session ends, including a halt.
- `GOOD_UNTIL_TIME` expires at its configured simulation microsecond. Wall time is
  never consulted.

A same-price quantity reduction preserves FIFO position only when
`preserve_priority_on_quantity_reduction=true`. Price changes, increases, and
reductions on venues that disable preservation are explicit cancel-plus-new
operations and lose priority. After a partial fill, replacement quantity is treated
as the new total order quantity; the new order receives only that total minus the
already-filled quantity. A requested total at or below cumulative fills rejects and
leaves the working order unchanged. Duplicate replacement order IDs also reject
before the cancel leg.

## Self-trade prevention and protections

Self-trade prevention is configured per account ID and therefore applies equally
to player accounts and synthetic agents. `NONE`, `CANCEL_AGGRESSOR`,
`CANCEL_RESTING`, and `CANCEL_BOTH` are supported. Continuous matching evaluates
reachable FIFO makers before submitting the aggressor. In an auction there is no
natural aggressor, so the later arrival is deterministically treated as the
aggressor.

Maximum size, absolute price-band, price-collar, fat-finger, and volatility
interruption triggers are separately labeled. A volatility interruption rejects
the triggering order and moves the session to `HALTED`. These are configurable
generic protections, not calibrated live-venue rules.

## Sessions and auctions

The explicit state graph contains `CLOSED`, `PREOPEN`, `OPENING_AUCTION`,
`CONTINUOUS`, `HALTED`, `REOPENING_AUCTION`, `CLOSING_AUCTION`, and `POSTCLOSE`.
Transitions can be commanded or driven by a simulation-time `SessionSchedule`.

Auction-only orders stay outside the continuous FIFO book. The indicative price
selection tie break is maximum matched quantity, minimum absolute imbalance,
closest price to the reference, then the lower tick. Allocation is market orders
first, better prices next, and FIFO at an equal price. Unmatched auction-only
quantity expires after uncrossing. Auction indications, self-trade prevention,
fills, expirations, uncrossing, halts, and resumes all use the monotonic mechanics
event sequence.

## Replay and acceptance

The portable recording includes the complete rule set, ordered commands, complete
event stream, completion time, and expected event/state digests. Replay compares
events structurally and verifies the full state digest, including the wrapped FIFO
journal.

```bash
python3 -m kirby2 mechanics-demo --scenario all
python3 -m kirby2 audit-market-mechanics
```
