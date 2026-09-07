# KIRBY2-MARKET-003 — direct implementation order

User authorized maintenance followed by Chapter 3 on 2026-09-07. Preserve Chapter
2, stop before Chapter 4, no push or visible Qt. Backend starting HEAD is
114af9c (Chapter 2 plus user-committed existing material); UI starting HEAD remains
ca07faf with the completed Chapter 2 working changes. Preserve those changes.

Active order: repair legacy New Practice ownership and three stale smoke tests;
then implement revision-3 Chapter 3, C3-01 through C3-07. Source contracts outrank
agent maps. Backend owns models, observation arithmetic, configuration identity,
Replay, persistence and report truth. UI owns controls, lifecycle routing and
protected presentation. No existing accepted preset may change meaning.

Chapter 3 starts at the governed materializer and its rejected queue/intraday
components. Prove implemented public capability and exact restore before exposing
profile controls. Reuse ordinary order-flow/matching. No forced price path, fill,
unbounded parameter composition, future leakage, live market feed or performance
claim. Six bounded behavior families and a frozen seed population precede result
inspection. Actual feature arithmetic separates executions, cancels, replacement,
price response, activity, and unavailable baselines.

This file records work authority and decisions, not acceptance. Implementation,
frozen diagnostic envelopes and evidence will be appended as the seams resolve.

User override, 2026-09-07: do not capture, generate, render, or inspect any images.
Visual proof is excluded from this order because it can terminate the session.
Use source, numerical, contract and non-image protected application checks;
report visual acceptance as NOT RUN under this explicit constraint.

## Frozen experiment definition (before first new market run)

V1 has ten finite recipes: six families (quiet, warming, replenishment, thin,
 cooling, reactivation) and four warming variants (fast heating, longer
persistence, slower cooling, accepted Hawkes clustering). Regime policy and
native order-size/depth distributions stay unchanged. The replenishment pair
changes only the limit-arrival response peak (1.4 to 3.0); market-arrival inputs
are shared, but realized flow and prices can diverge after that intervention.

All recipes run for 30 synthetic seconds, coordinate SYNTHETIC_ELAPSED_UTC from
00:00:00; this is not a real exchange session or timezone model. Warming phase
lengths are [3,3,8,3,3,10] seconds and event-rate multipliers are
[0.3,0.8,2.0,1.0,0.4,0.2]. Faster heating changes the first two to [1,1]; longer
persistence changes 8 to 12; slower cooling changes [3,3] to [5,5]. The final
quiet segment absorbs the duration difference. These are step schedules, not
continuous thermal equations. Queue response observes one second of depletion
(including cancellation) and modifies only limit arrivals, bounded [1,3], with
a 100-events/second per-channel policy-baseline ceiling (the accepted Hawkes
model separately retains its own total-intensity cap). Thin changes NORMAL to THIN liquidity.
Quiet uses BALANCED at constant 0.4; all other families use BUY_PRESSURE.
Cooling and reactivation change only their declared six-step activity schedule.

Evaluation seeds: 11,23,37,53,71,89, all reported. Independent synthetic prior:
quiet recipe, seeds 101,103, five-second windows at matching session coordinates.
This is a measured synthetic prior, never an empirical baseline. Representative
trace: first declared seed 11, selected independently of outcome. No automatic
search, reseeding or tuning after inspection. Numerical diagnostics per run:
1..12,000 flow events, at least one executed share, spread <=500 ticks, absolute
midpoint movement <=1,000 ticks, invariant checks and verified Replay required.
Envelope failures are diagnostic failures and remain visible; there is no
requirement that every seed advances or replenishment always suppresses price.

First observation set: executed buy/sell shares and price-tick notional, actual
cancelled shares, newly resting shares, same-side/same-price replenishment after
prior executed depletion within the window, signed midpoint response, spread,
depth and measured relative activity. Five-second trailing windows exclude their
left endpoint. Insufficient coverage, sequence gaps, stale delivery, missing and
zero priors are explicit. No hidden family label enters blind observations.

## Implementation and review disposition

Backend production seams are confined to optional `ui/market_profiles.py`,
`ui/market_workbench.py`, `features/market_observations.py`, and the existing
SessionFlowConfiguration / governed catalog, Start and Replay materializers.
The base profile catalog and all existing public golden records remain unchanged.
No top-level CLI command, market feed, strategy evaluator or broker was added.

The optional UI Markets page owns selection, numerical report presentation and
background job routing. It consumes public backend capability/configuration,
report and verified Replay calls. The existing application/controller owns every
practice transition. Report jobs never export mutable session handles. Their
application fence holds Start, Reset, Close/New Practice and Exit until settlement;
backend import locking is released before numerical work begins. Reopening a
report recomputes its observations from verified source. Report claims, population
identity, diagnostics and measurement summaries are checked before publication.

Backend storage is append-only: report directories contain immutable Replay
bytes and report JSON, resolved practice configurations are immutable records,
and preview exposure is recorded separately from the report. Exposure survives
report removal. User notes and the Chapter 2 library remain separate. These
features do not extend process-local partial Replay for free practice into a
claim of durable session saving or mutable restore.

Late review repairs preserve unavailable price measurements and seed positions
in distribution summaries, reject rehashed unsupported report claims, and
check resting shares as initial/additional supply minus executions and cancels.
No recipe, seed population, or diagnostic envelope was retuned after outputs.
The first matrix remains historical; the `final/` matrix supersedes it for current
report measurement schema verification.

C3-01: optional governed capability, invalid selection refusal, all ten recipe
restorations, reset freshness, archived backend fallback, unchanged base goldens.
C3-02: ordinary matching and original model policies; quantity conservation and
book invariants for every population cell; no assigned price or forced fill.
C3-03: all nine warming comparisons use the six predeclared seeds; all cells,
distributions and diagnostic failures are retained; first-seed trace policy.
C3-04: independently calculated execution/cancel/add fixture, matched price/side
replenishment, identical terminal depth with different observable history.
C3-05: separately prepared quiet synthetic priors, zero/missing/mismatched prior
status and explicitly insufficient five-second windows.
C3-06: blind projections ignore hidden labels and future suffixes; explicit gaps,
delivery age and stale status; durable exposure survives report removal.
C3-07: model state invariance under different observation cadence with identical
simulation-time player action, meaningful units and unsupported-combination
refusal. Source and protected UI behavior checks passed. Visual acceptance is
NOT RUN under the user's no-image instruction; no visual or human acceptance is
claimed.

Final evidence directory:
`/Users/kogaryu/.codex/visualizations/2026/09/05/01a06f5a-8e17-7093-9896-cde5a6038268/kirby2-chapter3-build/final/`

Stop before Chapter 4. Leave the implementation uncommitted for review, preserve
all pre-existing Chapter 2 UI work, and do not push or run release qualification.
