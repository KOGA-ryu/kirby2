# Kirby2 Product Specification

**Status:** Phase 0 product definition  
**Product:** Deterministic market-execution training sandbox  
**First playable release:** Kirby2 v0.1, Phases 1–5

## Purpose

Kirby2 trains a person to read market microstructure, follow an explicit trading plan, and execute orders under changing volume and liquidity. It is not a brokerage simulator, a signal-selling system, or a claim that synthetic Level 2 represents historical fact.

The player works through three distinct training layers:

1. **Read** — What is the market doing?
2. **Script** — Does the player's rule set say `GO`, `WAIT`, or `NO TRADE`?
3. **Execute** — Can the player enter, manage, and exit efficiently?

Kirby2 succeeds when the market ladder feels alive enough to support deliberate execution practice and when a replay can explain what the market did, what the player's rules allowed, and what the player actually did.

## Core experience

The player sees a Level 2 ladder, time-and-sales tape, position and P&L, traffic-light script state, and hotkey feedback. A synthetic order-flow model drives a price-time-priority exchange. Named regimes alter order-flow probabilities and liquidity conditions; they do not command prices directly. The player must infer conditions from observable market state and never receives the simulator's hidden regime label.

Every session is reproducible from its complete configuration and integer seed. Given the same version, seed, scenario, and inputs, Kirby2 must produce the same ordered event stream and fills.

## Simulation contract

- Prices use integer ticks internally; floating-point prices are forbidden in exchange state.
- The order book uses price-time priority and supports limit orders, market orders, cancellations, replacements, partial fills, and player queue position.
- Market movement results from order submission, cancellation, replenishment, and queue depletion—not direct scripted price changes.
- Volume and liquidity are independent controls. Event frequency, size distributions, replenishment, cancellations, impact, and spread stability respond coherently to them.
- Runtime invariants continuously protect book integrity, nonnegative quantities, fill bounds, and position accounting.
- Synthetic, replayed, and reconstructed data must always be labeled distinctly. Reconstruction is never presented as historical Level 2 fact.

## Kirby2 v0.1 scope

v0.1 contains only the capabilities needed to prove that the synthetic market is deterministic, legible, and playable:

1. **Deterministic exchange core:** a small bid/ask book, orders, matching, fills, cancellation and replacement, player orders, invariant checks, and a reproducible event log.
2. **Synthetic order flow:** stochastic limit, market, and cancel events with explicit distributions for arrival, size, depth, aggression, and spread behavior.
3. **Observable regimes:** at minimum balanced flow, directional pressure and momentum, bid/ask absorption, thin liquidity, liquidity vacuum, mean reversion, high cancellation, and panic.
4. **Volume and liquidity scaling:** controlled combinations ranging from quiet/deep to high-volume/thin conditions; neither control is treated as a proxy for the other.
5. **Minimal playable UI:** Level 2 ladder, tape, traffic-light status placeholder, position, P&L, and keyboard-first order entry. No development tools are required during a five-minute practice session.

The v0.1 gate is experiential as well as technical: two scenarios run from comparable starting conditions must be distinguishable from their observable behavior without exposing their labels. If the ladder is not compelling, work returns to the simulator rather than expanding the product.

## Explicitly deferred

Hotkey experiments and input replay, user-authored traffic-light rules, scored training sessions, curriculum, historical replay or reconstruction, Hawkes and queue-reactive models, empirical calibration, advanced strategy logic, charts, news, watchlists, portfolio management, broker connectivity, paper trading, and live execution are outside v0.1.

No chess code, chess schemas, ParlAWL tests, legacy evidence machinery, or stock-research data model is imported into Kirby2. Future historical work may consume market data, but it must remain behind the same exchange-driver boundary as synthetic flow and must preserve provenance.

## Phase gates

Each phase ends in an inspectable or playable artifact:

- Phase 1: `kirby2 run --seed 482913` emits a deterministic, believable event log.
- Phase 2: several simulated minutes exhibit plausible Level 2 without scripted price paths.
- Phase 3: absorption and momentum runs are behaviorally distinguishable under blind review.
- Phase 4: every supported regime runs across multiple independent volume/liquidity combinations.
- Phase 5: a player can execute against Kirby2 for five minutes using only the application and keyboard.

## Product decisions left open

The implementation language, UI toolkit, persistence format, simulation clock resolution, default tick size, default book depth, and quantitative plausibility thresholds will be chosen in later phase specifications. Those choices must preserve determinism, integer price representation, model interchangeability, observable-versus-hidden state separation, and replayable audit events.
