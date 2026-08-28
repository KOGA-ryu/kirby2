# Kirby2 Work Orders 31-40 Goal Execution Contract

**Status:** canonical expansion sequence
**Scope:** Work Orders 31-40 only
**Starting closeout HEAD:** `4a962c58feab88f25e5dccfcd85c66dcf8723313`
**Audited implementation commit:** `e84047e42f4079c83f9542b2caa66058e7051381`
**Inherited audit packet:** `audit-bffd05b9d74bb12b0840bcf0`
**Inherited packet manifest SHA-256:** `7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578`
**Inherited acceptance record:** `acceptance-8a34abc8a267b064eaeb`
**Inherited automated status:** `PASS_WITH_WARNINGS`
**Inherited manual status:** `PENDING_HUMAN_REVIEW`
**Expansion boundary:** stop after the WO40 release-candidate closeout

## 1. Goal objective

Complete every implementation card in this document in order. Work on exactly one
card at a time. A card is complete only after its runtime evidence passes, its
limitations and unexercised capabilities are reported truthfully, its exact diff is
reviewed, it is committed with the specified subject, and the worktree is clean.
After verifying that clean commit, retrieve and begin the next card automatically.

This document replaces the source batch instruction to "complete only the active
work order, then stop." Under this long-running goal, the stop boundary is a card's
commit gate: no two cards share an implementation commit, but a successful card
automatically hands off to the next one.

The sequence implements the user-supplied Work Orders 31-40. It does not authorize
live brokerage, live order submission, cloud accounts, telemetry, subscriptions,
social feeds, public leaderboards, arbitrary code in scenario or pack content,
empirical claims unsupported by evidence, or a second test framework.

One explicit dependency-order amendment applies: WO39-A through WO39-C execute before
WO38-A through WO38-E even though their source work-order number is larger. Those
three cards establish the one hostile-input-checked, content-addressed pack/staging
protocol that WO38-C/D must reuse for worker transfer. This prevents the distributed
layer from inventing a second bundle protocol; WO39-D1 through WO39-E remain after
WO38 because they consume orchestration only for qualification, not substrate.

## 2. Sealed baseline capsule

The baseline has several identities and statuses. Never collapse them into one
ambiguous `REPAIRED_BASELINE_COMMIT` or describe the inherited packet as an
unqualified green acceptance.

| Field | Required value at goal start |
|---|---|
| `SEALED_CLOSEOUT_HEAD` | `4a962c58feab88f25e5dccfcd85c66dcf8723313` |
| `AUDITED_IMPLEMENTATION_COMMIT` | `e84047e42f4079c83f9542b2caa66058e7051381` |
| `BASELINE_AUDIT_PACKET_ID` | `audit-bffd05b9d74bb12b0840bcf0` |
| `BASELINE_PACKET_MANIFEST_SHA256` | `7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578` |
| `BASELINE_ACCEPTANCE_RECORD_ID` | `acceptance-8a34abc8a267b064eaeb` |
| `AUTOMATED_STATUS` | `PASS_WITH_WARNINGS` |
| `MANUAL_ACCEPTANCE_STATUS` | `PENDING_HUMAN_REVIEW` |

The inherited packet is regression evidence for the repaired implementation. It
does not certify any capability introduced by this sequence. Verify the existing
packet and run non-persisting smoke checks; do not create a new persistent baseline
packet merely because the batch began.

## 3. Non-negotiable design laws

Every card preserves:

- integer-tick prices and integer quantities;
- explicitly owned seeded RNG state and deterministic substream derivation;
- simulation time independent from wall-clock time;
- FIFO matching and the repaired exchange ownership boundary;
- immutable, append-only run and audit evidence;
- exact event sequencing and replay provenance;
- observable, reveal-only, and ground-truth separation;
- no-lookahead decisions at their recorded information cutoff;
- fail-closed runtime invariants and minimized failure reproduction;
- immutable source evidence with versioned derived sidecars;
- ordinary order, cancellation, auction, venue, latency, and agent interfaces as
  the only mechanisms that may move a synthetic market;
- no target-price writes, post-hoc path edits, or forced favorable outcomes.

All identity-bearing wire values are exact: ticks, shares, event sequences, and
microseconds are integers; ratios/probabilities are reduced integer numerator and
denominator pairs or schema-named fixed-point integers; booleans are canonical JSON
booleans; maps are key-sorted; and no NaN, infinity, negative zero, locale-dependent
number, or binary float enters a semantic digest. Seed substreams derive from a
versioned SHA-256 construction over the root seed and stable semantic labels, never
container position, process ID, wall time, or Python's randomized `hash()`.

Prefer an existing interface over a parallel abstraction. A new abstraction is
allowed only when the current card names it or a concrete blocker is recorded under
the amendment protocol.

Use TOML for new human-authored static configuration. Use canonical JSON only for
machine event streams, manifests already governed by a JSON schema, or external
compatibility. Use Parquet for columnar evidence where the existing research store
does so.

## 4. Evidence and claim vocabulary

### 4.1 Automated and human status are independent

Automated gates may emit only:

```text
PASS
PASS_WITH_WARNINGS
FAIL
NOT_EXERCISED
```

Human review sidecars may emit only:

```text
PENDING
ACCEPTED
REJECTED
NEEDS_EDIT
SUPERSEDED
```

Automation may produce `READY_FOR_HUMAN_REVIEW`; it may not create `ACCEPTED`.
Profile plausibility, lesson usefulness, learner-model validity, scientific
interpretation, and final release approval remain human decisions.

Legacy evidence keeps its original vocabulary: the sealed packet remains
`STATISTICAL_STATUS WARNING`, aggregate `PASS_WITH_WARNINGS`, and manual
`PENDING_HUMAN_REVIEW`. New human-review sidecars use `PENDING`. Statistical and
platform substatuses are reported independently as
`PASS | WARNING | FAIL | NOT_EXERCISED` and
`PASS | FAIL | NOT_RUN | UNSUPPORTED` respectively.
Specific conditions such as `RESTORE_NOT_IMPLEMENTED` are typed `reason_code` values,
not new gate statuses.

### 4.2 Historical capability record

All scenario compilation, lesson mining, replay views, packs, and release claims
preserve the existing declared source classes:

```text
BARS_ONLY
TRADES
TRADES_AND_QUOTES
LEVEL2_SNAPSHOTS
LEVEL2_DELTAS
MARKET_BY_ORDER
```

Those names are not compared as an ordinal hierarchy. Every source also resolves to
an explicit capability record such as: bars, trades, quotes, aggressor side, depth
snapshots, depth deltas, order identity, event sequence completeness, session events,
timestamp precision, and source provenance. Validators and detectors test their exact
required flags and quality conditions. They never infer that one label automatically
contains every fact from another label.

All historical values also carry one evidence class:

```text
OBSERVED
DERIVED_FROM_SOURCE
SYNTHETIC_RECONSTRUCTION
SCENARIO_ASSUMPTION
COUNTERFACTUAL
UNAVAILABLE
REJECTED
```

Only a clean, ordered, complete market-by-order source may support exact historical
order-book replay. A synthetic fixture proves an implementation path, not real-market
provenance. Missing evidence is never encoded as zero, false, an empty queue, or an
inferred fact.

### 4.3 Identity families must not be conflated

- Raw source bytes and import bytes produce a source-provenance digest.
- Canonical fully resolved behavior produces a semantic-plan digest.
- Exact serialized artifacts produce artifact digests.
- Logical pack identity is separate from archive transport-byte digest.
- A logical distributed work identity excludes leases and attempt numbers.
- Operational attempts have separate append-only attempt identities.
- Human review status is excluded from the immutable identity of the reviewed
  object and stored in a sidecar.

### 4.4 Preregistration rule

Behavioral envelopes, thresholds, fixed seed sets, sampling rules, detector
versions, partitions, search budgets, objectives, stopping rules, and performance
limits must be digest-bound before observing the corresponding acceptance result.
Changing one after inspection creates a new version and invalidates the former
decision; it never edits history.

### 4.5 Engineering correctness is not scientific validation

Report engineering correctness separately from:

- resemblance to a real market;
- causal inference outside the declared Kirby2 intervention model;
- pedagogical usefulness;
- learner mastery or educational effectiveness;
- strategy profitability or real-world robustness;
- human release acceptance.

## 5. Per-card execution protocol

### 5.1 Preflight

1. Run `git status --short --branch` and `git rev-parse HEAD`.
2. Require a clean worktree. Classify any unexpected path before editing; unrelated
   user changes are a stop condition and are never reset, discarded, or absorbed.
3. Read only the active card, its completed dependencies, and the owned source
   files. Do not implement a later card.
4. Record the starting commit in the progress update.

### 5.2 Implementation

1. Modify only the owned paths and minimum call sites named by the card.
2. Add executable evidence to the existing runtime audit/invariant system. Do not
   add `pytest`, mocks, coverage infrastructure, or a parallel test tree.
3. Treat unsupported capabilities as `NOT_EXERCISED`, never `PASS`.
4. Treat an expected hostile rejection as evidence, not a product failure.
5. Preserve source artifacts; repairs create superseding records.

### 5.3 Verification

Every implementation card after the registration seam runs:

```text
git diff --check
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate CARD_ID
```

K2X-00 and K2X-01 necessarily precede that command and run only their literal
structural/baseline evidence. K2X-02 implements the seam and runs its gate after
registration. No later card may use this exception. WO31-I1, WO35-F1, WO40-D1, and
WO40-F through WO40-J are frozen evidence-only cards: they invoke generic commands
and gates implemented/preregistered by their preceding implementation card and do not
use the standing source-registration exception.

It also runs the card's named regression commands and deterministic demonstration.
Capture actual exit status, counts, digests, statuses, warnings, `NOT_EXERCISED`
capabilities, and performance environment where applicable.

### 5.4 Commit gate

1. Inspect `git diff --stat` and `git diff --name-only`.
2. Confirm one card and no generated `.kirby2` evidence is staged.
3. Stage only the card's files.
4. Run `git diff --cached --check`; this is mandatory because ordinary diff checks
   do not inspect a newly created untracked file before staging.
5. Commit with the exact subject in the slice index.
6. Run `git show --stat --oneline --summary HEAD`.
7. Run `git status --short`; it must print nothing.
8. Only then retrieve and start the next card.

### 5.5 Unforeseen-event amendment protocol

- If the surprise has the same root cause and fits the active ownership boundary,
  add its reproducer to the active audit and repair it in the same card.
- If it is a prerequisite defect in a sealed or differently owned subsystem, create
  `KIRBY2_WORK_ORDERS_31_40_DEVIATIONS.md` if it does not exist and append exactly one
  monotonically allocated card ID `DEV-0001`, `DEV-0002`, and so on. Record its
  interrupted canonical card, exact first-parent predecessor, reproducer, root cause,
  owned paths, evidence commands, inherited gates, and exact unique commit subject.
  Register its `DEV-[0-9]{4}` gate through K2X-02, commit the amendment repair
  immediately before resuming the interrupted card, and never renumber or edit an
  earlier deviation record.
- If the problem is environmental, preserve the command and exact error. Retry only
  after a concrete correction; do not replace the gate with an easier one.
- If completion needs new user authority, external credentials, a human judgment,
  or an unavailable platform, preserve completed evidence and request that input.
- Never weaken an invariant, detector, threshold, replay comparison, security rule,
  or capability label to advance the sequence.

### 5.6 Deterministic card retrieval and resumption

1. Locate the unique first-parent commit whose subject is
   `Document work orders 31-40 execution sequence` and whose tree contains this file;
   that commit is the immutable sequence anchor. Read the canonical card order and
   exact subjects from the anchored file, not from memory or a later edited copy.
2. Starting immediately after the anchor, scan first-parent commits in chronological
   order. A canonical subject may occur at most once and only when every earlier card
   is present. Deviation subjects are valid only when their records all name the
   interrupted card and the entire contiguous deviation chain appears immediately
   before that card's eventual commit in exact recorded order.
3. Refuse duplicate, skipped, reordered, ambiguous, or unclassified goal commits.
   Never infer completion from files, a branch name, an audit result, or an in-progress
   worktree without the prescribed clean commit.
4. The active card is the first canonical card whose exact subject is absent. Before
   editing, require a clean worktree and every predecessor gate recorded. `HEAD` must
   be either the canonical predecessor or the tip of one contiguous validated
   `DEV-[0-9]{4}` chain targeting this active card whose base is that predecessor; all
   deviation IDs, subjects, order, owned paths, and passing gates must exactly match
   the append-only deviation records. If no card is absent, verify WO40-J and mark the
   goal complete.
5. Record anchor, predecessor, selected card, and starting HEAD in each progress
   update. This algorithm is also the meaning of "retrieve the next slice" in the goal
   objective.

### 5.7 Canonical V1 policy constants

This section is normative for producer cards WO31-H, WO33-A/A1, WO34-A/B/C,
WO35-D/E, and WO40-D. Their committed schemas and TOML manifests transcribe these
values exactly. Consumer
and evidence cards WO31-I/I1, WO33-B1-E, WO34-D, WO35-F/F1, and WO40-D1/F-J verify and
use those exact manifest/policy digests and may not reinterpret or replace a constant.
No implementer may choose alternate formulas, seeds, rounding, weights, budgets,
perturbations, samples, or thresholds without a section-5.5 deviation.

#### 5.7.1 Exact arithmetic and ordering

`POLICY_CONSTANTS_V1` uses:

- `S = 1_000_000` for fixed-point ratios, shares, probabilities, normalized
  components, multipliers, and weights;
- integer ticks, shares, microseconds, milliticks, bytes, event counts, attempt
  ordinals, and basis values;
- `round_div_even(n,d)`, `d > 0`: let `q,r = divmod(abs(n),d)`; increment `q` when
  `2*r > d` or when `2*r == d and q is odd`; restore the sign of `n`;
- `mul_ppm(a,b) = round_div_even(a*b,S)`;
- unbounded `ratio_ppm(n,d) = round_div_even(n*S,d)` for ratios that may exceed one,
  such as shock/pre-event, cancel/add, and sell/buy ratios;
- signed `share_ppm(n,d) = clamp(ratio_ppm(n,d),-S,S)` and unsigned
  `unsigned_share_ppm(n,d) = clamp(ratio_ppm(n,d),0,S)`;
- a zero denominator produces the declared missing result, never zero;
- `clamp(x,lo,hi) = min(hi,max(lo,x))`;
- nearest-rank percentile: for nonempty ascending values use one-based rank
  `max(1,round_div_ceiling(p_ppm*n,S))`, where
  `round_div_ceiling(n,d) = (n+d-1)//d`; `p=0` returns the minimum;
- time-weighted nearest-rank percentile: require nonempty `(value,duration_us,
  canonical_segment_order)` rows with every duration strictly positive; sort by
  value then canonical segment order, let `D=sum(duration_us)`, and for `p_ppm>0`
  use target `max(1,round_div_ceiling(p_ppm*D,S))`, returning the first value whose
  cumulative duration reaches the target; `p=0` returns the minimum value. Empty,
  nonpositive-duration, or zero-total input is `INSUFFICIENT_EVIDENCE`;
- median `P50`; dispersion `MAD = P50(abs(x-P50(x)))`;
- percentile arguments outside `0..S` fail;
- every reduction in ascending semantic ID, then root seed, then event/attempt
  ordinal order.

The common tie digest is the 32-byte SHA-256 of
`b"KIRBY2_POLICY_TIE_V1\0" || utf8_nfc(policy_version) || b"\0" ||
utf8_nfc(context_id) || b"\0" || u64be(selection_root_seed) || b"\0" ||
bytes.fromhex(semantic_digest)`. Policy/context IDs are nonempty NFC strings, semantic
digests are exactly 64 lowercase hexadecimal characters, and roots are nonnegative
eight-byte unsigned big-endian values. A ranking applies its section-specific
primary/secondary keys, then ascending unsigned tie digest, then the NFC UTF-8 stable
ID bytes. Fixed tie policy/root pairs are `FULL_DAY_PROFILE_V1/3199001`,
`LESSON_MINING_V1/3399001`, and `STRATEGY_DISCOVERY_V1/3599001`. WO34 requires the
nonnegative root in `CurriculumSelectionRequestV1`, has no default, and uses
`ADAPTIVE_SELECTION_V1`; its demonstration root is `42`.

`u32be(x)` is exactly one unsigned four-byte big-endian integer and accepts only
`0<=x<2**32`; `u64be(x)` is the analogous unsigned eight-byte encoding. In
evolutionary search, later generations are `g=1..7` and their six child slots are
`j=0..5`; generation zero has no tournament child indices.

A labeled simulation substream seed is
`int.from_bytes(SHA256(u64be(root_seed) || b"\0" || utf8_nfc(policy_version) ||
b"\0" || utf8_nfc(label))[0:8],"big") & ((1<<63)-1)`. Empty labels, negative roots,
roots above `2**63-1`, and non-NFC strings are refused. Rank seeds are never consumed
as simulator RNG streams; neither kind may use wall clock, process state, global RNG
state, or Python's randomized `hash()`.

A missing required component yields `INSUFFICIENT_EVIDENCE`. An explicitly
inapplicable component is `NOT_APPLICABLE` and is omitted from both weighted
numerator and weight denominator. Nothing is silently imputed.

#### 5.7.2 WO31-H full-day profiles and qualification

All four candidates use development roots `3101000..3101003`, qualification roots
`3102000..3102007`, and one-time holdout roots `3103000..3103003`. Ranges are
inclusive and disjoint. Each candidate derives component streams from candidate ID
and root, so sharing a numeric root across IDs does not share a substream. Review
selection uses root `3199001`, label `full_day/review`, never a generator stream.

For continuous-phase normalized time,
`u_ppm = floor((t-continuous_start_us)*S/continuous_duration_us)`. Values below are
multipliers against the committed base plan:

| Candidate/interval | Volume | Liquidity | Volatility | Aggressive flow | Cancel flow |
|---|---:|---:|---:|---:|---:|
| `QUIET_RANGE_PRESSURE`, all continuous | 700000 | 1300000 | 650000 | 700000 both sides | 800000 |
| `TREND_PRESSURE`, `u < 150000` | 950000 | 1000000 | 900000 | 1000000 both | 1000000 |
| `TREND_PRESSURE`, `150000 <= u < 850000` | 1250000 | 900000 | 1100000 | favored 1600000; other 700000 | 1100000 |
| `TREND_PRESSURE`, `u >= 850000` | 1000000 | 1000000 | 900000 | favored 1200000; other 850000 | 900000 |
| `EVENT_SHOCK_PRESSURE`, `u < 450000` | 1000000 | 1000000 | 1000000 | 1000000 | 1000000 |
| same, `450000 <= u < 550000` | 2200000 | 550000 | 2500000 | shock 1800000; other 800000 | 1800000 |
| same, `550000 <= u < 750000` | 1300000 | 850000 | 1400000 | shock 1200000; other 900000 | 1200000 |
| same, `u >= 750000` | 1000000 | 1000000 | 1000000 | 1000000 | 1000000 |
| `DISORDERLY_OPEN_STABILIZATION_PRESSURE`, `u < 80000` | 2200000 | 550000 | 2200000 | 1600000 both | 2000000 |
| same, `80000 <= u < 200000` | 1500000 | 800000 | 1500000 | 1250000 both | 1300000 |
| same, `u >= 200000` | 1000000 | 1000000 | 1000000 | 1000000 | 1000000 |

A normalized boundary `b` maps to exact simulation time
`continuous_start_us+floor((b*continuous_duration_us+S-1)/S)`; this ceiling rule is
used once per boundary before interval comparison. The five columns target only these
identity-bearing base-plan paths, composing left-to-right with `mul_ppm` and rounding
after every call:

- limit buy/sell rates: base `/base_flow/rates/limit_{buy,sell}` times Volume then
  Liquidity;
- market buy/sell rates: base `/base_flow/rates/market_{buy,sell}` times Volume,
  Volatility, then that side's Aggressive-flow factor;
- cancel bid/ask rates: base `/base_flow/rates/cancel_{bid,ask}` times Volume then
  Cancel-flow;
- every limit-size distribution value: base
  `/base_flow/order_sizes/limit_{buy,sell}/values/*` times Volume then Liquidity;
- every market-size distribution value: base
  `/base_flow/order_sizes/market_{buy,sell}/values/*` times Volume then Volatility;
- every initial-queue distribution value: base
  `/base_flow/initial_queue_sizes/values/*` times Liquidity;
- every scheduled participant parent quantity and scheduled shock order quantity:
  its base `quantity_shares` times Volume; shock order quantities then also multiply
  by Volatility.

No other field changes. Aggressive/Cancel columns modify rates only; volatility never
sets a price, spread, or target path. Integer quantities clamp to one only where their
own base field is positive and required; zero rates remain zero. Derived child
quantities partition an already-scaled parent and are not scaled again.

Trend favored side uses labeled substream
`full_day/TREND_PRESSURE/favored_side`; event shock side uses
`full_day/EVENT_SHOCK_PRESSURE/shock_side`. Each takes its labeled-seed low bit: zero
buy, one sell, using policy version `FULL_DAY_PROFILE_V1` and that run's root seed.
Trend metaorder is active on `[200000,800000)` of continuous time. Event
publication is at `u=450000`; distressed flow is active on `[450000,550000)`. These
controls affect normal rates/participants only and never set price or force a trade.

Universal per-run gates are: all runtime invariants pass; exact replay passes; no
safety abort; at least 100 trades; continuous two-sided quote occupancy at least
`950000`; no non-halt empty-side episode over `5_000_000 us`; maximum continuous
spread at most `20 ticks`; and zero target-price/forced-trade operations.

Behavioral gates use these exact derived values. Aggressive imbalance is
`abs(share_ppm(buy_shares-sell_shares,buy_shares+sell_shares))`; favored-side share
is `unsigned_share_ppm(favored_shares,total_aggressive_shares)`; displacement is
always trade-price displacement in integer ticks; and maximum displacement is
`max(abs(trade_price_ticks-first_continuous_trade_ticks))`. Shock/pre-event, first-
eight-percent/midday, sell/buy, and cancel/add ratios use unbounded `ratio_ppm`. A
missing first trade or zero required comparison denominator is
`INSUFFICIENT_EVIDENCE`, not zero.

Behavioral windows are exact half-open continuous-time intervals in `u_ppm`. Quiet
uses `[0,S)`. Trend first/last displacement uses the first and last trade in `[0,S)`.
Event pre is `[350000,450000)`, shock is `[450000,550000)`, and recovery is
`[550000,750000)`; pre and shock therefore have equal continuous duration before any
halt exclusion, and a halt makes the affected ratio `INSUFFICIENT_EVIDENCE` rather
than resizing either window. Disorderly-open first-eight-percent is `[0,80000)`,
midday is `[350000,650000)`, and final-eighty-percent is `[200000,S)`. Spread
distributions use two-sided quote-duration segments only; trade ranges use all trades
in the named window; cancel rates divide cancellation event count by eligible
non-halt duration in microseconds.

For every occupancy in this section, eligible duration is the named continuous-time
window intersected with continuous session state that is not halted. Occupied duration
is the sum of its positive-duration segments on which both best bid and best ask are
present. Occupancy is `unsigned_share_ppm(sum(occupied_us),sum(eligible_us))`; zero
eligible duration is `INSUFFICIENT_EVIDENCE`. Cancel-rate eligible duration uses this
same non-halted continuous-session intersection, without the two-sided-quote
requirement.

Behavioral gates use qualification aggregates and repeat unchanged on holdout. The
reduction is fixed before any outcome is read. Ratio/share gates pool their raw
integer numerators and denominators across roots and divide once; time-weighted
distribution gates pool every constant-value duration segment across roots in
ascending root/time order; occupancy pools occupied duration over eligible duration;
and a maximum is the maximum of the per-root maxima. A ratio whose numerator or
denominator is itself a nonadditive range is computed per root and reduced by P50.
Paired distribution comparisons use the two separately pooled distributions. The
only root-count and per-root-signed reduction is the explicitly stated trend
displacement rule. Thus there is no mean-of-ratios, worst-root, or other implicit
reduction.

- quiet pools spread durations, aggressive-share numerators/denominators, and uses
  the maximum displacement across all roots;
- trend pools favored/total aggressive shares, while first-to-last signed
  displacement is `last_trade_ticks-first_trade_ticks` for favored BUY and
  `first_trade_ticks-last_trade_ticks` for favored SELL, computed once per root,
  reduced by P50, and counted strictly positive root-by-root; fewer than two
  continuous-window trades is `INSUFFICIENT_EVIDENCE`;
- event pools shock/pre-event aggressive shares and durations, reduces each root's
  shock/pre spread-range-or-trade-range ratio by P50 (using spread range when both
  periods have quotes, otherwise trade range), pools recovery occupancy, and compares
  pooled recovery and shock spread medians;
- disorderly open pools the two spread distributions, pools cancellation counts and
  eligible durations before forming the exact rate ratio
  `ratio_ppm(open_cancel_count*mid_eligible_us,
  mid_cancel_count*open_eligible_us)`, and pools final-period occupancy and spread.
  Either eligible duration zero or the denominator cancel count zero is
  `INSUFFICIENT_EVIDENCE`; zero open cancellations with a valid denominator is a
  valid zero ratio.

The resulting exact gates are:

- quiet: median time-weighted spread `<=4 ticks`, P95 `<=8 ticks`, absolute
  aggressive-volume imbalance `<=250000`, and maximum absolute trade-tick
  displacement from the first continuous trade `<=80 ticks`;
- trend: favored aggressive-volume share `>=600000`, median favored-signed first-to-
  last displacement `>=2 ticks`, positive on six of eight qualification and three of
  four holdout roots;
- event: shock/pre-event equal-duration aggressive-volume ratio `>=1500000`; shock/
  pre-event spread-range or trade-range ratio `>=1200000`; recovery two-sided
  occupancy `>=900000`; recovery median spread no greater than shock median;
- disorderly open: first-eight-percent median spread no lower than midday; first-
  eight-percent/midday cancel-rate ratio `>=1500000`; final-eighty-percent two-sided
  occupancy `>=950000` and median spread `<=8 ticks`.

Universal miss is `FAIL`. Behavioral miss is `WARNING` plus
`AUTOMATED_READY=false`, never an engine defect or realism conclusion. A candidate
passes automated behavioral qualification only when every aggregate rule passes.

Review strata are opening, ordinary morning, midday, event/post-event, ordinary
afternoon, and close. Opening is opening auction plus `u<100000`; morning
`[100000,350000)`; midday `[350000,600000)`; afternoon `[600000,900000)`; close
`[900000,S)` plus closing auction. Event/post-event is
`[event_time-120_000_000,event_time+480_000_000)`; ordinary strata exclude it. Only
`EVENT_SHOCK_PRESSURE` has that stratum. `QUIET_RANGE_PRESSURE`, `TREND_PRESSURE`, and
`DISORDERLY_OPEN_STABILIZATION_PRESSURE` record it as `NOT_APPLICABLE` and never
invent an event time.

Each review window is `60_000_000 us`, begins on a one-second boundary, lies within
one stratum, and crosses no halt/session boundary. Rank with context
`WO31_REVIEW/<candidate_id>/<stratum>/<run_digest>/<start_us>` and tie root `3199001`;
take the first two with pairwise time intersection-over-union `<=500000`. Report
shortfall without replacement. Blind
candidate ID, root seed, event type, pressure controls, future outcome, and truth;
retain observable feed and phase-relative time. Packet order is display-label order,
stratum order above, then selection digest.

The window universe is exhaustive. For each run, enumerate every integer `k>=0` with
`start_us=session_start_us+k*1_000_000` and
`start_us+60_000_000<=session_end_us`, in ascending `k`; retain the start iff the
entire half-open window belongs to exactly one named stratum and contains no halt or
session-phase boundary in its open interior. Auction/continuous endpoints may be a
window endpoint but never an interior point. No sampled subset, event-anchored start,
or stratum-end special candidate is permitted before ranking.

`FULL_DAY_PERFORMANCE_V1` is the exact committed `QUIET_RANGE_PRESSURE` plan under
`SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1`, root `3102000`. Checkpoints occur at
initialization, every `900_000_000 us` of continuous time, and every phase boundary;
coincident cuts emit one checkpoint. In sequential fresh audit-owned roots, run one
unmeasured warmup and three measured generations, then replay each measured artifact
once in a fresh process. No retry or sample substitution is allowed. Wall duration
uses `time.perf_counter_ns`; throughput is
`round_div_even(sum(outer_event_count)*1_000_000_000,sum(generation_elapsed_ns))`;
peak RSS is maximum fresh-child `resource.getrusage(RUSAGE_SELF).ru_maxrss`, normalized
to bytes by the recorded platform rule; sizes are exact artifact byte lengths. Timing
and RSS never enter semantic identity.

The sole V1 threshold-qualified predicate is `system == "Darwin"`,
`machine == "arm64"`, `python_implementation == "CPython"`, Python major/minor `3.14`,
logical CPU count `>=4`, physical memory `>=8*1024**3 bytes`, and free governed-store
bytes `>=12*1024**3` immediately before warmup. Exact patch/runtime, CPU count, memory,
and free bytes remain in the fingerprint. Any other environment reports
`UNSUPPORTED` with raw measurements and cannot emit a threshold PASS; deterministic
correctness gates still run. Limits are:

| Metric | PASS | WARNING | FAIL/abort |
|---|---:|---:|---:|
| generation P50 | `<=300 s` | `<=900 s` | `>900 s` |
| replay P50 | `<=180 s` | `<=600 s` | `>600 s` |
| generation throughput | `>=500 events/s` | `>=100 events/s` | `<100 events/s` |
| peak RSS | `<=4 GiB` | `<=8 GiB` | `>8 GiB` |
| largest checkpoint | `<=256 MiB` | `<=512 MiB` | `>512 MiB` |
| complete run bytes | `<=4 GiB` | `<=12 GiB` | `>12 GiB` |

For every row, evaluate PASS first, else WARNING, else FAIL; a hard abort is FAIL.
Aggregate is FAIL if any row fails, else WARNING if any warns, else PASS. Limits are
inclusive. Before accepting an operation that would make outer events exceed
`5_000_000`, pending items exceed `250_000`, distinct microsteps at one timestamp
exceed `128`, emitted events at one timestamp exceed `100_000`, a canonical
checkpoint exceed `512*1024**2` bytes, or a complete staged run exceed `12*1024**3`
bytes, emit a deterministic failure artifact and refuse the uncommitted operation.
Generation also aborts after elapsed time becomes strictly greater than `900 s`,
replay after `600 s`, or normalized peak RSS after `8 GiB`; operational abort data is
outside simulation identity. Equality is allowed: a `512 MiB` checkpoint is WARNING,
while one byte more is FAIL/abort.

#### 5.7.3 WO33-A1 mining policy

Qualification sources are fixed before mining: quiet `full_day.toml` with
`QUIET_RANGE_PRESSURE`, root `3102000`; event `opening_momentum.toml` with
`EVENT_SHOCK_PRESSURE`, root `3102000`; `hidden_liquidity.toml` through
`HIDDEN_LIQUIDITY_RECORDING_V1`, root `3302001`; `fragmented_venue.toml` through
`MULTIVENUE_RECORDING_V1`, root `3302002`; and
`historical_reconstruction.toml` through `HISTORICAL_LESSON_V1`, seed
`NOT_APPLICABLE`. A1 binds their exact bytes/digests/bounds/capabilities later without
changing these selections.

Detector calculations use nonoverlapping `100_000 us` observable bins. A bin is its
half-open interval; an event on the right boundary belongs to the next bin. `bid_top`
and `ask_top` are displayed shares at the best price in the left-limit projection:
the state after every canonical source event with timestamp strictly less than the
bin's left edge and before every event exactly on that edge. Edge-time events belong
to that bin's event totals and can first affect the next bin's boundary projection.
No right-edge or post-edge sample is substituted. In that left-limit state,
`I=share_ppm(bid_top-ask_top,bid_top+ask_top)`, and
`mid_x2=best_bid_ticks+best_ask_ticks`. Midpoint movement of `k ticks` means signed
`mid_x2` movement of `2*k`. Aggressive-flow imbalance is
`share_ppm(buy_shares-sell_shares,buy_shares+sell_shares)`. Trailing windows exclude
the active bin.

Every state-valued detector clause uses only these ordered left-limit projections at
eligible bin edges, including starts, minima/maxima, queue cycles and returns,
best-price stability, spread continuity/persistence, and midpoint extrema. Only event
count/quantity clauses aggregate canonical events inside bins; an intra-bin post-event
state is never an additional state sample.

The “one-second” bins used only by aggressive-flow burst and cancellation burst are
nonoverlapping groups of exactly ten consecutive observable bins aligned to the
source lower bound: group `g` is bins `10*g..10*g+9` and spans
`[source_lower_us+g*1_000_000,source_lower_us+(g+1)*1_000_000)`. The previous twenty
are the twenty complete groups immediately preceding `g`; a missing or partial group
makes `g` ineligible. Activation is the active group's right edge, and no rolling
one-second construction is permitted.

For a side with starting displayed quantity `q0>0`, depletion share is
`unsigned_share_ppm(q0-minimum_quantity_in_window,q0)`. For removed quantity, cancel
share is `unsigned_share_ppm(cancelled_shares,cancelled_shares+executed_shares)`.
Best-price stability always refers to its integer tick, not displayed size.

| Detector | Exact activation rule |
|---|---|
| strong queue imbalance | `abs(I)>=600000`, top total `>=500 shares`, continuously for `>=2_000_000 us` |
| queue depletion | side starts `>=500 shares` and depletion share `>=700000` within `1_000_000 us` |
| queue replenishment | within `5_000_000 us`, at least three cycles in which `unsigned_share_ppm(cycle_start-cycle_minimum,cycle_start)>=500000` and displayed quantity at that price returns to `>=round_div_even(cycle_start,2)` within `500_000 us`; cumulative ordinary-add plus reserve-refresh quantity `>=1000 shares` |
| bid absorption | aggressive sells `>=1000 shares` in `2_000_000 us`; best bid tick never decreases; total bid adds plus reserve refreshes at the opening best-bid tick `>=300 shares` |
| ask absorption | aggressive buys `>=1000 shares` in `2_000_000 us`; best ask tick never increases; total ask adds plus reserve refreshes at the opening best-ask tick `>=300 shares` |
| failed breakout | upward: `mid_x2>=prior_five_second_max+2`, the last sample beyond that max is `<2_000_000 us` after first, then `mid_x2<=prior_max-2` within `3_000_000 us`; downward is exactly symmetric around the prior minimum |
| liquidity vacuum | three-level displayed depth starts positive, depletion share `>=750000` within `1_000_000 us`, cancel share `>=600000`, and spread expands `>=2 ticks` or the side becomes empty |
| spread expansion | spread changes from `<=2` to `>=4 ticks` within `500_000 us` and remains `>=4` continuously for `>=500_000 us` |
| spread recovery | following a spread-expansion candidate, spread reaches `<=2 ticks` within `5_000_000 us` and remains `<=2` continuously for `>=1_000_000 us` |
| aggressive-flow burst | one-second aggressive volume `>=max(2000 shares,4*P50(previous twenty one-second bins))` and `abs(aggressive_flow_imbalance)>=700000` |
| cancellation burst | one-second cancelled shares `>=max(2000,4*P50(previous twenty one-second bins))` and unbounded `ratio_ppm(cancelled_shares,added_shares)>=3000000`; zero adds with positive cancellations is explicit sentinel `POSITIVE_INFINITY` and satisfies the ratio clause |
| hidden reserve refresh | at one price, executed quantity `>=1500 shares` in `5_000_000 us`, maximum displayed quantity `<=500`, and at least three engine-labeled reserve-refresh events occur after executions there; sources without authoritative reserve-refresh evidence are unsupported |
| apparent liquidity mirage | freeze the cohort as all IDs at one side/price on the first bin boundary where their displayed sum reaches `>=2000`; within `500_000 us`, `unsigned_share_ppm(cohort_cancelled,cohort_peak)>=800000` and `unsigned_share_ppm(cohort_executed,cohort_peak)<=200000` |
| latency-sensitive opportunity | replay one checkpoint with identical information/action and arrival latency `250 us` versus `2500 us`; `unsigned_share_ppm(abs(fast_filled-slow_filled),objective_shares)>=250000` or absolute fee-adjusted average-cost difference `>=1000 milliticks/share` |
| cancel/fill race | baseline cancel and opposing-fill arrivals differ by `<=2_000 us`; replay the same checkpoint with cancel-path latency `max(0,baseline_latency_us-1000)` and `baseline_latency_us+1000`; terminal filled/cancelled winner differs |
| multi-venue fragmentation | among venues with at least `100 executable shares`, `max(best_bid)-min(best_bid)>=2 ticks` or `max(best_ask)-min(best_ask)>=2 ticks`, continuously for `>=5_000 us` |
| routing dilemma | two routes are Pareto-incomparable where lower fee-adjusted cost, higher executable quantity, and lower expected receipt time are better; each is strictly better on one axis and worse on another; at least one absolute difference is `>=1000 milliticks/share`, `>=250 shares`, or `>=1000 us` |
| auction imbalance change | consecutive published values within `30_000_000 us` have absolute share change `>=10000` and `unsigned_share_ppm(abs(new-old),max(1,abs(old)))>=250000`, or signs differ and both magnitudes are `>=5000 shares` |
| halt/reopening | halt then reopen; compare first post-reopen trade with last pre-halt trade and first-five-second post-reopen spread with P50 spread over the final five seconds before halt; price gap `>=3 ticks` or post/pre spread ratio `>=2000000` |
| distressed liquidation | authoritative distressed-flow sells `>=5000 shares` in `5_000_000 us`, unbounded sell/buy ratio `>=4000000`, and signed first-to-last `mid_x2` movement `<=-2`; sources without participant identity are unsupported |
| momentum exhaustion | `mid_x2` moves at least `10` units in one direction over `10_000_000 us` with same-direction aggressive-imbalance magnitude `>=700000`; in the next `5_000_000 us`, additional same-direction movement is at most `2` units and `abs(aggressive_flow_imbalance)<=200000` |
| mean-reversion transition | `mid_x2` displacement from trailing-thirty-second P50 is at least `8` units; within `15_000_000 us`, movement from the activation extreme toward that frozen P50 divided by absolute initial displacement is `>=500000`; aggressive-flow imbalance changes sign and each signed magnitude is `>=300000` |

Ordinary detectors exclude auction, halt, session-boundary, and missing-capability
intervals. Auction and halt/reopening are the only exceptions. Missing required book,
trade, participant, venue, latency, or cohort-order capability produces
`NOT_EXERCISED`, not detector false. A zero/unavailable denominator is insufficient
unless the rule above declares an explicit infinity sentinel.

Candidate enumeration is stateless and exhaustive, not implementation-dependent
re-arming. Every candidate key is exactly `(detector_id,direction,side,venue,price,
witness_key,anchor_start_us,evidence_discriminator)`. Direction and side use
`BUY|SELL|NOT_APPLICABLE`; venue uses one
NFC venue ID, `CONSOLIDATED`, or `NOT_APPLICABLE`; price is one positive integer tick
or `NOT_APPLICABLE`. For each source, scan those axes in that literal enum/sentinel
order with real venue IDs NFC-byte sorted and integer prices ascending.
`witness_key` is `NOT_APPLICABLE` when no entity identity is needed; otherwise it is
the lowercase SHA-256 of compact sorted-key canonical JSON
`{"kind":kind,"ids":[ids...]}`. IDs are nonempty NFC strings. Order/cohort IDs are
NFC-byte sorted; route and venue pairs are unordered and sorted; consecutive
publication and halt/reopen message IDs retain causal order. Witness schemas are
exact: `LATENCY_ACTION` IDs are checkpoint digest, recorded executable-action ID,
venue-or-`CONSOLIDATED`, and objective direction in that order;
`CANCEL_FILL_TUPLE` IDs are order ID, cancel-command ID, and contra-arrival ID in
causal order; `ORDER_COHORT` IDs are every frozen cohort order ID sorted;
`VENUE_PAIR` and `ROUTE_PAIR` contain their two sorted IDs;
`AUCTION_PUBLICATION_PAIR` and `HALT_REOPEN_PAIR` contain two source-event IDs in
causal order; `SPREAD_EXPANSION_PARENT` contains the parent candidate ID. Every other
detector uses `NOT_APPLICABLE`; an unknown kind or wrong arity fails. Enumerate every
authoritative recorded executable action/checkpoint/venue/objective tuple for latency
opportunity, every eligible order/cancel/contra-arrival tuple for cancel/fill race,
every eligible cohort for apparent mirage, every unordered venue pair
for fragmentation, every unordered route pair for routing dilemma, every consecutive
publication pair, every complete halt/reopen episode, and every qualifying parent for
spread recovery. Other detectors use the
sentinel. Scan witness objects by their canonical JSON bytes. Every eligible
anchor/witness combination that satisfies the complete rule
emits one raw candidate; overlapping anchors are retained until the explicit
deduplication pass below. `anchor_start_us` is exactly `active_start_us`.
`evidence_discriminator` is the lowercase SHA-256 of the
compact canonical JSON array of every contributing stable source-event ID in
canonical source sequence, with IDs encoded as NFC strings; it distinguishes, among
other cases, multiple spread-recovery parents at one timestamp. Scan it last by raw
digest bytes. Empty contributing evidence is invalid. For bin-derived clauses,
activation is the right edge of the
first bin at which every clause is knowable; for exact-message clauses it is the last
required message timestamp. `active_start_us` is the left edge of the first
contributing bin or first contributing message, and `active_end_us` is one microsecond
after activation. `warmup_start_us` is the maximum of the source lower bound and the
active start minus the largest declared trailing lookback. `post_end_us` is the
minimum of the source upper bound and activation plus the largest declared forward
or persistence horizon; a detector with neither uses activation plus one microsecond.
Every bound is half-open and recorded. Equal-time messages use canonical source event
sequence.

Queue-replenishment cycles are greedy within a fixed side/price anchor. A cycle starts
at the first positive displayed-quantity sample not already consumed by a prior
cycle, freezes that quantity as `cycle_start`, and freezes the first subsequent
minimum that satisfies depletion. The first later sample within `500_000 us` that
returns to at least `round_div_even(cycle_start,2)` closes the cycle. The next cycle
can start only at the following bin. A missed return discards that start and resumes
at the first positive sample after its deadline. No sample belongs to two cycles.
For cancel/fill race, terminal outcome is `FULL_FILL` only when original quantity is
fully filled before effective cancellation; any positive remainder cancelled is
`CANCEL`, with partial-filled quantity retained as evidence. For distressed
liquidation, positive sells and zero buys yield `POSITIVE_INFINITY` and satisfy the
ratio clause; zero sells never does.

For halt/reopening, the pre-spread distribution contains every two-sided constant-
spread duration segment in `[halt_time_us-5_000_000,halt_time_us)` and the post
distribution those in `[reopen_time_us,reopen_time_us+5_000_000)`. Require full source
coverage of both wall-time intervals and at least one positive-duration two-sided
segment in each; reduce each by the section-5.7.1 time-weighted P50. The spread ratio
is unbounded `ratio_ppm(post_p50,pre_p50)`. Price gap uses the last trade strictly
before halt and first trade at or after reopen. A missing trade/quote distribution,
zero pre P50, or clipped interval is `INSUFFICIENT_EVIDENCE`.

The detector registry below is normative. Key columns are `direction/side/venue/
price`; `C` means `CONSOLIDATED`, `V` the one source venue ID, `P` the frozen relevant
integer tick, and `NA` the exact `NOT_APPLICABLE` sentinel. `SIGN` is BUY for positive
and SELL for negative; a zero sign is NA. Capability bundles are: `Q2` = quotes,
depth snapshots, depth deltas, complete event sequence, session events, and microsecond
timestamp precision; `TQ` = trades, quotes, aggressor side, complete event sequence,
session events, and microsecond precision; `MBO` = `Q2` plus order identity; `HID` =
`MBO` plus authoritative reserve-refresh labels; `LAT` = `TQ` plus portable checkpoint
and deterministic latency intervention; `VEN` = per-venue quotes, executable depth,
fees, receipt-latency model, and complete event sequence; `AUC` = auction state,
published imbalance, indicative price, and complete event sequence; `HALT` = halt
state, trades, quotes, and complete event sequence; `PART` = `TQ` plus authoritative
participant identity. Evidence `S` is authoritative synthetic ground truth, `H` is
observed historical market-by-order plus the named bundle, and `R` is explicitly
labeled reconstruction/counterfactual plus the named observable bundle. No unlisted
class is supported. Supporting skills are the exact comma-separated sorted IDs shown.

| Detector | Key axes | Bundle / evidence | Primary skill | Supporting skills |
|---|---|---|---|---|
| strong queue imbalance | `SIGN/SIGN/C/P` | `Q2 / S,H` | `BOOK_READING` | `QUEUE_POSITION` |
| queue depletion | `NA/affected/C/P` | `Q2 / S,H` | `QUEUE_POSITION` | `BOOK_READING` |
| queue replenishment | `NA/affected/C/P` | `MBO / S,H` | `ABSORPTION_RECOGNITION` | `QUEUE_POSITION,TAPE_READING` |
| bid absorption | `BUY/BUY/C/P` | `MBO+TQ / S,H` | `ABSORPTION_RECOGNITION` | `BOOK_READING,TAPE_READING` |
| ask absorption | `SELL/SELL/C/P` | `MBO+TQ / S,H` | `ABSORPTION_RECOGNITION` | `BOOK_READING,TAPE_READING` |
| failed breakout | `SIGN/NA/C/NA` | `TQ / S,H,R` | `REGIME_RECOGNITION` | `EXIT_EXECUTION,TAPE_READING` |
| liquidity vacuum | `NA/affected/C/P` | `MBO+TQ / S,H` | `LIQUIDITY_WITHDRAWAL` | `BOOK_READING,POSITION_MANAGEMENT` |
| spread expansion | `NA/NA/C/NA` | `TQ / S,H,R` | `SPREAD_DECISION` | `BOOK_READING` |
| spread recovery | `NA/NA/C/NA` | `TQ / S,H,R` | `SPREAD_DECISION` | `REGIME_RECOGNITION` |
| aggressive-flow burst | `SIGN/SIGN/C/NA` | `TQ / S,H,R` | `TAPE_READING` | `AGGRESSIVE_ENTRY,VOLUME_CONTEXT` |
| cancellation burst | `NA/affected/C/NA` | `MBO / S,H` | `LIQUIDITY_WITHDRAWAL` | `QUEUE_POSITION` |
| hidden reserve refresh | `NA/affected/V/P` | `HID / S,H` | `HIDDEN_LIQUIDITY` | `ABSORPTION_RECOGNITION,BOOK_READING` |
| apparent liquidity mirage | `NA/affected/V/P` | `MBO / S,H` | `HIDDEN_LIQUIDITY` | `BOOK_READING,SCRIPT_DISCIPLINE` |
| latency-sensitive opportunity | `objective/objective/V-or-C/NA` | `LAT / S,R` | `LATENCY_AWARENESS` | `PASSIVE_ENTRY` |
| cancel/fill race | `NA/order-side/V-or-C/P` | `LAT+MBO / S,R` | `CANCEL_TIMING` | `LATENCY_AWARENESS,PARTIAL_FILL_MANAGEMENT` |
| multi-venue fragmentation | `NA/affected/NA/NA` | `VEN / S,H` | `MULTI_VENUE_ROUTING` | `BOOK_READING` |
| routing dilemma | `objective/objective/NA/NA` | `VEN / S,R` | `MULTI_VENUE_ROUTING` | `LATENCY_AWARENESS,SPREAD_DECISION` |
| auction imbalance change | `SIGN/SIGN/C/NA` | `AUC / S,H` | `AUCTION_EXECUTION` | `BOOK_READING` |
| halt/reopening | `gap-sign/NA/C/NA` | `HALT / S,H,R` | `HALT_REOPENING` | `SCRIPT_DISCIPLINE` |
| distressed liquidation | `SELL/SELL/C/NA` | `PART / S,H` | `POSITION_MANAGEMENT` | `EXIT_EXECUTION,TAPE_READING` |
| momentum exhaustion | `SIGN/SIGN/C/NA` | `TQ / S,H,R` | `REGIME_RECOGNITION` | `EXIT_EXECUTION,TAPE_READING` |
| mean-reversion transition | `SIGN/NA/C/NA` | `TQ / S,H,R` | `REGIME_RECOGNITION` | `SPREAD_DECISION,TAPE_READING` |

`affected` is the side named by the activation clause; `objective` is the declared
buy/sell direction; `gap-sign` is the signed price gap or NA when only spread triggers;
and `V-or-C` is V only when the intervention targets one authoritative venue,
otherwise C. Every detector's suggested objective is exactly
`OBSERVE_CLASSIFY_V1(detector_id,direction)`: submit at most one classification during
`[activation_us,post_end_us)`. Exact detector and applicable direction is `COMPLETED`;
correct detector family but wrong detector or direction is `PARTIALLY_COMPLETED`;
missing/other classification is `NOT_COMPLETED`; nonempty known ambiguity is
`AMBIGUOUS_EVIDENCE` and overrides those results. `AVOIDED_INVALID_ACTION` and
`NO_ACTIONABLE_OBJECTIVE` are valid vocabulary but unproduced by this V1 registry.

Sample frequency is computed before deduplication or review. An eligible sampling
unit is one detector/source/`100_000 us` bin with every required capability, no
ordinary exclusion, and complete source coverage and required fields for that
detector's maximum pre-activation lookback plus its complete post-activation evidence/
persistence horizon. A boundary-clipped or field-incomplete unit is excluded from the
denominator, not counted as detector false. Aggressive-flow burst and cancellation burst instead use one
complete aligned one-second group, latency and cancel/fill use one bound replay pair,
auction uses one consecutive publication pair, and halt/reopening uses one complete
halt/reopen episode instead. Multiple qualifying keys in one unit count once. For each
`(detector_id,qualification_source_row)`, numerator is qualifying units and
denominator is eligible units; `sample_frequency_ppm=unsigned_share_ppm(numerator,
denominator)` and `rarity_ppm=S-sample_frequency_ppm`. Zero eligible units is
`NOT_EXERCISED`. WO33-C's reference population is exactly every eligible unit in the
five preregistered qualification rows, before deduplication and review. Its overall
detector frequency pools numerators and denominators across rows once, while retaining
the per-row values; candidate rarity uses its own detector/source-row value.

For every detector candidate, signal strength derives mechanically from its satisfied
non-time activation clauses. Before scoring, orient signed clauses to nonnegative
magnitude: `x<=-L` becomes `-x>=L`; `abs(x)>=L` uses `abs(x)`; a directional signed
movement uses `x` for BUY and `-x` for SELL. An exact `POSITIVE_INFINITY` ratio has
legibility `S`; it is never passed to integer arithmetic. Any other noninteger
sentinel is missing evidence. A lower-bound magnitude clause `x>=L`, `L>0`, has
legibility `clamp(round_div_even(x*S,2*L),0,S)`. An upper-bound magnitude clause
`x<=U` requires nonnegative `x,U` and has legibility
`clamp(round_div_even((2*U-x)*S,max(1,2*U)),0,S)`. A required Boolean contributes `S`
when true; equality, ordering, membership, category, and exact-state predicates are
Booleans rather than invented numeric distances. AND uses minimum clause legibility;
OR uses the maximum legibility of a
fully satisfied branch.

Duration legibility is
`clamp(round_div_even(observed_contiguous_duration*S,2*required_duration),0,S)` for a
persistence clause and `NOT_APPLICABLE` otherwise. Signal-duration legibility is the
mean of applicable signal and duration values; its difficulty component is exactly
`S-signal_duration_legibility`. Conflict is
`unsigned_share_ppm(opposite_direction_aggressive_shares,
same_direction_aggressive_shares+opposite_direction_aggressive_shares)` for a
directional candidate and `NOT_APPLICABLE` otherwise. Reaction time is integer
microseconds. For every V1 classification objective it is exactly
`post_end_us-activation_us`, the half-open response interval; a nonpositive interval
is invalid. A later nonclassification objective must preregister its last
replay-proven completion timestamp or mark reaction `NOT_APPLICABLE`.

Evidence quality is fixed by evidence class: authoritative synthetic ground truth
`1000000`, exact historical market-by-order evidence `850000`, and labeled
reconstruction/counterfactual evidence `500000`; weaker unsupported evidence is
missing. Hidden uncertainty is respectively `0`, `250000`, and `750000`, and is
`NOT_APPLICABLE` when hidden liquidity is outside the candidate capability.

Signal and duration observations use the complete activation evidence, including the
final activating event. Conflict counts aggressive shares in the exact half-open
interval `[active_start_us,activation_us+1)`, also including that final event. Spread,
three-level depth, and venue count freeze from the last complete observable projection
strictly before `activation_us`; they never read the post-activation state. Latency
alone uses the final activating event's delivery timestamps as specified below.
Spread is the current two-sided integer spread in that pre-activation projection.
Latency is `max(0,client_visible_time_us-source_event_time_us)` for the final
activating event and is `NOT_APPLICABLE` without authoritative delivery timestamps.
Three-level depth is the displayed sum at the best three prices on the side consumed
by the objective direction, falling back to the detector's affected side; when both
are `NOT_APPLICABLE` it is the smaller of complete bid/ask
three-level sums. Venue count is the number of authoritative venues with positive
executable depth on that side, or with complete two-sided quotes for a nondirectional
candidate. `feature_count` is the number of distinct observable field paths used by
satisfied activation clauses, not the number of value-bearing tokens. Every V1
`OBSERVE_CLASSIFY_V1` objective has no share quantity, so
objective-size/depth is `NOT_APPLICABLE`; a later version must declare both integers
before that component can apply. A missing quote/depth/delivery field makes only its
component `NOT_APPLICABLE`, never an invented zero.

The eleven nominal weights are inverse signal-duration legibility `160000`, conflict
`100000`, short reaction `110000`, spread `100000`, latency `70000`, inverse liquidity
`100000`, venues `80000`, hidden uncertainty `60000`, objective-size/depth `80000`,
relevant features `80000`, and inverse evidence quality `60000`. Reaction hardness is
`clamp((2_000_000-reaction_us)*S/2_000_000,0,S)`; spread is
`clamp((spread_ticks-1)*S/9,0,S)`; latency is
`clamp(latency_us*S/10_000,0,S)`; inverse liquidity is
`S-clamp(three_level_depth*S/5000,0,S)`; venues are
`clamp((venue_count-1)*S/3,0,S)`; objective-size/depth is
`clamp(objective_shares*S/max(1,executable_depth),0,S)`; relevant features are
`clamp((feature_count-1)*S/9,0,S)`; inverse quality is `S-evidence_quality_ppm`.
Difficulty is
`round_div_even(sum(applicable_weight*component),sum(applicable_weight))`; an empty
applicable set is insufficient. Every scenario, volume, liquidity, venue, Jaccard,
and difficulty division uses section 5.7.1 rounding. Jaccard of two empty sets is `S`;
one empty and one nonempty is zero.

Deduplication inputs have one canonical construction. Observable-feature tokens are
the set of `event_type|field_path|type_tag|canonical_value` for every observable field
used by a satisfied activation clause: NFC strings, lowercase `true|false`, and base-
10 integers with no leading zero except zero; IDs, timestamps, and ground truth are
excluded. The regime signature is canonical JSON with sorted keys and separators
`(",",":")` containing exactly phase, declared regime ID, volume band, liquidity
band, and spread band, where volume/liquidity use section 5.7.4 bands and spread is
`ONE=1`, `TWO=2`, `MODERATE=3..4`, `WIDE=5..8`, or `EXTREME>=9` ticks.
Historical/reconstruction sources without authoritative regime, volume, or liquidity
metadata use literal `NOT_APPLICABLE` independently for those fields and never infer
them from the detected outcome. An event token
is `event_type|side|price_relation`, with side `BUY|SELL|NONE` and price relation to
the pre-event best quotes `BELOW_BID|AT_BID|INSIDE|AT_ASK|ABOVE_ASK|NO_PRICE|
NO_REFERENCE_QUOTE`. `NO_PRICE` means the event has no price;
`NO_REFERENCE_QUOTE` means it has a price but either pre-event best quote is absent; the
five-gram set contains every consecutive five-token tuple in canonical event order,
or the one complete shorter tuple when fewer than five exist. The objective set is
exactly the candidate's one `primary_skill_id` plus its sorted supporting skill IDs.
All sets compare decoded NFC strings, not implementation hashes.

Every candidate also has `source_ancestry_sha256`, the lowercase SHA-256 of compact
sorted-key canonical JSON with exactly `source_kind`, `source_id`, `source_sha256`,
nullable `checkpoint_id`, nullable `checkpoint_sha256`, nullable
`event_prefix_sha256`, and nullable `parent_source_ancestry_sha256`. Kind is
`RUN|DATASET|RECONSTRUCTION`; IDs are nonempty NFC strings and nonnull digests are
lowercase hex. A missing inapplicable value is explicit null, while a required
missing value invalidates the candidate. “Ancestry matches” means these complete
digest bytes are identical.

`LessonCandidateIdentityProjectionV1` is compact sorted-key canonical JSON with ASCII
escapes, separators `(",",":")`, and no final LF. It has exactly these keys and no
others: `schema_version=1`; `source_ancestry_sha256`; `source_identity`; `candidate_key`;
`detector`; `bounds`; nullable `checkpoint`; `observable_feature_summary_sha256`;
nullable `ground_truth_summary_sha256`; `lesson_type="OBSERVE_CLASSIFY"`;
`difficulty_projection`; `rarity_projection`; `source_window_outcome`;
`primary_skill_id`; sorted `supporting_skill_ids`; `objective_projection`; nullable
`reveal_material_sha256`; sorted unique `known_ambiguity`; `capability_record_sha256`;
`evidence_class`; and `proposal_state="PROPOSED"`.

The nested projections are exact. `source_identity` is
`{"kind":kind,"id":id,"sha256":sha256}`, with kind
`RUN|DATASET|RECONSTRUCTION`. `candidate_key` is the JSON array
`[detector_id,direction,side,venue,price,witness_key,anchor_start_us,
evidence_discriminator]` using the types and sentinels above. `detector` is
`{"id":detector_id,"version":version,"threshold_sha256":threshold_sha256}`.
`bounds` is `{"source_start_us":int,"source_end_us":int,"warmup_start_us":int,
"active_start_us":int,"active_end_us":int,"post_end_us":int}`. A nonnull checkpoint
is exactly `{"id":id,"sha256":sha256}`. `objective_projection` is exactly
`{"kind":"OBSERVE_CLASSIFY_V1","detector_id":detector_id,"direction":direction,
"response_start_us":activation_us,"response_end_us":post_end_us,
"outcome_mapping_id":"OBSERVE_CLASSIFY_OUTCOME_V1"}`.

`rarity_projection` is exactly `{"policy_id":"MINING_RARITY_V1",
"qualification_source_row":id,"qualifying_units":int,"eligible_units":int,
"sample_frequency_ppm":int,"rarity_ppm":int}`. `difficulty_projection` is exactly
`{"policy_id":"LESSON_DIFFICULTY_V1","signal_legibility_ppm":nullable-int,
"duration_legibility_ppm":nullable-int,"signal_duration_legibility_ppm":nullable-int,
"inverse_signal_duration_ppm":nullable-int,"conflict_ppm":nullable-int,
"reaction_us":nullable-int,"reaction_hardness_ppm":nullable-int,
"spread_ticks":nullable-int,"spread_hardness_ppm":nullable-int,
"latency_us":nullable-int,"latency_hardness_ppm":nullable-int,
"three_level_depth":nullable-int,"inverse_liquidity_ppm":nullable-int,
"venue_count":nullable-int,"venue_hardness_ppm":nullable-int,
"hidden_uncertainty_ppm":nullable-int,"objective_shares":nullable-int,
"executable_depth":nullable-int,"objective_depth_hardness_ppm":nullable-int,
"feature_count":int,"feature_hardness_ppm":int,"evidence_quality_ppm":int,
"inverse_quality_ppm":int,"applicable_weight_sum":int,"difficulty_ppm":int}`.
Every nullable integer is JSON null exactly when its component is `NOT_APPLICABLE`;
`INSUFFICIENT_EVIDENCE`, a missing required key, an extra key, or an inconsistent
derived value invalidates the candidate instead of entering identity.

The four summary/record digests also have one canonical preimage each. All use the
same compact sorted-key JSON encoding as candidate identity. `ObservableFeatureSummaryV1`
has exactly `schema_version=1`, `feature_tokens`, `regime_signature`,
`event_five_grams`, and `contributing_source_event_ids`. `feature_tokens` is the
unique NFC UTF-8-sorted array of the exact observable tokens defined below;
`regime_signature` is that exact five-key object; `event_five_grams` is the unique
array of token arrays sorted by each array's compact canonical JSON bytes; and
`contributing_source_event_ids` is the nonempty array of NFC IDs in canonical source
event sequence, retaining causal duplicates only when distinct source events carry
the same ID (which makes the source invalid). Hashing the final ID array alone must
reproduce `evidence_discriminator`.

`GroundTruthSummaryV1` is nonnull only for evidence class `S` and has exactly
`schema_version=1`, `evidence_class="S"`, `expected_classification`,
`authoritative_activation=true`, and `supporting_source_event_ids`.
`expected_classification` is exactly `{"detector_id":detector_id,
"direction":direction}`; supporting IDs are the canonical-source-order subsequence
of contributing IDs carrying the authoritative synthetic labels. It must be nonempty.
Evidence classes `H` and `R` use JSON null and may not synthesize this summary.

`RevealMaterialV1` has exactly `schema_version=1`,
`policy_id="OBSERVE_CLASSIFY_REVEAL_V1"`, `detector_id`, `detector_version`,
`direction`, `outcome_mapping_id="OBSERVE_CLASSIFY_OUTCOME_V1"`,
`observable_feature_summary_sha256`, nullable `ground_truth_summary_sha256`, and
`supporting_source_event_ids`; the final array is the same canonical sequence used by
the ground-truth summary when nonnull, otherwise the complete contributing-ID array.
Every V1 registry candidate has this nonnull reveal record; null is reserved for a
future registry row that explicitly declares `NO_REVEAL` before mining.

`CapabilityRecordV1` has exactly `schema_version=1`, `source_identity`, `detector`,
and `records`. The first two nested objects are byte-identical to candidate identity.
`records` has exactly one row per registry-required capability, sorted by capability
NFC UTF-8 bytes; each row is exactly `{"capability":id,"status":"AVAILABLE",
"evidence":refs}`. `refs` is a nonempty unique array sorted by `(kind,id,sha256)`;
each reference has exactly those three keys, kind
`SOURCE_MANIFEST|EVENT_RANGE|CHECKPOINT|ADAPTER_CONTRACT`, a nonempty NFC ID, and a
lowercase SHA-256. A missing capability produces `NOT_EXERCISED` and no candidate, so
`MISSING` is not an identity-bearing candidate status.

`observable_feature_summary_sha256`, `ground_truth_summary_sha256`,
`reveal_material_sha256`, and `capability_record_sha256` are SHA-256 of exactly those
complete bytes. Their objects are persisted by content digest and read back before
candidate admission; arbitrary prose, unordered evidence, omitted nulls, extra
source events, or an alternate summary projection fails.

For diversity only, `source_window_outcome` is a mined market-path label, never a
fabricated learner result. Freeze `mid_x2` from the last complete two-sided quote at
or before activation and the last complete two-sided quote strictly before
`post_end_us`. For BUY direction use final minus activation; for SELL use activation
minus final. Oriented movement `>=2` is `CONTINUATION`, `<=-2` is `REVERSAL`, and the
remainder is `STASIS`. A nondirectional candidate is `NOT_APPLICABLE`; missing either
required quote is `NOT_OBSERVABLE`. These five strings are the complete enum, and the
field is blinded with future outcome in technical-review packets.

`candidate_digest=SHA256(LessonCandidateIdentityProjectionV1_bytes)` and
`candidate_id="lesson-candidate-"+candidate_digest`, retaining all sixty-four
lowercase hex characters. Neither `candidate_id`, `candidate_digest`, review
projection, nor any reviewer sidecar enters the projection. This exact candidate ID
is used by parent witnesses, ordering, deduplication, and review ties; no shortened or
storage-generated ID is permitted.

Candidates are duplicates only when ancestry matches, time intersection/union of
their exact `[active_start_us,post_end_us)` half-open intervals is `>=800000`,
observable-feature Jaccard `>=900000`, regime signatures match, canonical
event-five-gram Jaccard `>=850000`, and objective Jaccard `>=500000`. Collapse is one
ordered greedy pass, not connected components: exclude candidates whose difficulty is
`INSUFFICIENT_EVIDENCE`; sort the rest by ascending `difficulty_ppm`, then
`active_start_us`, then NFC UTF-8 candidate-ID bytes; retain a candidate iff it is not
duplicate to any already retained candidate. Otherwise record `duplicate_of` as the
first retained duplicate in that same order. This defines nontransitive chains
without iteration-order dependence.

For the time test, both intervals must have positive duration. Let
`intersection=max(0,min(a_end,b_end)-max(a_start,b_start))` and
`union=(a_end-a_start)+(b_end-b_start)-intersection`; time IoU is exactly
`unsigned_share_ppm(intersection,union)`. Zero/negative duration or zero union is an
invalid candidate, not an equality special case.

Detector families are fixed: `QUEUE` = strong queue imbalance, queue depletion,
queue replenishment; `ABSORPTION` = bid absorption, ask absorption, hidden reserve
refresh, apparent liquidity mirage; `PRICE_LIQUIDITY` = failed breakout, liquidity
vacuum, spread expansion, spread recovery; `FLOW` = aggressive-flow burst,
cancellation burst, distressed liquidation, momentum exhaustion, mean-reversion
transition; `EXECUTION` = latency-sensitive opportunity, cancel/fill race;
`FRAGMENTATION` = multi-venue fragmentation, routing dilemma; and `SESSION` = auction
imbalance change, halt/reopening. A played objective's learner outcome is exactly one of `COMPLETED`,
`PARTIALLY_COMPLETED`, `NOT_COMPLETED`, `AVOIDED_INVALID_ACTION`,
`NO_ACTIONABLE_OBJECTIVE`, or `AMBIGUOUS_EVIDENCE`, assigned by the detector's frozen
objective/outcome mapping rather than title text.

Greedy diversity weights are skill `250000`, detector family `200000`, source
`200000`, phase `100000`, source-window outcome `100000`, difficulty band `150000`; bands are
`[0,250000)`, `[250000,500000)`, `[500000,750000)`, `[750000,S]`. Each dimension
contributes novelty `round_div_even(S,1+selected_count_for_value)` and
`marginal_score=round_div_even(sum(dimension_weight*novelty),S)`; select highest
marginal score with the global tie-break.

Dimension values are exact: skill is `primary_skill_id` only; family is the detector's
one registry family; source is `qualification_source_row`; phase is the recorded
session-phase enum at activation; source-window outcome is the exact mined label above;
and difficulty band is the one interval containing `difficulty_ppm`.
`selected_count_for_value` counts already selected candidates whose exact value in
that one dimension equals the candidate's value. Supporting skills, source run IDs,
and learner outcomes never introduce extra values or aliases.

Technical review target is exactly twenty after deduplication. With tie context
`WO33_REVIEW_V1`, root `3399001`, and source order event, quiet, hidden, fragmented,
historical: (1) select five materially distinct event-source candidates by greedy
diversity; (2) select three from each other source, excluding prior selections; and
(3) continue selecting from every remaining valid candidate by the same global
diversity rule with retained counts until twenty are selected or the pool is
exhausted. Every unfilled reserved position moves to step 3, while its source
shortfall remains explicit. An event candidate is materially distinct only when,
pairwise against every event candidate already selected, `(detector_family differs OR
primary_skill_id differs) AND (phase differs OR source_window_outcome differs OR difficulty_band
differs)`. The event-five gate
passes only if step 1 obtains five; later global selections cannot disguise the
shortfall. Never admit duplicates or weaken thresholds.

#### 5.7.4 WO34-B learner projection

`AttemptAssessmentV1.skill_evidence` contains at most one `SkillEvidenceV1` row per
stable skill ID. Each row has `skill_id`, `opportunity_present`, `observable`,
`score_ppm`, `scoring_policy_id`, `scoring_policy_digest`, and supporting evidence
references. `score_ppm` is an integer in `[0,S]` produced by the immutable lesson or
assignment scoring policy; WO34-B never infers it from P&L, free text, or an error
name. Missing/duplicate rows, unknown policy, digest mismatch, or out-of-range score
fail. No-opportunity or unobservable rows have zero projection weight regardless of
score. Every lesson/candidate declares exactly one `primary_skill_id`; remaining
declared skills are supporting skills.

For projection, `s_i` starts as exactly the row's `score_ppm`. A scored typed error
caps only the evidence row for the skill to which section 5.7.5 maps that error at
`250000`. Three critical caps override that value:
`ACTED_DURING_RED -> SCRIPT_DISCIPLINE` at zero,
`WRONG_HOTKEY -> HOTKEY_ACCURACY` at zero, and
`FAILED_TO_COMPLETE_OBJECTIVE -> lesson primary_skill_id` at zero. A missing mapped
skill row fails assessment construction rather than moving the cap. Multiple caps use
the minimum, and supporting-skill rows are otherwise unchanged.

`LEARNER_PROJECTION_V1` consumes opportunity-bearing observable rows. Base weights
are guided `250000`, practice `600000`, assessment `1000000`, and remediation
`700000`. `UNSCORABLE`, `AMBIGUOUS`, and `INSUFFICIENT_OBSERVABILITY` weigh zero; P&L
weighs zero. It uses attempt ordinal exclusively for decay;
its immutable input is exactly every schema-valid assessment/skill-evidence row for
that learner with `attempt_ordinal<=as_of_attempt_ordinal`, ordered by attempt ordinal,
then NFC UTF-8 assessment/evidence ID, then skill ID. Rows with a later ordinal are
excluded, not errors; an eligible past row may not be omitted or cherry-picked. Only
the declared zero-weight rules above then remove a row's influence. Simulation/study
timestamps remain provenance but do not enter this version.

For age `a=as_of_attempt_ordinal-attempt_ordinal`, recency factor is
`round_div_even(S*S,S+50000*a)` and effective weight is
`mul_ppm(base_weight,recency_factor)`. Each skill has four full-weight pseudo-
observations, two success and two failure:

`mastery_ppm = round_div_even(2*S*S + sum(w_i*s_i), 4*S + sum(w_i))`.

Evidence confidence is `min(S,round_div_even(sum(w_i),8))`. Scenario diversity counts
distinct semantic-plan digests. Volume bands are `LOW <750000`,
`NORMAL 750000..1250000`, and `HIGH >1250000` of bound base-volume multiplier;
liquidity uses the same bands. Source class is `SYNTHETIC` or
`HISTORICAL_OR_RECONSTRUCTION`. Diversity confidence is the rounded mean of
`unsigned_share_ppm(min(scenario_count,4),4)`,
`unsigned_share_ppm(min(volume_band_count,3),3)`,
`unsigned_share_ppm(min(liquidity_band_count,3),3)`, and
`unsigned_share_ppm(min(source_class_count,2),2)`. Final confidence is the minimum of
evidence and diversity confidence; uncertainty is `S-confidence`.

Opportunity count is the number of positive-weight rows for the skill. Recent history
and every latest-score query likewise exclude zero-weight, no-opportunity, or
unobservable rows. Evidence is
sufficient only with at least eight opportunities, effective weight `>=4*S`, three
scenarios, two volume bands, two liquidity bands, and confidence `>=500000`. Latest
post-cap `s_i >=700000` is demonstrated success; post-cap `s_i <=300000` is failure.
Recent history is the latest twenty positive-weight rows. Labels are `INSUFFICIENT`, then
`NEEDS_WORK <500000`, `DEVELOPING <700000`, or `STRONG >=700000`; all remain
unvalidated model projections.

#### 5.7.5 WO34-C adaptive selection

`SKILL_GRAPH_V1` has exactly four directed prerequisite edges:
`QUEUE_POSITION -> MULTI_VENUE_ROUTING`,
`TAPE_READING -> ABSORPTION_RECOGNITION`,
`LATENCY_AWARENESS -> CANCEL_TIMING`, and
`BOOK_READING -> HIDDEN_LIQUIDITY`. These encode respectively advanced passive
routing, absorption interpretation, cancel-race timing, and hidden-liquidity
inference. Every unlisted skill has no prerequisite in V1. Every edge uses the one
`PREREQUISITE_READY_V1` minimum-evidence policy below—no edge override or implicit
transitive edge exists.

`CurriculumSelectionRequestV1` requires projection digest, selection ordinal,
nonnegative root seed, mode, catalog digest, applicable plan/assignment digest, and
explicit `as_of_attempt_ordinal`; it has no implicit seed. Every lesson has exactly
one `primary_skill_id`.

A prerequisite is satisfied only when sufficient, mastery `>=650000`, confidence
`>=500000`. The target universe is the sorted set of stable `primary_skill_id` values
of catalog lessons that pass capability, consent, assignment, mode, and immutable
manual-plan eligibility before prerequisite/cooldown filtering. If every such target
is insufficient, cold start permits only catalog entries without an unsatisfied
prerequisite.

Ranking weights are weakness `250000`, uncertainty `150000`, prerequisite readiness
`100000`, recency need `100000`, recent-variety need `100000`, difficulty progression
`100000`, scenario diversity `60000`, volume diversity `50000`, liquidity diversity
`50000`, and historical/synthetic balance `40000`. Weakness is `S-mastery`, or
`500000` if insufficient. Recency need is
`round_div_even(min(age,20)*S,20)`, with never attempted equal to `S`. Variety need is
`S-min(S,round_div_even(recent_target_count_in_last_8*S,4))`. Scenario, volume,
liquidity, and source needs are each
`S-min(S,round_div_even(candidate_value_count_in_last_12*S,cap))` with caps four,
three, three, and two respectively. Target difficulty is
`200000+mul_ppm(600000,mastery)`;
progression is `S-abs(drill_difficulty-target)`. Prerequisite readiness is minimum
prerequisite confidence or `S`. Final score is
`round_div_even(sum(component_weight*component_ppm),S)`. For recent scenario, volume,
liquidity, and source need, the counted value is the candidate lesson's corresponding
value among the latest twelve opportunity-bearing attempts. Missing required metadata
makes that lesson ineligible.

Hard cooldown examines only positive-weight opportunity attempts and excludes same
lesson digest within five such attempts, parameter digest four, seed four, visible
queue-shape digest three, symbol two, and regime-parameter digest two. A dimension
declared `NOT_APPLICABLE` for both candidate and history neither matches nor excludes;
an inapplicable value never equals a concrete value. Missing metadata that is required
for the lesson/source class makes the lesson ineligible, while schema-declared
`NOT_APPLICABLE` remains valid. Empty eligibility returns `NO_ELIGIBLE_DRILL`; no
relaxation.

- `GUIDED`: during cold start, eligible targets are graph roots with at least one
  eligible drill; otherwise they are every prerequisite-valid skill with at least one
  eligible drill. Choose lowest mastery, then highest
  uncertainty, then greatest attempt age, then UTF-8 skill ID. A never-attempted skill
  sorts before every finite age; finite age is the nonnegative ordinal difference
  already used for recency. Rank only drills with
  that primary skill, choose the first, and show its concept/explanation before play
  with declared assistance.
- `PRACTICE`: rank the complete eligible set, choose the first, and reveal feedback
  after the attempt.
- `ASSESSMENT`: before attempt one, snapshot eligibility and apply normal cooldowns
  against pre-assessment history. First rank each primary skill's highest-ranked drill
  and choose the first four distinct-skill representatives. Refuse if fewer than four
  exist. Then repeatedly choose the highest-ranked remaining drill while forbidding
  repeated lesson, parameter, seed, visible-queue-shape, concrete symbol, or concrete
  regime-parameter digest and allowing at most two drills per primary skill, until
  exactly eight are frozen; refuse if impossible. `NOT_APPLICABLE` follows the rule
  above and does not create a false repeat.
  Frozen order is selection order. Identity/assistance stays hidden; score is
  `round_div_even(sum(eight_drill_scores),8)`, where each drill score is that attempt's
  post-cap `s_i` for the lesson's `primary_skill_id` and a missing primary row fails
  the assessment. Pass is `>=700000` with at most one total zero-capping critical
  error record across all eight attempts; two critical records in one attempt count as
  two, not one failed drill;
  reveal occurs only after closure.
- `REMEDIATION`: inspect the latest ten positive-weight opportunity attempts newest
  first and, within each, errors by the priority below. Select the first error whose
  mapped skill has at least one prerequisite-valid, cooldown-valid drill. An unmapped
  or empty mapped skill proceeds to the next error; exhaustion returns
  `NO_ELIGIBLE_DRILL`. Rank only drills with the selected primary skill.

Error priority, highest first, is `ACTED_DURING_RED`, `WRONG_HOTKEY`,
`OVERSIZED_RELATIVE_TO_LIQUIDITY`, `CHASED_AFTER_INVALIDATION`,
`CANCELLED_TOO_LATE`, `CROSSED_UNNECESSARILY`, `FAILED_TO_COMPLETE_OBJECTIVE`,
`FAILED_TO_ACT_DURING_GREEN`, `WAITED_PAST_USEFUL_LIQUIDITY`,
`CANCELLED_TOO_EARLY`, `IGNORED_SPREAD_EXPANSION`,
`CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH`, and `MISREAD_REPLENISHMENT`.
`UNSCORABLE`, `AMBIGUOUS`, and `INSUFFICIENT_OBSERVABILITY` are not remediation
errors.

| Error | Remediation skill |
|---|---|
| `ACTED_DURING_RED` | `SCRIPT_DISCIPLINE` |
| `FAILED_TO_ACT_DURING_GREEN` | `SCRIPT_DISCIPLINE` |
| `CROSSED_UNNECESSARILY` | `SPREAD_DECISION` |
| `WAITED_PAST_USEFUL_LIQUIDITY` | `AGGRESSIVE_ENTRY` |
| `CANCELLED_TOO_LATE` | `CANCEL_TIMING` |
| `CANCELLED_TOO_EARLY` | `CANCEL_TIMING` |
| `MISREAD_REPLENISHMENT` | `ABSORPTION_RECOGNITION` |
| `CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH` | `HIDDEN_LIQUIDITY` |
| `IGNORED_SPREAD_EXPANSION` | `SPREAD_DECISION` |
| `CHASED_AFTER_INVALIDATION` | `SCRIPT_DISCIPLINE` |
| `WRONG_HOTKEY` | `HOTKEY_ACCURACY` |
| `OVERSIZED_RELATIVE_TO_LIQUIDITY` | `POSITION_MANAGEMENT` |
| `FAILED_TO_COMPLETE_OBJECTIVE` | lesson `primary_skill_id` |

All ties use context `WO34/<mode>/<projection_digest>/<selection_ordinal>` and the
request root seed.

Assessment bytes, scoring, seed, and reveal freeze before attempt one. A manual plan
precedes ranking only after prerequisite, observability, capability, consent,
assignment, and assessment locks pass. Exactly zero or one applicable immutable
manual plan may exist: zero is `NOT_APPLICABLE`; more than one is a conflict and
refuses selection. If the sole applicable plan is invalid or its selected entry is
ineligible/exhausted, return `MANUAL_PLAN_REFUSED` with its digest and reason; never
fall back to adaptive ranking.

#### 5.7.6 WO35-D search and objectives

Inclusive, disjoint root ranges are train `3501000..3501011`, validation
`3502000..3502007`, one-time holdout `3503000..3503007`, one-time adversarial holdout
`3504000..3504007`, and robustness `3505000..3505003`. Scenario-family order is
`QUIET_RANGE_PRESSURE`, `TREND_PRESSURE`, `EVENT_SHOCK_PRESSURE`, then
`DISORDERLY_OPEN_STABILIZATION_PRESSURE`.

| Partition | Cell 0 | Cell 1 | Cell 2 |
|---|---|---|---|
| train | `0.50x/THIN` | `1.00x/NORMAL` | `2.00x/DEEP` |
| validation | `0.50x/DEEP` | `2.00x/THIN` | - |
| holdout | `0.25x/NORMAL` | `5.00x/NORMAL` | - |
| adversarial | `0.25x/VERY_DEEP` | `10.00x/VERY_THIN` | - |
| robustness | `1.00x/NORMAL` | - | - |

Volume labels expand to `(relative_volume_ppm,event_rate_ppm,order_size_ppm,
displayed_queue_ppm,market_frequency_ppm,cancellation_activity_ppm,
replenishment_ppm)`: `0.25x=(250000,500000,500000,700000,800000,700000,800000)`,
`0.50x=(500000,720000,700000,850000,900000,850000,900000)`,
`1.00x=(1000000,1000000,1000000,1000000,1000000,1000000,1000000)`,
`2.00x=(2000000,1400000,1400000,1200000,1100000,1150000,1100000)`,
`5.00x=(5000000,2250000,2200000,1500000,1250000,1400000,1250000)`, and
`10.00x=(10000000,3200000,3100000,1800000,1500000,1700000,1400000)`.
Liquidity labels expand to the exact vector `(initial_depth_ppm,queue_size_ppm,
replenishment_rate_ppm,replenishment_size_ppm,cancellation_rate_ppm,
placement_depth_offset_ticks)`: `VERY_THIN=(375000,250000,500000,500000,1800000,2)`,
`THIN=(625000,550000,750000,750000,1300000,1)`,
`NORMAL=(1000000,1000000,1000000,1000000,1000000,0)`,
`DEEP=(1500000,2000000,1350000,1400000,750000,-1)`, and
`VERY_DEEP=(2000000,4000000,1750000,2000000,500000,-2)`. Integer quantities use section-5.7.1 rounding and clamp
only to one where a positive base quantity is required.

Composition order is exact. For each limit-event base rate use
`mul_ppm(mul_ppm(mul_ppm(base_rate,volume.event_rate),
volume.replenishment),liquidity.replenishment_rate)`; market rate uses
`mul_ppm(mul_ppm(base_rate,volume.event_rate),volume.market_frequency)`; cancel rate
uses `mul_ppm(mul_ppm(mul_ppm(base_rate,volume.event_rate),
volume.cancellation_activity),liquidity.cancellation_rate)`. Every limit-order size
value uses `mul_ppm(mul_ppm(base_value,volume.order_size),
liquidity.replenishment_size)`; market/cancel quantity values use
`mul_ppm(base_value,volume.order_size)`. Initial displayed queue-size values use
`mul_ppm(mul_ppm(base_value,volume.displayed_queue),liquidity.queue_size)`;
initial book level count uses `mul_ppm(base_levels,liquidity.initial_depth)`. Apply
each rounding at the written inner call, then clamp a required positive rate/quantity
only as its owning simulator contract declares; zero rates remain zero. Placement
depth is `max(0,base_nonnegative_depth+placement_depth_offset_ticks)`, with no
multiplier. It is then interpreted as same-side ticks behind the non-crossing best
price; any transformed order that would rest crossed is refused rather than clamped
to another price. `relative_volume_ppm` is recorded dimension identity and does not
apply a second hidden scaling. No other vector elements compose.

For root `r` and partition base `b`, `o=r-b`; scenario is order entry `o%4` and the
cell is partition entry `o//4`. Out-of-range ordinals, missing cells, duplicate roots,
or cross-range roots fail. Candidate and base share root, family, cell, exogenous
substreams, objective, and pre-fork ancestry; only candidate-local decision/routing/
execution state may diverge. Source branches, windows, checkpoint ancestry, and
derived streams never cross partitions. Synthetic partitions record source-day and
historical-period fields `NOT_APPLICABLE`, never omitted.

`BOUNDED_SEARCH_CONTROLLED_V1` fixes the real experiment that WO35-D transcribes into
`bounded_search.toml`. To be eligible as its source, the WO31-H full-day plan's local
regime-transition matrix must encode each row as only its positive-probability
destinations, contain at least two destinations per row, sum exactly to `S`, and give
every destination weight at least `200001`; WO31-H qualification validates this
before selection and refuses an ineligible plan. Its source is the exact eligible
WO31 full-day plan selected by the root/family/cell mapping above. The base strategy
source is the following 144 UTF-8
bytes, including the final LF, with SHA-256
`1f0a39b847093703e58061e396fc80beb02509e3d3b128c3c8c5a22fd51d1df3`:

```text
setup BOUNDED_BASE_V1
window 5s
unavailable REFUSE
GREEN when
spread_ticks <= 2
book_imbalance >= 0.2
WAIT when
spread_ticks <= 4
RED otherwise
```

Its permission map is `GREEN=ALLOW`, `WAIT=DENY`, `RED=DENY`. Canonical parameter
paths and ordered domains are `/window_us:INTEGER=[2000000,5000000,10000000]`,
`/green/0/threshold_ticks:INTEGER=[1,2,3]`,
`/green/1/threshold_ppm:INTEGER=[100000,200000,300000,400000]`, and
`/wait/0/threshold_ticks:INTEGER=[2,4,6]`; base values are respectively
`5000000,2,200000,4`. Rendering threshold ppm uses the shortest exact base-ten
decimal, so `200000` renders `0.2`. A vector is invalid when wait spread is below
green spread. No other parameters, strategy source, objective, or source profile may
enter this controlled run. Controlled execution has exact baseline
`decision_latency_us=1` and `routing_latency_us=0`; they are runtime parameters, not
search dimensions.

Decision times are `continuous_start_us+30_000_000+n*1_000_000` for consecutive
integer `n` beginning at zero, ascending, while
the time is at most `continuous_end_us-10_000_000`. The strategy sees only the
observable state and trailing window ending at that time. `REFERENCE_EXECUTION_ORACLE_V1`
creates truth in an audit-only fork after source events through the decision time:
at `t+1 us` it submits a zero-latency 100-share GTC buy at the observed best bid,
behind the genuine queue; it replays the identical exogenous tape through
`t+2_000_001 us` exclusive, then cancels the remainder. Let `f` be filled shares.
Eligible future midpoint samples are the two-sided `mid_x2` immediately after every
canonical source event in `[t+1,t+2_000_001)` plus the state immediately before the
horizon boundary when two-sided. With decision midpoint `m0`,
`a=max(0,m0-min(eligible_future_mid_x2))`. A missing decision quote or empty eligible
future midpoint set is `RED` with opportunity false. Otherwise `GREEN` means decision
spread `<=2`, `f>=80`, and `a<=2`; `WAIT` means decision spread `<=4`, `f>=20`, and
`a<=4`; all other cases are `RED`. Executable opportunity is exactly state `GREEN`.
The oracle fork and future fields are inaccessible to candidate execution.

Before candidate execution, each partition binds only immutable
`ReferenceDecisionLabelV1` bytes: label ID, root, decision time, reference state,
opportunity Boolean, source event IDs, oracle ID/version/digest, and label digest.
After execution, `CandidateDecisionProjectionV1` records candidate state, permission,
typed discipline eligibility/violation, and the referenced immutable label ID. It
cannot contain or replace reference fields.

Discipline evidence has one exact rule. A decision is eligible iff its immutable
reference state is `WAIT` or `RED`; `GREEN` is ineligible. An eligible decision is a
violation iff the candidate permission is `ALLOW`. Its typed reason is
`CHASED_AFTER_INVALIDATION` for reference `WAIT` and `ACTED_DURING_RED` for reference
`RED`; an eligible nonviolation has reason `NONE`. The projection is constructed and
frozen immediately at the decision time, before any resulting command is admitted.
A missing reference label makes the decision and experiment `INSUFFICIENT_EVIDENCE`
rather than ineligible. Zero eligible decisions also makes discipline compatibility
`INSUFFICIENT_EVIDENCE`; it is never assigned a perfect score.

Execution has one `ROUND_TRIP_BUY_THEN_SELL` objective of 100 shares per root. At the
first candidate `GREEN/ALLOW`, after source events at decision time `t`, the entry
arrives at `entry_arrival=t+decision_latency_us+routing_latency_us`. Submit one
100-share GTC buy at the decision's observed best bid on that arrival; replay through
`entry_arrival+2_000_000 us` exclusive, apply cancellation of any remainder exactly
at that horizon, then, iff filled entry quantity is positive, submit one market sell
for exactly that quantity at `entry_arrival+2_000_001 us`. A zero fill emits no exit
command and records completed/traded quantities zero; it never constructs a
nonpositive order. With baseline latencies these are the originally fixed
`t+1`, `t+2_000_001`, and `t+2_000_002` timestamps. No second entry is allowed.
`completed_shares` is the quantity
filled on both entry and exit, capped at 100; `traded_shares` is every player entry and
exit fill, making the turnover allowance `2*objective_shares` an exact complete
round trip. Entry and exit cost, spread paid, and adverse selection use the actual
fills. A missing required denominator is `INSUFFICIENT_EVIDENCE`.

Pool classification/opportunity counts across a partition. For class `c`,
`recall_c=unsigned_share_ppm(correct_c,reference_c)`. Balanced classification is the
rounded mean of GREEN, WAIT, and RED recall. Discipline compatibility is
`S-unsigned_share_ppm(violations,eligible_decisions)`. Opportunity precision is
`unsigned_share_ppm(true_opportunities_predicted_GREEN_ALLOW,all_GREEN_ALLOW)`;
opportunity recall uses all true opportunities as denominator. If both are zero,
opportunity quality is zero; otherwise it is
`round_div_even(2*precision*recall,precision+recall)`. False-green rate is
`unsigned_share_ppm(non_GREEN_predicted_GREEN,all_non_GREEN)`. Missed-opportunity rate
is `unsigned_share_ppm(true_opportunities_not_GREEN_ALLOW,all_true_opportunities)`.

Each root has `objective_shares>0`. `completed_shares` is objective-completed quantity
under the bound objective semantics, capped at that target. `traded_shares` counts all
player fills, entry and exit. Recorded signed costs convert to milliticks/share;
positive is cost and negative improvement.

Every player fill of quantity `q` freezes `arrival_mid_x2`, the last two-sided
midpoint immediately before that order's submission, and the canonical venue's
maker/taker fee in integer micros/share. A fill is maker iff that candidate order was
already active before the incoming contra command began matching; otherwise it is
taker. A buy fill's signed spread-plus-fee
contribution is
`q*(fill_price_ticks*1000-arrival_mid_x2*500+fee_milliticks)`; a sell fill's is
`q*(arrival_mid_x2*500-fill_price_ticks*1000+fee_milliticks)`, where
`fee_milliticks=round_div_even(fee_micros_per_share*1000,tick_value_micros)`.
Positive fees are costs and maker rebates are negative. Sum the integer contributions
over every player entry and exit fill, divide once by total player-filled shares with
`round_div_even`, and call the result `spread_paid_milliticks_per_share`.

For each fill the adverse horizon is exactly `fill_time_us+2_000_000`. Freeze
`horizon_mid_x2` as the first two-sided midpoint immediately after the first canonical
source event whose time is at least that horizon; if the horizon exceeds the root,
use the last two-sided midpoint at or before root end. A buy fill contributes
`q*(fill_price_ticks*1000-horizon_mid_x2*500)` and a sell fill contributes
`q*(horizon_mid_x2*500-fill_price_ticks*1000)`. Sum all entry/exit contributions and
divide once by their total quantity with `round_div_even`; fees are excluded from
this `adverse_milliticks_per_share`. A missing arrival/horizon midpoint or zero fill
denominator is `INSUFFICIENT_EVIDENCE`, never zero. The controlled unperturbed
single-venue schedule has zero maker/taker fees; each `FEES` robustness setting adds
its declared milliticks/share before the spread formula, so that family changes a
weighted objective.

| Utility | Exact formula | Weight |
|---|---|---:|
| balanced classification | rounded three-class recall mean above | `180000` |
| discipline compatibility | formula above | `120000` |
| execution opportunity | harmonic precision/recall above | `130000` |
| false-green | `S-false_green_rate` | `90000` |
| missed opportunity | `S-missed_opportunity_rate` | `90000` |
| adverse selection | `S-clamp(round_div_even(max(0,adverse_milliticks_per_share)*S,5000),0,S)` | `80000` |
| turnover | `S-clamp(round_div_even(max(0,traded_shares-2*objective_shares)*S,18*objective_shares),0,S)` | `40000` |
| spread paid | `S-clamp(round_div_even(max(0,spread_paid_milliticks_per_share)*S,5000),0,S)` | `40000` |
| completion | `clamp(round_div_even(completed_shares*S,objective_shares),0,S)` | `100000` |
| cross-cell stability | formula below | `80000` |
| complexity | formula below | `50000` |

P&L is recorded with zero weight. Complexity points are
`4*conditions+3*features+8*states+6*transitions+2*rolling_windows+parameters`;
complexity utility is
`S-clamp(round_div_even(complexity_points*S,200),0,S)`, so complexity cannot improve
utility. To compute stability, form each root's weighted composite without stability
and renormalize by applicable remaining weight, take the median per volume/liquidity
cell in scenario-family order, then use
`S-(maximum_cell_median-minimum_cell_median)`. It requires two cells or is
`NOT_APPLICABLE`.

A root composite is
`round_div_even(sum(weight_i*utility_i for applicable i),sum(applicable_weight_i))`.
Pooled classification/opportunity utilities are shared across that partition's root
composites; execution utilities are per root; stability is shared after its defined
cross-cell reduction. No missing component becomes zero. For candidate `c`, base `b`,
partition `P`, root `r`, `delta[P,r,c]=composite[P,r,c]-composite[P,r,b]`.
`median_delta` and MAD reduce roots ascending. A pooled-component delta is candidate
minus base; a per-root component delta is its median paired difference.

Let `N` be unique valid non-base candidates actually trained; rejected, invalid,
no-op, and semantic duplicates do not count. `ceil_log2(1+N)` is least `k` with
`2**k>=1+N`. Multiplicity penalty is `5000*ceil_log2(1+N)` and partition statistic is
`median_delta-MAD-penalty`.

Validation qualification requires statistic `>=30000`, at least six of eight root
deltas strictly positive, classification or execution-opportunity delta `>=50000`,
every other required component delta `>=-50000`, false-green/missed/completion deltas
each `>=-20000`, at least thirty candidate trades summed across roots, nonzero base
trades, and unbounded candidate/base trade ratio in `[600000,1600000]`.

A manifest selects exactly one policy. `bounded_search.toml` and `no_winner.toml`
select `GRID`, manifest budget 64, finalist limit 8, and
`STRATEGY_DISCOVERY_V1`; WO35-D exercises all five policies only against its
development synthetic oracle, while WO35-F1 runs only the committed bounded grid.

Finite domains order parameter paths by NFC UTF-8; Booleans `false,true`, integers
numeric, enums NFC UTF-8. Cartesian vectors use first parameter slowest and last
fastest. Apply, validate, and canonicalize each vector; reject invalid/no-op vectors
and retain the earliest vector for semantic duplicates. Remaining ordered non-base
universe is `U`. CLI budget is `1..64`; effective budget is the minimum of CLI,
manifest, and 64. WO35-F1 requires 64. Budget counts first-time train evaluations of
unique valid non-base semantics. Training merit is train median delta minus train MAD;
the common multiplicity penalty applies after policy stop.

Canonical vector bytes are UTF-8 JSON with no BOM/final LF and separators `(",",":")`:
an array in canonical parameter-path order of `[path,type_tag,value]`, where strings
are NFC, type tags are `BOOLEAN|INTEGER|ENUM`, Booleans are JSON Booleans, integers
are base-ten JSON integers, and enums are strings. Whenever vector bytes enter a hash,
append `u32be(byte_length)||vector_bytes`. Common tie contexts are exactly
`WO35/COORDINATE/pass=<1..4>/parameter=<path>`, `WO35/BEAM/depth=<1..4>`,
`WO35/TRAINING_FINALISTS` and `WO35/VALIDATION_RANKING`;
they use policy `STRATEGY_DISCOVERY_V1`, root `3599001`, and the candidate semantic
digest. Training rank inside evolutionary selection uses context
`WO35/EVOLUTION/TRAINING/generation=<0..7>`. No caller invents another context for
these rankings.

Before policy stop, `PRE_STOP_TRAINING_RANK_V1` is the one exact ordering used for a
coordinate neighbor choice, beam retention, evolutionary elites, tournament parent
comparison/fallback, and every reference below to training rank or merit/tie-break:
higher `training_merit`, then higher train `median_delta`, then lower
`complexity_points`, then the section-5.7.1 ascending common tie digest for the named
context, then NFC UTF-8 stable candidate-ID bytes. The multiplicity penalty is
excluded because final `N` is not known before policy stop. No method may substitute
completion order, raw utility, or a provisional penalty.

- `GRID`: evaluate `U` in canonical order to budget/exhaustion.
- `RANDOM`: rank `U` by SHA-256 of
  `b"WO35_RANDOM_V1\0"||u64be(3500001)||b"\0"||semantic_digest_bytes`, then semantic
  digest and ID for collision, and evaluate without replacement.
- `COORDINATE`: start at base. Each of at most four passes visits canonical parameter
  order, evaluates immediate lower then upper unseen neighbors, and after both moves
  to the higher-ranked neighbor only when merit is at least `10000` over current;
  global tie-break resolves equality. Stop on budget/exhaustion or two consecutive
  complete passes without a move.
- `BEAM`: depth zero is base. At depths one through four, expand beam members in rank
  order by every one-coordinate immediate lower then upper neighbor in parameter
  order; reject duplicates/invalids, evaluate unseen children to budget, and retain
  best four by `PRE_STOP_TRAINING_RANK_V1`. Stop on empty expansion or two consecutive depths
  whose best child fails to improve the all-time best merit from every earlier depth
  by at least `10000`; update that all-time best after each completed depth.
- `EVOLUTIONARY`: generation zero is the first
  `min(8,effective_budget,|U|)` members of `U` ranked by SHA-256 of
  `b"WO35_EVOLUTION_INITIAL_V1\0"||u64be(3500004)||b"\0"||semantic_digest_bytes||
  u32be(len(vector_bytes))||vector_bytes`, then semantic digest and NFC UTF-8 ID.
  Here `population_size` is the immediately prior generation's population count.
  Each later generation retains its `min(2,population_size)` best training-ranked
  elites without reevaluation and creates at most six children. An empty population stops; a
  one-member population uses that member as the sole tournament parent and has no
  runner-up. For generation `g`, child `j`, rank population by
  `SHA256(b"WO35_TOURNAMENT_V1\0"||u64be(3500004)||u32be(g)||u32be(j)||semantic_digest_bytes||
  u32be(len(vector_bytes))||vector_bytes)`, then semantic digest and NFC UTF-8 ID;
  the better by training rank of the first two is parent. Rank its legal one-
  coordinate neighbors by that same hash construction using the neighbor's canonical
  vector bytes, then semantic digest and NFC UTF-8 ID; take
  first unseen valid child, then try tournament runner-up and remaining population in
  training-rank order if exhausted. With no runner-up, proceed directly to remaining
  population; with none, that child slot is empty. There is no crossover. The next
  population is exactly the retained elites in training-rank order followed by each
  accepted evaluated child in ascending child-slot order. Generation zero initializes
  the all-time best. For each completed later generation, compare its population's
  best merit with the all-time best before that generation; improvement requires at
  least `10000`, after which update the all-time best. Two consecutive completed
  non-improving later generations stop. Run at most eight generations including
  generation zero; budget/exhaustion stops after preserving every completed
  evaluation and never counts an incomplete generation for stagnation.

Grid/random are finite scans and ignore stagnation. Worker completion never affects
generation/evaluation/rank. After stop, compute
`training_statistic=training_merit-penalty`; rank higher statistic, higher median,
lower complexity, then global tie. Freeze first at most eight and their order before
validation. Evaluate all frozen finalists on all validation roots, disqualify failures,
then rank higher validation statistic, higher validation median, higher training
statistic, lower complexity, global tie. Freeze the first as validation candidate; if
none, close `NO_CANDIDATE_MET_CRITERIA` without relaxing thresholds. Before any
validation execution, also freeze `training_star` as the first candidate in the exact
`WO35/TRAINING_FINALISTS` ranking, including its canonical vector bytes and semantic
digest. The controlled acceptance fixture requires that preregistered candidate to be
distinct from the later validation selection and to fail validation; otherwise
WO35-F1 hard-pauses. It is never evaluated on robustness, holdout, or adversarial
partitions.

#### 5.7.7 WO35-E robustness and overfit

Robustness evaluates only the frozen validation candidate against the same base.
Each setting is one-factor-at-a-time from the committed unperturbed configuration;
candidate and base receive identical perturbation/root, and probes never combine.

1. `THRESHOLD`: simultaneously multiply exactly the three non-duration condition
   paths `/green/0/threshold_ticks`, `/green/1/threshold_ppm`, and
   `/wait/0/threshold_ticks` by each of `900000`, `950000`, `1050000`, `1100000`.
   Robustness accepts exact integer values in the separately preregistered bounds
   `/green/0/threshold_ticks=[1,5]`, `/green/1/threshold_ppm=[0,500000]`, and
   `/wait/0/threshold_ticks=[1,10]`; these bounds are not search-grid membership.
   Apply section-5.7.1 rounding at each multiplication. Outside those bounds fails and
   never clamps.
2. `ROLLING_WINDOW`: multiply only `/window_us` by `800000` and `1200000`, rounding
   section 5.7.1, requiring the separate robustness interval
   `[1000000,20000000]`, and never clamping. It is excluded from `THRESHOLD`.
3. `LATENCY`: separately add `250 us`, `1000 us` to every nonzero decision/routing
   latency. Thus the controlled source changes `decision_latency_us` from `1` to
   `251` or `1001`, leaves zero routing latency unchanged, and derives entry,
   cancellation, and exit times from the exact execution formula in section 5.7.6.
4. `FEES`: separately add `250`, `1000` milliticks/share to every maker/taker cost; a
   rebate becomes less favorable by that amount.
5. `VOLUME`: separately multiply each of the seven expanded volume-vector fields
   `(relative_volume_ppm,event_rate_ppm,order_size_ppm,displayed_queue_ppm,
   market_frequency_ppm,cancellation_activity_ppm,replenishment_ppm)` by `750000`
   and `1250000` using `mul_ppm`, then run the exact composition order in section
   5.7.6. No liquidity-vector field changes and `relative_volume_ppm` remains recorded
   identity rather than an additional hidden scale.
6. `LIQUIDITY`: separately multiply exactly the liquidity-vector fields
   `initial_depth_ppm`, `queue_size_ppm`, `replenishment_size_ppm`, and
   `replenishment_rate_ppm` by `750000` and `1250000` using `mul_ppm` before the
   section-5.7.6 composition. `cancellation_rate_ppm` and
   `placement_depth_offset_ticks` remain unchanged. Apply the written composition
   order and its rounding next; required positive integer quantities remain at least
   one only at the owning simulator boundary.
7. `REGIME_MIX`: the vectors are every normalized probability row in the committed
   full-day plan's local regime-transition matrix, in NFC UTF-8 source/destination
   order; macro pressure schedule weights are not regime vectors. In every row choose
   the first NFC UTF-8 maximum-weight destination and first distinct minimum. One
   global setting independently moves `200000` maximum-to-minimum in every row; the
   other independently moves it minimum-to-maximum in every row. If any required
   donor is at most `200000`, that whole setting is `INSUFFICIENT_EVIDENCE`; no row is
   skipped or renormalized by an alternate rule.
8. `VENUE_MIX`: in ascending ID remove each nonprimary venue once. Add one setting by
   sorting venues on `(all_in_fee_milliticks_per_share,median_route_latency_us,venue_id)`
   and assigning the complete fee-plus-latency tuples in reverse to those IDs;
   capabilities/books/IDs do not move. The V1 controlled source is intentionally
   single venue, so this family must be capability-declared `NOT_APPLICABLE`; the
   other seven families are all mandatory. This result supports no venue-robustness
   claim and cannot be replaced by a synthetic multivenue probe after results.

Run every applicable setting on all four robustness roots in ascending root then
setting order. Each perturbation family is its own evaluation group. Pool
classification and opportunity counts once across every setting/root in that family
for candidate and base, and use those family-pooled utilities in every cell composite.
Their component delta is the one direct candidate-minus-base pooled delta, not a
median of copied values. A cell is one family/setting/root paired candidate/base
result; its execution components are per-cell, stability is omitted, and remaining
weights are renormalized. Family composite and genuinely per-cell component values
are medians over cells. Robustness requires every cell observable, complete,
replay-valid, invariant-clean; exactly seven applicable mandatory families plus the
declared venue `NOT_APPLICABLE`; every family median `>=-20000`; at least
`round_div_ceiling(3*applicable_family_count,4)` nonnegative; minimum cell `>=-75000`;
every required component median `>=-50000`; false-green/missed/completion medians each
`>=-20000`; and for every family nonzero base trades plus aggregate unbounded trade
ratio in `[600000,1600000]`.

After validation selection freezes, run robustness exactly once. Failure closes
`INSUFFICIENT_EVIDENCE` without sealed reveal. Passing consumes one atomic token to
expose both holdout and adversarial manifests/digests; record access first, then run
all holdout roots ascending followed by all adversarial roots ascending. The token is
consumed even if execution later fails. No result permits mutation, reselection,
threshold change, added candidate, or rerun.

Holdout requires median delta `>=20000`, five of eight strictly positive,
`median_delta-MAD>=-10000`, every required component `>=-50000`, false-green/missed/
completion each `>=-20000`, no component more than `50000` below validation (only
`20000` for those three), at least thirty candidate trades, nonzero base trades, and
trade ratio `[600000,1600000]`. Adversarial qualification applies the complete
validation rule unchanged to its eight fixed extreme cells. No post-result
perturbation exists. `CONFIRMED_WITHIN_DECLARED_SCOPE` requires validation,
robustness, holdout, and adversarial qualification. Scientific misses produce
`INSUFFICIENT_EVIDENCE`; invariant/replay/access-order/mutation/reveal-token violations
produce `EXPERIMENT_INVALID`.

Overfit labels accumulate independently under this exact applicability matrix; a
dash means the label is not evaluated and no absent/false row is fabricated:

| Label | Pre-reveal input/output | Post-reveal additions |
|---|---|---|
| `TRAIN_VALIDATION_DIVERGENCE` | train versus validation, unqualified | none |
| `ONE_SEED_DEPENDENCE` | validation, unqualified | `_HOLDOUT` on holdout; `_ADVERSARIAL` on adversarial |
| `ONE_SCENARIO_DEPENDENCE` | validation, unqualified | `_HOLDOUT` on holdout; `_ADVERSARIAL` on adversarial |
| `THRESHOLD_SENSITIVITY` | robustness threshold-family settings, unqualified | none |
| `COMPLEXITY_WITHOUT_HOLDOUT_GAIN` | sealed/not evaluated | unqualified, candidate/base complexity plus holdout median |
| `TRADE_SUPPRESSION` | validation, unqualified | `_HOLDOUT` on holdout; `_ADVERSARIAL` on adversarial |
| `EXCESSIVE_TRADE_FREQUENCY` | validation, unqualified | `_HOLDOUT` on holdout; `_ADVERSARIAL` on adversarial |

Existing pre-reveal labels are never replaced or recalculated after reveal. The exact
predicates are:

- `TRAIN_VALIDATION_DIVERGENCE`: train median `>=80000`, validation median `<=0`;
- `ONE_SEED_DEPENDENCE`: removing one root contributes
  `unsigned_share_ppm(max(0,removed_delta),sum(max(0,delta)))>=500000` of positive
  delta or flips remaining median from positive to nonpositive;
- `ONE_SCENARIO_DEPENDENCE`: same after removing every root of one family;
- `THRESHOLD_SENSITIVITY`: threshold-setting medians contain both signs or range
  `>100000`;
- `COMPLEXITY_WITHOUT_HOLDOUT_GAIN`: complexity rises at least twenty points and
  holdout median `<30000`;
- `TRADE_SUPPRESSION`: fewer than thirty candidate trades or ratio `<600000`;
- `EXCESSIVE_TRADE_FREQUENCY`: ratio `>1600000` and false-green delta `<-20000`.

For `ONE_SEED_DEPENDENCE` and `ONE_SCENARIO_DEPENDENCE`, a zero total positive-delta
denominator makes the contribution clause false; the median-flip clause is still
evaluated. It never becomes global missing evidence. Trade count, candidate/base
ratio, and false-green delta in the two trade labels are validation aggregates for the
unqualified label and the suffix-named partition for a suffixed label.

The development-only synthetic fixture, disjoint from `bounded_search.toml`, has four
seed/family cells: train `[100000,100000,100000,100000]`, validation
`[600000,-20000,-20000,-20000]`; the validation `600000` cell is sole for its seed
and family.
It must receive train/validation, one-seed, and one-scenario labels. Controlled real
evidence uses unchanged rules without injected deltas.

#### 5.7.8 WO40-D release qualification and performance

The V1 `release_version` is the literal SemVer string `0.1.0`. WO40-E requires exact
equality with both candidate `pyproject.toml [project].version` and
`kirby2.__version__`; another version requires the section-5.5 release-restart
amendment. In every root formula, `{target}` is exactly the matching
`ReleaseArtifactIndexV1` target string `macos-arm64` or `linux-x86_64`.

`CANONICAL_RELEASE_ARCHIVE_V1` gives the source archive and install bundles distinct
closed inputs. Every source-archive member is derived from a regular blob in the exact
candidate Git tree; `git archive` may supply those source bytes, but `packaging.py`
validates and re-emits the final archive. Desktop/headless bundles admit only these
source classes declared per member in `release/artifact_layout.toml`: candidate-derived
project wheel, source bytes, launchers, documentation, and assets; dependency wheels
from the WO40-D1 preverified wheelhouse at their locked digests; generated
`ReleaseManifestV1`, license, notice, and layout bytes emitted by the WO40-D encoder
from the committed candidate/protocol/dependency lock; and the two starter packs from
the candidate tree. Every generated member records its encoder ID and complete input
digests. Ambient environment bytes, undeclared files, network results, timestamps,
and host metadata are forbidden. Source root is `kirby2-{release_version}/`; desktop root is
`kirby2-{release_version}-{target}/`; wheelhouse root is
`kirby2-{release_version}-{target}-wheelhouse/`. Each relative path is Unicode NFC, UTF-8, and
`/`-separated. Reject rather than rewrite any non-NFC name, empty segment, `.`, `..`,
absolute path, leading/trailing slash, backslash, drive prefix, NUL/control character,
duplicate normalized path, case-fold collision, or path not representable by POSIX
ustar: final component at most 100 encoded bytes, prefix at most 155, total at most
255. Only regular files are allowed; symlinks, hardlinks, devices, FIFOs, sockets,
sparse files, and members outside the one root fail. Directory members are omitted;
parents are implicit and mutable data directories are created only at first run.

The literal `source_class` enum and mapping are exhaustive:
`CANDIDATE_PROJECT_WHEEL`, `CANDIDATE_SOURCE`, `CANDIDATE_LAUNCHER`,
`CANDIDATE_DOCUMENTATION`, `CANDIDATE_ASSET`, `LOCKED_DEPENDENCY_WHEEL`,
`GENERATED_MANIFEST`, `GENERATED_LICENSE`, `GENERATED_NOTICE`, `GENERATED_LAYOUT`, and
`CANDIDATE_STARTER_PACK`, corresponding in that order to the member-source categories
above. A member has exactly one class; an unknown or context-inconsistent class fails.

The source archive inventory is exactly every recursive entry in the candidate
commit's Git tree with mode `100644` or `100755`, ordered by raw Git path bytes after
the required NFC/path validation and prefixed by the source root. A mode `120000`,
`160000`, tree anomaly, duplicate normalized path, or any other nonregular entry
fails the entire source build. `.gitattributes` export-ignore rules, working-tree
files, untracked files, generated build outputs, and path exclusions are not applied;
there is no implementer-selected subset.

The tar stream is POSIX ustar with exactly one regular-file member per
`ArchiveMemberPlanV1` entry
in ascending normalized UTF-8 path-byte order. Every file payload is zero-padded only
to its next 512-byte boundary. After the final member, emit exactly two 512-byte zero
blocks and end the archive immediately; there is no additional record padding.
Each member has uid/gid zero, empty uname/gname, integer `SOURCE_DATE_EPOCH` mtime, no
pax/GNU headers, and mode `0755` only for the three exact
`release/launchers/{macos,linux,headless}/kirby2` paths after stripping exactly the
one archive-root prefix; every other archive-root-relative path is `0644`. Size,
checksum, and content must match the manifest; extra/missing members fail.

Each 512-byte ustar header is byte-normative. Start with 512 NUL bytes. Encode the
complete rooted member path as UTF-8. If it is at most 100 bytes, place it in name
offset `0`, width `100`, and leave prefix empty; otherwise choose the rightmost `/`
whose preceding prefix is at most 155 bytes and following name at most 100 bytes,
remove that slash, and place the two byte strings at name `0:100` and prefix
`345:500`. Unused bytes in either field remain NUL. Write mode at `100:108`, uid at
`108:116`, and gid at `116:124` as exactly seven zero-padded ASCII octal digits plus
NUL; write size at `124:136` and mtime at `136:148` as exactly eleven zero-padded
ASCII octal digits plus NUL. Values that do not fit fail; base-256 is forbidden.
Set typeflag `156` to ASCII `0`; leave linkname `157:257` NUL;
write magic `b"ustar\0"` at `257:263` and version `b"00"` at `263:265`; leave uname
`265:297`, gname `297:329`, devmajor `329:337`, devminor `337:345`, and final padding
`500:512` NUL. Only after every other field is populated, set checksum `148:156` to
eight ASCII spaces, sum every byte of that completed 512-byte header as an unsigned
integer, and replace the field with exactly six zero-padded ASCII octal digits, NUL,
and one space. No alternate regular-file flag, numeric encoding, path split, checksum
terminator, or unused-field value is accepted.

The gzip wrapper is one member with fixed header CM=8, FLG=0, MTIME=0, XFL=2, OS=255,
no filename/comment/extra fields. Its payload is exactly one
`zlib.compressobj(level=9,method=zlib.DEFLATED,wbits=-15,memLevel=8,
strategy=zlib.Z_DEFAULT_STRATEGY)`, exactly one `compress(complete_tar_bytes)` call
followed by exactly one `flush(zlib.Z_FINISH)`; no chunked feed, intermediate/sync
flush, dictionary, or alternate parameter is allowed. Append the little-endian CRC32
and input-size-modulo-`2**32` trailer. The locked CPython/zlib fingerprint mismatch refuses
the reproducibility claim; repeat builds on the declared fingerprint are byte-identical.

`SOURCE_DATE_EPOCH` and `ReleaseManifestV1.build_timestamp` are the candidate commit's
integer committer timestamp, the latter rendered UTC `YYYY-MM-DDTHH:MM:SSZ`; neither
uses wall-clock build time. Actual build start/end times exist only in governed
evidence sidecars and never enter archive bytes or logical identity.

The embedded `ReleaseManifestV1` is compact sorted-key JSON with no final LF and
exactly these top-level keys: `schema_version=1`, `release_version`,
`candidate_commit`, `build_timestamp`, `target`, `runtime`, `dependencies`,
`schema_versions`, `starter_set`, `assets`, `supported_targets`,
`known_limitations`, `licenses`, `layout`, `logical_build_id`, `payload_members`,
`subordinate_artifacts`, and `payload_projection_sha256`. `target` is exactly
`{system,machine,artifact_form}`. `runtime` is exactly
`{python_implementation,python_version,cache_tag,compiler,zlib_version}`.
`dependencies` is the normalized-name-sorted array of exactly
`{name,version,wheel_filename,wheel_sha256,license_id}`; `schema_versions` is the
schema-ID-sorted array of exactly `{schema_id,version}`. `starter_set` is exactly
`{set_id,entries_sha256,entries}`, reusing the complete ordered entry objects below.
`assets` is the path-sorted array of exactly `{path,size,sha256}`.
`supported_targets` is exactly
`[{"system":"Darwin","machine":"arm64"},{"system":"Linux","machine":"x86_64"}]`.
`known_limitations` is the code-sorted array of nonempty `{code,detail}` objects.
`licenses` is exactly `{inventory_sha256,notices_sha256}`. `layout` is exactly
`{artifact_layout_sha256,archive_root}`. `subordinate_artifacts` is the
artifact-ID-sorted array of exactly `{artifact_id,size,sha256}` for included wheels,
packs, assets, and wheelhouse payloads.

`payload_members` lists every included payload other than the manifest itself as an
ascending archive-root-relative path array of exactly
`{path,size,sha256,source_class}`; `source_class` is one exact member-source enum from
the closed allowlist above. `payload_projection_sha256` hashes that complete array.
The manifest deliberately contains neither the digest of its own bytes nor the
transport SHA-256 of the archive that contains it; both would be self-referential.

For each wheelhouse or desktop archive, `ArchiveMemberPlanV1` is the ascending complete
rooted-path array of every `payload_members` row after prefixing `layout.archive_root`,
plus exactly one reserved row for `<archive_root>/RELEASE_MANIFEST.json` with its
actual size, SHA-256, and `source_class="GENERATED_MANIFEST"`. The source archive has
no embedded release manifest and its plan is exactly its candidate-source member
inventory. The encoder hashes the complete plan into governed build evidence, emits
exactly its rows, and verifies every member against it. The manifest member's digest
and the containing transport digest exist only in external `ReleaseArtifactIndexV1`,
stored in the governed artifact store and WO40-F evidence and never included in an
indexed archive.

`ReleaseArtifactIndexV1` is compact sorted-key canonical JSON with no final LF and
exactly `schema_version=1`, `candidate_commit`, `logical_build_id`, and `artifacts`.
`artifacts` is ordered by NFC UTF-8 `artifact_id` and each row has exactly
`artifact_id`, `artifact_form`, `target`, `size`, `transport_sha256`, and nullable
`embedded_manifest_sha256`.

`logical_build_id="kirby2-release-"+SHA256(ReleaseLogicalBuildProjectionV1_bytes)`
with the full lowercase digest. That compact canonical projection has exactly
`schema_version=1`, `release_version`, `candidate_commit`, `source_manifest_sha256`,
`protocol_files`, and `starter_set_entries_sha256`. Commit-derived
`SOURCE_DATE_EPOCH` remains transport normalization/provenance and is deliberately
excluded from this logical projection.
`protocol_files` is the ascending-path array of exactly `{path,sha256}` for
`release/artifact_layout.toml`, `release/performance_thresholds.toml`,
`release/platforms.toml`, `release/qualification.toml`, and
`release/requirements.lock`. Every digest is recomputed from the fully staged WO40-E
candidate and `source_manifest_sha256` is the exact `RUNNER_SOURCE_TREE_V1` value.

The artifact inventory has exactly these six rows in this literal sorted order:

| `artifact_id` | `artifact_form` | `target` | embedded manifest |
|---|---|---|---|
| `linux-x86_64-desktop-bundle` | `DESKTOP_TAR_GZ` | `linux-x86_64` | required |
| `linux-x86_64-wheelhouse` | `HEADLESS_WHEELHOUSE_TAR_GZ` | `linux-x86_64` | required |
| `macos-arm64-desktop-bundle` | `DESKTOP_TAR_GZ` | `macos-arm64` | required |
| `macos-arm64-wheelhouse` | `HEADLESS_WHEELHOUSE_TAR_GZ` | `macos-arm64` | required |
| `project-wheel` | `PY3_NONE_ANY_WHEEL` | `any` | null |
| `source-archive` | `SOURCE_TAR_GZ` | `source` | null |

Required manifest cells contain the digest of the verified manifest member; null is
permitted only in the two stated rows. Logical selector `macos-arm64/headless` is the
ordered set `project-wheel,source-archive,macos-arm64-wheelhouse`, and the Linux
selector substitutes its wheelhouse; each `/desktop` selector is its one matching
desktop-bundle row. No seventh transport or omitted row is permitted. The index
excludes its own digest. Its
separately recorded evidence-object SHA-256 binds the whole release set without any
self-reference.

Functional qualification runs once per clean root with no retry. Only a recorded
environmental interruption before user-data mutation permits an exact rerun; that is a
new attempt and cannot erase the first.

`RELEASE_STARTER_SET_V1` is serialized in committed `release/artifact_layout.toml`
and has exactly `schema_version=1`, `set_id="RELEASE_STARTER_SET_V1"`, ordered
`entries`, and `entries_sha256`. The entries array contains exactly two objects, in
this order: role `SCENARIO` at
`kirby2/packs/fixtures/samples/starter_scenario/manifest.toml`, then role `CURRICULUM`
at `kirby2/packs/fixtures/samples/five_lesson_curriculum/manifest.toml`. Each object
has exactly `role,manifest_path,manifest_sha256,pack_id`; WO40-D reads the verified
committed manifest, recomputes its content-derived WO39-A `pack_id`, and stores both
literal digests. `entries_sha256` hashes compact canonical JSON of the array. The
curriculum dependency must name the first entry's exact pack ID. Every desktop and
headless artifact manifest binds `set_id`, `entries_sha256`, both complete pack
inventories, and their archive digests; missing either pack or an unresolved
dependency fails offline installation and ACK setup.

`RELEASE_BENCHMARK_FIXTURES_V1` is serialized inside committed
`release/performance_thresholds.toml`. Every one of its 10,000 preregistered rows is
a `ReleasePerformanceRowTemplateV1` containing exactly `work_unit_id`, `cell`,
`root_seed`, `initial_attempt=1`, `generated_configuration`,
`generated_configuration_sha256`, `native_fixture_template`, `runner_id`,
`runner_source_policy_id="RUNNER_SOURCE_TREE_V1"`, `expected_capabilities`,
`required_checks`, `audit_argv`,
`result_schema_id="ReleasePerformanceCellResultV1"`, and `artifact_form`. A template
contains every source-independent constructor value; its only unresolved values are
the explicitly named source paths whose bytes WO40-E has not yet created.

WO40-E mechanically binds those templates through
`release/performance_runner_sources.lock`. A bound `ReleasePerformanceRowV1` contains
exactly the template fields except that `native_fixture_template` becomes
`native_fixture`, `runner_source_policy_id` becomes `runner_source_sha256`, and
`native_fixture_sha256` is added. Binding replaces each declared source-path field by
the matching source-digest field exactly as specified below and makes no other change.
Canonical JSON uses sorted keys, ASCII escapes, separators `(",",":")`, and no final
LF; every SHA is over those UTF-8 bytes. Missing/extra/defaulted fields, an undeclared
constructor default, source mismatch, or digest mismatch fails. WO40-D verifies all
committed templates and the resolver against synthetic trees; WO40-E verifies the
mechanical lock and every derived bound row against the staged candidate tree; WO40-I
consumes only that committed template/lock pair and cannot resolve a new default.
The designated performance
target is the installed `macos-arm64` headless/desktop artifact pair under CPython
3.14, with at least four logical CPUs, `8 GiB` RAM, and `20 GiB` free store. Linux
still runs the full functional matrix but cannot replace that 10,000-run target
without a section-5.5 amendment.

`RELEASE_INTERACTIVE_ACK_V1` installs both members of `RELEASE_STARTER_SET_V1`,
resolves their exact dependency, and restores the bundled starter lesson at its first
quiescent continuous-session checkpoint with a two-sided quote and paused background
flow. Through the real terminal action handler it executes 550 fixed pairs: submit
one-share GTC buy `perf-{0000..0549}` at the checkpoint best bid, wait for client
acknowledgement/render, cancel that ID, and wait for cancellation/render. The first 50
pairs, 100 acknowledgements, are warmup; the remaining 500 pairs are exactly 1,000
measured input-to-ack samples. Timing starts immediately before handler dispatch and
ends after the corresponding client-visible frame flush. Output uses a continuously
drained `120x40`, UTF-8, `TERM=dumb`, color-disabled pseudo-terminal; changing sink,
geometry, encoding, or drain policy is a different workload. Any fill, rejection,
missing/duplicate acknowledgement, or ID/order mismatch is hard FAIL.

`RELEASE_TERMINAL_UPDATE_V1` uses the client-observable event stream of the exact
`QUIET_RANGE_PRESSURE` root-`3102000` full-day artifact. In event-sequence order it
renders the first 5,100 client-visible state changes after continuous-session start
through a continuously drained `120x40`, UTF-8, `TERM=dumb`, color-disabled pseudo-
terminal. The first 100 are warmup and next 5,000 measured; fewer than 5,100, a
skipped/duplicate update, render error, or backpressure is FAIL.

Full-day generation/replay uses `QUIET_RANGE_PRESSURE` development root `3101000`,
which is outside the protected WO31 qualification/holdout ranges, in one warmup and
three measured fresh-process runs. Microscope load uses the first verified measured artifact in
`AS_OBSERVED` mode at
`continuous_start_us+floor(continuous_duration_us/2)`, loads every supported WO36
pane, and renders the complete standalone HTML report in one warmup plus twenty
measured fresh processes. Source run, cursor, and policy identities are frozen in the
source-independent workload template; renderer/source and installed-asset digests are
resolved only through the candidate lock and WO40-F artifact manifest as follows.

Checkpoints occur at t=0, every `900_000_000 us` of continuous time, and each phase
boundary; coincident cuts emit one. Ledger growth is
`round_div_even(event_ledger_bytes*1000,outer_event_count)` bytes per 1,000 events;
checkpoint growth is
`round_div_even(sum(checkpoint_bytes)*3_600_000_000,day_duration_us)` bytes per
simulation hour. Zero denominators fail.

All release wall samples use `time.perf_counter_ns` in the measured fresh process;
millisecond thresholds are compared as exact threshold times `1_000_000 ns` before
percentile selection. On the designated Darwin target `ru_maxrss` is bytes. Timing,
RSS, terminal bytes, and process scheduling are operational evidence outside semantic
run identity.

Workload provenance is fixed: input-to-ack and terminal-update run through the
installed desktop bundle's real terminal client; full-day generation/replay and all
10,000 work units run through the installed headless artifact; microscope load invokes
the desktop bundle's installed offline renderer in a fresh noninteractive process.
Peak-RSS `maximum` ranges over every measured fresh process in the ACK host,
terminal-update host, three full-day generators, three replayers, and twenty
microscope renders; the 10,000-run lane has its separate per-attempt 512-MiB rule and
does not enter this row. Largest checkpoint, full-day bytes, ledger growth, and
checkpoint growth are computed only from the three measured full-day generation
artifacts (not warmup/replay copies), with every source artifact ID recorded.

The same threshold manifest contains exactly five ordered
`ReleaseAuxiliaryPerformanceTemplateV1` objects for
`RELEASE_INTERACTIVE_ACK_V1`, `RELEASE_TERMINAL_UPDATE_V1`,
`RELEASE_FULL_DAY_GENERATION_V1`, `RELEASE_FULL_DAY_REPLAY_V1`, and
`RELEASE_MICROSCOPE_LOAD_V1`. Each has exactly `workload_id`, `sample_contract_id`,
`runner_source_policy_id="RUNNER_SOURCE_TREE_V1"`, ordered `entrypoint_paths`,
`artifact_selector`, and `input_identity`. `input_identity` always has exactly
`schema_version=1`, `kind`, and `parameters`; the exact parameter objects are below.
WO40-D stores only these ordered paths in the source-independent template. WO40-E
resolves every path to its matching digest in `RUNNER_SOURCE_TREE_V1`; result
provenance records the lock-resolved `{path,sha256}` pairs. `sample_contract_id`
equals `workload_id`.
Artifact selectors are
`macos-arm64/desktop` for ACK, terminal update, and microscope, and
`macos-arm64/headless` for generation and replay. Entrypoint paths are exact:

- ACK and terminal update: `kirby2/release/desktop.py`,
  `kirby2/release/commands.py`, `kirby2/ui/terminal.py`;
- full-day generation: `kirby2/release/headless.py`,
  `kirby2/full_day/commands.py`, `kirby2/full_day/runtime.py`,
  `kirby2/full_day/store.py`;
- full-day replay: `kirby2/release/headless.py`,
  `kirby2/full_day/commands.py`, `kirby2/full_day/restore.py`,
  `kirby2/full_day/store.py`;
- microscope load: `kirby2/release/desktop.py`,
  `kirby2/microscope/commands.py`, `kirby2/microscope/report.py`.

The five `input_identity.parameters` key sets and values are exact:

- ACK: `starter_set_id="RELEASE_STARTER_SET_V1"`, literal
  `starter_entries_sha256`, `scenario_manifest_path=
  "kirby2/packs/fixtures/samples/starter_scenario/manifest.toml"`, literal
  `scenario_manifest_sha256`, literal content-derived `scenario_pack_id`,
  `curriculum_manifest_path=
  "kirby2/packs/fixtures/samples/five_lesson_curriculum/manifest.toml"`, literal
  `curriculum_manifest_sha256`, literal content-derived `curriculum_pack_id`,
  `lesson_id="KIRBY2_STARTER_PLACE_CANCEL_V1"`,
  `checkpoint_selector_id="FIRST_QUIESCENT_CONTINUOUS_TWO_SIDED_V1"`,
  `lesson_seed_policy="FROM_LESSON_MANIFEST"`, `pairs=550`, `warmup_pairs=50`,
  `quantity_shares=1`, and `terminal` equal to exactly
  `{"columns":120,"rows":40,"encoding":"UTF-8","term":"dumb","color":false,
  "drain_policy":"CONTINUOUS"}`; `kind="INTERACTIVE_ACK"`.
- Terminal update: `qualification_evidence_path=
  "KIRBY2_FULL_DAY_QUALIFICATION_EVIDENCE.md"`, its literal
  `qualification_evidence_sha256`, `profile_id="QUIET_RANGE_PRESSURE"`,
  `root_seed=3102000`,
  `artifact_selection_policy_id="UNIQUE_VERIFIED_PROFILE_ROOT_V1"`, literal
  `source_artifact_manifest_sha256`, `start_selector="CONTINUOUS_START"`,
  `warmup_updates=100`, `measured_updates=5000`, and the same exact `terminal` object;
  `kind="TERMINAL_UPDATE"`. WO40-D obtains the artifact digest only by verifying the
  named evidence document, querying the governed store for that exact profile/root,
  and requiring one result; zero or multiple results fail.
- Full-day generation: `profile_manifest_path=
  "kirby2/full_day/profile_candidates.toml"`, its literal `profile_manifest_sha256`,
  `profile_id="QUIET_RANGE_PRESSURE"`, `root_seed=3101000`, literal
  `selected_plan_sha256` from the uniquely verified WO31-I1 evidence,
  `repetition_ordinals=[0,1,2,3]`, `warmup_ordinals=[0]`, and
  `measured_ordinals=[1,2,3]`; `kind="FULL_DAY_GENERATION"`.
- Full-day replay: `producer_workload_id="RELEASE_FULL_DAY_GENERATION_V1"`,
  `source_artifact_policy_id="MATCH_GENERATION_ORDINAL_V1"`,
  `repetition_ordinals=[0,1,2,3]`, `warmup_ordinals=[0]`, and
  `measured_ordinals=[1,2,3]`; `kind="FULL_DAY_REPLAY"`.
- Microscope: `producer_workload_id="RELEASE_FULL_DAY_GENERATION_V1"`,
  `source_ordinal=1`, `cursor_policy_id="CONTINUOUS_MIDPOINT_V1"`,
  `mode="AS_OBSERVED"`, `pane_ids` exactly
  `["LEVEL2_LADDER","TIME_AND_SALES","DEPTH_HEATMAP","INDIVIDUAL_QUEUE",
  "PLAYER_ORDERS","ORDER_LIFECYCLE","POSITION","TRAFFIC_LIGHT",
  "STRATEGY_EVIDENCE","FEATURE_PROVENANCE","AGENT_ACTIVITY",
  "LATENCY_TIMELINE","VENUE_QUOTES","CONSOLIDATED_QUOTES","FILLS",
  "EXECUTION_METRICS","MECHANISTIC_TRACE","COUNTERFACTUAL_COMPARISON"]`,
  `report_policy_id="PORTABLE_OFFLINE_REPORT_V1"`, `warmup_repetitions=1`, and
  `measured_repetitions=20`; `kind="MICROSCOPE_LOAD"`.

WO40-E resolves each path to its exact digest in
`release/performance_runner_sources.lock`; no path or digest is chosen at
measurement. The artifact digests must come from the immutable WO40-F manifest
selected by `artifact_selector`; a missing path, asset, or mismatch fails.

Each WO40-I auxiliary output is one compact canonical
`ReleaseAuxiliaryPerformanceResultV1` object with exactly `schema_version=1`,
`workload_id`, `status`, `provenance`, `warmup_series`, `measured_series`,
`reductions`, `threshold_results`, `hard_failure_codes`, and `evidence_records`.
`status` is `PASS|WARNING|FAIL`. `provenance` has exactly `candidate_commit`,
`source_manifest_sha256`, ordered `entrypoint_sources`,
`artifact_manifest_sha256`, nullable `asset_manifest_sha256`, and
`input_identity_sha256`; `entrypoint_sources` has exactly `{path,sha256}` per declared
path. Commit is forty lowercase hex and digests are sixty-four lowercase hex.
For all five workloads, `artifact_manifest_sha256` is SHA-256 of the exact complete
`ReleaseArtifactIndexV1` canonical bytes; `artifact_selector` in the verified template
chooses the exact selector set defined above. `asset_manifest_sha256` is null for ACK,
terminal update, generation, and replay. For microscope it is SHA-256 of compact
sorted-key canonical JSON (no final LF) of the selected macOS desktop
`ReleaseManifestV1.assets` array exactly as stored; a null microscope value or
nonnull value for another workload fails.
`warmup_series` and `measured_series` are objects from a declared metric ID to an
execution-order array of nonnegative integers. `reductions` is an ordered array of
objects with exactly `reduction_id,metric_id,statistic,value,unit,availability`, where
statistic is `P50|P95|P99|MAX|SUM|ROUND_DIV_EVEN`, value is a nonnegative integer or
null, availability is `AVAILABLE|UNAVAILABLE`, and unit is
`NANOSECONDS|BYTES|COUNT|BYTES_PER_1000_EVENTS|BYTES_PER_SIMULATION_HOUR`.
`threshold_results` is an ordered array with exactly
`reduction_id,metric_id,statistic,status,pass_upper_inclusive,
warning_upper_inclusive,hard_failure,reason_code`, using nullable integer bounds,
Boolean `hard_failure`, status `PASS|WARNING|FAIL|NOT_EVALUATED`, and nullable NFC
`reason_code`. It is one-to-one and in the same order as `reductions`.
`hard_failure_codes` is the
UTF-8-sorted unique array of NFC reason strings. `evidence_records` is the
evidence-ID-sorted array of exactly `{evidence_id,size,sha256}`. Raw and reduced
values must reconcile under section 5.7.1; a populated reduction has
`availability="AVAILABLE"`, nonnull value, and a threshold result. An unavailable
reduction has `availability="UNAVAILABLE"`, null value, and `NOT_EVALUATED` with
reason `INCOMPLETE_SAMPLES|UPSTREAM_HARD_FAILURE`. Omitted rows, negative/NaN/extra
values, or another pairing fail.

Metric key sets and success counts are exact. ACK warmup is
`{ack_latency_ns:100}` and measured is
`{ack_latency_ns:1000,peak_rss_bytes:1}`. Terminal warmup is
`{update_latency_ns:100}` and measured is
`{update_latency_ns:5000,peak_rss_bytes:1}`. Generation warmup is
`{wall_time_ns:1,peak_rss_bytes:1}` and measured is
`{wall_time_ns:3,peak_rss_bytes:3,largest_checkpoint_bytes:3,full_day_bytes:3,
ledger_growth_bytes_per_1000_events:3,
checkpoint_growth_bytes_per_simulation_hour:3}`. Replay warmup is
`{wall_time_ns:1,peak_rss_bytes:1}` and measured is
`{wall_time_ns:3,peak_rss_bytes:3}`. Microscope warmup is
`{wall_time_ns:1,peak_rss_bytes:1}` and measured is
`{wall_time_ns:20,peak_rss_bytes:20}`. Braces here declare key/count mappings; the
serialized values are arrays.

Ordered reduction IDs are: ACK `ACK_LATENCY_P95,ACK_LATENCY_P99,
ACK_PEAK_RSS_MAX`; terminal `TERMINAL_UPDATE_P99,TERMINAL_UPDATE_MAX,
TERMINAL_PEAK_RSS_MAX`; generation `GENERATION_WALL_P50,
GENERATION_PEAK_RSS_MAX,LARGEST_CHECKPOINT_MAX,FULL_DAY_BYTES_MAX,
LEDGER_GROWTH_MAX,CHECKPOINT_GROWTH_MAX`; replay `REPLAY_WALL_P50,
REPLAY_PEAK_RSS_MAX`; and microscope `MICROSCOPE_WALL_P95,
MICROSCOPE_PEAK_RSS_MAX`. Each name maps literally to the evident metric/statistic.
The normal bounds are the table below; every peak-RSS MAX uses its one peak row.
`TERMINAL_UPDATE_MAX` alone has pass/warning upper bounds both `500_000_000 ns` and
`hard_failure=true`; all other rows have `hard_failure=false`. The release aggregate
recomputes peak-RSS MAX across the five measured `peak_rss_bytes` series and derives
its status plus the overall status from these rows; it never accepts only reported
reductions.

For `PASS|WARNING`, every array has its exact count, every reduction is available,
every threshold is evaluated, `reason_code` is null, and hard-failure codes are empty.
An evaluated ordinary bound miss uses status `FAIL` and
`reason_code="THRESHOLD_MISS"`. `TERMINAL_UPDATE_MAX` above its bound uses status
`FAIL`, `reason_code="TERMINAL_UPDATE_PAUSE_OVER_500_MS"`, and records that same code
in `hard_failure_codes`; here `hard_failure=true` declares threshold policy, not the
observed outcome.
For `FAIL`, retain every completed warmup/measured prefix under all exact keys; arrays
may be shorter but never longer, and evidence records retain every fully written
object. A reduction is computed only when its complete declared measured sample set
exists; otherwise use the unavailable/null row above. The initiating hard threshold
uses `FAIL` and its stable reason code when its value exists; a nonmetric hard failure
appears in `hard_failure_codes` while unavailable threshold rows use
`UPSTREAM_HARD_FAILURE`. No missing sample, abort, render failure, or invariant failure
may be represented by a fabricated zero, shortened successful count, or omitted row.

| Metric | Samples after warmup | PASS | WARNING | FAIL |
|---|---:|---:|---:|---:|
| input-to-ack | 100 warmup, 1000 measured | P95 `<=50 ms`, P99 `<=100 ms` | P95 `<=100 ms`, P99 `<=250 ms` | otherwise |
| terminal update | 100 warmup, 5000 measured | P99 `<=50 ms` | P99 `<=150 ms` | otherwise |
| full-day generation | 1 warmup, 3 measured | P50 `<=300 s` | P50 `<=900 s` | otherwise |
| full-day replay | 1 warmup, 3 measured | P50 `<=180 s` | P50 `<=600 s` | otherwise |
| microscope load | 1 warmup, 20 measured | P95 `<=5 s` | P95 `<=15 s` | otherwise |
| peak RSS | maximum | `<=4 GiB` | `<=6 GiB` | `>6 GiB` |
| largest checkpoint | all measured | `<=512 MiB` | `<=1 GiB` | `>1 GiB` |
| full-day bytes | all measured | `<=4 GiB` | `<=8 GiB` | `>8 GiB` |
| ledger growth per 1,000 events | all measured | `<=1 MiB` | `<=2 MiB` | `>2 MiB` |
| checkpoint growth per simulation hour | all measured | `<=512 MiB` | `<=1 GiB` | `>1 GiB` |

Evaluate PASS first, else WARNING, else FAIL. Aggregate is FAIL if any row fails,
otherwise WARNING if any row warns, otherwise PASS.

Any render error, deadlock, invariant/replay/digest failure, missing update, negative
growth, or one measured update pause over `500 ms` is hard `FAIL`.

The ten-cell base config is `duration_us=10_000_000`, `duration_events=25_000`,
`initial_mid_ticks=10000`, `initial_depth=8`, `flow_model=simple`, `regime=BALANCED`,
`volume=1.00x`, `liquidity=NORMAL`, `latency=NORMAL`,
`session_phase=CONTINUOUS`, `order_types=MARKET_AND_LIMIT`,
`hidden_liquidity=NONE`, `venue_count=1`, `auction_state=NONE`,
`agent_population=liquidity_provision`, `agent_count=4`, `strategy=TWAP`, and
`objective=ACQUIRE`. Each cell uses roots `4000000..4000999` and section-5.7.1 label
`release/performance/<CELL>` with
`policy_version="RELEASE_BENCHMARK_FIXTURES_V1"`; shared numeric roots never share
substreams. There are
1,000 complete registered runs per cell, exactly 10,000 total.

Exact cell runner/overrides are:

- `CORE_FLOW`: runner `CORE_FLOW`, base config;
- `MARKET_MECHANICS`: runner `MECHANICS`; `session_phase=OPENING_AUCTION`,
  `order_types=IOC_FOK_POST_ONLY`, `auction_state=OPENING`;
- `QUEUE_REACTIVE`: runner `RELEASE_QUEUE_REACTIVE_V1`;
  `flow_model=queue_reactive`;
- `LATENCY`: runner `LATENCY`; `latency=STRESSED`;
- `HIDDEN_LIQUIDITY`: runner `FRAGMENTED`; `hidden_liquidity=ICEBERG`,
  `venue_count=2`;
- `MULTIVENUE`: runner `FRAGMENTED`; `venue_count=4`;
- `AGENT_ECOLOGY`: runner `ECOLOGY`; `agent_population=liquidation_ecology`,
  `agent_count=8`;
- `ALGORITHM`: runner `ALGORITHM`; base `TWAP` plus `ACQUIRE`;
- `HALT_REOPEN`: runner `MECHANICS`; `session_phase=HALTED`,
  `auction_state=REOPENING`;
- `FAULT_REPLAY`: runner `FAULT`; select by `(root-4000000) mod 10` from
  `DUPLICATE_MESSAGE`, `DROPPED_MARKET_DATA`, `DELAYED_ACKNOWLEDGEMENT`,
  `OUT_OF_ORDER_DELIVERY`, `SNAPSHOT_GAP`, `CORRUPTED_DATASET_ROW`,
  `VENUE_REJECTION`, `HALT_DURING_PENDING_ORDER`, `CANCEL_FILL_RACE`, and
  `SCHEMA_MISMATCH`, in that order.

The conversion to the existing exact `GeneratedConfiguration` schema is fixed. Let
`c` be the zero-based cell ordinal in the order above and `k=root-4000000`.
`work_unit_id="release-perf/<CELL>/<root>"`; `sequence=1000*c+k+1`;
`cell_id="release-<lowercase-cell-with-hyphens>-<k:04d>"`;
`seed` is the section-5.7.1 labeled seed for the root and
`release/performance/<CELL>`; `duration_us=10000000`; `duration_events=25000`;
`schema_version=2`; and `lane` is the exact cell mapping in the following paragraph.
For nonfault rows, `replicate_index=k mod 6`, partition is
`TRAIN` for indices zero through two and `HOLDOUT` for three through five, and
`injected_fault=null`. For fault rows, `replicate_index=k mod 10`, partition is
`FAULT`, and `injected_fault` is the same indexed fault above. The remaining base
fields are exactly `agent_count=4`, `flow_model="simple"`, `regime="BALANCED"`,
`volume="1.00x"`, `liquidity="NORMAL"`, `latency="NORMAL"`,
`session_phase="CONTINUOUS"`, `order_types="MARKET_AND_LIMIT"`,
`hidden_liquidity="NONE"`, `venue_count=1`, `auction_state="NONE"`,
`agent_population="liquidity_provision"`, `strategy="TWAP"`, and
`objective="ACQUIRE"`, followed only by the stated cell overrides. Those are all
twenty-four serialized fields; `max_events`, an implicit seed, and an extra release-
only field are forbidden.

`lane` maps exactly: `CORE_FLOW` and `QUEUE_REACTIVE` use `CORE_FLOW` (the latter is
consumed directly by the release queue adapter, never dispatched to
`CoreFlowExecutor`); `MARKET_MECHANICS` and `HALT_REOPEN` use `MECHANICS`; `LATENCY`
uses `LATENCY`; `HIDDEN_LIQUIDITY` and `MULTIVENUE` use `FRAGMENTED`;
`AGENT_ECOLOGY` uses `ECOLOGY`; `ALGORITHM` uses `ALGORITHM`; and `FAULT_REPLAY` uses
`FAULT`.

Native fixtures are also complete rather than defaults. `CORE_FLOW` and
`QUEUE_REACTIVE` use the committed `balanced` definition whose canonical definition
SHA-256 is `0efb751e880e3112ac5a99a28a8c86b9514ddf47c5c481d72f35328767537699`:
tick size `Decimal("0.01")`, mid `10000`, eight levels, half-spread one tick,
intensity `1.0`; rates `(limit_buy,limit_sell,market_buy,market_sell,cancel_bid,
cancel_ask)=(2.5,2.5,1.5,1.5,1.25,1.25)`; initial queue values/weights
`(50,100,200,400,800)/(10,25,30,25,10)`; every order-size family
`(50,100,200,400,800)/(10,25,30,25,10)`; and bid/ask placement depth
`(0,1,2,3,4)/(40,30,15,10,5)`. `CORE_FLOW` uses `SimpleFlowModel`.
`RELEASE_QUEUE_REACTIVE_V1` uses that same simple arrival model plus exactly
`QueueReactiveFlowModifier(default_queue_reactive_config())`, configuration digest
`15af8f20babde04a3ff0c02defa88213e6a871fe4bc53ec628cd2bd5323eabad`; WO40-D
implements this production-engine adapter in `kirby2/release/probes.py`, records the
modifier inspections/config in replay bytes, and does not route the unsupported value
through the existing `CoreFlowExecutor`.

Other native fixtures are exact production constructors. `MECHANICS` uses
`MechanicsExecutor` with the serialized session/order/auction fields and its canonical
program; `LATENCY` uses `cancel_race_for_seed(seed)` and the full STRESSED profile
whose canonical `as_dict()` digest is
`56160799b27723f954617bd095173c75dcfb895eb8f61de4e43e2ae51a3d51d8`;
`FRAGMENTED` uses venue IDs `AUDIT-V01..AUDIT-V04`, latency profiles
`ZERO_LATENCY,LOW_LATENCY,NORMAL,STRESSED`, taker-fee/maker-rebate micros per share
`(10,5),(20,10),(30,15),(40,20)` with `tick_value_micros=10000` at every venue, bid/ask ticks
`(102,104),(98,100),(99,103),(97,105)`, expected-fill bps
`7000,7500,8000,8500`, route time `1000 us`, market-flow quantity `80`, player
quantity `60` per venue, and hidden rules exactly
`allow_fully_hidden=true,allow_midpoint_hidden=true,feed_delay_us=0,
hidden_priority="AFTER_DISPLAYED",iceberg_refresh_priority="LOSE",
queue_data_mode="AGGREGATED_DEPTH"`; `ECOLOGY` uses
`compose_bounded_population("liquidation_ecology",8,duration_us=10000000)` with
identity digest `cd79fa1ecdaa7719cfe58b844153bb10e3c30b68a064c5af85e35f1bd1024654`;
`ALGORITHM` uses `default_algorithm_manifest("TWAP")`, digest
`9a641ef1f8a6be145b6bd017fb5dd7b94d2c09f27939b7a32c84a7b2a560495c`,
target `300`, internal duration `1000000 us`, decision interval `250000 us`, risk
limits `(maximum_child,working,position,spread)=(200,300,300,10)`, no price limit,
scenario `opening_momentum`, side `BUY`, and objective `ACQUIRE`. `FAULT` uses the indexed `FaultExecutor`
constructor and `FAULT_REPLAY_CONTEXT_V1` policy from the frozen executor source. A mismatch
between the release duration field and a runner's explicitly declared internal
fixture duration is visible metadata, not an implicit override.

For a fragmented row, `venue_count` selects the exact common ordered prefix of the
four venue IDs, latency profiles, fee objects, quote pairs, and expected-fill-bps
vectors above. `HIDDEN_LIQUIDITY` therefore serializes the first two entries of every
vector and `MULTIVENUE` all four. Flow quantity, player quantity, and hidden rules
apply only to those selected venues; retaining an unused suffix or independently
slicing any vector is invalid.

`native_fixture_template` and its bound `native_fixture` always have exactly five
keys: `schema_version=1`, `fixture_id="<CELL>_FIXTURE_V1"`, `constructor_id`,
`generated_configuration_sha256`, and `parameters`; `parameters` is an object and no
constructor default is omitted. Constructor IDs are fixed per cell:

| Cell | `constructor_id` |
|---|---|
| `CORE_FLOW` | `SIMPLE_FLOW_V1` |
| `MARKET_MECHANICS` | `MECHANICS_AUDIT_PROGRAM_V1` |
| `QUEUE_REACTIVE` | `SIMPLE_QUEUE_REACTIVE_V1` |
| `LATENCY` | `CANCEL_RACE_FOR_SEED_V1` |
| `HIDDEN_LIQUIDITY` | `FRAGMENTED_MARKET_V1` |
| `MULTIVENUE` | `FRAGMENTED_MARKET_V1` |
| `AGENT_ECOLOGY` | `BOUNDED_POPULATION_V1` |
| `ALGORITHM` | `DEFAULT_ALGORITHM_MANIFEST_V1` |
| `HALT_REOPEN` | `MECHANICS_AUDIT_PROGRAM_V1` |
| `FAULT_REPLAY` | `FAULT_REPLAY_CONTEXT_V1` |

For core/queue, `parameters` contains exactly `scenario_definition` (the full
canonical `ScenarioDefinition.as_dict()`), `simulation_config` (all nine
`SimulationConfig.as_dict()` fields), `regime_profile`, `base_flow_model`, and
`queue_reactive_config` (null for core, the full config object for queue).
For both cells `base_flow_model` is exactly the object
`{"model":"simple","replay_config":null}`; neither a class name nor an omitted
replay field is accepted.
`regime_profile` is an object with exactly these keys:
`regime,rate_multipliers,limit_buy_sizes,limit_sell_sizes,market_buy_sizes,
market_sell_sizes,bid_depth,ask_depth,imbalance_feedback,trend_feedback,
initial_queue_sizes`. Every `native_fixture_template` and bound `native_fixture`
parameter tree—including `scenario_definition`, `simulation_config`,
`regime_profile`, population definitions, algorithm manifests, and any other legacy
`as_dict()` value except the expressly named latency adapter below—is stored in
semantic fixture identity through
`RELEASE_NATIVE_FIXED_POINT_V1`: recursively preserve maps, arrays, strings,
Booleans, null, and integer JSON values, but replace every schema-declared decimal
number with the exact object `{"fixed_decimal_micros":n}` where
`n=decimal_value*1_000_000` must be integral. Source JSON is parsed with
`parse_float=Decimal`; fixed constructor literals are Decimal strings; converting a
binary float or using `repr(float)` as identity input is forbidden. The decoder
verifies the fixture digest first, then converts each tagged value through
`float(Decimal(n)/Decimal(1_000_000))` only at the legacy constructor boundary and
re-encodes it to the identical fixed-point projection before execution. Thus
`event_intensity=1.0` is `{"fixed_decimal_micros":1000000}`, rates `2.5`, `1.5`, and
`1.25` are tagged with `2500000`, `1500000`, and `1250000`; multipliers `1.0` are
tagged with `1000000`; feedback `0.2` is tagged with `200000`; and `0.0` with `0`.
Decimal tick size remains its existing
canonical string `"0.01"`. A decimal with more than six places or a tag at an
integer/string path fails.

Latency profiles use the narrower integer-source
`RELEASE_LATENCY_PROFILE_SOURCE_V1`, never rounded `mu` values. Its object has exactly
`schema_version=1`, `name`, `simulator_only=true`, and `components`. Components use
the nine `LatencyComponent` value strings and are key-sorted. A fixed component is
exactly `{kind:"FIXED",value_us:int}`; uniform is exactly
`{kind:"UNIFORM_BOUNDED",lower_us:int,upper_us:int}`; and lognormal is exactly
`{kind:"LOGNORMAL_BOUNDED",lower_us:int,upper_us:int,median_us:int,sigma_ppm:int}`.
The released profiles are the committed constructor inputs: `ZERO_LATENCY` is nine
fixed-zero components; `LOW_LATENCY` is publication fixed 50, downlink uniform
50..150, render fixed 100, input uniform 25..75, routing fixed 50, uplink uniform
75..200, gateway fixed 75, venue fixed 50, and fill-report uniform 75..200;
`NORMAL` is publication fixed 250, downlink uniform 300..700, render fixed 250,
input fixed 100, routing fixed 100, uplink fixed 1000, gateway fixed 1000, venue fixed
500, and fill-report fixed 500; and
`STRESSED` is publication uniform 500..2000, downlink lognormal
`(500,8000,median=2000,sigma_ppm=650000)`, render uniform 500..3000, input uniform
250..1000, routing uniform 300..1500, uplink lognormal
`(1000,12000,median=4000,sigma_ppm=750000)`, gateway uniform 1000..5000, venue
uniform 500..4000, and fill-report lognormal
`(1000,15000,median=5000,sigma_ppm=800000)`, in that component order.

Only after source-projection digest verification, the adapter constructs lognormal
`mu=math.log(median_us)` and `sigma=float(Decimal(sigma_ppm)/Decimal(S))` on the
locked runtime; other kinds use their integers directly. It then requires exact
legacy canonical-`as_dict()` digests `ZERO_LATENCY=
4c3185ca05f792dac93188b612004e87b75bee584ab17ea78271f308fe9f56af`,
`LOW_LATENCY=53b4b0f636b497fccbf0e922755c7ee41514b9972bcfc474c32970460a7637a3`,
`NORMAL=6a1217f3335dac4d4941ba0e3c41139e69ded25e318ffcc7212c1c459aedf7f9`, and
`STRESSED=56160799b27723f954617bd095173c75dcfb895eb8f61de4e43e2ae51a3d51d8`.
Those compatibility digests verify construction but never replace the integer-source
semantic identity. A runtime/libm mismatch refuses the adapter rather than changing
fixture bytes.

`rate_multipliers` is a six-element array of those tagged fixed-point objects in
`limit_buy,limit_sell,market_buy,market_sell,cancel_bid,cancel_ask` order; every size,
depth, and initial-queue value is the exact `WeightedDiscreteDistribution.as_dict()`
object with keys `kind="weighted_discrete",values,weights`; the two feedback fields
are tagged fixed-point objects. For `BALANCED`, the rate array is exactly six copies
of `{"fixed_decimal_micros":1000000}`, all five size/queue distributions are
`values=[50,100,200,400,800],weights=[10,25,30,25,10]`, both depth distributions are
`values=[0,1,2,3,4],weights=[40,30,15,10,5]`; fixed-point feedback values decode to
`imbalance_feedback=0.2` and `trend_feedback=0.0` only after identity verification.

Mechanics contains exactly `session_phase,order_types,auction_state,program_id=
"AUDITLAB_MECHANICS_PROGRAM_V1",program_source_path` in the template; binding
replaces only `program_source_path="kirby2/auditlab/executors/mechanics.py"` with
`program_source_sha256`. Latency contains exactly the full `latency_profile`,
`schedule_policy_id="CANCEL_RACE_FOR_SEED_V1"`, and `executor_source_path` in the
template; binding replaces only
`executor_source_path="kirby2/auditlab/executors/latency.py"` with
`executor_source_sha256`. Fragmented contains exactly ordered `venue_ids`, full
ordered latency-profile objects, ordered fee objects, ordered quote pairs,
`expected_fill_probability_bps`, `route_time_us`, `flow_quantity`,
`player_quantity_per_venue`, the full hidden-rules object, and
`executor_source_path`; ecology contains exactly the full
`PopulationDefinition.identity_dict()`, `definition_sha256`, and
`executor_source_path`;
algorithm contains exactly full `AlgorithmParameterManifest.as_dict()`, manifest
digest, target quantity, internal duration, decision interval, full risk-limit object,
scenario, ordered sides, objective, and `executor_source_path`; fault contains exactly fault enum,
`context_policy_id="FAULT_REPLAY_CONTEXT_V1"`, and `executor_source_path` in the
template. Exact values are `kirby2/auditlab/executors/ecology.py` for ecology,
`kirby2/auditlab/executors/algorithms.py` for algorithm,
`kirby2/auditlab/executors/fragmented.py` for fragmented, and
`kirby2/auditlab/executors/fault.py` for fault. Binding replaces only each such
`executor_source_path` with `executor_source_sha256`. These key sets plus the literal
values above determine every template and fixture byte; a constructor round-trip must
reproduce the bound object before its digest is accepted. Every
`program_source_sha256` or `executor_source_sha256` is exactly the matching digest in
the one candidate source lock, never another manually supplied constant.

Runner identity is exact and exhaustive. `release/performance_runner_sources.lock`
is compact canonical JSON with exactly `schema_version=1`,
`source_policy_id="RUNNER_SOURCE_TREE_V1"`, `source_manifest`, and
`source_manifest_sha256`. `source_manifest` is the array of exactly `{path,sha256}`
objects for every regular Git blob under `kirby2/`, plus `pyproject.toml`, in ascending
normalized UTF-8 path bytes from the fully staged WO40-E candidate tree; the lock file
itself lies outside that projection. No untracked file, directory, symlink, generated
bytecode, documentation outside those paths, or build artifact enters it.
`source_manifest_sha256` hashes the canonical array and becomes every bound row's
`runner_source_sha256`. The lock generator reads the Git index, not the working tree,
refuses any unstaged candidate-owned path or staged path outside WO40-E ownership,
and snapshots the complete index after all non-lock WO40-E files are staged. It
derives the source projection from that snapshot, writes and stages the lock, then
requires that the complete final index differs from the snapshot only by the lock and
that recomputing the projection from the final index is byte-identical. The row's
`runner_id` and source entrypoint
below identify the invoked subset; the exhaustive lock prevents a transitive
dependency from being selected ad hoc. Runner IDs, artifact forms, and capability
sets are below; every displayed source file is the literal project-relative path
looked up in `RUNNER_SOURCE_TREE_V1`, with no implicit package prefix:

| Cell | Runner ID / source file | Artifact form | Expected capabilities |
|---|---|---|---|
| `CORE_FLOW` | `CORE_FLOW` / `kirby2/auditlab/executors/core_flow.py` | `CORE_FLOW_EVENT_TAPE` | `seed,duration_us,flow_model,regime,volume,liquidity` |
| `MARKET_MECHANICS` | `MECHANICS` / `kirby2/auditlab/executors/mechanics.py` | `NATIVE_MECHANICS_RECORDING` | `session_phase,order_types,auction_state` |
| `QUEUE_REACTIVE` | `RELEASE_QUEUE_REACTIVE_V1` / `kirby2/release/probes.py` | `QUEUE_REACTIVE_EVENT_TAPE_V1` | `seed,duration_us,flow_model,regime,volume,liquidity,queue_reactive` |
| `LATENCY` | `LATENCY` / `kirby2/auditlab/executors/latency.py` | `NATIVE_LATENCY_RECORDING` | `latency` |
| `HIDDEN_LIQUIDITY` | `FRAGMENTED` / `kirby2/auditlab/executors/fragmented.py` | `NATIVE_FRAGMENTED_RECORDING` | `hidden_liquidity,venue_count` |
| `MULTIVENUE` | `FRAGMENTED` / `kirby2/auditlab/executors/fragmented.py` | `NATIVE_FRAGMENTED_RECORDING` | `hidden_liquidity,venue_count` |
| `AGENT_ECOLOGY` | `ECOLOGY` / `kirby2/auditlab/executors/ecology.py` | `NATIVE_ECOLOGY_RECORDING` | `agent_population,agent_count` |
| `ALGORITHM` | `ALGORITHM` / `kirby2/auditlab/executors/algorithms.py` | `NATIVE_ALGORITHM_RECORDINGS` | `strategy,objective` |
| `HALT_REOPEN` | `MECHANICS` / `kirby2/auditlab/executors/mechanics.py` | `NATIVE_MECHANICS_RECORDING` | `session_phase,order_types,auction_state` |
| `FAULT_REPLAY` | `FAULT` / `kirby2/auditlab/executors/fault.py` | `EXPLICIT_FAULT_OBSERVATION` | `injected_fault` |

Required-check arrays are exact and ordered: `CORE_CHECKS=(quantity_conservation,
fifo_book_ordering,non_crossed_book,contiguous_sequences,
player_position_reconciliation,player_cash_reconciliation,hawkes_stability,
event_rate_cap,observable_projection_boundary)`; `QUEUE_CHECKS` is `CORE_CHECKS`
followed by `queue_reactive_intensity_applied,queue_reactive_recording_replay`;
`MECHANICS_CHECKS=(order_lifecycle_reconciliation,quantity_conservation,
fifo_book_ordering,auction_allocation_reconciliation,
auction_indication_reconciliation,monotonic_event_time)`;
`LATENCY_CHECKS=(causal_timestamps,async_lifecycle_reconciliation,
quantity_conservation,terminal_cancel_fill_ordering,cancel_race_reconciliation,
latency_metric_reconciliation)`; `FRAGMENTED_CHECKS=(venue_invariants,
global_position_reconciliation,route_leg_conservation,observable_quote_construction,
observable_projection_boundary,crossed_composite_intervals_recorded)`;
`ECOLOGY_CHECKS=(agent_risk_bounds,agent_inventory_reconciliation,
observable_projection_boundary,owned_rng_determinism,monotonic_event_time)`;
`ALGORITHM_CHECKS=(observation_boundary,objective_quantity_conservation,
client_venue_fill_reconciliation,control_fork_identity,native_recording_replay)`; and
`FAULT_CHECKS=(fault_injected,production_detector_exercised,
unrelated_invariants_survive)`. Each cell uses its runner's named array.

Every row's `audit_argv` is the exact object with string keys `"1"` and `"2"`.
Value `"1"` is the JSON string array
`["kirby2","qualify-performance-row","--protocol",
"release/performance_thresholds.toml","--work-unit-id",work_unit_id,"--attempt","1"]`;
value `"2"` is the identical array ending in `"--attempt","2"`. Attempt 1 is initial
and attempt 2 may execute only as the one permitted retry. It is
executed by the installed artifact with only its clean provider's
recorded `KIRBY2_DATA_ROOT`, locale, and `PYTHONDONTWRITEBYTECODE=1` environment.
`ReleasePerformanceCellResultV1` is compact canonical JSON, schema version 1, with
exactly these keys/types: `schema_version:int`, `work_unit_id:str`, `attempt:int`,
`status:"COMPLETE"|"FAILED"`, the lowercase-hex string fields
`generated_configuration_sha256,native_fixture_sha256,runner_source_sha256`,
`capability_records:array`, `check_records:array`, nullable lowercase-hex
`run_manifest_sha256,native_recording_sha256,semantic_result_sha256,
artifact_set_sha256,audit_result_sha256`, `operational:object`, and
`failure_code:null|"PROCESS_FAILURE"|"RESOURCE_LIMIT"|"SEMANTIC_FAILURE"|
"INVARIANT_FAILURE"|"REPLAY_FAILURE"|"SCHEMA_FAILURE"|"DIGEST_FAILURE"`.
Capability records appear in expected-capability order and have exactly
`capability,configured_value,status,evidence_sha256`, status
`EXERCISED|NOT_EXERCISED`; check records appear in required-check order and have
exactly `check_id,status,evidence_sha256`, status `PASS|FAIL|NOT_EXERCISED`.
For each capability, `configured_value` is copied byte-for-byte as a value from the
matching exercise row inside the already legacy-digest-extracted and
`RELEASE_FLOAT_FREE_SEMANTIC_V1`-projected semantic-result payload, and
`evidence_sha256` hashes compact canonical JSON of that row's complete projected
`evidence` subtree. Each check digest is defined identically from its matching
projected check `evidence` subtree. Missing/duplicate mappings or hashing raw native
evidence fails; these wrapper arrays never create a second evidence projection.
`operational` has exactly integer `start_monotonic_ns,end_monotonic_ns,peak_rss_bytes,
max_temporary_bytes` and nullable `retry_reason`; operational bytes are excluded from
`semantic_result_sha256`.

The semantic artifact-set manifest is the canonical ordered array for exactly
`run_manifest.json`, `native_recording.json`, `semantic_result.json`,
`capabilities.json`, `checks.json`, and `audit_result.json`, in that order, each entry
exactly `{"name":str,"size":int,"sha256":lowercase_hex}`. Each file is compact
canonical JSON with no final LF. `artifact_set_sha256` hashes that array.
`operational_attempt_1.json` and, only after retry, `operational_attempt_2.json` are
append-only sidecars outside the semantic artifact-set digest but inside aggregate
artifact accounting. `legacy_digest_bindings.json`, when native output is produced,
is another compatibility-only sidecar under the exact rule below. The complete result,
six semantic members, that compatibility sidecar, and every executed operational
sidecar form the work unit. All bytes are read back and verified before
`COMPLETE`; a failed result uses null unavailable digests and never fabricates empty
artifacts.

Before projecting the raw production `CaseRecording.as_dict()` or
`GeneratedCaseResult.as_dict()` payload, `RELEASE_LEGACY_DIGEST_EXTRACTION_V1` replaces
every string value matching exactly sixty-four lowercase hexadecimal characters by
`{"__kirby2_legacy_digest_ref_v1__":json_pointer}`. JSON pointers are RFC 6901 from
the respective raw payload root, use decimal array indices without leading zero, and
are sorted by NFC UTF-8 pointer bytes. A source map already containing that reserved
key fails. This deliberately catches wrapper `sha256`, `result_sha256`, recording,
state, event, observable, and any transitive native hash without relying on its field
name.

`legacy_digest_bindings.json` is compact canonical JSON with exactly
`schema_version=1`, `work_unit_id`, `native_recording_bindings`, and
`semantic_result_bindings`; each binding array has exactly `{json_pointer,
legacy_sha256}` rows in pointer order. These legacy values are compatibility
diagnostics outside `native_recording_sha256`, `semantic_result_sha256`, the six-member
artifact-set digest, retry semantic equality, and every CAS semantic key. Their
sidecar transport digest is operational artifact accounting only.

For replay, a fresh scratch execution from the decoded verified configuration and
native fixture regenerates both native outputs. The adapter extracts every matching
path again, requires the exact pointer set and legacy values to equal the sidecar,
injects only those verified values, and then invokes the existing native loader/replay.
Missing/extra paths, a changed legacy value, inability to regenerate, or byte-different
sidecars across a retry fails. No legacy digest value may remain in or be copied into
the projected semantic payload.

Before any of those six identity-bearing members is encoded,
`RELEASE_FLOAT_FREE_SEMANTIC_V1` recursively projects its complete JSON-like value.
Null, Boolean, integer, and NFC string values are preserved; arrays preserve order;
maps require unique NFC string keys, reject reserved key
`__kirby2_release_scalar_v1__`, and are key-sorted. A finite Python float other than
negative zero is replaced by the exact object
`{"__kirby2_release_scalar_v1__":"EXACT_RATIONAL","numerator":n,
"denominator":d}`, where `(n,d)=float.as_integer_ratio()`, `d>0`, and the pair is
coprime. Decimal values, if not already schema strings, use the same object with tag
`EXACT_DECIMAL` but exact keys `__kirby2_release_scalar_v1__,coefficient,exponent`,
where `value=coefficient*10**exponent`; coefficient has no trailing zero unless zero.
NaN, infinity, negative zero, an unrecognized object,
or any raw JSON float after projection fails. These integer pairs—not binary-float
JSON numbers—are the identity-bearing wire values permitted by section 3.

The release replay adapter decodes only at the declared native schema boundary,
requires an exact representable rational/decimal value, constructs the native value,
and immediately reprojects it byte-identically before invoking production replay.
Thus platform-scoped native computations remain honest while every semantic member,
member digest, retry comparison, CAS key, and artifact-set digest is float-free.
Cross-platform equality remains limited to `CROSS_PLATFORM_INTEGER_CORE_V1`.

The six member projections are also fixed. `run_manifest.json` has exactly
`schema_version=1,work_unit_id,cell,root_seed,generated_configuration_sha256,
native_fixture_sha256,runner_id,runner_source_sha256,artifact_form,
expected_capabilities,required_checks`; `native_recording.json` is exactly
`{"schema_version":1,"projection_policy":"RELEASE_FLOAT_FREE_SEMANTIC_V1",
"legacy_digest_policy":"RELEASE_LEGACY_DIGEST_EXTRACTION_V1",
"native_schema_id":the_named_runner_recording_type,"payload":projected_recording}`;
`semantic_result.json` is exactly the same five-key envelope with
`native_schema_id="GENERATED_CASE_RESULT_AS_DICT_V2"` and payload equal to the
float-free projection of production `GeneratedCaseResult.as_dict()` with operational
fields removed;
`capabilities.json` and `checks.json` are exactly the two ordered record arrays above;
and `audit_result.json` has exactly `schema_version=1,work_unit_id,status,
capability_projection_sha256,check_projection_sha256,native_recording_sha256,
semantic_result_sha256,failures`, where status is `PASS|FAIL`, failures are NFC reason
codes in UTF-8 order, and the two projection digests hash their complete arrays.

A cell passes only when its named production runner reports the named capability
exercised and emits its complete run manifest, result, artifact set, and audit result;
adapter substitution or `NOT_EXERCISED` fails.

Resources are exactly four ready worker processes, at most four concurrent attempts,
and a FIFO coordinator queue of 256 rows. Each attempt gets a fresh owned temporary
directory. Per-attempt wall time starts immediately before runner dispatch and ends
only after canonical result/artifact/audit bytes are written, read back, digest-
verified, committed to CAS, and fsynced. Darwin peak RSS is final child
`ru_maxrss` in bytes, supplemented by 100-ms coordinator liveness samples; the maximum
is authoritative. Temporary bytes are the maximum sum of `st_size` for unique regular
files by `(st_dev,st_ino)` below that attempt directory after every write/rename and
before cleanup; shared read-only CAS objects are excluded.

The inclusive limits are RSS `<=512*1024**2`, temporary bytes `<=8*1024**2`, and
wall time `<=120*1_000_000_000 ns`. Equality is allowed. The first strictly greater
sample/elapsed value terminates that attempt as `RESOURCE_LIMIT`; a completed attempt
over a limit is never admitted retroactively. One and only one retry is allowed for a
process failure or `RESOURCE_LIMIT`; it is not another work unit, its time/resources
remain in operational evidence, and any bytes already committed must reproduce
exactly. Exact reproduction means native recording, semantic result projection,
artifact-set members, and audit/check projections have identical digests; attempt
ordinal, monotonic timing, RSS, temporary bytes, process diagnostics, and retry reason
are operational sidecars excluded from those semantic digests. A semantic, invariant,
replay, schema, or digest failure is not retryable.

Total measured time starts after all four workers report ready, immediately before
the coordinator exposes the first row, and ends after the 10,000th final tuple plus
the aggregate manifest have been reread, digest-verified, committed, and fsynced. It
includes dispatch, queueing, worker scheduling, runner work, serialization, every
audit/check, retry, CAS commit/verification, coordinator aggregation, and final
verification; it excludes only preregistration parsing, resource preflight, process
startup before all workers are ready, and no warmup row exists. The total limit is
inclusive `36*60*60*1_000_000_000 ns`; equality may complete, but once elapsed time is
strictly greater the coordinator stops admitting/accepting work and records hard
`FAIL`. Any final incomplete/failed unit, conflicting retry, invariant failure, or
missing tuple is hard `FAIL`.

Let `C` be verified complete work units and `E` that exact total interval in
nanoseconds. Report `throughput_microruns_per_second=round_div_even(C*10**15,E)`.
Evaluate PASS first: `C*10**9 >= E` is `PASS`; otherwise
`10*C*10**9 >= E` is `WARNING`; otherwise `FAIL`. Retry and coordinator time therefore
cannot disappear from throughput. Aggregate artifact bytes are the sum of exact
`st_size` for unique governed CAS objects attributable to these rows, plus their
manifest/result/audit objects, counted once by content digest even when referenced by
many rows. Preexisting read-only protocol/source inputs are excluded and logical
referenced bytes are reported separately. Evaluate PASS first: bytes
`<=12*1024**3` is `PASS`; otherwise `<=18*1024**3` is `WARNING`; otherwise `FAIL`.
Warnings yield `PASS_WITH_WARNINGS` only if every hard gate passes and remain visible.

`CROSS_PLATFORM_INTEGER_CORE_V1` is a separate qualification workload, not the
float/libm-driven 10,000-run cell bytes. For `CORE_FLOW` and each root
`4000000..4000015`, execute the exact `kirby2.scenarios.demo.run_demo(root)` command
tape, which uses integer RNG draws only. For `MARKET_MECHANICS` and each same root,
execute `MECHANICS_SCENARIOS[(root-4000000)%7]` in the tuple order frozen by the
candidate source. Neither lane may invoke `SIMPLE`, Poisson, lognormal, Hawkes, or any
float/libm generator. Compare canonical command, event, result, and replay bytes
exactly across both targets. Other workloads compare bytes only within one exact
platform/runtime fingerprint and compare cross-platform invariants/typed metrics, not
unsupported byte equality.

## 6. Canonical slice index

| Order | Card | Deliverable | Exact commit subject |
|---:|---|---|---|
| 0 | K2X-00 | This canonical goal contract | `Document work orders 31-40 execution sequence` |
| 1 | K2X-01 | Sealed-baseline attestation | `Attest repaired expansion baseline` |
| 2 | K2X-02 | Modular command/audit registration seam | `Modularize CLI command registration` |
| 3 | WO31-A | Full-day plan and calendar contracts | `Define full day execution contracts` |
| 4 | WO31-B | Duration-aware hierarchical state | `Add duration aware market states` |
| 5 | WO31-C | Portable checkpoint envelope and data paths | `Define portable runtime checkpoints` |
| 6 | WO31-D | Core-session fresh-process restoration | `Restore deterministic core sessions` |
| 7 | WO31-E1 | Mechanics/agent-spine restoration | `Restore full day mechanics and agents` |
| 7.1 | WO31-E2 | Flow/Hawkes/queue-reactive restoration | `Restore full day flow models` |
| 7.2 | WO31-E3 | Latency/observable-delivery restoration | `Restore full day delivery state` |
| 7.3 | WO31-E4 | Feature/strategy/player restoration | `Restore full day observable research state` |
| 7.4 | WO31-E5 | Standalone multivenue/hidden restoration | `Restore full day multivenue state` |
| 7.5 | WO31-E6 | Standalone execution-algorithm restoration | `Restore full day algorithm state` |
| 8 | WO31-F | Full-day composition runtime | `Compose deterministic full trading days` |
| 9 | WO31-G | Storage, inspection, seek, and summaries | `Store inspect and seek full trading days` |
| 9.1 | WO31-H | Preregistered profile/envelope manifest | `Preregister full day profile envelopes` |
| 10 | WO31-I | Frozen qualification machinery | `Implement full day qualification gates` |
| 10.1 | WO31-I1 | Evidence-only profile qualification | `Qualify full day profile candidates` |
| 11 | WO32-A | Scenario source model and semantic identity | `Define canonical scenario source model` |
| 12 | WO32-B | Confined imports, definitions, inheritance | `Resolve confined scenario imports` |
| 13 | WO32-C | Immutable compiler and seed policy | `Compile immutable scenario plans` |
| 14 | WO32-D | Static validation and capability refusal | `Enforce scenario capability contracts` |
| 15 | WO32-E | Authoring CLI, explain, diff, examples | `Add scenario authoring diagnostics` |
| 16 | WO33-A | Versioned detector and skill contracts | `Define capability aware lesson detectors` |
| 16.1 | WO33-A1 | Preregistered mining/selection manifest | `Preregister lesson mining thresholds` |
| 17 | WO33-B1 | Queue/flow/liquidity detectors | `Detect queue and liquidity lessons` |
| 17.1 | WO33-B2 | Latency/venue/mechanics detectors | `Detect execution mechanics lessons` |
| 18 | WO33-C | Difficulty, deduplication, and diversity | `Rank diverse lesson candidates` |
| 19 | WO33-D | Observable playable lesson extraction | `Extract observable playable lessons` |
| 20 | WO33-E | Human review sidecars and mining CLI | `Add immutable lesson review workflow` |
| 21 | WO34-A | Immutable learner evidence and skill graph | `Record immutable learner evidence` |
| 22 | WO34-B | Versioned learner-state projections | `Project deterministic learner estimates` |
| 23 | WO34-C | Explainable adaptive selection and modes | `Select explainable adaptive drills` |
| 24 | WO34-D | Synthetic learner and curriculum audit | `Audit adaptive curriculum behavior` |
| 25 | WO35-A | Canonical strategy AST identity | `Canonicalize strategy syntax trees` |
| 26 | WO35-B | Sealed experiment partitions | `Seal strategy experiment partitions` |
| 27 | WO35-C | Constrained mutation engine | `Generate constrained strategy mutations` |
| 28 | WO35-D | Deterministic multi-objective search | `Search deterministic strategy candidates` |
| 29 | WO35-E | Robustness, observability, and overfit gates | `Audit strategy robustness and overfit` |
| 30 | WO35-F | Frozen discovery lineage and CLI machinery | `Implement strategy discovery lineage` |
| 30.1 | WO35-F1 | Evidence-only controlled discovery | `Preserve strategy discovery lineage` |
| 31 | WO36-A | Mechanistic execution trace index | `Index mechanistic execution traces` |
| 32 | WO36-B | Enforced observation/reveal query policies | `Enforce replay observation policies` |
| 33 | WO36-C | Synchronized replay read models | `Build synchronized replay read models` |
| 34 | WO36-D | Portable offline microscope report | `Render portable replay microscope reports` |
| 35 | WO36-E | Counterfactual comparison and annotations | `Compare counterfactual replay branches` |
| 36 | WO37-A | Pseudonymous profiles and consent | `Separate learner identity from evidence` |
| 37 | WO37-B | Versioned assignments, rubrics, reviews | `Version assignments rubrics and reviews` |
| 38 | WO37-C | Locked reproducible local studies | `Lock reproducible local studies` |
| 39 | WO37-D | Instructor/research console queries | `Build instructor research console queries` |
| 40 | WO37-E | Redacted export and profile deletion | `Export redacted research bundles` |
| 41 | WO39-A | Canonical data-only pack identity | `Define canonical Kirby2 pack identity` |
| 42 | WO39-B | Hostile archive validation and staging | `Validate hostile pack archives` |
| 43 | WO39-C | Registry, dependencies, atomic installation | `Install Kirby2 packs atomically` |
| 44 | WO38-A | Logical work and attempt identity | `Define deterministic work unit identity` |
| 45 | WO38-B | Single/local multiprocess orchestration | `Orchestrate local experiment workers` |
| 46 | WO38-C | Verified content-addressed artifact exchange | `Transfer verified experiment artifacts` |
| 47 | WO38-D | Authenticated trusted-LAN backend | `Secure trusted LAN orchestration` |
| 48 | WO38-E | Recovery, idempotence, deterministic aggregation | `Recover and verify distributed experiments` |
| 49 | WO39-D1 | Scenario/training/model pack adapters | `Package Kirby2 training artifacts` |
| 49.1 | WO39-D2 | Historical/replay/research pack adapters | `Package Kirby2 evidence artifacts` |
| 50 | WO39-E | Portability, compatibility, and hostile-pack audit | `Audit Kirby2 pack portability` |
| 51 | WO40-A | Release data, schema, and migration policy | `Version release data and migrations` |
| 52 | WO40-B | Exact interactive crash recovery | `Recover interactive sessions exactly` |
| 52.1 | WO40-B1 | Non-destructive backup and restore | `Back up and restore Kirby2 data` |
| 53 | WO40-C | First-run flow and redacted diagnostics | `Add first run release diagnostics` |
| 54 | WO40-D | Frozen packaging/platform/performance protocol | `Preregister Kirby2 release qualification` |
| 54.1 | WO40-D1 | Read-only release-resource preflight | `Verify release build resources` |
| 55 | WO40-E | Frozen release-candidate source | `Freeze the first Kirby2 release candidate` |
| 56 | WO40-F | Immutable headless and desktop artifacts | `Build Kirby2 release artifacts` |
| 57 | WO40-G | macOS clean-environment qualification | `Qualify macOS release artifacts` |
| 58 | WO40-H | Linux clean-environment qualification | `Qualify Linux release artifacts` |
| 58.1 | WO40-I | Preregistered 10,000-complete-run performance | `Measure Kirby2 release performance` |
| 58.2 | WO40-J | Release closeout evidence and documentation | `Close the first Kirby2 release candidate` |

## 7. Enabling cards

### K2X-00 — Canonical goal contract

Objective: preserve the revised architecture, truth boundaries, sequence, pause
gates, card ownership, evidence, and exact commit subjects before production work.

Owned file:

- create `KIRBY2_WORK_ORDERS_31_40_GOAL.md` only.

Required evidence:

```text
git diff --check
! rg -n '[[:blank:]]+$' KIRBY2_WORK_ORDERS_31_40_GOAL.md
rg -n '^### (K2X-[0-9]{2}|WO(31|32|33|34|35|36|37|38|39|40)-[A-Z][0-9]*) ' KIRBY2_WORK_ORDERS_31_40_GOAL.md
./.venv/bin/python -c 'import re; from pathlib import Path; text=Path("KIRBY2_WORK_ORDERS_31_40_GOAL.md").read_text(encoding="utf-8"); index=re.findall(r"^\| [0-9]+(?:\.[0-9]+)? \| (K2X-[0-9]{2}|WO(?:31|32|33|34|35|36|37|38|39|40)-[A-Z][0-9]*) \|",text,re.M); cards=re.findall(r"^### (K2X-[0-9]{2}|WO(?:31|32|33|34|35|36|37|38|39|40)-[A-Z][0-9]*) —",text,re.M); assert index==cards,(index,cards); assert len(index)==len(set(index)); print("ROADMAP_STRUCTURE PASS cards="+str(len(cards)))'
git status --short
```

Acceptance:

- cards occur once and in index order;
- each future card names ownership, fixed decisions, evidence, acceptance, and a
  commit subject;
- every source work-order requirement maps to a card in section 20;
- no production file changes in this card.

Commit: `Document work orders 31-40 execution sequence`

### K2X-01 — Sealed-baseline attestation

Objective: verify and reference the inherited repaired evidence without creating a
duplicate scientific run or misrepresenting its statuses.

Owned files:

- create `KIRBY2_WORK_ORDERS_31_40_BASELINE.md`;
- create `KIRBY2_WORK_ORDERS_31_40_BASELINE.json` as the canonical machine-readable
  attestation referenced by K2X-02;
- no production Python files.

Fixed decisions:

1. Record all seven baseline fields from section 2 and the current environment.
2. Verify the existing v2 packet in place using its manifest and artifact bytes.
3. Run non-persisting deterministic smoke gates; confine any scratch output to an
   automatically cleaned temporary directory.
4. Record inherited warnings `calibration_train_vs_holdout`, `distribution_drift`,
   and `unstable_hawkes` plus manual `PENDING_HUMAN_REVIEW` verbatim.
5. Do not run the persisting `audit-lab` command, call
   `AuditLabStore.record()`/`record_acceptance()`, or append to either inherited audit
   ledger.
6. The JSON attestation uses schema version 1 and records the seven section-2 fields,
   packet and acceptance-ledger SHA-256 values, 7/7 counts, sorted pre-existing root
   command names, a canonical projection and SHA-256 of every pre-existing command/
   nested parser, command-specific `format_help()` digests, representative demo/audit
   stdout/stderr/exit digests, runtime versions, warnings, commands, and limitations.
   The projection records action class, option strings, destination, positional order,
   `nargs`, required, default, choices, metavar, help, type identity, and nested command
   order. The Markdown file renders those same facts; it is not a second identity.
7. The sealed legacy parser inventory is 55 top-level commands and 62 command/nested
   parser nodes (excluding the root), with canonical projection digest
   `8b5da0c0ff5d2e769f7252104f8eb907c91c1653d3c82ebb38c2d93a72770a8e`.
   This is a compatibility artifact fingerprint of the pre-existing argparse graph,
   not a simulation semantic digest; it intentionally records existing finite Python
   float defaults through their canonical JSON number rendering. New FullDayPlan or
   other semantic identities remain governed by section 3 and reject binary floats.
8. `KIRBY2_WORK_ORDERS_31_40_BASELINE.json` is compact sorted-key UTF-8 JSON with no
   trailing newline and exactly these top-level keys: `schema_version`, `baseline`,
   `immutable_tree`, `cli`, `runtime`, `warnings`, `smoke`, and `limitations`.
   `baseline` uses the exact seven uppercase names from section 2; `immutable_tree`
   contains `root`, `entry_count`, `file_count`, `file_bytes`, `tree_sha256`, both
   named ledger SHA-256/count values, and both target-occurrence counts; `cli`
   contains `help_columns`, ordered `command_order`, sorted
   `command_names_sorted`, full `parser_projection`,
   `command_count`, `parser_count`, `projection_sha256`, and `additions`; `runtime`
   contains `python`, `duckdb`, and `platform`; `warnings` is
   the sorted three inherited warning IDs; each ordered `smoke` row contains argv,
   exit code, stdout/stderr SHA-256, exact terminal status line, and
   `compatibility_pinned`. The four rows are demo, scenario audit, inline live replay,
   and model-risk audit. The exact K2X-02 byte-pinned set is the two rows marked true:
   `demo --seed 42` and `audit-scenarios`; `--help` is represented by parser/help
   digests. Live replay records its stable bytes/status, while model-risk records its
   actual stdout digest and stable terminal status but is not byte-pinned across source
   changes because its provenance legitimately changes. The Markdown file names the JSON artifact SHA-256 and renders every status,
   warning, count, digest, environment, and limitation from it.
9. Before and after all smoke commands, hash every regular file and symlink beneath
   the ignored `.kirby2` tree as a sorted canonical inventory. Require 139 files/
   entries, 337279773 file bytes, and tree digest
   `577ded9ae3a9a8230a9723df6838a348a2e61b3593603c8bfdd4afe2f7ee729e`.
   A mismatch stops the card and identifies changed paths; it is never normalized
   away.
10. `limitations` contains exactly the sorted reason codes `BASELINE_SCOPE_ONLY`,
    `INHERITED_MANUAL_REVIEW_PENDING`, `INHERITED_STATISTICAL_WARNINGS`, and
    `NO_NEW_CAPABILITY_CERTIFIED`, each with a nonempty truthful detail. It is not a
    place to hide a failed smoke or changed baseline.

Required evidence:

```text
# Run this sealed packet/ledger assertion both before and after the smoke commands.
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import hashlib,json; from pathlib import Path; root=Path(".kirby2"); rows=[]; [rows.append({"path":p.relative_to(root).as_posix(),"kind":"symlink","target":p.readlink().as_posix()}) if p.is_symlink() else rows.append({"path":p.relative_to(root).as_posix(),"kind":"file","size":len(data),"sha256":hashlib.sha256(data).hexdigest()}) for p in sorted(root.rglob("*"),key=lambda p:p.relative_to(root).as_posix()) if p.is_symlink() or (p.is_file() and not (data:=p.read_bytes()) is None)]; payload=json.dumps(rows,sort_keys=True,separators=(",",":")).encode(); files=sum(row["kind"]=="file" for row in rows); size=sum(row.get("size",0) for row in rows); digest=hashlib.sha256(payload).hexdigest(); assert (len(rows),files,size,digest)==(139,139,337279773,"577ded9ae3a9a8230a9723df6838a348a2e61b3593603c8bfdd4afe2f7ee729e"),(len(rows),files,size,digest); print("IMMUTABLE_TREE PASS entries=139 files=139 bytes=337279773 digest="+digest)'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import hashlib,json; from pathlib import Path; root=Path(".kirby2/research/audit_lab"); expected={"ledger.jsonl":"af75122eee697274c0002360b83faa26395172135c0042f73adff71b63c4b5cf","acceptance_ledger.jsonl":"b928eb1684f7802d115d93e494c2a85a70066716ba864b2f8f95ce4658ebfe39"}; actual={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in expected}; assert actual==expected,(actual,expected); packet_rows=[json.loads(line) for line in (root/"ledger.jsonl").read_text(encoding="utf-8").splitlines()]; acceptance_rows=[json.loads(line) for line in (root/"acceptance_ledger.jsonl").read_text(encoding="utf-8").splitlines()]; assert len(packet_rows)==7 and len(acceptance_rows)==7; assert sum(row.get("packet_id")=="audit-bffd05b9d74bb12b0840bcf0" for row in packet_rows)==1; assert sum(row.get("record_id")=="acceptance-8a34abc8a267b064eaeb" for row in acceptance_rows)==1; assert len(tuple((root/"packets").iterdir()))==7 and len(tuple((root/"acceptance_records").glob("acceptance-*.json")))==7; print("BASELINE_LEDGERS PASS packet_sha256="+actual["ledger.jsonl"]+" acceptance_sha256="+actual["acceptance_ledger.jsonl"]+" inventories=7/7 targets=1/1")'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import json; from pathlib import Path; from kirby2.auditlab import AuditLabStore; root=Path(".kirby2/research/audit_lab"); assert root.is_dir(), "sealed audit store is missing"; store=AuditLabStore(root); packet=store.verify("audit-bffd05b9d74bb12b0840bcf0"); ledgers=store.verify_ledgers(); acceptance=json.loads((packet.directory/"acceptance_record.json").read_text(encoding="utf-8")); provenance=json.loads((packet.directory/"provenance.json").read_text(encoding="utf-8")); report=(packet.directory/"report.txt").read_text(encoding="utf-8"); assert packet.verification_status=="PASS" and packet.schema_version==2 and packet.identity_scope=="IDENTITY_AND_ARTIFACTS" and packet.artifact_count==13 and packet.manifest_sha256=="7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578"; assert ledgers["status"]=="PASS" and not ledgers["failures"] and ledgers["packet_count"]==7 and ledgers["acceptance_record_count"]==7; assert acceptance["record_id"]=="acceptance-8a34abc8a267b064eaeb" and acceptance["reviewer_decision"]=="PENDING_HUMAN_REVIEW"; assert provenance["git_commit"]=="e84047e42f4079c83f9542b2caa66058e7051381" and provenance["working_tree_dirty"] is False; expected=("STRUCTURAL_STATUS PASS","COVERAGE_STATUS PASS","REPLAY_STATUS PASS","DETERMINISM_STATUS PASS","FAULT_STATUS PASS","STATISTICAL_STATUS WARNING","PROVENANCE_STATUS PASS","MANUAL_ACCEPTANCE PENDING_HUMAN_REVIEW","AGGREGATE_STATUS PASS_WITH_WARNINGS","RUNTIME_INVARIANTS PASS"); assert all(value in report for value in expected); print("SEALED_BASELINE PASS packet="+packet.packet_id+" manifest="+packet.manifest_sha256+" ledgers="+str(ledgers["packet_count"])+"/"+str(ledgers["acceptance_record_count"])+" automated=PASS_WITH_WARNINGS manual="+acceptance["reviewer_decision"])'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 demo --seed 42
PYTHONDONTWRITEBYTECODE=1 COLUMNS=80 ./.venv/bin/python -m kirby2 --help
PYTHONDONTWRITEBYTECODE=1 COLUMNS=80 ./.venv/bin/python - <<'PY'
import argparse
import hashlib
import json
from decimal import Decimal
from enum import Enum
from pathlib import Path

from kirby2.__main__ import _parser

BASELINE_COMMANDS = (
    "demo", "latency-demo", "mechanics-demo", "agent-ecology",
    "hidden-liquidity-demo", "multivenue-demo", "benchmark-execution",
    "counterfactual", "simulate", "compare-flow", "inspect-intensity",
    "probe-intensity", "features", "inspect-distribution", "inspect-session",
    "measure-compare", "calibrate", "scenario", "audit-scenarios",
    "audit-hawkes-stability", "audit-strategy-time", "audit-distribution-truth",
    "audit-historical-features", "audit-historical-lessons", "audit-run-store",
    "audit-market-data", "audit-latency", "audit-market-mechanics",
    "audit-hidden-liquidity", "audit-multivenue", "audit-execution-algorithms",
    "audit-counterfactuals", "audit-agent-ecology", "audit-model-risk-lab",
    "audit-lab", "ingest-market-data", "inspect-dataset", "validate-dataset",
    "replay-capability", "record-run", "inspect-run", "query-runs",
    "verify-run", "matrix", "ui", "strategy", "experiment", "layout",
    "replay", "report", "curriculum", "timeline", "lesson-list",
    "lesson-run", "historical",
)
EXPECTED_DIGEST = "8b5da0c0ff5d2e769f7252104f8eb907c91c1653d3c82ebb38c2d93a72770a8e"

def stable(value):
    if value is argparse.SUPPRESS:
        return {"kind": "argparse.SUPPRESS"}
    if value is None or type(value) in {bool, int, float, str}:
        return value
    if isinstance(value, Decimal):
        return {"kind": "Decimal", "value": str(value)}
    if isinstance(value, Path):
        return {"kind": "Path", "value": value.as_posix()}
    if isinstance(value, Enum):
        return {
            "kind": value.__class__.__module__ + "." + value.__class__.__qualname__,
            "value": stable(value.value),
        }
    if isinstance(value, (tuple, list)):
        return [stable(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): stable(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if callable(value):
        return {
            "kind": "callable",
            "value": getattr(value, "__module__", "") + "." + getattr(
                value,
                "__qualname__",
                getattr(value, "__name__", type(value).__qualname__),
            ),
        }
    return {
        "kind": type(value).__module__ + "." + type(value).__qualname__,
        "value": str(value),
    }

def project(parser, path):
    actions = []
    children = []
    for action in parser._actions:
        row = {
            "action": action.__class__.__module__ + "." + action.__class__.__qualname__,
            "choices": None if isinstance(action, argparse._SubParsersAction) else stable(action.choices),
            "const": stable(action.const),
            "default": stable(action.default),
            "dest": action.dest,
            "help": action.help,
            "metavar": stable(action.metavar),
            "nargs": stable(action.nargs),
            "option_strings": list(action.option_strings),
            "required": action.required,
            "type": stable(action.type),
        }
        if isinstance(action, argparse._SubParsersAction):
            row["subcommands"] = list(action.choices)
            for name, child in action.choices.items():
                children.extend(project(child, [*path, name]))
        actions.append(row)
    return [{
        "actions": actions,
        "description": parser.description,
        "epilog": parser.epilog,
        "help_sha256": hashlib.sha256(parser.format_help().encode("utf-8")).hexdigest(),
        "path": path,
        "prog": parser.prog,
    }, *children]

parser = _parser()
root = next(
    action for action in parser._actions
    if isinstance(action, argparse._SubParsersAction)
)
missing = [name for name in BASELINE_COMMANDS if name not in root.choices]
observed_order = tuple(name for name in root.choices if name in BASELINE_COMMANDS)
assert not missing, missing
assert observed_order == BASELINE_COMMANDS, observed_order
rows = []
for name in BASELINE_COMMANDS:
    rows.extend(project(root.choices[name], [name]))
payload = json.dumps(
    {"commands": list(BASELINE_COMMANDS), "parsers": rows},
    sort_keys=True,
    separators=(",", ":"),
)
digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
additions = tuple(name for name in root.choices if name not in BASELINE_COMMANDS)
assert len(BASELINE_COMMANDS) == 55 and len(rows) == 62
assert digest == EXPECTED_DIGEST, digest
assert additions == (), additions
print(
    "LEGACY_CLI_INVENTORY PASS "
    f"commands={len(BASELINE_COMMANDS)} parsers={len(rows)} "
    f"digest={digest} additions=none"
)
PY
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-scenarios
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python - <<'PY'
import json
from kirby2.auditlab.executors import EXECUTOR_REGISTRY
from kirby2.auditlab.generator import generate_configurations
from kirby2.auditlab.kernel import run_generated_case
from kirby2.auditlab.models import CaseRecording, canonical_json, canonical_sha256

rows = []
for configuration in generate_configurations(seed=771, budget=7):
    original = run_generated_case(configuration)
    loaded = CaseRecording.from_dict(
        json.loads(canonical_json(original.recording.as_dict()))
    )
    replayed = EXECUTOR_REGISTRY.replay(loaded)
    assert original.passed and replayed.passed
    assert original.replay_expectations() == replayed.replay_expectations()
    rows.append(
        {
            "lane": configuration.lane.value,
            "recording_sha256": original.recording.sha256,
            "result_sha256": original.result_sha256,
        }
    )
print(
    "LIVE_REPLAY PASS "
    f"lanes={','.join(row['lane'] for row in rows)} "
    f"digest={canonical_sha256(rows)}"
)
PY
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import json,platform,sys,duckdb; value={"duckdb":duckdb.__version__,"platform":{"cache_tag":sys.implementation.cache_tag,"machine":platform.machine(),"python_implementation":platform.python_implementation(),"system":platform.system(),"system_release":platform.release()},"python":platform.python_version()}; print("RUNTIME "+json.dumps(value,sort_keys=True,separators=(",",":")))'
# Repeat both immutable assertions after every smoke command has completed.
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import hashlib,json; from pathlib import Path; root=Path(".kirby2"); rows=[]; [rows.append({"path":p.relative_to(root).as_posix(),"kind":"symlink","target":p.readlink().as_posix()}) if p.is_symlink() else rows.append({"path":p.relative_to(root).as_posix(),"kind":"file","size":len(data),"sha256":hashlib.sha256(data).hexdigest()}) for p in sorted(root.rglob("*"),key=lambda p:p.relative_to(root).as_posix()) if p.is_symlink() or (p.is_file() and not (data:=p.read_bytes()) is None)]; payload=json.dumps(rows,sort_keys=True,separators=(",",":")).encode(); files=sum(row["kind"]=="file" for row in rows); size=sum(row.get("size",0) for row in rows); digest=hashlib.sha256(payload).hexdigest(); assert (len(rows),files,size,digest)==(139,139,337279773,"577ded9ae3a9a8230a9723df6838a348a2e61b3593603c8bfdd4afe2f7ee729e"),(len(rows),files,size,digest); print("IMMUTABLE_TREE PASS entries=139 files=139 bytes=337279773 digest="+digest)'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import hashlib,json; from pathlib import Path; root=Path(".kirby2/research/audit_lab"); expected={"ledger.jsonl":"af75122eee697274c0002360b83faa26395172135c0042f73adff71b63c4b5cf","acceptance_ledger.jsonl":"b928eb1684f7802d115d93e494c2a85a70066716ba864b2f8f95ce4658ebfe39"}; actual={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in expected}; assert actual==expected,(actual,expected); packet_rows=[json.loads(line) for line in (root/"ledger.jsonl").read_text(encoding="utf-8").splitlines()]; acceptance_rows=[json.loads(line) for line in (root/"acceptance_ledger.jsonl").read_text(encoding="utf-8").splitlines()]; assert len(packet_rows)==7 and len(acceptance_rows)==7; assert sum(row.get("packet_id")=="audit-bffd05b9d74bb12b0840bcf0" for row in packet_rows)==1; assert sum(row.get("record_id")=="acceptance-8a34abc8a267b064eaeb" for row in acceptance_rows)==1; assert len(tuple((root/"packets").iterdir()))==7 and len(tuple((root/"acceptance_records").glob("acceptance-*.json")))==7; print("BASELINE_LEDGERS PASS packet_sha256="+actual["ledger.jsonl"]+" acceptance_sha256="+actual["acceptance_ledger.jsonl"]+" inventories=7/7 targets=1/1")'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -c 'import json; from pathlib import Path; from kirby2.auditlab import AuditLabStore; root=Path(".kirby2/research/audit_lab"); assert root.is_dir(), "sealed audit store is missing"; store=AuditLabStore(root); packet=store.verify("audit-bffd05b9d74bb12b0840bcf0"); ledgers=store.verify_ledgers(); acceptance=json.loads((packet.directory/"acceptance_record.json").read_text(encoding="utf-8")); provenance=json.loads((packet.directory/"provenance.json").read_text(encoding="utf-8")); report=(packet.directory/"report.txt").read_text(encoding="utf-8"); assert packet.verification_status=="PASS" and packet.schema_version==2 and packet.identity_scope=="IDENTITY_AND_ARTIFACTS" and packet.artifact_count==13 and packet.manifest_sha256=="7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578"; assert ledgers["status"]=="PASS" and not ledgers["failures"] and ledgers["packet_count"]==7 and ledgers["acceptance_record_count"]==7; assert acceptance["record_id"]=="acceptance-8a34abc8a267b064eaeb" and acceptance["reviewer_decision"]=="PENDING_HUMAN_REVIEW"; assert provenance["git_commit"]=="e84047e42f4079c83f9542b2caa66058e7051381" and provenance["working_tree_dirty"] is False; expected=("STRUCTURAL_STATUS PASS","COVERAGE_STATUS PASS","REPLAY_STATUS PASS","DETERMINISM_STATUS PASS","FAULT_STATUS PASS","STATISTICAL_STATUS WARNING","PROVENANCE_STATUS PASS","MANUAL_ACCEPTANCE PENDING_HUMAN_REVIEW","AGGREGATE_STATUS PASS_WITH_WARNINGS","RUNTIME_INVARIANTS PASS"); assert all(value in report for value in expected); print("SEALED_BASELINE PASS packet="+packet.packet_id+" manifest="+packet.manifest_sha256+" ledgers="+str(ledgers["packet_count"])+"/"+str(ledgers["acceptance_record_count"])+" automated=PASS_WITH_WARNINGS manual="+acceptance["reviewer_decision"])'
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python - <<'PY'
import hashlib
import json
from pathlib import Path

json_path = Path("KIRBY2_WORK_ORDERS_31_40_BASELINE.json")
markdown_path = Path("KIRBY2_WORK_ORDERS_31_40_BASELINE.md")
raw = json_path.read_bytes()
payload = json.loads(raw)
assert raw == json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
assert set(payload) == {"schema_version", "baseline", "immutable_tree", "cli", "runtime", "warnings", "smoke", "limitations"}
assert payload["schema_version"] == 1
expected_baseline = {
    "SEALED_CLOSEOUT_HEAD": "4a962c58feab88f25e5dccfcd85c66dcf8723313",
    "AUDITED_IMPLEMENTATION_COMMIT": "e84047e42f4079c83f9542b2caa66058e7051381",
    "BASELINE_AUDIT_PACKET_ID": "audit-bffd05b9d74bb12b0840bcf0",
    "BASELINE_PACKET_MANIFEST_SHA256": "7300e61b8c133f993daa9279b856e9708fce7de030f023bd31a181a768b98578",
    "BASELINE_ACCEPTANCE_RECORD_ID": "acceptance-8a34abc8a267b064eaeb",
    "AUTOMATED_STATUS": "PASS_WITH_WARNINGS",
    "MANUAL_ACCEPTANCE_STATUS": "PENDING_HUMAN_REVIEW",
}
assert payload["baseline"] == expected_baseline
tree = payload["immutable_tree"]
assert (tree["entry_count"], tree["file_count"], tree["file_bytes"], tree["tree_sha256"]) == (139, 139, 337279773, "577ded9ae3a9a8230a9723df6838a348a2e61b3593603c8bfdd4afe2f7ee729e")
assert (tree["packet_ledger_count"], tree["acceptance_ledger_count"], tree["packet_target_occurrences"], tree["acceptance_target_occurrences"]) == (7, 7, 1, 1)
assert tree["packet_ledger_sha256"] == "af75122eee697274c0002360b83faa26395172135c0042f73adff71b63c4b5cf"
assert tree["acceptance_ledger_sha256"] == "b928eb1684f7802d115d93e494c2a85a70066716ba864b2f8f95ce4658ebfe39"
assert payload["warnings"] == ["calibration_train_vs_holdout", "distribution_drift", "unstable_hawkes"]
cli = payload["cli"]
assert cli["help_columns"] == 80 and cli["command_count"] == 55 and cli["parser_count"] == 62
assert len(cli["command_order"]) == len(set(cli["command_order"])) == 55
assert cli["command_names_sorted"] == sorted(cli["command_order"])
assert len(cli["parser_projection"]) == 62
assert [row["path"][0] for row in cli["parser_projection"] if len(row["path"]) == 1] == cli["command_order"]
projection_bytes = json.dumps({"commands": cli["command_order"], "parsers": cli["parser_projection"]}, sort_keys=True, separators=(",", ":")).encode("utf-8")
assert hashlib.sha256(projection_bytes).hexdigest() == cli["projection_sha256"] == "8b5da0c0ff5d2e769f7252104f8eb907c91c1653d3c82ebb38c2d93a72770a8e"
assert cli["additions"] == []
empty_sha256 = hashlib.sha256(b"").hexdigest()
expected_smoke = [
    {"argv": ["demo", "--seed", "42"], "compatibility_pinned": True, "exit_code": 0, "stdout_sha256": "4657d132082fc48ca2d354b50d591267d542a7fcb03401d3509f91a3c1d4995a", "stderr_sha256": empty_sha256, "terminal_status_line": "RUNTIME_INVARIANTS PASS"},
    {"argv": ["audit-scenarios"], "compatibility_pinned": True, "exit_code": 0, "stdout_sha256": "74b5b8e18f916990936377b08025a8e51560b7d0b28b81cf33645cb3995124a9", "stderr_sha256": empty_sha256, "terminal_status_line": "SCENARIO_AUDIT PASS accepted=12 failures=0"},
    {"argv": ["inline-live-replay", "--seed", "771", "--budget", "7"], "compatibility_pinned": False, "exit_code": 0, "stdout_sha256": "14914d62620fcd7bd1e7656516c65b64db688bb5dacdbf51d5872e801d4ea92e", "stderr_sha256": empty_sha256, "terminal_status_line": "LIVE_REPLAY PASS lanes=CORE_FLOW,MECHANICS,LATENCY,FRAGMENTED,ECOLOGY,ALGORITHM,FAULT digest=5a50577e7d8b64923946762d6efd949eb531da1306ca41ad1e3b217f30bc8b6d"},
]
assert len(payload["smoke"]) == 4 and payload["smoke"][:3] == expected_smoke
model_risk = payload["smoke"][3]
assert model_risk["argv"] == ["audit-model-risk-lab"] and model_risk["compatibility_pinned"] is False and model_risk["exit_code"] == 0
assert model_risk["stderr_sha256"] == empty_sha256 and len(model_risk["stdout_sha256"]) == 64 and all(char in "0123456789abcdef" for char in model_risk["stdout_sha256"])
assert model_risk["terminal_status_line"] == "MODEL_RISK_LAB_AUDIT PASS cases=21 failures=0"
runtime = payload["runtime"]
assert set(runtime) == {"python", "duckdb", "platform"} and all(isinstance(runtime[key], (str, dict)) and runtime[key] for key in runtime)
assert set(runtime["platform"]) == {"cache_tag", "machine", "python_implementation", "system", "system_release"}
assert all(isinstance(value, str) and value for value in [runtime["python"], runtime["duckdb"], *runtime["platform"].values()])
limitation_codes = [row["code"] for row in payload["limitations"]]
assert limitation_codes == ["BASELINE_SCOPE_ONLY", "INHERITED_MANUAL_REVIEW_PENDING", "INHERITED_STATISTICAL_WARNINGS", "NO_NEW_CAPABILITY_CERTIFIED"]
assert all(set(row) == {"code", "detail"} and isinstance(row["detail"], str) and row["detail"] for row in payload["limitations"])
artifact_sha256 = hashlib.sha256(raw).hexdigest()
markdown = markdown_path.read_text(encoding="utf-8")
tokens = [artifact_sha256, *expected_baseline.values(), tree["tree_sha256"], tree["packet_ledger_sha256"], tree["acceptance_ledger_sha256"], *payload["warnings"], *limitation_codes, *runtime["platform"].values(), runtime["python"], runtime["duckdb"], *[row["stdout_sha256"] for row in payload["smoke"]], *[row["terminal_status_line"] for row in payload["smoke"]], "139", "337279773", "55", "62", "7/7"]
assert all(str(token) in markdown for token in tokens)
print("BASELINE_ATTESTATION_CONSISTENCY PASS sha256=" + artifact_sha256)
PY
git diff --check
```

Acceptance: exact baseline identities, verification output, warnings, commands,
environment, help/command inventory, and limitations are recorded; JSON and Markdown
agree; this card creates or modifies no `.kirby2` artifact; and both inherited ledger
byte digests and 7/7 inventories remain unchanged.

Commit: `Attest repaired expansion baseline`

### K2X-02 — Modular command and expansion-audit registration

Objective: stop the already-large CLI dispatcher from growing through another ten
orders while preserving the inventoried existing public command surface.

Owned files:

- create `kirby2/cli/__init__.py`, `kirby2/cli/registry.py`, and
  `kirby2/cli/expansion.py`;
- create `kirby2/audit/expansion.py`;
- modify `kirby2/__main__.py` only at the registration and dispatch seams.

Fixed decisions:

1. Existing command names, their parser defaults, their subcommand help bytes, and
   exit behavior remain unchanged. Root help changes only by deterministic addition
   of the new registered commands. K2X-02 compares against the K2X-01 JSON inventory;
   commands not mechanically moved remain in the legacy dispatcher untouched. Output
   byte comparison selects only K2X-01 smoke rows with
   `compatibility_pinned=true`; unpinned evidence still must retain its declared
   terminal status and exit semantics.
2. Add exactly one expansion parser-registration call at the end of `_parser()` and
   one private-handler dispatch immediately after `parse_args()` and before the
   legacy `if` ladder. Registered parsers use `set_defaults` with a private handler;
   this card does not migrate old business handlers.
3. `CommandRegistry` registers explicit modules in canonical declared order. IDs and
   command names are case-sensitive; duplicates fail during parser construction;
   unknown commands preserve argparse's nonzero refusal. There is no package scan,
   entry-point discovery, filesystem traversal, or import-time write.
4. Add `audit-expansion --gate CARD_ID`. `ExpansionGateRegistry` uses explicit
   case-sensitive canonical card IDs plus recorded `DEV-[0-9]{4}` IDs, rejects
   duplicate/unknown/not-yet-registered gates, and invokes one callable per gate.
   Canonical gates follow slice-index order; deviation gates occur at their recorded
   first-parent insertion point immediately before the interrupted card. `PASS` and
   `PASS_WITH_WARNINGS` return zero; `FAIL` and `NOT_EXERCISED` return nonzero for a
   requested card. Exact requested-card exits are `PASS=0`,
   `PASS_WITH_WARNINGS=0`, `FAIL=1`, and `NOT_EXERCISED=2`; unknown/not-yet-
   registered lookup is reason `NOT_REGISTERED` with exit 2. An overall card may
   still be `PASS` while optional capabilities
   inside its structured report are `NOT_EXERCISED`.
5. Reserve lowercase `all` as the only non-card selector. It executes every registered
   expansion gate once in canonical slice-index order without short-circuiting and
   emits each row plus a deterministic aggregate. Any `FAIL` or overall
   `NOT_EXERCISED` makes the aggregate `FAIL`/nonzero; otherwise any warning makes
   `PASS_WITH_WARNINGS`, else `PASS`. WO40-J additionally requires the registered set
   to equal every implementation card from K2X-02 through WO40-J; a missing/future
   registration is `INCOMPLETE_GATE_SET` and nonzero. `all` also runs every recorded
   deviation gate; WO40-J compares canonical completeness separately and requires
   every deviation record to have exactly one passing registered gate. K2X-00/K2X-01
   remain represented by their immutable charter/baseline evidence, not invented
   retrofit gates.
6. Later cards receive one narrow standing ownership exception: they may add their
   explicit parser/handler registration to `kirby2/cli/expansion.py` and their one
   gate registration to `kirby2/audit/expansion.py`. They may not alter registry
   semantics or the legacy dispatch ladder without an amendment card. WO31-I1,
   WO35-F1, WO40-D1, and WO40-F through WO40-J are the only standing exceptions to
   this standing exception: their generic evidence validators/gates are preregistered
   by WO31-I, WO35-F, or WO40-D before freeze, so those cards add no source
   registration.
7. Every compatibility `format_help()` digest sets `COLUMNS=80` explicitly; ambient
   terminal width is not part of the baseline or K2X-02 comparison.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 COLUMNS=80 ./.venv/bin/python -m kirby2 --help
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 demo --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-scenarios
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate K2X-02
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: every pre-existing subcommand help digest and the exact two-command
compatibility smoke pair (`demo --seed 42` and `audit-scenarios`) matches the K2X-01
output/exit inventory; `audit-model-risk-lab` matches its terminal status/count only;
filtering only the new command still yields 55
top-level commands, 62 parser nodes, and legacy projection digest
`8b5da0c0ff5d2e769f7252104f8eb907c91c1653d3c82ebb38c2d93a72770a8e`;
root help differs only by deterministic additions/wrapping caused by them; registered
dispatch occurs once before the untouched legacy ladder; an unregistered legacy
command falls through; duplicate commands, attempts to shadow legacy names, duplicate
gate IDs, wrong-case IDs, unknown gates, and future gates are refused before audit
execution. Unknown/future refusal uses nonzero status with reason code
`NOT_REGISTERED`, never automated `PASS`/`NOT_EXERCISED`. The registry audit exercises
all four implemented gate status/exit mappings without filesystem writes.

Commit: `Modularize CLI command registration`

## 8. Work Order 31 — Full-day synthetic market composer

### WO31-A — Full-day plan and session-calendar contracts

Objective: freeze the sole immutable full-day runtime IR, outer event envelope,
calendar boundary semantics, and supported composition matrix before any executable
composer or source language is built.

Owned files:

- create `kirby2/full_day/__init__.py`, `kirby2/full_day/models.py`,
  `kirby2/full_day/calendar.py`, `kirby2/full_day/states.py`,
  `kirby2/full_day/events.py`, `kirby2/full_day/composition.py`, and
  `kirby2/full_day/checkpoint_contract.py`;
- create `kirby2/full_day/pilot_limits.toml` and register it as package data in
  `pyproject.toml`;
- create `kirby2/audit/full_day.py`;
- register the audit gate only through the K2X-02 seam.

Fixed decisions:

1. Freeze `FullDayPlan` schema version 1 here. It contains a referenced/versioned
   market profile, referenced/versioned instrument profile plus canonical mechanics
   rules; synthetic local date, IANA timezone name, resolved UTC offsets
   and fold choices; all five phase intervals; bounded volume/liquidity/volatility
   pressure profiles; day/local state definitions; an explicit macro-regime schedule;
   the complete local-regime transition model; participant and event schedules;
   unscheduled-shock policy; halt/reopen rules; root seed/substream policy;
   checkpoint policy; resource/zero-time abort limits; composition profile ID/version;
   and every referenced component configuration digest. WO31-B through WO31-I1 may
   consume this schema but may not add fields to `models.py`, `events.py`, or
   `composition.py`; an omitted field requires an amendment and schema version.
2. The machine IR is strict canonical JSON. TOML is only the later WO32 human source.
   Prices/shares/times are integers and all ratios, weights, rates, and parameter
   modifiers use the exact wire rules in section 3. Unknown fields, floats, implicit
   defaults, noncanonical maps, and ambient configuration fail before identity.
3. `TradingDayCalendarV1` is a synthetic one-local-date calendar, not a holiday or
   exchange-calendar claim. `t=0` is the start of preopen. Preopen,
   opening-auction, continuous, closing-auction, and postclose are contiguous,
   nonempty half-open intervals `[start_us,end_us)` with no overlap or gap. The IANA
   zone, local date/time, selected fold, and resolved UTC offset round-trip and reject
   ambiguous/nonexistent local times unless explicitly resolved.
4. `FullDayRuntime` is the sole session-calendar owner. Its
   `MarketMechanicsEngine` is constructed with an empty `InstrumentRules.session_schedule`
   so the engine cannot independently repeat transitions. Each calendar boundary is
   one explicit operation containing destination state and `uncross_before`; its
   mechanics order is `AUCTION_UNCROSS`, transition-owned order expirations,
   `SESSION_STATE_CHANGED`/`HALT`/`RESUME`, then scheduled GTT expiry. Existing
   mechanics events and their local sequence order are preserved.
5. Scheduled-work ordering uses `(simulation_time_us, microstep, stage_ordinal,
   source_component_id, component_local_sequence)`. Microstep zero stages are:
   0 atomic calendar boundary; 1 scheduled information; 2 day-state transition;
   3 local-state transition; 4 participant activation/deactivation/retune;
   5 previously pending venue arrival; 6 endogenous participant decision;
   7 background-flow proposal; 8 observable/client delivery; 9 feature update;
   10 strategy/algorithm deadline; and 11 checkpoint capture. Synchronous exchange
   consequences are emitted immediately in native local-sequence order as children of
   the work item that caused them, not independently re-sorted into the past. Stable
   source component ID and component-local sequence break ties within a stage.
6. `ScheduledWorkKeyV1` is the queue identity from decision 5; it is not an emitted
   event identity. `FullDayEventV1` is the outer replay contract: schema version,
   global event sequence, integer simulation time, microstep, stage, source component
   ID, component-local sequence, event type, causal-parent IDs, and a typed canonical
   payload. Dequeuing one scheduled work item may emit zero, one, or many outer
   events. The sole global event allocator assigns a new integer separately to every
   emitted outer event in deterministic consequence order; each preserves its native
   subsystem event identity and cites the dequeued work ID or immediately causal
   outer event. Any deferred one-to-many consequence is enqueued as a new
   `ScheduledWorkKeyV1` child at the bounded later microstep required by decision 7.
7. Zero-delay work created while processing a timestamp is scheduled at a strictly
   later microstep at that timestamp, so even an earlier stage can never sort behind
   the already-dequeued key. Calendar and scheduled-information stages execute only
   at microstep zero and cannot be regenerated at that timestamp. The plan's maximum
   microsteps/events-per-time limits are identity-bearing; overflow or a zero-time
   cycle fails with a minimized diagnostic rather than silently advancing time.
8. The atomic stage-0 boundary is one dequeued work item. It emits the ordered
   uncross/expiration/session events from decision 4 completely before stage 1
   information publication, later state/participant stages, or any client delivery at
   that timestamp. A checkpoint cut is quiescent: all work due at or before the cut
   and all generated
   microsteps through the checkpoint stage have completed; no queued item has a key at
   or before the cut; and the event-prefix digest includes the last emitted global
   event. `checkpoint at t=0` follows completion of the t=0 boundary/microsteps, not
   an uninitialized constructor snapshot.
9. `CompositionMatrixV1` has its own schema version, canonical digest, append-only
   profile IDs, component versions, active predicates, dependencies, and explicit
   clock/event/order/RNG/exchange ownership. Adding an adapter creates a new matrix
   version or profile; it never mutates the meaning of an older profile.
10. The initial `SINGLE_VENUE_AGENT_MECHANICS_V1` profile has exactly one
    `FullDayRuntime`-owned `MarketMechanicsEngine`, book, simulation clock, global
    event allocator, and order allocator. WO31-E1 extracts an engine-injected agent
    scheduler from `AgentEcology`; the legacy `AgentEcology` constructor becomes a
    compatibility wrapper over that same scheduler plus its own single engine. The
    full-day runtime never embeds an ecology that owns a second engine or book.
11. `RegimeOrderFlow`, simple/Hawkes/queue-reactive background flow,
    `AsynchronousExecutionSession`, hidden liquidity, multivenue routing, features,
    strategies, algorithms, player overlay, and historical replay are refused in the
    initial profile until a named later adapter and append-only composition profile
    exercise them. There is no generic "enable everything" profile.
12. Seed derivation is versioned SHA-256 over canonical root-seed bytes plus a stable
    semantic path such as `full_day/participant/<participant_id>/<purpose>`.
    Adding/removing an unrelated component cannot perturb existing substreams.
13. Define the complete always-present and conditional checkpoint inventory plus
    component state ownership here. WO31-C defines its portable envelope and WO31-D/E
    implement each adapter; an active omitted component fails closed.
14. `pilot_limits.toml` preregisters WO31-F pilot duration, maximum outer events,
    pending-work items, microsteps/events per timestamp, checkpoint bytes, and
    operational wall-time/memory safety aborts. This card validates only its schema
    and digest. Any later limit change requires a new committed manifest version
    before observing another acceptance pilot.
15. WO32 must compile to this exact IR. WO31 defines neither imports nor inheritance.
    This contract-only card reports overall gate `PASS` with runtime capability
    `NOT_EXERCISED` and reason `RESTORE_NOT_IMPLEMENTED`; it never claims a full day.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-agent-ecology
```

Acceptance: canonical plan/calendar/event/matrix round trips are byte-identical;
relocated roots and map insertion order do not change semantic identity; overlap,
gaps, invalid phase order, DST ambiguity/nonexistence, offset/fold mismatch, backward
time, floats, bad seed labels, duplicate owners, double calendars/books, unsupported
composition, same-time misordering, and nonquiescent cuts fail; the exact mechanics
boundary trace matches existing uncross/expiry/session behavior; runtime capability
is truthfully `NOT_EXERCISED` because this is a contract/refusal slice.

Commit: `Define full day execution contracts`

### WO31-B — Duration-aware hierarchical state

Objective: implement deterministic day and local microstructure states with explicit
age and transitions that influence flow parameters but never command prices.

Owned files:

- create `kirby2/full_day/transitions.py`;
- modify `kirby2/audit/full_day.py`; consume the frozen WO31-A model contracts
  without changing them.

Fixed decisions:

1. Day states: `QUIET`, `NORMAL`, `RISK_ON`, `RISK_OFF`, `EVENT_DRIVEN`,
   `DISORDERLY`.
2. Local states are side-explicit where direction matters: `BALANCED`,
   `BUY_PRESSURE`, `SELL_PRESSURE`, `ABSORPTION_BID`, `ABSORPTION_ASK`,
   `MOMENTUM_UP`, `MOMENTUM_DOWN`, `MEAN_REVERSION`, `LIQUIDITY_WITHDRAWAL`,
   `PANIC`, `RECOVERY`.
3. Duration law is a finite categorical table of integer microsecond values and
   positive integer weights with a declared minimum/maximum. Any expected duration is
   a derived reduced rational for inspection, never an independently editable input.
4. Each transition declares source/successor, minimum-age eligibility, duration-law
   exhaustion behavior, integer weight, trigger ID/version, trigger information class
   (`OBSERVABLE_AT_TIME` or `SYNTHETIC_GROUND_TRUTH`), and bounded parameter effects.
   A trigger cannot read future events, reveal-only fields, or post-transition state.
5. Runtime state records current day/local state, entered time, elapsed age, sampled
   duration/deadline, next eligible transition, trigger memory, component-local
   sequence, and exact transition RNG/substream state. It round-trips through the
   WO31-A canonical wire contract for WO31-C checkpoints.
6. State age advances from simulation time and survives every event. Equal-time
   triggers enter the WO31-A stage order; a transition emits one typed event before
   any newly parameterized participant/flow action.
7. Outputs are bounded exact fixed-point modifiers consumed by existing distributions,
   populations, or schedules. No output contains target price, forced trade, forced
   close, desired return, or an imperative book mutation.
8. This card audits simple bounded mock consumers of the frozen output contract. The
   Hawkes audit is regression evidence only; Hawkes composition remains
   `NOT_EXERCISED` until WO31-E2.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-distribution-truth
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hawkes-stability
```

Acceptance: large and subdivided advances produce identical transitions and event
keys; duration bounds and integer-weight sampling hold; state runtime wire round trips
exactly at pre-eligibility, trigger, and deadline boundaries; zero-time cycles,
forbidden trigger information, invalid graphs, and out-of-range modifiers fail; the
same seed reproduces state events; price can move only through ordinary exchange
activity.

Commit: `Add duration aware market states`

### WO31-C — Portable checkpoint envelope and data paths

Objective: specify one versioned checkpoint artifact and one explicit data-root map
before restoration or full-day persistence.

Owned files:

- create `kirby2/research/paths.py`;
- create `kirby2/runtime_state.py` for neutral component-state vocabulary and strict
  canonical JSON codecs;
- create `kirby2/full_day/checkpoints.py`;
- modify `kirby2/counterfactual/models.py` only for a compatibility re-export or
  explicit conversion from existing `BranchSnapshot` to the neutral vocabulary;
- modify `kirby2/audit/full_day.py`.

Fixed decisions:

1. `DataPaths` is the one path provider reused by WO31-40. From an explicit resolved
   root it derives non-aliasing contained paths for immutable runs/evidence,
   checkpoints, installed packs, erasable identity mappings, configuration, cache,
   staging, backups, diagnostics, and release artifacts. Construction and validation
   create nothing; an explicit `ensure(area_ids)` creates only requested directories.
   Reject symlink/rebind escape, absolute child paths, `..`, containment collision,
   case/Unicode collision, and a file where a directory is required.
2. `RuntimeComponentStateV1` has only `PRESERVED` or `ABSENT`. `PRESERVED` carries
   component ID/schema/implementation version, canonical state, state digest, and
   dependencies. `ABSENT` carries a stable reason proven by the exact composition
   plan. Unknown status, missing active state, preserved inactive state, duplicate ID,
   dependency cycle, and digest mismatch fail.
3. `RuntimeCheckpointV1` records checkpoint schema, engine/runtime compatibility,
   compiler identity or literal `ABSENT_NATIVE_PLAN`, semantic plan and composition
   digests, quiescent cut time/microstep/global sequence, event-prefix digest,
   ordered component inventory, and component records. It cannot claim compatibility
   with an unknown Python/RNG-state encoding or component implementation version.
4. Canonical state is strict JSON data only—no pickle, repr, object hook, code name,
   NaN/infinity, or platform-native binary. Owned PRNG state has an explicit
   algorithm/codec/runtime-compatibility record and validated integer structure; an
   incompatible runtime fails rather than reseeding.
5. Required inventory includes the sole clock/calendar cursor/global allocator,
   exchange/order allocator, each active venue, mechanics/auction, RNG substreams,
   state-age runtime, scheduled/pending event queues, agents/metaorders, flow/Hawkes,
   latency/client messages, player/working orders, features, strategies/algorithms,
   observable publication cursor, and ledger prefix. The frozen composition profile
   determines which conditional records must be `PRESERVED` versus `ABSENT`.
6. Checkpoint identity hashes a canonical semantic projection that excludes file path,
   display metadata, and any self-digest field. Exact serialized bytes receive an
   `artifact_sha256` in the containing `ArtifactReference`; never place a digest
   inside the bytes it hashes. Relocation therefore preserves checkpoint identity but
   not an unverified transport substitution.
7. The cut must satisfy WO31-A quiescence and bind the last global event sequence and
   prefix digest. A t=0 checkpoint is taken after all t=0 boundary microsteps.
   Branching creates a new child reference to this immutable checkpoint; it does not
   rewrite or relabel the parent.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: full, inactive, missing-active, preserved-inactive, corrupt, duplicate,
dependency-cycle, wrong-runtime/RNG-codec, unknown-schema, nonquiescent-cut, t=0,
self-digest, and path-relocated checkpoint fixtures produce declared results;
canonical JSON round trips byte-for-byte; data-root construction writes nothing and
its areas cannot alias, escape, or be rebound through symlinks.

Commit: `Define portable runtime checkpoints`

### WO31-D — Core-session fresh-process restoration

Objective: restore only the authoritative single-venue mechanics core from canonical
serialized state in a fresh process rather than regenerating its prefix from a seed.

Owned files:

- create `kirby2/full_day/restore.py` and `kirby2/full_day/restore_worker.py`;
- minimally modify exactly `kirby2/exchange/book.py`,
  `kirby2/exchange/mechanics_engine.py`, `kirby2/exchange/auction.py`,
  `kirby2/session/events.py`, `kirby2/simulation/clock.py`, and
  `kirby2/player/position.py` to add validated state codecs/constructors;
- modify `kirby2/audit/full_day.py`.

Fixed decisions:

1. Scope is the one `MarketMechanicsEngine`: `OrderBook`, `EventJournal`, continuous
   and auction orders, managed-order records, session state/schedule cursor, clock,
   last trade, player auction and continuous fills/position, and every event/trade/
   arrival/resting/command/order allocator. No background flow, agent scheduler,
   latency, feature, strategy, algorithm, multivenue, or historical cursor is added or
   claimed in this card.
2. Each owner emits strict canonical state and has one validation-first constructor.
   Rehydrate new owned objects; never retain caller-owned mutable mappings/lists or
   patch private fields from an unvalidated payload.
3. Restore allocators exactly; require contiguous event prefixes, unique active and
   historical order IDs, valid FIFO/resting order, consistent managed/core/auction
   quantities, and no allocator below an observed maximum.
4. `PlayerPosition` serialization includes ordered canonical fill history as well as
   bought/sold/net totals. Restore recomputes and reconciles totals from fills and
   cross-checks mechanics auction accounting; there is no invented cash field.
5. `restore_worker.py` is a documented fresh-interpreter protocol: it reads one
   checkpoint plus canonical suffix-command array from UTF-8 JSON stdin, writes one
   canonical JSON result to stdout, writes diagnostics to stderr, and exits nonzero
   on schema/digest/invariant failure. It imports no test fixture state or source
   prefix and performs no filesystem write.
6. Fresh-process acceptance compares only the suffix after the bound checkpoint:
   outer/local event bytes, final mechanics/book/position state, fill records,
   allocator values, observables, and invariant digest. The checkpoint prefix is
   verified by digest but not re-emitted into the suffix comparison.
7. Regeneration from time zero is replay, not restoration. Matching only final state
   is insufficient. Core RNG is `ABSENT` unless this exact core owns a stochastic
   component; no fake RNG state is serialized to fill inventory.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 demo --seed 42
```

Acceptance: fresh-process restores at post-t=0 quiet time, auction order/imbalance,
post-uncross, partial fill, working player order, queued FIFO depth, halt, and reopen
boundaries match uninterrupted suffixes byte-for-byte; duplicate IDs, corrupt FIFO,
mutated fill history, bad allocator, noncanonical wire, and prefix regeneration fail.

Commit: `Restore deterministic core sessions`

### WO31-E1 — Mechanics and agent-spine restoration

Objective: create the authoritative full-day scheduling kernel and restore the
selected single-venue mechanics plus an injected agent scheduler without ever owning
two clocks, engines, books, or allocators.

Owned files:

- create `kirby2/full_day/runtime.py`, `kirby2/full_day/components.py`, and
  `kirby2/full_day/components_mechanics.py`;
- minimally add validated state adapters to `kirby2/exchange/mechanics_engine.py`,
  `kirby2/exchange/auction.py`, `kirby2/agents/ecology.py`,
  `kirby2/agents/models.py`, `kirby2/agents/families.py`, and
  `kirby2/agents/populations.py` where inventory or schedule state lives;
- modify `kirby2/full_day/restore.py` and `kirby2/audit/full_day.py`.

Fixed decisions:

1. `FullDayRuntime` owns the WO31-A plan/calendar, one `SimulationClock`, scheduling
   heap, global sequence allocator, quiescent-cut controller, and exactly one
   `MarketMechanicsEngine`. Its only executable profile in this card is
   `SINGLE_VENUE_AGENT_MECHANICS_V1`.
2. Extract an engine-injected `AgentScheduler` (or equivalently named narrow owner)
   from `AgentEcology`. It owns agent policies, inventories, pending decisions,
   participant activation state, metaorders, and labeled RNG substreams, but accepts
   the runtime's engine/clock/order interface. The legacy `AgentEcology` public API
   constructs one engine plus this scheduler and preserves its old audit bytes.
3. The runtime advances calendar boundaries and outer stages; the scheduler proposes
   ordinary requests only. Neither scheduler nor compatibility wrapper may advance a
   second clock, construct a second book, allocate global events, or uncross outside
   the boundary operation.
4. Component adapters declare schema/implementation version, active predicate,
   dependencies, snapshot, validation, restore, canonical state digest, and exact
   owner IDs. Restore dependency order is explicit and acyclic.
5. Exercise auction imbalance/uncross, halt/reopen, participant activation/withdrawal,
   active metaorder, agent inventories, next scheduled decision, substream state,
   order allocator, exchange queues, and same-time microsteps at separate
   fresh-process cuts.
6. Components outside this profile are `ABSENT` with stable composition-derived
   reasons. An active omitted component, duplicate owner, pending unowned event, or
   compatibility-wrapper engine inside the runtime fails closed.
7. This card upgrades `SINGLE_VENUE_AGENT_MECHANICS_V1` to restorable but does not
   claim flow, latency, hidden, multivenue, feature, strategy, algorithm, player, or
   historical composition.
8. After all composed fresh-process evidence passes, publish a new append-only matrix
   version promoting `ENGINE_MARKET_MECHANICS_V1`, `AGENT_SCHEDULER_V1`, and
   `SINGLE_VENUE_AGENT_MECHANICS_V1` to `EXECUTABLE`; every unrelated entry keeps its
   prior status/reason.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-agent-ecology
```

Acceptance: existing ecology output remains byte-identical; the selected runtime
spine has one owner for each clock/engine/book/allocator and restores in a fresh
process with exact suffix digests at every named boundary; one-shot versus subdivided
advance is identical; inactive runtimes remain `ABSENT`; double-engine, double-
calendar, unowned-event, and compatibility-wrapper smuggling fixtures fail.

Commit: `Restore full day mechanics and agents`

### WO31-E2 — Flow, Hawkes, and queue-reactive restoration

Objective: add explicit adapters for the independent background-flow runtimes without
letting them bypass participant/exchange interfaces or share implicit RNG ownership.

Owned files:

- create `kirby2/full_day/components_flow.py`;
- minimally modify `kirby2/simulation/flow.py`,
  `kirby2/simulation/flow_models.py`, `kirby2/simulation/regimes.py`,
  `kirby2/simulation/queue_reactive.py`, and `kirby2/simulation/rng.py`;
- modify `kirby2/full_day/runtime.py`, `kirby2/full_day/restore.py`, append new
  profiles to `kirby2/full_day/composition.py`, and modify
  `kirby2/audit/full_day.py`.

Fixed decisions:

1. Add append-only profile `SINGLE_VENUE_AGENT_FLOW_V1`. It extends the E1 owner
   graph with exactly one plan-selected adapter ID from `FLOW_SIMPLE_V1`,
   `FLOW_HAWKES_V1`, or `FLOW_QUEUE_REACTIVE_V1` and retains the same sole
   runtime/mechanics engine/clock/allocator owners; multiple simultaneous background
   flow adapters are refused in this version.
2. Refactor each selected flow model behind a proposal interface that, given the
   explicit observation cut and owned state, returns a typed next-time/order/cancel
   proposal. It never calls its legacy runner's clock/book directly. `FullDayRuntime`
   enqueues the proposal and submits it through the authoritative mechanics interface.
3. Adapters preserve model ID/version, intensity state, Hawkes excitation/last-decay
   time, queue-reactive retained windows and observation cutoff, pending proposal,
   diagnostic draw sequence, and every labeled RNG state.
4. Background flow never shares an agent substream, reads reveal-only/future fields,
   commands a price, or bypasses ordinary order/cancel validation. Queue-reactive
   observations are time-bound immutable projections from the sole engine.
5. Adding/removing/retuning an unrelated participant cannot reseed or reorder existing
   flow streams. A rejected order is recorded and advances state exactly as declared;
   retry loops may not consume an unbounded number of draws.
6. Simple, Hawkes, queue-reactive, inactive, corrupt, wrong-model, second-owner,
   stale-observation, and pending-proposal restore cases are distinct.
7. After every selected flow adapter passes composed restoration, publish a new matrix
   version promoting `FLOW_SIMPLE_V1`, `FLOW_HAWKES_V1`,
   `FLOW_QUEUE_REACTIVE_V1`, and `SINGLE_VENUE_AGENT_FLOW_V1` to `EXECUTABLE` for
   their exact allowed selection; older matrix meaning is unchanged.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E2
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hawkes-stability
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-distribution-truth
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-scenarios
```

Acceptance: each named profile has exactly one exchange owner; supported flow models
restore across active excitation/window/pending-proposal boundaries with exact outer
suffix and draw-trace digests; one-shot/subdivided advance and unrelated-participant
perturbation preserve unaffected streams; invalid ownership or observation time fails.

Commit: `Restore full day flow models`

### WO31-E3 — Latency and observable-delivery restoration

Objective: preserve the separate venue-truth and client-knowledge timelines through
pending acknowledgement, delivery, cancel, and fill races.

Owned files:

- create `kirby2/full_day/components_delivery.py`;
- minimally modify `kirby2/latency/engine.py`, `kirby2/latency/models.py`,
  `kirby2/latency/replay.py`, `kirby2/observability/queue.py`,
  `kirby2/observability/venue.py`, and `kirby2/observability/replay.py`;
- modify `kirby2/full_day/runtime.py`, `kirby2/full_day/restore.py`, append
  `SINGLE_VENUE_AGENT_FLOW_DELIVERY_V1` to
  `kirby2/full_day/composition.py`, and modify `kirby2/audit/full_day.py`.

Fixed decisions:

1. Extract `DELIVERY_ASYNC_V1` as a passive venue-arrival/client-delivery scheduler
   over the authoritative runtime. It receives the runtime clock, command proposals,
   and mechanics events; it may not instantiate `AsynchronousExecutionSession`, a
   second clock/book, or an independent global allocator.
2. `SINGLE_VENUE_AGENT_FLOW_DELIVERY_V1` selects exactly one E2 flow adapter and one
   delivery adapter. Commands enter the venue only through due-arrival work; exchange
   truth produces separately scheduled client messages. The outer event envelope
   references rather than rewrites subsystem-local sequences.
3. Preserve latency profile/version, RNG/draw state, venue-receipt queue,
   client-delivery queue, pending cancels/acknowledgements/fill reports,
   message/route allocators, client-known working-order state, observable publication
   cursor, and source/venue/client timestamps.
4. Ground truth, venue receipt, and client observables have separate canonical
   payloads and dependencies. No observed projection is derived by merely filtering a
   future-complete truth record.
5. Restore never delivers a pending message early, twice, or at a different outer
   stage/microstep. Exercise pending acknowledgement, partial fill, cancel/fill race,
   stale quote, and simultaneous delivery/calendar boundary.
6. Latency-free profiles prove the component `ABSENT`; an empty active queue cannot
   masquerade as absence. Unknown message kinds, orphan causal IDs, and pending items
   at/before a quiescent cut fail.
7. After composed restoration passes, publish a new matrix version promoting
   `DELIVERY_ASYNC_V1` and `SINGLE_VENUE_AGENT_FLOW_DELIVERY_V1` to `EXECUTABLE`.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E3
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hidden-liquidity
```

Acceptance: the delivery profile has one authoritative clock/book; fresh-process
suffixes preserve truth, venue receipt, feed/message timing, outer ordering, and
decision-time knowledge exactly; latency-free and delayed variants are distinct; no
hidden field, future delivery, duplicate message, or second-session owner crosses the
restore boundary.

Commit: `Restore full day delivery state`

### WO31-E4 — Feature, strategy, and player restoration

Objective: attach the passive feature/strategy/player consumer to the authoritative
single-venue E3 spine and prove exact observable-decision restoration.

Owned files:

- create `kirby2/full_day/components_research.py`;
- minimally modify `kirby2/features/engine.py`, `kirby2/features/models.py`,
  `kirby2/strategy/runtime.py`, `kirby2/strategy/state_machine.py`, and
  `kirby2/player/position.py` for the passive single-venue adapter;
- modify `kirby2/full_day/runtime.py`, `kirby2/full_day/restore.py`, append only the
  named profile to `kirby2/full_day/composition.py`, and modify
  `kirby2/audit/full_day.py`.

Fixed decisions:

1. `FEATURE_STRATEGY_PLAYER_V1` consumes only the E3 client-observable cutoff. It
   preserves feature windows/provenance, traffic-light/state-machine state/timers,
   player working orders, quantity position, fill history, pending decisions, and each
   decision's information cutoff. Kirby2 has no cash-ledger contract here; do not
   invent or claim cash state.
2. Append `SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1`. It adds only this passive
   adapter to E3 and leaves the runtime/mechanics engine as sole exchange owner.
   Actions return ordinary requests through delivery/venue stages; they cannot read
   truth/reveal fields or call the book directly.
3. Restore recorded causal windows and decisions, never postmortem recomputations from
   a future-complete ledger. Recomputed diagnostics are separately labeled and
   excluded from suffix identity.
4. Unknown feature/strategy versions, orphan working orders, future cutoffs, and
   player/fill conservation failures stop before a suffix action. Historical cursor is
   `ABSENT` with `SYNTHETIC_PLAN_HAS_NO_HISTORICAL_CURSOR`.
5. After fresh-process restoration passes, publish one append-only matrix version
   promoting `FEATURE_STRATEGY_PLAYER_V1` and
   `SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1` to `EXECUTABLE`; multivenue,
   algorithms, and historical remain at their previous refused/contract status.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E4
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
```

Acceptance: the profile restores exact observable features, decisions, timers,
working orders, fills, and quantity position on one authoritative engine; hidden-state
leakage, future recomputation, invented cash, and direct-book access fail.

Commit: `Restore full day observable research state`

### WO31-E5 — Standalone multivenue and hidden-liquidity restoration

Objective: prove fresh-process restoration for the existing multivenue/hidden owner
without pretending it can coexist with the WO31 single-engine profile.

Owned files:

- create `kirby2/full_day/components_multivenue.py`;
- minimally modify `kirby2/multivenue/coordinator.py`,
  `kirby2/multivenue/venue.py`, `kirby2/multivenue/models.py`,
  `kirby2/observability/venue.py`, and `kirby2/observability/models.py`;
- modify `kirby2/full_day/restore.py`, append only named component/profile rows to
  `kirby2/full_day/composition.py`, and modify `kirby2/audit/full_day.py`.

Fixed decisions:

1. `VENUE_MULTIVENUE_HIDDEN_V1` preserves every venue/hidden-reserve book, coordinator
   route and pending leg, consolidated feed, allocator, truth cursor, and observable
   cursor in a standalone fresh process.
2. Hidden reserve never enters observable payloads. Venue/consolidated and player/fill
   conservation, pending-route order, and exact suffix identity are rechecked after
   restore.
3. The component is `RESTORABLE_COMPONENT_ONLY`.
   `MULTIVENUE_HIDDEN_RESEARCH_V1` remains `CONTRACT_ONLY` because these venue owners
   replace rather than wrap the selected `MarketMechanicsEngine`; construction of both
   exchange owners fails before mutation.
4. Unknown venue/schema versions, orphan routes, early delivery, or reserve leakage
   fail closed. Historical mixing remains refused.
5. Publish one append-only matrix version adding those exact statuses without changing
   E4's executable single-venue profile.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E5
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
```

Acceptance: every standalone multivenue/hidden suffix restores exactly with reserve
privacy and conservation intact; component-only status is explicit; double exchange
ownership and mixed historical/full-day claims fail.

Commit: `Restore full day multivenue state`

### WO31-E6 — Standalone execution-algorithm restoration

Objective: restore the existing execution-algorithm schedule/tracker as a standalone
component without claiming a composed WO31 full-day algorithm runtime.

Owned files:

- create `kirby2/full_day/components_algorithms.py`;
- minimally modify `kirby2/algorithms/models.py`, `kirby2/algorithms/policies.py`, and
  `kirby2/algorithms/benchmark.py` for schedule/tracker state;
- modify `kirby2/full_day/restore.py`, append only the algorithm rows to
  `kirby2/full_day/composition.py`, and modify `kirby2/audit/full_day.py`.

Fixed decisions:

1. `EXECUTION_ALGORITHM_V1` preserves policy ID/version, objective/risk parameters,
   schedule progress, child orders/fills, client latency/venue state, pending deadline/
   action, allocator state, and tracker metrics.
2. Restore uses the recorded information cutoff and outstanding child-order state; it
   cannot recompute a decision from later observations or bypass venue/client stages.
3. Status is exactly `RESTORABLE_COMPONENT_ONLY`; it is not a full-day executable
   profile in WO31. Historical replay remains `REFUSED`.
4. Unknown policy/schema versions, objective mismatch, quantity nonconservation,
   orphan child orders, and late/early deadlines fail before suffix execution.
5. Publish one append-only matrix version adding the algorithm component status while
   preserving every prior profile/status byte.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E6
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-execution-algorithms
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-latency
```

Acceptance: algorithm schedule, deadlines, child orders/fills, latency state, and
metrics restore exactly in a fresh process; quantity conservation holds; any
full-day/composed/historical capability overclaim is refused.

Commit: `Restore full day algorithm state`

### WO31-F — Full-day composition runtime

Objective: complete calendar, state, participant, scheduled-event, shock, microstep,
and checkpoint orchestration over the E1 authoritative runtime kernel, executing only
composition profiles already promoted to `EXECUTABLE`.

Owned files:

- create `kirby2/full_day/participants.py`, `kirby2/full_day/scheduled.py`, and
  `kirby2/full_day/shocks.py`;
- modify `kirby2/full_day/runtime.py`, `kirby2/full_day/transitions.py`, and
  `kirby2/audit/full_day.py`; consume the frozen WO31-A models/events/matrix;
- any additional existing call site requires the section-5.5 amendment protocol.

Fixed decisions:

1. Participant schedules version and activate/retune/deactivate market makers, noise
   flow, metaorders, distressed flow, liquidity providers, and auction participants at
   deterministic boundaries. Deactivation prevents future decisions and cancels
   working orders through ordinary cancellation; inventory/history remain. Retuning
   creates a new immutable spec version and cannot rewrite prior events. The required
   profile behavior is explicit: makers increase quoting activity near the open;
   scheduled metaorders begin and end at declared boundaries; liquidity providers
   withdraw through their normal cancel/retune interface during shocks; auction
   participants activate near the close; noise-flow intensity falls at midday; and
   distressed flow activates after its event. None bypasses ordinary order, cancel,
   auction, venue, or agent interfaces.
2. Scheduled types are exactly economic announcement, earnings-like release, news
   shock, large scheduled metaorder, auction imbalance publication, volatility
   interruption, halt, and reopening. Each has a typed information/population/
   mechanics/bounded-parameter payload and declared outer stage; invalid phase/type
   combinations fail.
3. Deterministic unscheduled shocks derive only from the plan's labeled shock
   substream and bounded policy. Same plan/seed yields identical candidate draws,
   accepted shocks, timing, payload, events, and final state. Rejection/exclusion is
   recorded without an unbounded resampling loop.
4. Events alter information, participant availability, normal mechanics controls, or
   bounded flow parameters. They never write book/mid/last price, force a trade,
   liquidate inventory, guarantee recovery, select a desired return, or bypass normal
   agent/order/cancel/auction interfaces.
5. Every outer envelope uses the WO31-A global key/sequence and references the exact
   subsystem event type/local sequence; subsystem ledgers are neither renumbered nor
   rewritten. Same-time consequences finish in bounded later microsteps and every
   checkpoint cut is quiescent.
6. Run the exact digest-bound WO31-A `pilot_limits.toml` workload before the complete
   day. Deterministic event/pending/checkpoint limits are correctness gates; wall-time
   and peak-memory are operational diagnostics/safety aborts excluded from semantic
   identity. Report the bottleneck before any streaming rewrite.
7. Run one complete bounded day only under
   `SINGLE_VENUE_AGENT_FLOW_DELIVERY_STRATEGY_V1`, if E4 promoted it to
   `EXECUTABLE`, plus shorter cases for every other executable single-venue profile.
   `MULTIVENUE_HIDDEN_RESEARCH_V1`, historical replay, and standalone execution-
   algorithm adapters stay `NOT_EXERCISED` in WO31.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-F
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-distribution-truth
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-agent-ecology
```

Acceptance: the preregistered pilot and one complete bounded day preserve runtime
invariants, one-shot/subdivided equality, exact outer/subsystem sequences, participant
history, session mechanics, quiescent checkpoints, fresh-process suffix equality, and
the no-target-price rule; every nonexecutable profile is refused with its matrix
reason; operational diagnostics do not perturb replay identity.

Commit: `Compose deterministic full trading days`

### WO31-G — Full-day storage, inspection, seek, and summaries

Objective: expose durable full-day runs and prove close/reopen/seek/extract behavior.

Owned files:

- create `kirby2/full_day/store.py`, `kirby2/full_day/summary.py`, and
  `kirby2/full_day/commands.py`;
- create `kirby2/full_day/examples/audit_full_day_plan.json`;
- modify exactly `kirby2/research/models.py`, `kirby2/research/store.py`, and
  `kirby2/research/tables.py` only as required for `RunType.FULL_DAY` and typed
  full-day artifact references;
- modify `kirby2/audit/full_day.py` and `pyproject.toml` package data.

Fixed decisions:

1. The existing research run manifest gains `RunType.FULL_DAY` plus typed
   `ArtifactReference` entries for plan, outer event ledger, subsystem ledgers,
   checkpoint index/files, summary, qualification, and diagnostics. There is one
   append-only run ledger and one canonical run directory; no parallel full-day
   ledger or mutable `latest` pointer becomes identity.
2. Every store write uses `DataPaths`, revalidates resolved containment/symlinks
   immediately before staging and activation, writes a fresh staging directory,
   fsyncs as declared by the store contract, verifies bytes/digests, then atomically
   activates. A failed write cannot expose a partial run.
3. Checkpoint index entries are typed artifact references with cut time, last global
   key/sequence, prefix digest, checkpoint semantic digest, exact artifact SHA-256,
   schema/runtime compatibility, and relative contained path.
4. `seek` chooses the greatest compatible quiescent checkpoint whose cut is at or
   before the target, restores it, and processes all work with time `<= target` to a
   new quiescent cut. A target before the first cut uses the explicit initialization
   checkpoint. Seeking never samples a new schedule or returns a half-processed
   timestamp.
5. `extract-window` creates an immutable child artifact binding parent run, source
   checkpoint, event-prefix digest, window/reveal policy, and observable context.
   Hidden schedule and RNG/checkpoint state remain sealed lineage required for exact
   replay/branching; they are not placed in a public assessment payload or returned by
   the ordinary inspect command.
6. `DaySummaryV1` defines open/high/low/close as first/max/min/last executed trade
   ticks in global event order; a no-trade day or phase reports `UNAVAILABLE`, never a
   quote or zero. Volume and trade count sum executed trades. Spread and N-level depth
   are time-weighted over half-open quote intervals; phase volatility uses declared
   integer-tick return/difference formula, sample denominator, fixed-point scale, and
   rounding. State occupancy sums integer microseconds. Halts/auctions/participation,
   liquidity withdrawals, and absolute price moves use explicit deterministic
   eligibility/tie-break rules. Summary also records invariant status and replay
   digest; every formula/version is in its schema.
7. Register a complete non-persisting `audit-full-day` command for the implemented WO31
   gates in addition to the individual expansion-card gates.
8. Audit cases create their own clean `TemporaryDirectory`; public commands require
   an explicit data root. No acceptance depends on ephemeral output from a prior card.
9. Public commands are `generate-day`, `inspect-day`, `seek`, and `extract-window`.
   The audit captures the generated content-derived run ID and passes it explicitly;
   `--latest` is not an acceptance mechanism.
10. The committed audit plan is canonical JSON because it is native machine IR; it is
   not mislabeled TOML source. Human-authored TOML arrives only in WO32.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 full-day-storage-demo --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-G
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-full-day
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: a complete session is stored, closed, reopened in a fresh process, sought
before/at/after phase, microstep, fill, halt, and delivery boundaries, replayed with
identical declared digests, and extracted with coherent prior volume/volatility/
inventory/participant context; no public artifact leaks sealed schedule/RNG truth;
no-trade and summary tie fixtures are exact; corrupt/escaped/partial artifacts fail.
The audit captures the content-derived run ID programmatically and never uses
`--latest` or stale fixed `/tmp` contents.

Commit: `Store inspect and seek full trading days`

### WO31-H — Preregistered full-day profile and envelope manifest

Objective: freeze four causal-pressure candidates, seed partitions, behavioral
envelopes, window sampling, metrics, and performance workloads before qualification.

Owned files:

- create `kirby2/full_day/profiles.py`;
- create `kirby2/full_day/profile_candidates.toml`,
  `kirby2/full_day/profile_envelopes.toml`, and
  `kirby2/full_day/performance_thresholds.toml`;
- modify `kirby2/audit/full_day.py` only to validate manifests without generating
  qualification days;
- modify package data in `pyproject.toml`.

Fixed decisions:

1. Candidate IDs: `QUIET_RANGE_PRESSURE`, `TREND_PRESSURE`,
   `EVENT_SHOCK_PRESSURE`, and `DISORDERLY_OPEN_STABILIZATION_PRESSURE`.
2. Each records the source-requested display label (`QUIET_RANGE_DAY`, `TREND_DAY`,
   `EVENT_SHOCK_AND_RECOVERY`, or `DISORDERLY_OPEN_STABILIZING`) as a requested
   hypothesis, not a guaranteed realized path or accepted realism claim. Identity and
   automation use the causal-pressure ID.
3. Bind disjoint fixed development, qualification, and holdout seeds; exact plan,
   composition, and matrix digests; metric/formula versions; integer/fixed-point units;
   estimators, denominators, rounding, missing-data outcomes, warning/failure rules,
   stopping rules, behavioral envelopes, and resource aborts. No seed is generated,
   replaced, or inspected in this card.
   Every constant and algorithm is exactly section 5.7.1-5.7.2; the manifests only
   transcribe that normative policy.
   Any plan eligible for the later `BOUNDED_SEARCH_CONTROLLED_V1` source also validates
   that every local transition row contains at least two positive destinations, sums
   exactly to `S`, and gives each destination at least `200001`; an ineligible
   candidate cannot be selected for that downstream role.
4. Bind deterministic applicable window strata across open, ordinary morning, midday,
   event/post-event, ordinary afternoon, and close. Sampling uses a separate labeled
   review RNG that cannot consume generator streams. A quiet profile may mark an event
   stratum `NOT_APPLICABLE`; qualification cannot manufacture an event to fill it.
5. Bind window count, eligibility, selection/tie-break algorithm, blind fields,
   review-packet ordering, reviewer rubric, and selected-window-manifest schema before
   any qualification window is observed.
6. Performance preregistration separates deterministic workload/count/byte gates,
   operational safety aborts, and platform-qualified wall-time/peak-memory/throughput
   thresholds. It names an exact eligible platform predicate/fingerprint. Eligible
   results are `PASS | WARNING | FAIL`; other machines report `UNSUPPORTED | NOT_RUN`
   plus raw diagnostics—never a vague hardware-normalized pass.
7. This card performs schema, digest, partition, and internal-consistency validation
   only. It may use declared development evidence but does not execute qualification/
   holdout seeds, select their windows, or alter policy after observing outcomes.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-H
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hawkes-stability
```

Acceptance: all three manifests and four candidate definitions are canonical,
complete, internally valid, committed, and digest-bound before WO31-I1; seed sets are
disjoint; applicable strata, formulas, workload identity, platform predicate, and
aggregation are explicit; qualification, holdout, platform performance, automated
readiness, and human acceptance remain `NOT_EXERCISED`/`PENDING` as applicable.

Commit: `Preregister full day profile envelopes`

### WO31-I — Frozen profile-qualification machinery

Objective: implement, audit, and commit all qualification, review-packet, performance,
and evidence-verification code before any WO31-H qualification or holdout seed is run.

Owned files:

- create `kirby2/full_day/qualification.py`, `kirby2/full_day/review.py`,
  `kirby2/full_day/performance.py`, and
  `kirby2/full_day/fixtures/qualification_development.toml`;
- modify `kirby2/full_day/commands.py`, `kirby2/audit/full_day.py`,
  `kirby2/research/models.py`, `kirby2/research/store.py`, and
  `kirby2/research/tables.py` only for typed qualification artifacts;
- preregister the generic evidence validator/gate for WO31-I1 through the K2X-02 seam.

Fixed decisions:

1. Resolve and verify the exact committed WO31-H manifests, but refuse every real
   qualification/holdout seed while the WO31-I worktree is dirty or HEAD is not the
   exact clean committed WO31-I implementation commit. Only WO31-I1 may execute those
   seeds against that exact clean HEAD and a fresh evidence destination.
2. Implement all arithmetic, denominators, abort handling, one-time reveal token,
   review-only RNG/window selection, blinded packet rendering, immutable persistence,
   replay verification, platform classification, and reviewer-sidecar rules now.
3. The development fixture uses disjoint explicitly labeled development-only seeds
   and toy envelopes that are absent from every WO31-H qualification/holdout set. It
   exercises every success/failure/refusal branch without estimating a real candidate
   disposition.
4. The public qualification command requires a clean committed implementation HEAD,
   exact WO31-H manifest/preregistration commit, a fresh evidence destination, and an
   unused qualification identity. Re-entry verifies stored evidence only; it never
   reruns or overwrites a one-time holdout.
5. Engineering, behavioral-envelope, statistical, platform-performance, automated
   disposition, and human-review statuses are separate. Only a reviewer sidecar may
   set human `ACCEPTED`, `REJECTED`, `NEEDS_EDIT`, or `SUPERSEDED`.
6. WO31-I1's pre-registered gate reports `NOT_EXERCISED` until the exact immutable
   evidence appears. Model-risk/release aggregation reads that evidence; it cannot
   regenerate it.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 full-day-qualification-dev-demo --fixture kirby2/full_day/fixtures/qualification_development.toml
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-I
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: development-only fixtures exercise every computation, refusal, persistence,
and verification path; qualification/holdout access remains absent from the ledger;
the committed implementation and preregistered WO31-H bytes are sufficient for the
next evidence-only card without a source change.

Commit: `Implement full day qualification gates`

### WO31-I1 — Evidence-only profile qualification and measured performance

Objective: execute the already committed WO31-H manifests once with the frozen WO31-I
implementation, preserving every result without changing code or thresholds.

Owned files:

- create `KIRBY2_FULL_DAY_QUALIFICATION_EVIDENCE.md` only in Git;
- write immutable qualification/review/performance artifacts to the governed evidence
  store; do not modify source, manifests, commands, gates, or package data.

Fixed decisions:

1. Require the exact clean WO31-I implementation commit and exact WO31-H
   preregistration commit/digests. Refuse a dirty tree, used qualification identity,
   pre-existing partial destination, or any changed policy byte.
2. Execute every fixed qualification seed and the exact one-time holdout policy. No
   seed replacement, early reveal, rerun-selection, window cherry-pick, or
   outcome-dependent exclusion is allowed; abort/failure remains in the denominator.
3. Select each applicable stratum with the frozen review-only RNG, persist the selected
   window manifest/digest, and build the blinded packet. Report missing/nonapplicable
   strata rather than substituting another profile/time.
4. Persist deterministic counts/bytes/digests/envelope outcomes separately from raw
   wall time, peak memory, throughput, host, and platform status. An ineligible host
   is `UNSUPPORTED`, never `PASS`.
5. Report bottlenecks, inherited warnings, new warnings, shortfalls, failures,
   unexercised capabilities, and human `PENDING`. A later invocation verifies the
   immutable evidence and refuses to execute the qualification/holdout again.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 qualify-day-profiles-demo --manifest kirby2/full_day/profile_envelopes.toml --evidence-root .kirby2/full_day/qualification
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-full-day --qualification-evidence .kirby2/full_day/qualification
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-I1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: all four candidates execute over every committed applicable seed or record
the preregistered failure; envelope arithmetic, windows, summaries, costs, raw
performance, warnings, and shortfalls are immutable and reproducible by verification,
not holdout rerun; human review remains `PENDING` absent a real sidecar; no source or
preregistered file changes after outcome revelation.

Commit: `Qualify full day profile candidates`

## 9. Work Order 32 — Scenario language and compiler

### WO32-A — Canonical scenario source and identity model

Objective: define a declarative TOML source schema and the three non-conflicting
identities used throughout compilation.

Owned files:

- create `kirby2/scenario_lang/__init__.py`, `kirby2/scenario_lang/models.py`,
  `kirby2/scenario_lang/schema.py`, and `kirby2/scenario_lang/identity.py`;
- create `kirby2/audit/scenario_language.py`.

Fixed decisions:

1. Source sections cover metadata, market profile, instrument, venues, session
   schedule, flow model, regimes/day-local states, volume, liquidity, latency, agent
   populations, scheduled/unscheduled events, transition rules, historical
   constraints, player objective, strategy, curriculum metadata, reveal policy,
   checkpoint policy, seed policy, accepted behavioral envelopes, and required source
   capabilities.
2. Units are explicit in field names or tagged values. The v1 normalized vocabulary
   includes `duration_ms`, `price_ticks`, `quantity_shares`, `rate_per_second`,
   `latency_us`, `volume_multiplier` as a reduced rational/fixed-point value, and
   integer probability weights; compilation normalizes time to integer microseconds
   and preserves exact quantities. Bare ambiguous numeric fields fail.
3. `source_bundle_digest` hashes ordered raw source/import bytes;
   `semantic_plan_digest` hashes canonical resolved behavior only;
   `compiled_artifact_digest` hashes the exact compiled artifact and provenance.
4. Unknown fields, duplicate logical names, floats where canonical identity requires
   integer/fixed representation, and unsupported schema versions fail.
5. The compiled target for full days is the WO31 `FullDayPlan`; no second runtime IR.
6. `ScenarioPlanEnvelopeV1` is a tagged immutable dispatch envelope, not another
   behavioral IR. Its `target_kind` is exactly one of `FULL_DAY_PLAN_V1`,
   `MARKET_SCENARIO_V1`, `HIDDEN_LIQUIDITY_RECORDING_V1`,
   `MULTIVENUE_RECORDING_V1`, or `HISTORICAL_LESSON_V1`, and its payload is the
   canonical native plan/configuration/command recording already owned by WO31,
   `kirby2.scenarios.market.ScenarioDefinition`,
   `kirby2.observability.replay.ObservabilityRecording`,
   `kirby2.multivenue.replay.MultiVenueRecording`, or the existing historical lesson
   contract respectively. Target kind/version/adapter/capability digest enter semantic
   identity; arbitrary import paths and untagged payloads fail.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO32-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-full-day
```

Acceptance: comments and formatting change source provenance but not semantic
identity; semantic edits change both semantic and artifact identity; strict parse and
canonical round-trip cases pass.

Commit: `Define canonical scenario source model`

### WO32-B — Confined imports, definitions, and inheritance

Objective: resolve reusable TOML definitions deterministically without filesystem or
network escape.

Owned files:

- create `kirby2/scenario_lang/imports.py` and
  `kirby2/scenario_lang/resolution.py`;
- modify `kirby2/scenario_lang/models.py` and
  `kirby2/audit/scenario_language.py`.

Fixed decisions:

1. Imports resolve only beneath an explicit source root or an activated pack
   namespace. URL imports and automatic Internet access are forbidden.
2. Reject absolute paths, `..`, backslashes, Windows drive/UNC forms, NUL, symlinks
   escaping the root, duplicate canonical paths, cycles, case/Unicode logical-name
   collisions, excessive depth/count, and excessive expanded bytes.
3. Reusable definitions have stable qualified names and exactly these v1 types:
   market, venue, latency, agent population, regime, historical source, and objective
   template. Single inheritance is permitted only within one definition type;
   multiple inheritance and cross-type inheritance are not.
4. Override merge rules are type-specific and explicit. Lists never concatenate by
   accident; replacement versus keyed merge is declared by schema.
5. The complete ordered import graph and byte digests enter source provenance.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO32-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: valid nested imports and inheritance resolve identically from relocated
roots; traversal, symlink, collision, cycle, limit, and URL fixtures fail before any
runtime or write.

Commit: `Resolve confined scenario imports`

### WO32-C — Immutable compiler and seed policy

Objective: materialize a source bundle into a canonical immutable plan artifact while
refusing execution until WO32-D supplies the full validator.

Owned files:

- create `kirby2/scenario_lang/compiler.py`,
  `kirby2/scenario_lang/defaults.py`, and `kirby2/scenario_lang/seeds.py`;
- modify `kirby2/scenario_lang/models.py` and
  `kirby2/audit/scenario_language.py`.

Fixed decisions:

1. Compilation phases are parse, import resolution, inheritance, explicit default
   materialization, unit normalization, reference binding, canonicalization, then
   immutable artifact creation. Capability validation is a required finalization phase
   implemented in WO32-D.
2. The compiled artifact contains compiler/schema versions, the ordered import graph,
   every reference/inheritance/default fully materialized, exact normalized units,
   required capability declarations and decisions, source/semantic/native-plan/
   compiled-artifact digests, warnings, provenance, and the deterministic RNG/
   substream derivation policy. Runtime resolution performs no remaining lookup.
3. A closed `ScenarioTargetRegistry` maps the five WO32-A target tags to explicit
   parse/validate/run/persist/replay adapters. Registration is static and duplicate-
   refusing; target payloads cannot select a Python symbol. Adapter version and native
   plan digest are stored in the compiled artifact.
4. Runtime receives only a WO32-D validated compiled plan and cannot consult source
   files or ambient defaults. Artifacts emitted in this card carry
   `execution_eligible=false` with reason code `VALIDATOR_NOT_IMPLEMENTED`.
5. CLI seed override is allowed only where the source seed policy permits it; the
   selected seed and policy version enter run identity.
6. V1 has no generic scenario expression language or new evaluator. Conditional
   behavior can reference only the already-typed transition and strategy contracts
   through their existing bounded parsers. Any new expression grammar/evaluator is a
   later explicit amendment; source text can never import, reflect, call Python, or
   access time/files/network.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO32-C
```

Acceptance: recompilation is byte-identical; ambient directory, mapping order, and
unrelated definitions do not alter the artifact; runtime mutation and unsafe
expressions are refused; attempting to run the unvalidated artifact fails closed.

Commit: `Compile immutable scenario plans`

### WO32-D — Static validation and capability refusal

Objective: reject internally inconsistent or unsupported plans before execution and
state clearly what cannot be proven statically.

Owned files:

- create `kirby2/scenario_lang/validation.py` and
  `kirby2/scenario_lang/capabilities.py`;
- modify `kirby2/scenario_lang/compiler.py`, `kirby2/scenario_lang/models.py`, and
  `kirby2/audit/scenario_language.py`.

Fixed decisions:

1. Findings are `ERROR`, `WARNING`, or `NOT_PROVABLE_STATICALLY`; only error-free
   plans compile for execution.
2. Validate session/auction/halt transitions, state graph references and bounded
   reachability, transition weights/durations, Hawkes stability, venue/instrument
   compatibility, latency/replay compatibility, required feature observability,
   strategy no-lookahead, resource limits, checkpoint adapters, and historical
   capability.
3. Explicit structural cases include missing references, circular imports, invalid
   time ranges, overlapping exclusive states, impossible auction configuration,
   unsupported order instructions, negative quantities, crossed starting books,
   invalid venue combinations, exact replay from insufficient data, hidden truth
   exposed to strategy, future information exposed to algorithms, unbounded agent
   budgets, unreachable strategy states, and invalid transition graphs.
4. Static reachability covers the finite declared graph. It never claims a general
   proof about arbitrary strategy expressions.
5. Static no-lookahead analysis supplements rather than replaces runtime
   information-cutoff enforcement.
6. Historical market-by-order requirements fail on weaker data; reconstruction is an
   explicit different capability, never an implicit upgrade.
7. Only a completed passing validation report can set
   `execution_eligible=true`; its digest enters the compiled artifact identity.
8. Target-specific validation proves the selected native adapter exists, its
   capability/observability contract matches the source, and persist/replay is
   supported. WO31's refusal of a mixed historical or multivenue full-day profile does
   not forbid separately tagged existing hidden, multivenue, or historical runtimes;
   no target may be silently coerced to `FULL_DAY_PLAN_V1`.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO32-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hawkes-stability
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-historical-features
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-scenarios
```

Acceptance: one fixture per validation family emits stable diagnostics with source
locations and refusal reasons; required unknown proof is never translated to pass.

Commit: `Enforce scenario capability contracts`

### WO32-E — Authoring CLI, diagnostics, examples, and deterministic run

Objective: make valid scenarios authorable and invalid ones explainable without
editing Python.

Owned files:

- create `kirby2/scenario_lang/commands.py` and
  `kirby2/scenario_lang/examples/` with declared valid and hostile-invalid TOML;
- modify `kirby2/audit/scenario_language.py` and `pyproject.toml` package data.

Fixed decisions:

1. Commands are `scenario-source lint`, `scenario-source compile`,
   `scenario-source explain`, `scenario-source diff`, and `scenario-source run`
   through modular registration. The pre-existing `scenario NAME` command and its
   grammar remain untouched; the new family may not shadow or reinterpret it.
2. `explain` answers, in named sections and without executing: what market is created;
   what may change; what is hidden; what is observable; what events are scheduled;
   what stochastic components remain; what terminates the scenario; and every known
   condition that invalidates/refuses the run. It also prints provenance, materialized
   defaults, capabilities, units, RNG policy, and semantic identity.
3. `diff` compares semantic paths and separately reports source-only changes.
4. `run` persists the compiled artifact and delegates to the existing applicable
   target-registry adapter; it does not evaluate source directly. Full-day examples
   use WO31; hidden-liquidity uses `ObservabilityRecording`; fragmented venue uses
   `MultiVenueRecording`; and historical reconstruction uses the existing historical
   lesson runner with its source capability/evidence labels.
5. The six required valid examples are named and distinct: quiet full day, opening
   momentum drill, hidden-liquidity lesson, fragmented-venue execution task,
   historical reconstruction lesson, and halt/reopening exercise. Additional compact
   inheritance and unit examples are allowed.
6. Invalid examples cover every WO32-B/D family, with stable source span, semantic
   path, error code, explanation, and suggested correction where one is safe. They
   include every hostile import/expression class and never execute or write a run.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 scenario-source lint kirby2/scenario_lang/examples/full_day.toml
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 scenario-language-demo --source kirby2/scenario_lang/examples/full_day.toml
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO32-E
```

Acceptance: all six required valid examples lint/compile; every one runs through its
declared tagged adapter and persists/replays deterministically without Python edits;
wrong-tag/coerced-target fixtures fail; every invalid family
fails before execution with stable useful diagnostics; `explain` answers all eight
questions; formatting-only diff reports no semantic change and a one-field semantic
edit changes semantic/compiled identity.

Commit: `Add scenario authoring diagnostics`

## 10. Work Order 33 — Drill miner and lesson extractor

### WO33-A — Versioned detector and skill contracts

Objective: define reviewable candidate identity, stable skill references, detector
capability gates, and assessment-safe reveal policy before mining.

Owned files:

- create `kirby2/mining/__init__.py`, `kirby2/mining/models.py`,
  `kirby2/mining/detectors.py`, and `kirby2/mining/skills.py`;
- create `kirby2/audit/drill_mining.py`.

Fixed decisions:

1. `LessonCandidateV1` contains candidate ID, source run/dataset identity and digest,
   the exact `source_ancestry_sha256` projection from section 5.7.3,
   exact source/warmup/active/post-event-reveal bounds, checkpoint, observable-feature
   summary, separately access-controlled ground-truth summary where available,
   candidate lesson type, transparent difficulty and rarity estimates, detector
   ID/version/threshold digest/evidence, exactly one `primary_skill_id`, zero or more
   sorted supporting skill IDs, suggested player
   objective, suggested reveal material, known ambiguity, exact source capability
   record/evidence class, candidate digest, and review projection.
2. Skill IDs are stable and versioned for WO34; objectives are not free-text-only.
3. Human review is excluded from candidate identity and stored as a sidecar. Source
   lifecycle `PROPOSED` maps to candidate proposal state while human review is
   `PENDING`; sidecars may produce `ACCEPTED`, `REJECTED`, `NEEDS_EDIT`, or
   `SUPERSEDED` without changing candidate bytes.
4. Every detector declares synthetic-ground-truth, historical, or reconstruction
   support. Insufficient evidence returns `NOT_EXERCISED`.
5. Detector names and outcome-loaded titles are hidden in assessment mode until
   reveal.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-historical-lessons
```

Acceptance: candidate identity changes with any evidentiary boundary but not review
status; unsupported capability and unknown skill fixtures fail truthfully.

Commit: `Define capability aware lesson detectors`

### WO33-A1 — Preregistered mining and selection manifest

Objective: freeze detector thresholds, source matrix, sampling, difficulty inputs,
deduplication, diversity weights, and review sampling before mining real source runs.

Owned files:

- create `kirby2/mining/detector_thresholds.toml` and
  `kirby2/mining/mining_plan.toml`;
- create `kirby2/mining/fixtures/qualification_sources.toml`;
- modify `kirby2/mining/models.py` only for strict manifest parsing;
- modify `kirby2/audit/drill_mining.py` only to validate manifests/source identities
  without running detectors or inspecting candidate outcomes.

Fixed decisions:

1. Bind every detector ID/version to operational thresholds, units, capability flags,
   ambiguity/exclusion rules, and evidence scope. Required detector IDs cover strong
   queue imbalance, queue depletion, queue replenishment, bid absorption, ask
   absorption, failed breakout, liquidity vacuum, spread expansion, spread recovery,
   aggressive-flow burst, cancellation burst, hidden reserve refresh, apparent
   liquidity mirage, latency-sensitive opportunity, cancel/fill race, multi-venue
   price fragmentation, routing dilemma, auction imbalance change, halt/reopening,
   distressed liquidation, momentum exhaustion, and mean-reversion transition.
   Arithmetic, ordering, detector rules, difficulty, deduplication, diversity, and
   sampling are exactly section 5.7.1 and section 5.7.3.
2. Bind the mandatory five-source matrix: one quiet full day, one event-driven full
   day, one hidden-liquidity day, one fragmented-market day, and one historical
   fixture with its exact capability/evidence class. Every row records the exact
   WO32 example/source generator, target adapter ID/version, source/config bytes and
   digests, seed, expected native run/replay digest, session bounds, and required/
   provided capability record. The quiet and event-driven rows use WO31 full-day
   plans; hidden and fragmented rows use the tagged WO32 deterministic recording
   adapters spanning their declared synthetic session and are not represented as the
   unsupported WO31 mixed full-day profile; historical uses its exact existing lesson
   source/provenance contract. Additional historical or explicitly labeled
   reconstruction/counterfactual sources are separate strata and never substitute for
   a missing mandatory source.
3. Bind the transparent difficulty formula using signal strength/duration,
   conflicting evidence, reaction time, spread, latency, liquidity, venue count,
   hidden-liquidity uncertainty, required objective size, relevant-feature count, and
   evidence quality. Bind units, normalization, missing inputs, fixed-point rounding,
   and the sample-frequency denominator.
4. Bind deduplication using overlapping windows, observable feature signatures,
   day/local-regime signatures, event-sequence similarity, learning-objective overlap,
   and source ancestry. Bind diversity weights across skills, detector families,
   qualification source rows, phases, source-window outcomes, and difficulty bands,
   material-distinctness, and the
   stratified twenty-candidate technical/human review sample.
5. Bind candidate shortfall behavior; no quota permits threshold weakening.
6. This card verifies all five source artifacts to freeze their exact bytes and replay
   identities. The quiet and event rows must resolve the unique immutable WO31-I1
   artifacts for their exact candidate/root `3102000`; A1 may not regenerate them or
   execute any protected WO31 qualification/holdout seed. It may deterministically
   generate only the hidden and fragmented synthetic rows at their section-5.7.3
   nonprotected roots; the historical row resolves its existing immutable source.
   It validates and digests policy and source provenance, but does not invoke any
   detector, mine any candidate, or inspect candidate/selection outcomes.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-A1
```

Acceptance: both manifests are complete, canonical, digest-bound, and unexercised
against the qualification source matrix.

Commit: `Preregister lesson mining thresholds`

### WO33-B1 — Queue, flow, and liquidity detectors

Objective: implement queue/flow/liquidity event families as versioned operational
detectors without treating interpretations as historical facts.

Owned files:

- create `kirby2/mining/queue_detectors.py` and
  `kirby2/mining/flow_detectors.py`;
- create `kirby2/mining/runtime.py`;
- modify `kirby2/audit/drill_mining.py`.

Fixed decisions:

1. Implement separate operational detectors for strong queue imbalance, queue
   depletion, queue replenishment, bid absorption, ask absorption, failed breakout,
   liquidity vacuum, spread expansion, spread recovery, aggressive-flow burst,
   cancellation burst, hidden reserve refresh, apparent liquidity mirage, momentum
   exhaustion, and mean-reversion transition. No family name may stand in for an
   omitted detector ID.
2. Each detector consumes the committed WO33-A1 threshold manifest and explicit
   denominator; a missing or changed manifest fails before mining.
3. Labels such as absorption or failed breakout are detector interpretations.
4. Historical queue/hidden/order-level detectors require matching capabilities;
   reconstruction remains `SYNTHETIC_RECONSTRUCTION`.
5. Mining is deterministic over a canonical event order and records every considered
   opportunity, exclusion, and emitted candidate.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-B1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hidden-liquidity
```

Acceptance: synthetic fixtures activate each supported detector; deliberately weak
historical sources return `NOT_EXERCISED`; reordered input storage preserves results.

Commit: `Detect queue and liquidity lessons`

### WO33-B2 — Latency, venue, mechanics, and execution detectors

Objective: implement operational detectors whose evidence comes from asynchronous,
venue, mechanics, player, and algorithm records.

Owned files:

- create `kirby2/mining/mechanics_detectors.py`,
  `kirby2/mining/latency_detectors.py`, and
  `kirby2/mining/venue_detectors.py`;
- modify `kirby2/mining/runtime.py` and `kirby2/audit/drill_mining.py`.

Fixed decisions:

1. Implement required detectors for latency-sensitive opportunity, cancel/fill race,
   multi-venue price fragmentation, routing dilemma, auction imbalance change,
   halt/reopening, and distressed liquidation. Additional versioned detectors may
   cover stale quote, partial fill, adverse selection, day/local state transitions,
   and execution-algorithm decision points, but cannot replace the required IDs.
2. Consume the committed WO33-A1 manifest; every detector declares exact required
   events, fields, capabilities, timing bounds, ambiguity/exclusion, and label scope.
3. Adverse selection is a declared retrospective metric, not information shown during
   the original decision.
4. Historical absence of client delivery, route, order identity, or queue evidence is
   `NOT_EXERCISED`; it is never reconstructed silently.
5. Detection order is canonical and source ancestry is preserved.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-B2
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-market-mechanics
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-multivenue
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-execution-algorithms
```

Acceptance: exact synthetic fixtures activate every supported detector; each missing
capability returns `NOT_EXERCISED`; outcome timing never leaks into assessment data.

Commit: `Detect execution mechanics lessons`

### WO33-C — Difficulty, deduplication, and diversity

Objective: rank transparent candidate evidence without optimizing toward a required
quota or presenting the score as validated pedagogy.

Owned files:

- create `kirby2/mining/ranking.py`, `kirby2/mining/deduplication.py`, and
  `kirby2/mining/selection.py`;
- modify `kirby2/mining/models.py` and `kirby2/audit/drill_mining.py`.

Fixed decisions:

1. Difficulty is a versioned `UNVALIDATED_ESTIMATE` with visible components for
   signal strength/duration, conflicting evidence, available reaction time, spread,
   latency, liquidity, venue count, hidden-liquidity uncertainty, required objective
   size, relevant-feature count, and evidence quality; missing components and exact
   fixed-point calculation are shown.
2. Rarity is reported only against an explicit reference population and denominator;
   otherwise report sample frequency.
3. Semantic deduplication binds source ancestry, overlapping windows, observable
   feature signatures, day/local-regime signatures, canonical event-sequence
   similarity, learning-objective overlap, and decision opportunity—not scenario name
   alone.
4. Diversity selection uses preregistered weights across skills, detector families,
   qualification source rows, phases, source-window outcomes, and difficulty bands.
5. If fewer than the requested number meet criteria, report the shortfall; never
   weaken thresholds or duplicate a candidate.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-C
```

Acceptance: ranking is deterministic and inspectable; near-duplicates collapse;
diversity improves under declared metrics; quota pressure cannot change validity.

Commit: `Rank diverse lesson candidates`

### WO33-D — Observable playable lesson extraction

Objective: turn a candidate into a deterministic playable lesson whose assessment
view comes from the recorded client feed, not filtered omniscience.

Owned files:

- create `kirby2/mining/extraction.py` and `kirby2/mining/lesson_builder.py`;
- minimally extend `kirby2/historical/lesson_models.py` and
  `kirby2/curriculum/models.py` for mined-source lineage;
- modify `kirby2/audit/drill_mining.py`.

Fixed decisions:

1. Warmup gives sufficient observable context and never starts after information the
   player is assumed to know.
2. Assessment hides detector type, selection reason, future-derived difficulty,
   outcome, post-event boundary, hidden state, and future schedule.
3. Reveal/debrief is a separately authorized payload.
4. The built lesson's immutable source record preserves all seven required lineage
   fields: source run reference, exact source time bounds, checkpoint reference,
   observable-feed policy, hidden-state reveal policy, historical provenance, and
   detector ID/version. Its supporting envelope also preserves RNG state, hidden
   schedule, event-prefix digest, exact observable feed, capability labels, and parent
   linkage.
5. Source replay remains authoritative. Player actions are a labeled overlay or
   counterfactual branch and never mutate the mined history.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-historical-lessons
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
```

Acceptance: source-prefix and extracted-prefix digests match; blind presentation
cannot access reveal fields; replay and branch behavior are deterministic.

Commit: `Extract observable playable lessons`

### WO33-E — Immutable review sidecars and mining CLI

Objective: expose proposal/review workflows and qualify review-ready lessons without
letting software award pedagogical acceptance.

Owned files:

- create `kirby2/mining/reviews.py` and `kirby2/mining/commands.py`;
- consume the committed `kirby2/mining/fixtures/qualification_sources.toml` without
  altering its rows or expected identities;
- modify `kirby2/audit/drill_mining.py`, `kirby2/research/models.py`,
  `kirby2/research/store.py`, and `kirby2/research/tables.py` for typed mining/review
  artifacts only.

Fixed decisions:

1. Public operations are explicitly `mine-drills RUN_ID`, `list-candidates RUN_ID`,
   `inspect-candidate CANDIDATE_ID`, `accept-candidate CANDIDATE_ID`, and
   `build-lesson CANDIDATE_ID`. `accept-candidate` requires an authenticated/local
   reviewer reference and writes an immutable sidecar; automation/demo code may only
   submit `PENDING` or `READY_FOR_HUMAN_REVIEW`. Additional compare/review-history
   operations use the same store.
2. Review sidecars record reviewer identity/reference, rubric version, decision,
   reasons, timestamp metadata, and superseded review; they never edit candidates.
3. Materialize each source from the exact committed WO33-A1 manifest into an
   audit-owned temporary root, verify its expected source/native-run/replay digests,
   then execute the exact five-source matrix. Automated qualification samples at
   least twenty candidates using its preregistered
   stratified selection and produces at least five materially distinct
   `READY_FOR_HUMAN_REVIEW` candidates from the one preregistered event-driven full-day
   source, plus the cross-source strata, when frozen evidence supports them. The
   one-source requirement is reported separately and is never satisfied by pooling
   candidates from the other four sources.
4. A shortfall is a truthful result, not a failed engineering implementation.
5. Performance on outcome-conditioned mined windows is never equated with performance
   over unselected market time.
6. The twenty-candidate review packet has one row per candidate and explicit fields
   for useful-candidate judgment, duplicate, false positive, unfair window, missing
   context, and detector-adjustment recommendation. Software completes technical
   checks and leaves each human field `PENDING`; it never fabricates manual findings.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 lesson-miner-demo --source-matrix kirby2/mining/fixtures/qualification_sources.toml --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO33-E
```

Acceptance: review-ready candidates, including the five-materially-distinct-from-one-
full-day requirement or its exact preregistered shortfall, capabilities, lineage, selection
method, and `PENDING` human status are explicit; replay parity survives persistence.
The audit produces a stratified twenty-candidate technical review packet when enough
valid candidates exist and accounts for every mandatory source; actual human
inspection and five `ACCEPTED` lessons remain separate `PENDING` gates. Engineering
may continue with review-ready candidates, but no artifact or closeout may call them
human-accepted until real sidecars exist.

Commit: `Add immutable lesson review workflow`

## 11. Work Order 34 — Adaptive curriculum engine

### WO34-A — Immutable learner evidence and skill graph

Objective: define the evidence ledger, stable acyclic skill graph, and error vocabulary
without claiming that a model estimate is observed mastery.

Owned files:

- create `kirby2/curriculum/skills.py`, `kirby2/curriculum/evidence.py`, and
  `kirby2/curriculum/errors.py`;
- create `kirby2/audit/adaptive_curriculum.py`;
- modify `kirby2/curriculum/catalog.py` and `kirby2/curriculum/models.py` only to map
  existing lesson objectives to stable skill IDs.

Fixed decisions:

1. The initial graph contains exactly these stable skill IDs:
   `BOOK_READING`, `TAPE_READING`, `QUEUE_POSITION`, `PASSIVE_ENTRY`,
   `AGGRESSIVE_ENTRY`, `CANCEL_TIMING`, `REPLACE_TIMING`,
   `PARTIAL_FILL_MANAGEMENT`, `ADVERSE_SELECTION`, `SPREAD_DECISION`,
   `VOLUME_CONTEXT`, `REGIME_RECOGNITION`, `ABSORPTION_RECOGNITION`,
   `LIQUIDITY_WITHDRAWAL`, `HIDDEN_LIQUIDITY`, `LATENCY_AWARENESS`,
   `MULTI_VENUE_ROUTING`, `AUCTION_EXECUTION`, `HALT_REOPENING`,
   `SCRIPT_DISCIPLINE`, `HOTKEY_ACCURACY`, `POSITION_MANAGEMENT`, and
   `EXIT_EXECUTION`. Graph/version changes are append-only; cycles and missing nodes
   fail.
2. The complete edge set, rationale mapping, roots, and uniform
   `PREREQUISITE_READY_V1` minimum-evidence policy are exactly section 5.7.5; the
   fixtures transcribe them without adding an edge or per-edge threshold.
3. `AttemptAssessmentV1` and each lesson's single primary skill follow the exact
   `SkillEvidenceV1` score contract in section 5.7.4. The assessment records
   opportunity, action, observable context, scoring version/digest, skill evidence,
   typed error, ambiguity, and sufficiency.
4. Exact evidence families include correct classification, appropriate no-trade,
   discipline compliance, fill quality, reaction timing, cancel mistake, queue
   misunderstanding, routing error, adverse selection, and hotkey error. P&L may be a
   labeled auxiliary outcome but cannot create mastery evidence by itself.
5. Error taxonomy includes `ACTED_DURING_RED`, `FAILED_TO_ACT_DURING_GREEN`,
   `CROSSED_UNNECESSARILY`, `WAITED_PAST_USEFUL_LIQUIDITY`,
   `CANCELLED_TOO_LATE`, `CANCELLED_TOO_EARLY`, `MISREAD_REPLENISHMENT`,
   `CONFUSED_DISPLAYED_WITH_EXECUTABLE_DEPTH`, `IGNORED_SPREAD_EXPANSION`,
   `CHASED_AFTER_INVALIDATION`, `WRONG_HOTKEY`,
   `OVERSIZED_RELATIVE_TO_LIQUIDITY`, and `FAILED_TO_COMPLETE_OBJECTIVE`, plus
   `UNSCORABLE`, `AMBIGUOUS`, and `INSUFFICIENT_OBSERVABILITY`.
6. Failure to act is an error only when a valid opportunity and sufficient observable
   reaction time are proven.
7. Raw evidence is immutable and independent of learner projection versions.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO34-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-historical-lessons
```

Acceptance: graph and evidence round trips are immutable; ambiguous/unobservable
cases do not become negative evidence; legacy lesson mapping is explicit.

Commit: `Record immutable learner evidence`

### WO34-B — Versioned learner-state projections

Objective: derive deterministic, rebuildable learner estimates from immutable evidence.

Owned files:

- create `kirby2/curriculum/learner.py` and `kirby2/curriculum/projections.py`;
- modify `kirby2/audit/adaptive_curriculum.py`.

Fixed decisions:

1. The model is exactly `LEARNER_PROJECTION_V1` in section 5.7.4. Every update
   equation, prior, weight, minimum opportunity count, confidence rule, uncertainty
   calculation, and decay policy is versioned and documented.
2. Each skill projection exposes estimated mastery, confidence, attempt count, recent
   attempt history, explicit recency, scenario/volume/liquidity diversity, last
   demonstrated success, last demonstrated failure, known error types, observed
   counts, model evidence score, uncertainty, sufficiency, last opportunity, and
   recommendation eligibility.
3. Model status is `UNVALIDATED_FOR_LEARNING_OUTCOMES` until external study evidence
   justifies a superseding label.
4. V1 recency uses only explicit attempt ordinal as specified in section 5.7.4.
   Simulation/study time remains provenance and does not affect this model; ambient
   wall clock is forbidden.
5. Rebuilding from the same ledger and model version is identical. A new model
   version creates a new projection and never rewrites old evidence.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO34-B
```

Acceptance: same evidence yields identical estimates; ordering, clock, version, and
insufficiency cases behave as declared; no P&L-only update exists.

Commit: `Project deterministic learner estimates`

### WO34-C — Explainable adaptive selection and modes

Objective: select drills deterministically from eligible evidence while preserving
prerequisites, variety, assessment integrity, and inspectable reasons.

Owned files:

- create `kirby2/curriculum/selection.py`, `kirby2/curriculum/plans.py`, and
  `kirby2/curriculum/adaptive_modes.py`;
- modify `kirby2/curriculum/models.py` for compatible mode parsing;
- modify `kirby2/audit/adaptive_curriculum.py`.

Fixed decisions:

1. Modes are behaviorally distinct: `GUIDED` teaches one declared concept and shows
   explanation; `PRACTICE` adaptively mixes eligible skills and provides feedback;
   `ASSESSMENT` hides lesson/detector identity, restricts assistance, and uses a fixed
   preregistered scoring/reveal policy; `REMEDIATION` targets a diagnosed error with
   the required prerequisite context. Map or version legacy `LEARN` and `BLIND`
   rather than changing their old meaning.
2. Selection records complete eligible set, exclusions, prerequisite checks, ranking
   components, variety/anti-memorization state, policy version, explicit `as_of`, RNG
   seed, manual overrides, and chosen drill.
3. Ranking balances weakness, uncertainty, prerequisites, recency, variety,
   difficulty progression, scenario diversity, volume diversity, liquidity diversity,
   and historical-versus-synthetic exposure using declared exact weights/tie-breaks.
   Eligibility, ranking, cooldowns, mode behavior, assessment, and ties are exactly
   section 5.7.5.
4. Assessment selection/scoring is fixed and cannot consume learner-hidden outcomes.
5. Anti-memorization uses semantic content/parameter/source digests and prevents
   repeated seed, symbol, visible queue shape, and regime parameterization—not names.
6. Recommendation explanations cite evidence and uncertainty without presenting the
   estimate as fact.
7. Do not add XP, loot, streak economies, achievements, public ranks, or other
   gamification. Progress views expose evidence, uncertainty, practice history, and
   declared skill state only.
8. A valid immutable manual `CurriculumPlan` takes deterministic precedence over
   adaptive ranking for the plan's declared scope and interval, and the selection
   record shows that precedence. It may choose among prerequisite-eligible drills and
   set mode/sequence, but cannot bypass required prerequisites, assignment locks,
   consent/reveal policy, assessment assistance/scoring locks, capability gates, or
   observability rules. An invalid override is refused, never silently weakened.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO34-C
```

Acceptance: cold start is broad; weak-skill selection respects prerequisites;
assessment is stable; manual plans/overrides are immutable sidecars and fully shown.

Commit: `Select explainable adaptive drills`

### WO34-D — Synthetic learner and curriculum audit

Objective: demonstrate deterministic differentiated sequences while explicitly
limiting the claim to routing behavior.

Owned files:

- create `kirby2/curriculum/adaptive_commands.py`;
- complete `kirby2/audit/adaptive_curriculum.py`;
- modify `kirby2/research/models.py`, `kirby2/research/store.py`, and
  `kirby2/research/tables.py` for learner update/projection artifacts only.

Fixed decisions:

1. The six required synthetic learners are strong reader/weak execution, weak
   reader/strong hotkeys, over-aggressive trader, over-passive trader,
   hidden-liquidity confusion, and new learner with insufficient evidence. Their
   immutable evidence is constructed to isolate those declared patterns; no fixture
   injects the expected recommendation directly.
2. Demonstration logs update sequence, projection digest, eligible drills, selection
   rationale, and replay digest.
3. Adaptive practice results are not naively compared across learners because the
   selection policy creates different exposure.
4. The audit proves deterministic routing and explanation consistency, not real
   weakness measurement or educational effectiveness.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 adaptive-curriculum-demo --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO34-D
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: distinct evidence creates explainably distinct sequences; replay and
projection rebuild match; claims retain the unvalidated-model label.

Commit: `Audit adaptive curriculum behavior`

## 12. Work Order 35 — Strategy mutation and discovery laboratory

### WO35-A — Canonical strategy AST identity

Objective: create semantic strategy identity and lineage without silently replacing
the established raw-source identity used by prior experiments.

Owned files:

- create `kirby2/discovery/__init__.py`, `kirby2/discovery/ast.py`,
  `kirby2/discovery/identity.py`, and `kirby2/discovery/lineage.py`;
- minimally extend `kirby2/strategy/language.py` and
  `kirby2/experiments/models.py`;
- create `kirby2/audit/strategy_discovery.py`.

Fixed decisions:

1. Parse existing strategy language into a typed canonical AST with stable ordering,
   normalized numeric/unit representation, and deterministic rendering.
2. Preserve both legacy source-byte digest and new semantic AST digest with explicit
   migration/import provenance.
3. Semantic duplicates collapse even when formatting or commutative representation
   differs under explicitly supported equivalences.
4. Lineage nodes bind parent semantic digest, operation ID/version, parameters, RNG
   substream, child digest, validity, and semantic diff.
5. Unsupported grammar features fail before mutation.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
```

Acceptance: parse/render/parse is semantically stable; legacy identities remain
inspectable; duplicate and invalid AST fixtures behave deterministically.

Commit: `Canonicalize strategy syntax trees`

### WO35-B — Sealed experiment partitions

Objective: make train, validation, holdout, and adversarial holdout enforceable
access boundaries rather than descriptive labels.

Owned files:

- create `kirby2/discovery/partitions.py`, `kirby2/discovery/experiment.py`, and
  `kirby2/discovery/access.py`;
- minimally extend `kirby2/experiments/models.py`,
  `kirby2/research/models.py`, and `kirby2/research/store.py` for partition manifests
  and immutable access artifacts;
- modify `kirby2/audit/strategy_discovery.py`.

Fixed decisions:

1. Partition manifests bind source days, scenario families, historical periods,
   seeds, extracted-window ancestry, branch ancestry, and dataset digests.
2. Related windows or branches of one parent cannot cross an independence boundary.
3. Search sees train and only the predeclared validation schedule. Holdout metrics are
   unavailable until the candidate set and selection record are frozen.
4. Revealing holdout terminates search/tuning for that experiment version and begins
   its terminal holdout/adversarial evaluation. Failure after reveal remains terminal;
   any later search starts a new experiment with a new untouched holdout.
5. Every access is immutable and audit-visible.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: ancestry leakage, early holdout access, manifest mutation, and post-reveal
search all fail; valid partition creation and one-shot reveal are deterministic.

Commit: `Seal strategy experiment partitions`

### WO35-C — Constrained mutation engine

Objective: generate valid strategy children through declared AST transformations, not
source-text accidents.

Owned files:

- create `kirby2/discovery/mutations.py`, `kirby2/discovery/generation.py`, and
  `kirby2/discovery/diffs.py`;
- modify `kirby2/audit/strategy_discovery.py`.

Fixed decisions:

1. Required operators separately cover threshold, rolling window, required duration,
   add/remove condition, feature replacement, logical operator, transition condition,
   cooldown, state timeout, confirmation count, invalidation rule, position
   constraint, spread limit, and volume requirement. Optional versioned operators may
   cover sizes, order types, routing, and exit/cancel logic but cannot replace a
   required operation ID.
2. Each operator declares input node kinds, bounded parameter domain, observability
   requirements, semantic validation, complexity delta, machine-readable reason, and
   human-readable reason/inverse/diff description. Every mutation record contains
   parent/child IDs, semantic digests/diff, operation/version/parameters, affected
   rule path, reasons, and exact complexity change.
3. Mutation RNG uses stable labeled substreams. Candidate order never depends on set,
   dict, filesystem, or worker completion order.
4. Invalid, no-op, duplicate, future-dependent, unavailable-feature, and
   resource-excessive children are recorded as rejected and never evaluated.
5. Mutation cannot introduce arbitrary code or widen permissions.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
```

Acceptance: every supported operator has deterministic valid/invalid fixtures;
semantic diffs and lineage agree; no-lookahead/permission checks hold.

Commit: `Generate constrained strategy mutations`

### WO35-D — Deterministic multi-objective search

Objective: define and audit budgeted search mechanics plus a committed experiment
protocol without yet evaluating real strategy candidates.

Owned files:

- create `kirby2/discovery/objectives.py`, `kirby2/discovery/search.py`, and
  `kirby2/discovery/evaluation.py`;
- create `kirby2/discovery/examples/bounded_search.toml` and
  `kirby2/discovery/examples/no_winner.toml`;
- modify `kirby2/audit/strategy_discovery.py`.

Fixed decisions:

1. Support bounded grid, random, coordinate, beam, and evolutionary policies through
   one deterministic interface; each records budget/stopping policy.
   `bounded_search.toml` is the exact grid/budget-64 protocol in section 5.7.6; other
   policies are development-oracle conformance cases, never a combined meta-search.
2. Preregister objectives, aggregation, minimum practical effect, uncertainty,
   multiplicity handling, complexity penalty, robustness thresholds, and tie-breaks.
   Complexity explicitly counts conditions, features, states, transitions, rolling
   windows, and parameters; material equivalence prefers the declared simpler
   candidate.
   Partitions, objectives, budgets, uncertainty, multiplicity, stopping, and ranking
   are exactly section 5.7.6.
3. Required non-P&L objectives cover classification quality, discipline
   compatibility, execution-opportunity quality, false-green rate, missed-opportunity
   rate, adverse-selection exposure, turnover, spread paid, completion, stability
   across seeds/volume regimes/liquidity regimes, and complexity. P&L is optional and
   can never be the sole objective.
4. Candidate comparisons occur only within compatible scenario/objective/evidence
   groups and deterministic numeric/reduction order.
5. Valid outcome may be `NO_CANDIDATE_MET_CRITERIA`.
6. Example manifests bind partitions, source ancestry, search space, budget,
   objectives, validation access schedule, practical effect, uncertainty,
   multiplicity, complexity, robustness thresholds, holdout policy, and stopping rule.
7. This card tests search-engine mechanics against a deterministic synthetic score
   oracle only. It does not execute real source runs or reveal validation/holdout
   results. WO35-F1 performs the first real search after WO35-E gates exist and
   WO35-F freezes the lineage machinery.
8. Use bounded Kirby2-native evaluation and standard-library/existing numeric
   facilities. Do not add a large machine-learning framework for search, scoring, or
   lineage.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-D
```

Acceptance: repeated search is identical; budget and access are enforced; a no-winner
synthetic-oracle fixture completes successfully without threshold changes; real
experiment partitions remain unexercised.

Commit: `Search deterministic strategy candidates`

### WO35-E — Robustness, observability, and overfit gates

Objective: reject brittle or illegally informed candidates and qualify claims only on
their named partitions.

Owned files:

- create `kirby2/discovery/robustness.py`, `kirby2/discovery/overfit.py`, and
  `kirby2/discovery/observability.py`;
- modify `kirby2/audit/strategy_discovery.py`.

Fixed decisions:

1. Required probes separately vary threshold, rolling window, latency, fees, volume,
   liquidity, and regime mix under preregistered bounds; venue mix is the explicit
   capability-declared `NOT_APPLICABLE` eighth family for this single-venue V1.
   Additional seed, scenario-family, day-profile, spread/depth, participant-mix, and
   adversarial probes are descriptive only and cannot affect qualification unless a
   section-5.5 deviation preregisters them before results. Perturbations, gates, holdout
   rules, and overfit labels are exactly section 5.7.7.
2. Use a predetermined one-seed/one-family overfit fixture to prove rejection.
3. Search remains closed before one-time holdout/adversarial reveal.
4. A candidate is described as improved only on named metrics/partitions;
   `CONFIRMED_WITHIN_DECLARED_SCOPE` requires validation, robustness, holdout, and
   adversarial qualification.
5. Detect and label large train improvement with validation degradation, one-seed
   dependence, one-scenario dependence, extreme threshold sensitivity, complexity
   growth without holdout gain, excessive trade suppression, and excessive trade
frequency using preregistered operational rules.
6. Endogenous execution divergence produces simulator counterfactual evidence, not a
   direct real-market superiority claim.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: the overfit fixture is rejected; unavailable observations fail; one-time
holdout semantics hold; no-winner and insufficient-evidence paths remain valid.

Commit: `Audit strategy robustness and overfit`

### WO35-F — Frozen discovery lineage and CLI machinery

Objective: implement and commit persistence, lineage inspection, reporting, and
one-time reveal enforcement before executing any real controlled search or holdout.

Owned files:

- create `kirby2/discovery/store.py`, `kirby2/discovery/commands.py`,
  `kirby2/discovery/report.py`, and
  `kirby2/discovery/examples/lineage_development.toml`;
- modify `kirby2/research/models.py`, `kirby2/research/store.py`,
  `kirby2/research/tables.py`, and `kirby2/audit/strategy_discovery.py` for discovery
  artifacts only;
- preregister the generic evidence validator/gate for WO35-F1 through K2X-02.

Fixed decisions:

1. Required public commands are `discover-strategy --base BASE --experiment FILE
   --budget N`, `inspect-lineage DISCOVERY_ID`, and `compare-strategies STRATEGY_A
   STRATEGY_B`; create/freeze/run/close are deterministic inspectable phases.
2. Store source/semantic identities, parentage, mutations, rejections, evaluations,
   partition accesses, selection freeze, reveal, scientific outcome, and warnings.
3. Holdout/adversarial details stay sealed through validation and robustness, then
   become available only after the robustness-pass atomic reveal and remain immutable
   through terminal evaluation and closure. The execution command requires a clean
   committed implementation HEAD, exact committed protocol/partition/robustness
   digests, a fresh evidence root, and an unused reveal token. A dirty source tree or
   repeat token refuses before partition access.
4. Scientific outcomes are `CONFIRMED_WITHIN_DECLARED_SCOPE`,
   `NO_CANDIDATE_MET_CRITERIA`, `INSUFFICIENT_EVIDENCE`, or `EXPERIMENT_INVALID`;
   no output claims live profitability/deployability.
5. The lineage browser shows ancestor, mutation, training result, validation result,
   holdout result after reveal, rejection reason, and selected descendants. Sealed
   values appear as visibly sealed, not absent.
6. The development fixture uses a deterministic synthetic score oracle and partitions
   disjoint from `bounded_search.toml`. It exercises persistence, crash/reopen,
   selection freeze, single-use reveal, no-winner, conflicting-result, and rendering
   paths without executing a real Kirby2 candidate or reading its holdout.
7. WO35-F1's gate reports `NOT_EXERCISED` until immutable controlled evidence exists;
   later audits verify it and never rerun the holdout.
8. No large machine-learning framework or opaque optimizer is introduced.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 strategy-discovery-dev-demo --manifest kirby2/discovery/examples/lineage_development.toml
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-F
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: development-only evidence proves exact lineage reload, sealed-field
enforcement, single-use reveal, and every terminal/report path; no real controlled
validation/holdout access exists; the next card needs no source change.

Commit: `Implement strategy discovery lineage`

### WO35-F1 — Evidence-only controlled strategy discovery

Objective: run the committed controlled real-Kirby2 experiment once with the clean
frozen WO35-F machinery and preserve its complete lineage and holdout decision.

Owned files:

- create `KIRBY2_STRATEGY_DISCOVERY_EVIDENCE.md` only in Git;
- write immutable search/run/lineage/reveal artifacts to the governed evidence store;
  do not modify source, manifests, thresholds, partitions, commands, or gates.

Fixed decisions:

1. Require the exact clean WO35-F implementation commit and exact committed
   `bounded_search.toml`, partition, objective, and robustness digests. Refuse dirty
   source, used reveal token, partial destination, or changed byte.
2. The controlled parameterized Kirby2 fixture contains both a candidate that must
   genuinely pass validation, robustness, holdout, and adversarial gates and a
   training-star candidate, defined mechanically as the exact first
   `WO35/TRAINING_FINALISTS` candidate frozen under section 5.7.6 before any
   validation access, that must be distinct from the selected winner and rejected by
   validation while receiving the preregistered pre-reveal
   `TRAIN_VALIDATION_DIVERGENCE` label under section 5.7.7. Only the frozen validation
   winner may reach robustness/reveal; therefore a
   training-star rejected at validation is never illegally run on sealed partitions.
   Expected winners are not injected through a score oracle; results come from actual
   Kirby2 runs.
3. Frozen access order is validation-finalist freeze, validation-selection freeze,
   exactly one robustness execution, atomic holdout/adversarial reveal, complete
   holdout, then complete adversarial execution. Persist every access/rejected branch;
   the reveal token is consumed even if later execution fails, and no reveal reruns.
4. Improvement is reported only for named metrics/partitions. The generic committed
   no-winner experiment remains a valid scientific path but cannot substitute for this
   controlled acceptance fixture.
5. If the improved candidate does not meet the frozen gate or the overfit candidate is
   not rejected, record the exact outcome and hard-pause; do not lower thresholds,
   alter the fixture, or repair source after seeing holdout.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 strategy-discovery-demo --manifest kirby2/discovery/examples/bounded_search.toml --evidence-root .kirby2/discovery/controlled
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO35-F1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: complete lineage/access history verifies identically; the selected actual
candidate passes the frozen validation, robustness, holdout, and adversarial rules and
earns `CONFIRMED_WITHIN_DECLARED_SCOPE`; the training-star overfit candidate is
rejected by the frozen validation gate and has its exact
`TRAIN_VALIDATION_DIVERGENCE` evidence; no sealed field was exposed early;
every conclusion links stored runs/objectives. `NO_CANDIDATE_MET_CRITERIA` here is
truthful but leaves WO35-F1 incomplete and pauses the goal.

Commit: `Preserve strategy discovery lineage`

## 13. Work Order 36 — Causal replay microscope

### WO36-A — Mechanistic execution trace index

Objective: derive a deterministic, source-linked mechanistic trace from immutable
recorded IDs without inventing missing causality from timestamp proximity.

Owned files:

- create `kirby2/microscope/__init__.py`, `kirby2/microscope/models.py`,
  `kirby2/microscope/index.py`, `kirby2/microscope/lineage.py`, and
  `kirby2/microscope/fixtures.py`;
- create `kirby2/audit/replay_microscope.py`;
- do not alter existing event writers in this card. Missing recorded correlation IDs
  are `UNAVAILABLE`; a demonstrated need to change a differently owned writer uses an
  amendment card and never rewrites old evidence.

Fixed decisions:

1. The required chain is explicit: observable events -> feature updates -> strategy
   rule evaluation -> traffic-light transition -> player input -> client-order
   creation -> routing -> venue receipt -> queue placement -> fill/cancel -> later
   adverse selection. Each implemented edge links the corresponding immutable event;
   any genuinely absent edge is typed `UNAVAILABLE`, never inferred from proximity.
2. Every edge cites event/correlation IDs and provenance. Missing links are
   `UNAVAILABLE`; timestamp adjacency is not causality.
3. Strategy explanations use the recorded decision artifact from that software
   version. If absent, they remain unavailable rather than being recomputed.
4. The term is `MECHANISTIC_TRACE_WITHIN_KIRBY2_MODEL`; it is not general causal
   inference about an unobserved real-market counterfactual.
5. Derived index identity binds source run/event digest and index schema/version.
6. `UNAVAILABLE` is permitted for legacy or capability-insufficient source evidence,
   but not for the new WO36 acceptance fixture. Every player action in that committed
   fixture must record the complete decision/execution chain from observation through
   later adverse selection, with no missing required edge.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-latency
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
```

Acceptance: complete and deliberately incomplete legacy traces show exact source links
or `UNAVAILABLE`; the new acceptance fixture has no required-edge gap for any player
action; index rebuild is deterministic; source runs are unmodified.

Commit: `Index mechanistic execution traces`

### WO36-B — Enforced observation and reveal policies

Objective: make `AS_OBSERVED` and `POSTMORTEM` query-layer policies, not cosmetic UI
toggles.

Owned files:

- create `kirby2/microscope/policy.py`, `kirby2/microscope/query.py`, and
  `kirby2/microscope/data_age.py`;
- modify `kirby2/audit/replay_microscope.py`.

Fixed decisions:

1. `AS_OBSERVED` reads immutable client-delivered feed and decision snapshots only;
   it never filters a ground-truth reconstruction.
2. `POSTMORTEM` may add truth/reveal data only when source capability and reveal
   authorization permit it.
3. Each displayed value records source/event time, venue-receipt time,
   client-receive/knowledge time, render cursor time, and age-at-action where
   applicable.
4. Held-last-known-state rendering is permitted; interpolation may not invent a
   quote, acknowledgement, order, fill, or feature value.
5. Mode labels survive query, export, screenshot metadata, and portable report.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-hidden-liquidity
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-strategy-time
```

Acceptance: forbidden fields are inaccessible through observed queries; timing
fixtures cannot render information early; historical hidden state stays unavailable.

Commit: `Enforce replay observation policies`

### WO36-C — Synchronized replay read models

Objective: build deterministic data for synchronized market, order, feature,
strategy, position, and event panes before presentation code.

Owned files:

- create `kirby2/microscope/timeline.py`, `kirby2/microscope/panes.py`, and
  `kirby2/microscope/overlays.py`;
- modify `kirby2/audit/replay_microscope.py`.

Fixed decisions:

1. Pane models explicitly cover Level 2 ladder, Time & Sales, depth heatmap,
   individual queue view where supported, player orders, order-state lifecycle,
   position, traffic-light state, strategy state/rule evidence, feature values with
   provenance, agent activity after authorized reveal, latency timeline, venue quotes,
   consolidated quotes, fills, execution metrics, mechanistic trace, and a
   counterfactual comparison slot. Unsupported panes return a typed explanation.
2. One integer simulation-time cursor drives all panes under one observation policy.
3. Play, pause, event step, fixed-time step, jump to player action, fill,
   traffic-light transition, revealed regime transition, invariant warning, and branch
   divergence are pure cursor/query operations over immutable evidence. Bookmark and
   annotate create sidecars only in WO36-E.
4. Queue estimates display capability, estimator version, uncertainty, and truth only
   in authorized postmortem synthetic modes.
5. Overlay models separately provide spread, microprice, imbalance, trade velocity,
   cancellation velocity, replenishment, relative volume, short-term volatility, and
   implementation shortfall with versions, exact windows/units, and source events.
   Presentation groups them into readable panes rather than one mandatory chart.
6. Every datum links to a source event or declared derived calculation.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-multivenue
```

Acceptance: all panes agree at hard timing boundaries; cursor partitioning is
deterministic; unsupported Level 2/queue panes explain unavailability.

Commit: `Build synchronized replay read models`

### WO36-D — Portable offline microscope report

Objective: render the read models as a self-contained local analysis artifact without
introducing a network service or native GUI framework.

Owned files:

- create `kirby2/microscope/report.py`, `kirby2/microscope/commands.py`, and
  exact assets `kirby2/microscope/assets/report.html`,
  `kirby2/microscope/assets/report.css`, and
  `kirby2/microscope/assets/report.js`;
- modify `pyproject.toml` package-data entries and
  `kirby2/audit/replay_microscope.py`.

Fixed decisions:

1. Desktop v1 analysis is deterministic bundled offline HTML/CSS/JavaScript generated
   from canonical data; no CDN, remote font, telemetry, server, or account.
2. Generated reports use a deterministic entry order and normalized metadata; any
   nonidentity display timestamp is explicitly excluded from semantic identity.
3. All observation modes are visibly watermarked. Hidden payload is omitted from an
   observed-only export, not merely hidden in CSS.
4. Assets are fixed, bundled, license-inventoried, and subject to content hashes.
5. The existing terminal trainer remains the interactive execution surface; this
   card does not create a native desktop GUI.
6. Standalone exported HTML escapes embedded data and applies a restrictive content
   security policy. Renderer code/assets come from the installed digest-bound Kirby2
   version, never from an installed content pack.
7. The v1 report schema reserves typed sections for run references, bookmarks,
   annotations, selected snapshots, causal traces, branch comparison, metric summary,
   provenance, active observation/reveal policy, renderer version, and limitations.
   In this card, bookmarks/annotations/comparison render exact typed empty or
   `NOT_AVAILABLE_UNTIL_WO36_E` slots; WO36-E supplies their immutable producers and
   completes those sections without changing the schema/renderer contract.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 microscope-demo --fixture stale_partial_cancel_race --mode as-observed
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-D
```

Acceptance: report generation is offline and deterministic; observed export contains
no forbidden values; assets resolve from a relocated directory.

Commit: `Render portable replay microscope reports`

### WO36-E — Counterfactual comparison, annotations, and timing-lie audit

Objective: compare parent/branch outcomes and preserve analysis sidecars without
rewriting either run.

Owned files:

- create `kirby2/microscope/comparison.py` and
  `kirby2/microscope/annotations.py`;
- modify `kirby2/microscope/commands.py`, `kirby2/microscope/report.py`, and
  `kirby2/audit/replay_microscope.py`.

Fixed decisions:

1. Branch view shows parent ID/prefix digest, intervention, divergence event/time,
   RNG policy, branch mode, synchronized prefix, and distinct suffixes.
2. Comparison shows the first differing event, orders, queue states, fills, declared
   metrics, and later endogenous market path after a synchronized prefix. Overlays
   include all nine WO36-C metrics plus latency, stale-quote age, queue estimates,
   adverse selection, algorithm schedule, regimes/states, and agent truth only where
   authorized.
3. Bookmarks and annotations are immutable versioned sidecars.
4. Acceptance fixture is explicitly multi-venue with hidden liquidity and contains a
   stale quote, partial fill, cancel/fill race, strategy action, later adverse
   selection, and counterfactual branch.
5. Manual inspection rubric searches for an order shown before acknowledgement, fill
   shown before client report, future quote/feature interpolation, hidden-field
   leakage, recomputed explanations, and misleading causal wording. It records exact
   pane/time/snapshot references. Software records `READY_FOR_HUMAN_REVIEW`; the
   actual timing-lie judgment remains `PENDING` until a reviewer sidecar exists.
6. For every player action in this new acceptance fixture, the comparison/report must
   expose the complete immutable observation -> feature -> rule -> traffic-light ->
   input -> client-order -> route -> venue-receipt -> queue -> fill/cancel -> later-
   adverse-selection chain. A required `UNAVAILABLE` edge fails this fixture; that
   label remains valid only for separately identified legacy/capability-insufficient
   sources.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 microscope-demo --fixture stale_partial_cancel_race --compare-counterfactual
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO36-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
```

Acceptance: prefix synchronization/divergence and metric deltas are exact;
annotations preserve source identity; timing-lie audit and human status are explicit;
every player action in the fixture has the complete linked chain and the full outcome
is explainable without reading Kirby2 source code; no required fixture edge is
`UNAVAILABLE`.

Commit: `Compare counterfactual replay branches`

## 14. Work Order 37 — Instructor and researcher console

### WO37-A — Pseudonymous profiles and consent

Objective: make local profile deletion compatible with append-only evidence by
separating direct identity from pseudonymous run identity.

Owned files:

- create `kirby2/instructor/__init__.py`, `kirby2/instructor/identity.py`,
  `kirby2/instructor/consent.py`, and `kirby2/instructor/models.py`;
- create `kirby2/audit/instructor_console.py`;
- modify `kirby2/research/paths.py` to add the erasable identity-mapping area defined
  by the already-versioned `DataPaths` provider.

Fixed decisions:

1. The model vocabulary explicitly defines `InstructorProfile`, `LearnerProfile`,
   `Assignment`, `AssignmentAttempt`, `ReviewAnnotation`, `Rubric`, `CurriculumPlan`,
   `Cohort`, and `ResearchStudy`, each with a schema/version. Instructor and learner
   evidence uses immutable pseudonymous profile IDs plus separately erasable direct-
   identity mappings. Every other named type is an immutable revision record; a
   change creates a successor ID and predecessor linkage instead of mutating history.
2. Immutable runs contain opaque pseudonymous learner IDs only. Display names and
   direct identifiers live in an erasable local mapping.
3. Consent records version, scope, time, retention, pseudonymous-run retention after
   deletion, export permission, and withdrawal policy.
4. Pseudonymization is not called anonymity. Retention after profile deletion requires
   prior consent/study policy.
5. Identity mapping is never packaged or exported by default.
6. Deletion cannot rewrite or relabel source evidence; it removes permitted mappings
   and creates an immutable deletion receipt outside the deleted identity payload.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO37-A
```

Acceptance: create/resolve/delete flows work locally; immutable evidence has no direct
identity; unauthorized retention/export fails.

Commit: `Separate learner identity from evidence`

### WO37-B — Versioned assignments, rubrics, and reviews

Objective: create immutable instructional plans and reviews linked to exact content
and scoring versions.

Owned files:

- create `kirby2/instructor/assignments.py`, `kirby2/instructor/rubrics.py`, and
  `kirby2/instructor/reviews.py`;
- modify `kirby2/audit/instructor_console.py`, `kirby2/research/models.py`,
  `kirby2/research/store.py`, and `kirby2/research/tables.py` for instructor artifacts
  only.

Fixed decisions:

1. Assignments bind exact lesson or lesson-pool, curriculum/strategy/scenario/pack
   digests, allowed scenario variations, seed policy, mode, strategy policy,
   hotkey-layout policy, explicit objective, attempt limit, optional deadline metadata,
   feedback timing, reveal policy, scoring/rubric versions, and required research-
   consent metadata.
2. Locks explicitly cover latency, volume, liquidity, strategy, objective, venue
   count, hidden-state reveal policy, and seed policy. They are enforced by runtime and
   bound into attempt manifests, not merely disabled in a view.
3. Review operations explicitly open attempt, replay attempt, inspect causal trace,
   compare attempts, annotate timeline, tag errors, write feedback, mark review
   complete, and attach rubric result. Reviews link exact replay/trace, rubric items,
   evidence/event IDs, error tags, feedback, and reviewer sidecar; source runs remain
   immutable.
4. Rubric correction creates a new version and derived score sidecar. Original scores
   retain their original version.
5. Deadline metadata has no enforcement claim unless an explicit recorded clock and
   policy are later authorized.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO37-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: locked-parameter circumvention fails; assignment/attempt/review lineage
reloads exactly; correction never edits the source attempt.

Commit: `Version assignments rubrics and reviews`

### WO37-C — Reproducible local studies

Objective: bind cohort/research comparisons to a complete immutable study protocol
before included attempts are observed.

Owned files:

- create `kirby2/instructor/cohorts.py`, `kirby2/instructor/studies.py`, and
  `kirby2/instructor/statistics.py`;
- modify `kirby2/audit/instructor_console.py`.

Fixed decisions:

1. Study manifest records question, hypothesis, assignment set, exploratory/
   confirmatory status, preregistration digest/time, population, allocation/
   randomization, blinding/reveal, content/parameter locks, declared metrics and
   primary/secondary outcomes, sample rationale, stopping, missing-data, multiplicity,
   inclusion/exclusion, analysis plan/version, seed policy, software version, consent,
   retention, and data-export policy.
2. Amendments and protocol deviations are immutable sidecars.
3. Cohort summaries show counts, denominators, uncertainty, missingness, score/model
   versions, and compatibility decisions.
4. Mixed incompatible versions are stratified or refused, never silently pooled.
5. Views default to `DESCRIPTIVE`; causal language requires a design and analysis that
   support it.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO37-C
```

Acceptance: post-attempt protocol mutation fails; amendments are visible; incompatible
pooling and unsupported causal claims are refused.

Commit: `Lock reproducible local studies`

### WO37-D — Instructor and research console queries

Objective: expose local profile, assignment, review, cohort, and study workflows over
immutable source artifacts.

Owned files:

- create `kirby2/instructor/query.py`, `kirby2/instructor/commands.py`, and
  `kirby2/instructor/console.py`;
- modify `kirby2/audit/instructor_console.py`.

Fixed decisions:

1. Console operations create/list profiles, assignments, attempts, reviews, cohorts,
   studies, amendments, comparisons, and links into the microscope.
2. Every view displays content/scoring/model versions, sample count, uncertainty,
   capability, consent/export eligibility, and source identity.
3. Query results are deterministic for an explicit `as_of` ledger point.
4. Comparison views explicitly cover same learner across attempts, same lesson across
   learners, same skill across scenarios, same hotkey layout across sessions, same
   strategy across volume regimes, and manual execution versus benchmark algorithm.
5. Demonstration data contains exactly two learner profiles, one hidden-liquidity
   assignment, three attempts per learner, one rubric, one review bundle containing a
   completed review/annotation for every one of both learners' six attempts, and one
   cohort comparison. It proves workflow and isolation only; it does not prove learner
   or cohort differences.
6. No account, network service, telemetry, cloud synchronization, subscription,
   social feed, or public leaderboard is introduced.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 instructor-demo --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO37-D
```

Acceptance: two pseudonymous learners each complete three attempts under the one
hidden-liquidity assignment; all six attempts are reviewed and annotated; every
required comparison view is queryable; the rubric/review/cohort artifacts have exact
lineage, counts, and uncertainty; no cross-profile leakage or one-attempt ranking
occurs.

Commit: `Build instructor research console queries`

### WO37-E — Redacted export and profile deletion

Objective: export portable authorized evidence and exercise deletion without
misrepresenting redaction or mutating retained runs.

Owned files:

- create `kirby2/instructor/export.py`, `kirby2/instructor/redaction.py`, and
  `kirby2/instructor/deletion.py`;
- create `kirby2/instructor/fixtures/privacy_export.toml`;
- modify `kirby2/instructor/commands.py` and
  `kirby2/audit/instructor_console.py`.

Fixed decisions:

1. Until WO39 exists, export an unpacked canonical directory plus manifest using the
   repaired path-containment/canonical-digest primitives. Do not create an archive,
   installer, CAS, or competing pack format. WO39 later packages this exact directory.
2. Export uses an allowlist and writes a field-level redaction manifest, consent
   decision, retained references, content digests, and compatibility versions.
3. Authorized portable evidence explicitly contains assignment, attempt manifest,
   scores, annotations, selected causal traces, provenance, software version, and
   limitations. Omitted items are listed with reason; export never silently widens
   consent.
4. Identity mapping, secrets, local paths, and hidden/reveal data outside authorized
   scope are excluded.
5. Profile deletion is non-destructive to consent-authorized pseudonymous evidence and
   refuses retention when policy does not allow it.
6. Import into a clean data root verifies all bytes before registration.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 instructor-export-demo --fixture kirby2/instructor/fixtures/privacy_export.toml --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO37-E
```

Acceptance: redaction manifest matches bytes; clean-root import works; deletion removes
permitted identity while retained evidence keeps only authorized pseudonymous links.

Commit: `Export redacted research bundles`

## 15. Work Order 39 prerequisite — Secure pack substrate

The data-only pack container and atomic installation boundary precede LAN artifact
transport. This prevents WO38 from creating a second unverified bundle protocol.

### WO39-A — Canonical data-only pack identity

Objective: define `.k2pack` as a deterministic manifest plus content inventory whose
logical identity is independent of incidental archive metadata.

Owned files:

- create `kirby2/packs/__init__.py`, `kirby2/packs/models.py`,
  `kirby2/packs/identity.py`, and `kirby2/packs/formats.py`;
- create `kirby2/audit/packs.py`.

Fixed decisions:

1. Manifest records pack/schema/canonicalization version, namespace/name, human title,
   version, creator metadata, type, engine/compiler/schema compatibility, dependencies
   with constraints/digests, provenance, license/redistribution, capability labels,
   complete file inventory, and entrypoint references that are data identifiers only.
2. `pack_id` hashes canonical manifest identity plus every allowed file path, byte
   count, and digest. `transport_sha256` separately hashes exact archive bytes.
3. ZIP entry order, timestamps, owner IDs, permissions, and compression choices are
   normalized and do not create different logical identities.
4. Pack contents are allowlisted TOML, Parquet, canonical JSON/event streams, report
   data, and specifically declared non-executable binary evidence. Analysis packs may
   not provide HTML/JavaScript renderer code; installed digest-bound Kirby2 code
   renders their data. No Python, shell, native libraries, macros, or arbitrary
   executable content.
5. Compatibility distinguishes `READABLE`, `INSTALLABLE`, `EXECUTABLE`, and
   `REPLAY_EQUIVALENT`.
6. Namespace/version policy is exact: `creator_id` is the SHA-256 of canonical creator
   metadata and is an identity key, not proof of authorship; `namespace` is a
   lowercase dot-separated identifier whose segments match `[a-z][a-z0-9-]*`;
   `name` follows the same segment rule; and `version` is canonical SemVer 2.0.0 with
   no leading-zero aliases. The registry key is
   `(creator_id, namespace, name, version)`. Distinct creator IDs may use the same
   textual namespace without collision; the UI must show creator identity. One key is
   immutable: differing bytes/digest under it fail.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-A
```

Acceptance: normalized builds share logical ID; semantic/file-byte changes alter it;
raw archive changes affect transport digest; executable or unknown types fail.

Commit: `Define canonical Kirby2 pack identity`

### WO39-B — Hostile archive validation and bounded staging

Objective: validate an entire untrusted archive before safe extraction and leave no
partial active content after failure.

Owned files:

- create `kirby2/packs/validation.py`, `kirby2/packs/archive.py`, and
  `kirby2/packs/staging.py`;
- create `kirby2/packs/hostile_fixtures.py` and governed manifests beneath
  `kirby2/packs/fixtures/hostile_archive/`;
- modify `kirby2/audit/packs.py`.

Fixed decisions:

1. Before extraction reject absolute/parent/backslash/drive/UNC/NUL paths, symlinks,
   hardlinks, devices, FIFOs, special files, duplicate entries, case-fold and Unicode
   normalization collisions, manifest mismatch, undeclared files, and type spoofing.
2. Enforce per-file, total expanded bytes, entry count, path length/depth, compression
   ratio/method, nested-archive, dependency-count, and parse complexity limits.
3. Validate central directory/inventory first, extract only into a new confined staging
   directory, verify bytes/parsers again, then make the staging result eligible for
   activation.
4. Defend against check/use replacement through opened handles or post-extraction
   revalidation; never follow extracted links.
5. Failure may quarantine the original bundle and diagnostic only. It never exposes
   partial extracted content as installed.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: every hostile class is rejected with stable code before activation; a
safe nested-directory pack stages and revalidates; no path escapes or partial install.

Commit: `Validate hostile pack archives`

### WO39-C — Registry, dependencies, and atomic installation

Objective: activate verified packs under deterministic namespaces without automatic
network resolution or damage to referenced evidence.

Owned files:

- create `kirby2/packs/registry.py`, `kirby2/packs/dependencies.py`, and
  `kirby2/packs/install.py`;
- modify `kirby2/research/paths.py` to activate its already-contracted pack/staging
  areas;
- modify `kirby2/audit/packs.py`.

Fixed decisions:

1. Resolve only already available local packs by exact namespace/version/digest.
   Missing dependencies fail; no automatic Internet fetch.
2. Reject cycles, ambiguous providers, version/digest conflict, incompatible engine,
   and activation outside the data root.
3. Activation is atomic after complete validation. Registry/activation lockfile is
   canonical and crash-safe.
4. Removal refuses active dependents and preserves completed run evidence. Deactivate
   before recoverable removal; never mutate a referenced run.
5. Installed artifacts remain content-addressed and read-only through the registry.
6. Dependency constraints use canonical SemVer ranges plus an exact expected pack
   digest. Resolution considers only installed candidates, sorts by full registry key,
   and fails on zero or multiple compatible matches; it never chooses an ambiguous
   automatic `latest` version.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: dependency order and registry are deterministic; crashes/failures preserve
the prior activation; removal and reference protections hold.

Commit: `Install Kirby2 packs atomically`

## 16. Work Order 38 — Distributed experiment orchestration

### WO38-A — Logical work and attempt identity

Objective: define deterministic experiment decomposition without placing operational
retry state inside scientific work identity.

Owned files:

- create `kirby2/orchestration/__init__.py`, `kirby2/orchestration/models.py`,
  `kirby2/orchestration/planner.py`, and `kirby2/orchestration/seeds.py`;
- create `kirby2/audit/orchestration.py`.

Fixed decisions:

1. `LogicalWorkUnit` binds experiment/partition/cell, canonical configuration,
   scenario digest, market-profile digest, dataset/strategy/pack digests, seed,
   software/source version, engine/compiler/schema/capability identities, expected
   outputs, and resource class. It excludes attempt, worker, lease, and wall-clock
   fields.
2. `WorkAttempt` binds logical ID, attempt number, worker, lease/heartbeat operational
   records, outcome, diagnostics, and returned artifact digest.
3. Planner derives unique seeds from versioned master identity and stable logical cell
   ID, never list index or process ordering.
4. Canonical aggregation order is logical work-unit ID.
5. Reissue retains logical ID; two successful differing result digests are a
   determinism failure and are quarantined.
6. The coordinator contract explicitly partitions an experiment, derives seeds,
   enqueues logical units, assigns compatible workers, tracks leases, validates
   returned results/artifacts, retries operational attempts, aggregates in canonical
   order, and persists immutable results plus operational history.
7. V1 distributes only independent complete runs, counterfactual branches,
   calibrations, and strategy evaluations. It never distributes one mutable order book
   or one session's event loop across machines.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO38-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: planner is stable under input permutation and worker-count changes; seeds
are unique/complete; attempt metadata cannot change logical identity.

Commit: `Define deterministic work unit identity`

### WO38-B — Single-process and local multiprocess orchestration

Objective: execute independent logical runs through one coordinator/worker protocol,
reusing the repaired audit worker seam where applicable.

Owned files:

- create `kirby2/orchestration/coordinator.py`,
  `kirby2/orchestration/worker.py`, `kirby2/orchestration/local.py`, and
  `kirby2/orchestration/protocol.py`;
- create `kirby2/orchestration/commands.py` with the initial plan/coordinator/worker
  registration shared by every later backend;
- create `kirby2/orchestration/examples/small.toml`;
- minimally generalize `kirby2/auditlab/worker.py` rather than creating a conflicting
  execution mechanism;
- modify `kirby2/audit/orchestration.py`.

Fixed decisions:

1. Protocol accepts typed data-only work and returns typed artifacts/diagnostics; it
   never accepts shell commands, Python source, pickle, or dynamic imports.
2. Single-process and local multiprocess backends implement identical interfaces.
3. Worker compatibility binds exact implementation/source digest, Python/runtime and
   dependency identity, schemas, compiler, and capabilities—not a display version.
4. Coordinator independently verifies returned manifest, bytes, logical/result
   identity, replay, and invariants before registration.
5. Completion order and worker count do not influence aggregate bytes.
6. A worker fetches only referenced manifests/artifacts, validates digests and exact
   compatibility, runs the declared logical unit, executes its required runtime audit,
   and returns data-only result artifacts/status/diagnostics. It cannot select another
   task or weaken a gate.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 orchestration-demo --manifest kirby2/orchestration/examples/small.toml --backends single,local --workers 3
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO38-B
```

Acceptance: complete experiment results, artifacts, and aggregate digest match between
single and local backends; incompatible or forged workers fail.

Commit: `Orchestrate local experiment workers`

### WO38-C — Verified content-addressed artifact exchange

Objective: transfer required immutable inputs and outputs using the validated pack/CAS
boundary rather than ambient shared paths.

Owned files:

- create `kirby2/orchestration/artifacts.py`,
  `kirby2/orchestration/content_store.py`, and
  `kirby2/orchestration/compatibility.py`;
- consume `kirby2/packs/validation.py`, `kirby2/packs/archive.py`, and
  `kirby2/packs/staging.py` without changing their security semantics;
- modify `kirby2/audit/orchestration.py` and `kirby2/audit/packs.py` for the explicit
  transfer/clean-root integration cases.

Fixed decisions:

1. Workers request content by digest; coordinator sends bounded validated data-only
   bundles. No path references outside registered roots.
2. Receiver verifies transport bytes, logical pack/content identity, compatibility,
   and parser limits before activation.
3. Results are immutable content-addressed artifacts with independent coordinator
   verification; worker self-report alone is insufficient.
4. Temporary material is per-attempt and removable only before registration.
5. Historical licensing/redistribution restrictions can refuse transfer even when
   bytes are locally readable.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO38-C
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-C
```

Acceptance: clean-root worker receives only required digests and reproduces the
reference result; corruption, capability, license, and path attacks fail before use.

Commit: `Transfer verified experiment artifacts`

### WO38-D — Authenticated trusted-LAN orchestration

Objective: add explicit opt-in LAN workers with bounded authenticated integrity and no
claim that a LAN is inherently trusted.

Owned files:

- create `kirby2/orchestration/lan.py`, `kirby2/orchestration/security.py`,
  `kirby2/orchestration/leases.py`, and `kirby2/orchestration/resources.py`;
- create committed test-only PKI fixtures beneath
  `kirby2/orchestration/fixtures/test_pki/` and register them as package data;
- modify `kirby2/orchestration/protocol.py`, `kirby2/orchestration/commands.py`, and
  `kirby2/audit/orchestration.py`.

Fixed decisions:

1. Default bind is loopback. LAN v1 requires Python stdlib `ssl.SSLContext` with
   minimum and maximum exactly `ssl.TLSVersion.TLSv1_3`, operator-provided local
   CA/server/worker certificates, mutual authentication, pinned coordinator
   certificate fingerprint, hostname/identity validation, and no plaintext fallback.
   A platform without TLS 1.3 is `UNSUPPORTED` and cannot pass this card's LAN gate.
   The repository never generates an operator CA or invents cryptography. Any move
   beyond stdlib TLS requires an amendment/approval.
2. Protocol has strict schemas, message/stream/time/resource limits, nonces/session
   identity, and replay protection. It carries no executable payload.
3. Leases, heartbeats, backpressure, cancellation, and worker capability/resource
   advertisements are operational records excluded from simulation/result identity.
4. Stage-level access control prevents active WO35 search workers from receiving
   sealed holdout artifacts.
5. Coordinator restart state is crash-safe and distinguishes queued, leased,
   completed-unverified, registered, failed, cancelled, and quarantined work.
6. The committed PKI is a deterministic, explicitly flagged audit fixture valid only
   for loopback/test identities. Production/LAN startup refuses it and requires
   operator-provided credentials outside the repository; fixture private keys never
   establish a security claim for real LAN use.
7. Resource policy declares and enforces maximum concurrent runs, per-run memory,
   disk and elapsed-time bounds, queue backpressure, whole-experiment cancellation,
   and per-attempt temporary cleanup. Limits and decisions are operational ledger
   records; a resource abort never becomes a successful scientific result.
8. Do not add Kubernetes, cloud orchestration, a service mesh, remote shell, or an
   executable job format.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO38-D
```

Acceptance: unauthenticated, replayed, oversized, incompatible, unauthorized-holdout,
and executable-payload requests fail; loopback remains default; valid LAN protocol
preserves local result identity.

Commit: `Secure trusted LAN orchestration`

### WO38-E — Recovery, idempotence, and deterministic aggregation

Objective: prove killed workers, duplicate/late results, coordinator restart, and
completion reordering cannot lose work or alter scientific results.

Owned files:

- create `kirby2/orchestration/recovery.py` and
  `kirby2/orchestration/aggregation.py`;
- modify `kirby2/orchestration/commands.py` to complete submit/status/cancel/resume;
- complete `kirby2/audit/orchestration.py`.

Fixed decisions:

1. Lease expiry reissues the same logical unit with a new attempt.
2. Identical late success is idempotent; conflicting success is quarantined as
   `DETERMINISM_FAILURE` and no result is selected.
3. Aggregation sorts by logical ID and uses deterministic reduction; floating sums
   may not depend on arrival order.
4. Cleanup removes only unregistered attempt staging and never immutable evidence.
5. The demonstration is one complete multi-seed strategy experiment. It runs once
   with the single-process reference and once with one local worker plus at least one
   additional process or authenticated-LAN worker, kills at least one worker, restarts
   the coordinator, varies worker count/completion order, and compares the whole
   experiment, not a convenient subset. If real LAN is unavailable, an additional
   local process exercises recovery while LAN remains separately `NOT_EXERCISED`.
6. Modular commands expose `orchestrate plan`, `coordinator`, `worker`, `submit`,
   `status`, `cancel`, and `resume`; every state-changing command emits an operational
   event without changing logical scientific identity.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 distributed-demo --seed 42 --kill-worker
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO38-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: no seed is lost/reused; retries retain logical identity; single/local/LAN
canonical results match where LAN is exercised; unavailable LAN is `NOT_EXERCISED`,
not silently passed.

Commit: `Recover and verify distributed experiments`

## 17. Work Order 39 — Portable domain packs

### WO39-D1 — Scenario, lesson, curriculum, strategy, and profile packs

Objective: package authoring/training/model artifacts without inventing weaker
pack-local representations.

Owned files:

- create `kirby2/packs/builders.py`, `kirby2/packs/types.py`, and
  `kirby2/packs/commands.py`;
- create exact adapters `kirby2/packs/scenario_pack.py`,
  `kirby2/packs/lesson_pack.py`, `kirby2/packs/curriculum_pack.py`,
  `kirby2/packs/strategy_pack.py`, and `kirby2/packs/profile_pack.py`;
- modify `kirby2/audit/packs.py`.

Fixed decisions:

1. Pack types in this card: scenario, lesson, curriculum, strategy, and market
   profile.
2. Embedded runs/audits keep their original identities. Pack identity wraps rather
   than replaces them.
3. Lesson/curriculum content preserves source, detector, capability, observable/reveal,
   skill, scoring, and review-sidecar identity.
4. Strategies preserve legacy source and canonical AST identity plus experiment
   lineage; profiles preserve preregistration/review status.
5. This card implements exact generic lifecycle commands `pack build
   SOURCE_DIRECTORY`, `pack inspect FILE.k2pack`, `pack verify FILE.k2pack`,
   `pack install FILE.k2pack`, `pack list`, and `pack remove PACK_ID` using only the
   WO39-A-C substrate. `pack export-run RUN_ID` is added by WO39-D2 once replay/
   analysis adapters exist.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 pack-build-demo --type scenario --source kirby2/scenario_lang/examples/full_day.toml
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-D1
```

Acceptance: all five types round-trip canonical identity and provenance; unsupported
type, missing dependency, and reveal-policy violations are explicit.

Commit: `Package Kirby2 training artifacts`

### WO39-D2 — Historical, replay, analysis, and research packs

Objective: package evidence-bearing artifacts with capability, license, privacy, and
redaction rules intact.

Owned files:

- create `kirby2/packs/historical_pack.py`, `kirby2/packs/replay_pack.py`,
  `kirby2/packs/analysis_pack.py`, and `kirby2/packs/research_pack.py`;
- modify `kirby2/packs/builders.py`, `types.py`, `commands.py`, and
  `kirby2/audit/packs.py`.

Fixed decisions:

1. Historical packs declare explicit capability record, provenance, source license,
   redistribution policy, and `SELF_CONTAINED` versus `REFERENCE_ONLY`.
2. Replay packs preserve original run/checkpoint/event/result identities and required
   renderer/engine compatibility.
3. Analysis packs contain canonical report data and annotations only, never
   JavaScript/HTML renderer code.
4. Learner/review/research packs enforce consent and field-level redaction; direct
   identity is excluded unless an explicitly authorized private export mode exists.
5. Existing run/audit identities remain authoritative and are never replaced by a
   weaker pack-local digest.
6. Add `pack export-run RUN_ID`; it selects only registered typed artifacts, applies
   capability/license/privacy rules, and delegates archive identity/validation to the
   same builders. It never copies an ambient run directory blindly.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-D2
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-historical-lessons
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-counterfactuals
```

Acceptance: all four types round-trip canonical evidence/provenance; privacy,
licensing, capability, reference-only, and renderer-injection refusals are explicit.

Commit: `Package Kirby2 evidence artifacts`

### WO39-E — Portability, compatibility, signatures, and hostile-pack audit

Objective: qualify safe offline movement into a clean data root and expose optional
authenticity without equating a signature with safety or scientific validity.

Owned files:

- create `kirby2/packs/signatures.py` and governed sample/hostile fixtures;
- create exact source manifests
  `kirby2/packs/fixtures/samples/starter_scenario/manifest.toml`,
  `kirby2/packs/fixtures/samples/five_lesson_curriculum/manifest.toml`,
  `kirby2/packs/fixtures/samples/traffic_light_strategy/manifest.toml`,
  `kirby2/packs/fixtures/samples/historical_reconstruction/manifest.toml`, and
  `kirby2/packs/fixtures/samples/portable_completed_lesson/manifest.toml` plus only
  their declared payload files;
- create governed hostile sources beneath `kirby2/packs/fixtures/hostile/`;
- complete `kirby2/audit/packs.py` and `kirby2/packs/commands.py`.

Fixed decisions:

1. Signature support is provider-based and optional. Do not implement novel
   cryptography; adding a dependency requires approval. Unsigned packs remain clearly
   labeled and undergo identical structural validation.
2. Verification reports structural safety, digest integrity, signer/authenticity,
   compatibility, capability, provenance, privacy, and scientific status separately.
3. Clean-root acceptance is offline, starts without source installation state, and
   builds/inspects/verifies/installs/lists/opens/replays/exports/removes through the
   exact command family. The completed lesson plus replay is imported to a second
   clean root and must reproduce the expected replay digest.
4. The five governed sample groups are a starter scenario pack, five-lesson
   curriculum pack, traffic-light strategy pack, historical reconstruction pack, and
   portable completed-lesson/replay/analysis pack.
   The first has stable `scenario_id="KIRBY2_STARTER_PLACE_CANCEL_SCENARIO_V1"`.
   The five-lesson curriculum contains exactly one entry with
   `lesson_id="KIRBY2_STARTER_PLACE_CANCEL_V1"` that declares the scenario pack by
   its content-derived dependency ID and checkpoint selector
   `FIRST_QUIESCENT_CONTINUOUS_TWO_SIDED_V1`. Every `pack_id` remains the exact
   content-derived hash required by WO39-A; duplicate/missing identities or a literal
   symbolic pack ID fail.
5. Hostile suite includes at minimum path traversal, digest mismatch, undeclared file,
   oversized expansion, unsupported schema, missing dependency, capability lie, and
   embedded executable, plus all WO39-B classes, dependency cycles, malformed
   TOML/Parquet, canonicalization mismatch, spoofed extensions, and signed-hostile
   content.
6. Safe removal refuses active dependencies/references and never deletes completed
   run evidence.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 pack-portability-demo --sample-set kirby2/packs/fixtures/samples --hostile-set kirby2/packs/fixtures/hostile --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
```

Acceptance: clean-root replay/export retains declared digest; every hostile fixture is
rejected before activation; the completed lesson/replay reproduces its expected digest;
optional signature status never overrides other gates. The demo owns and cleans its
temporary build/install roots.

Commit: `Audit Kirby2 pack portability`

## 18. Work Order 40 — First distributable release and release gate

### WO40-A — Release data, schema, and migration policy

Objective: finalize platform-standard data locations and versioned compatibility
without rewriting immutable evidence in place.

Owned files:

- create `kirby2/release/__init__.py`, `kirby2/release/models.py`,
  `kirby2/release/platform_paths.py`, and `kirby2/release/migrations.py`;
- modify `kirby2/research/paths.py` through the one platform-aware provider;
- create `kirby2/audit/release.py`.

Fixed decisions:

1. Resolve contained writable areas for configuration, installed packs, datasets,
   immutable runs/evidence, checkpoints, logs, crash reports, temporary/staging files,
   user exports, backups, diagnostics, erasable identity mappings, cache, and release
   artifacts through platform conventions with explicit overrides for acceptance and
   portable use. No writable state is placed in the installation/package directory.
   `release/platform_paths.py` selects platform defaults and constructs the single
   `kirby2.research.paths.DataPaths`; it does not define a second path map.
2. Inventory engine, source, run, checkpoint, pack, compiler, learner, scoring, study,
   and report schemas with readable/writable/replay-equivalent ranges.
3. Migration is staged, digest-verified, resumable, and produces new derived metadata
   or artifacts. Before applying any mutation, `migrations.py` creates a byte-for-byte
   pre-migration backup of every mutable target in the governed backups area, writes a
   canonical source/destination/schema/digest inventory, reads it back, and verifies
   every byte. Backup or verification failure refuses migration. The broader portable
   user-selected backup product arrives in WO40-B1; this minimal mandatory safety
   snapshot is part of migration itself. Migration never rewrites old immutable run/
   event evidence.
4. Unsafe downgrade or unknown future schema is refused with recovery instructions.
5. Developer checkout defaults remain compatible through explicit development mode;
   release behavior does not depend on current working directory.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-A
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-run-store
```

Acceptance: clean, legacy, interrupted-migration, corrupt, downgrade, and relocated
data-root fixtures behave safely; immutable source digests remain unchanged.

Commit: `Version release data and migrations`

### WO40-B — Exact interactive crash recovery

Objective: provide truthful recovery from durable complete checkpoints and refuse
exact-continuation claims when causally required state was not durable.

Owned files:

- create `kirby2/release/recovery.py` and `kirby2/session/journal.py`;
- modify `kirby2/session/live.py`, `kirby2/session/records.py`,
  `kirby2/research/store.py`, `kirby2/research/tables.py`, and
  `kirby2/ui/terminal.py` to write and consume the durable recovery boundary in the
  actual interactive trainer;
- modify `kirby2/audit/release.py` and reuse full-day checkpoint validation.

Fixed decisions:

1. Exact continuation requires a verified complete checkpoint plus durable action,
   message, event, and ledger suffix. Otherwise offer safe replay or abandonment and
   label exact continuation unavailable.
2. Crash probes terminate at pending acknowledgement, partial fill, cancel/fill race,
   checkpoint write, ledger write, pack activation, and profile update boundaries.
3. Recovery record binds active lesson, compiled scenario digest, seed/substreams,
   last complete checkpoint, player actions, client-known working orders, pending
   delivery/replay state, event prefix, and source run.
4. Recovery runs in a fresh process and compares the entire uninterrupted/restored
   suffix under the same observation policy.
5. Incomplete or corrupt recovery state offers safe replay or abandonment and records
   a reason code; it never guesses live exchange state.
6. The live-session journal durably orders player actions, pending client messages,
   acknowledgements, checkpoint commits, and event/ledger prefix commits. Recovery is
   not a detached helper: normal trainer startup detects it, verifies the source run
   and observation policy, and offers only exact continuation, safe replay, or
   abandonment according to the proven durable cut.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-B
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO31-E3
```

Acceptance: exact-restorable crashes match uninterrupted suffixes; incomplete state is
refused with no fabricated continuation.

Commit: `Recover interactive sessions exactly`

### WO40-B1 — Non-destructive backup and restore

Objective: back up selected mutable user state and portable evidence, then restore it
into a separate clean data root without damaging either side.

Owned files:

- create `kirby2/release/backup.py` and `kirby2/release/restore.py`;
- create `kirby2/release/commands.py` with backup/restore registration;
- modify `kirby2/audit/release.py`;
- reuse WO39 validation for bundled portable artifacts.

Fixed decisions:

1. Backup selection covers configuration, pseudonymous profiles, strategies,
   curricula, annotations, learner evidence/projections, run manifests, and portable
   artifacts. Large datasets are explicitly embedded or digest-referenced.
2. Manifest records included/referenced/omitted items, schemas, digests, provenance,
   consent/redaction, encryption status, and source data-root identity.
3. Restore stages and verifies all bytes, parsers, schemas, dependencies, consent, and
   conflicts before atomic activation.
4. Existing destination content is never overwritten without an explicit deterministic
   conflict policy; immutable IDs with differing bytes fail.
5. Failure leaves source/destination active content untouched and preserves diagnostic
   evidence according to policy.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 backup-restore-demo --fixture release-user-data --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-B1
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO39-E
```

Acceptance: selected content restores into an audit-owned clean temporary root with
identical declared digests; conflict, corruption, missing reference, and privacy cases
fail non-destructively.

Commit: `Back up and restore Kirby2 data`

### WO40-C — First-run flow and redacted diagnostics

Objective: make a clean offline installation understandable and diagnosable without
accounts, telemetry, secrets, or ambient developer state.

Owned files:

- create `kirby2/release/first_run.py`, `kirby2/release/doctor.py`, and
  `kirby2/release/diagnostics.py`;
- modify `kirby2/release/commands.py`;
- modify `kirby2/audit/release.py`.

Fixed decisions:

1. First run performs the whole committed flow, offline: create the required mutable
   directories; verify they are writable; show engine/source/runtime/schema version;
   run the health check; install both dependency-resolved members of the bundled
   `RELEASE_STARTER_SET_V1` when the namespace is empty or
   explicitly offer it without overwriting a conflict; launch a short deterministic
   starter demonstration; and print every governed data path. A skipped/refused step
   is explicit and makes the flow incomplete.
2. `version` reports release/source/runtime/schema identity; `doctor` verifies paths,
   permissions, packs, schemas, manifests, dependency/runtime compatibility, and
   recovery state.
3. Diagnostic export uses an explicit allowlist, field-level redaction report, preview,
   and destination chosen by the user. It excludes direct identity, secrets, full
   proprietary data, and hidden lesson truth unless explicitly authorized.
4. No background service, update check, Internet access, telemetry, login,
   subscription, social feed, or public leaderboard.
5. Uninstall guidance preserves user data unless separately requested.
6. Commands are exactly `doctor`, `version`, `data-paths`, `verify-installation`, and
   `export-diagnostics`; legacy aliases, if any, delegate to these handlers.
7. Offer the bundled two-pack `RELEASE_STARTER_SET_V1` from WO39-E and run one short
   deterministic place/cancel demonstration without requiring installation of it.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 version
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 release-first-run-demo --seed 42
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-C
```

Acceptance: first run and diagnostics are offline, deterministic where declared, and
free of forbidden fields; a broken install produces actionable truthful failures.

Commit: `Add first run release diagnostics`

### WO40-D — Preregistered release qualification and packaging protocol

Objective: freeze artifact formats, platform matrices, build commands, dependencies,
licenses, functional workloads, performance thresholds, and abort rules before the
release candidate is frozen or measured.

Owned files:

- create `kirby2/release/build.py`, `kirby2/release/manifest.py`,
  `kirby2/release/licenses.py`, `kirby2/release/packaging.py`,
  `kirby2/release/qualification.py`, `kirby2/release/performance.py`, and
  `kirby2/release/probes.py`;
- modify `kirby2/release/commands.py` to implement `build-release`,
  `verify-release-artifacts`, `release-resource-preflight`, `qualify-release`,
  `qualify-performance`, `close-release`, and `verify-release-closeout` before the
  candidate source freeze;
- create `release/platforms.toml`, `release/qualification.toml`,
  `release/performance_thresholds.toml`, `release/requirements.lock`, and
  `release/artifact_layout.toml`;
- modify `kirby2/audit/release.py` and preregister the generic frozen-evidence
  validators/gates for WO40-D1 and WO40-F through WO40-J through the K2X-02 seam. This card uses
  only synthetic/nonqualification fixtures and does not build or inspect candidate
  results; the future gates report `NOT_EXERCISED` until exact evidence exists.

Fixed decisions:

1. Minimum targets are macOS arm64 and Linux x86_64. Windows is outside this release.
   Both minimum targets must pass before final goal completion unless the user records
   an explicit roadmap amendment.
2. Headless artifacts are a pure project wheel plus deterministic source archive and
   platform wheelhouse. Desktop artifacts are deterministic platform `.tar.gz`
   bundles containing that wheelhouse, local launchers, terminal trainer, CLI
   authoring, installed offline microscope renderer/assets, both complete members of
   `RELEASE_STARTER_SET_V1`, manifest,
   licenses, and notices. They are not marketed as a native widget GUI.
3. V1 build frontends are `./.venv/bin/pip wheel --no-deps --no-build-isolation` for
   Kirby2, `git archive` for source, and repository-owned stdlib tar/gzip assembly.
   Dependency wheels must already exist in the declared wheelhouse; release builds
   run with `--no-index` and do not fetch from the network.
4. `ReleaseManifestV1` has exact required fields: release version, candidate Git
   commit, build timestamp, compiler/runtime versions, dependency versions and wheel
   digests, schema versions, included pack/asset digests, supported OS/architecture,
   known limitations, every included payload-member and subordinate-artifact digest,
   plus licenses/layout/logical build identity. It excludes its own member digest and
   its containing archive's transport digest; the external `ReleaseArtifactIndexV1`
   records those digests and release-set identity. Build timestamp is provenance
   metadata excluded from logical identity.
5. Functional matrix includes clean install; launch; the full first-run flow; starter
   lesson; place and cancel; complete and save; open the replay microscope; export a
   pack; close; reopen; verify the saved session; import into a second clean root,
   replay the imported lesson there, and compare its declared replay digest exactly;
   restore backup; crash recovery; diagnostics; and uninstall preserving user data.
   The headless matrix additionally exercises simulation, audit, calibration, and one
   distributed-worker run through the installed artifact.
6. Performance manifest freezes interactive latency/UI stability, full-day generation
   and replay, peak memory, ledger/checkpoint growth, analysis load, and exactly 10,000
   complete run work units. Each work unit is one full registered run with its complete
   result/artifact/audit record—not an event, subtask, shard fragment, or retry. Cells,
   seeds, resources, retries, and abort policy are fixed before measurement.
   Archive normalization, samples, workloads, resources, thresholds, warnings,
   aborts, and the integer-core subset are exactly section 5.7.8.
7. Cross-platform byte comparison is exact only for a preregistered
   `CROSS_PLATFORM_INTEGER_CORE_V1` workload whose mechanics, inputs, reductions, and
   outputs are integer-only and do not call float/libm stochastic generation. Exact
   same-platform desktop/headless replay remains required. Float/libm-driven full-day
   outputs are runtime/platform scoped and are not claimed cross-platform exact unless
   a later separately preregistered proof establishes it.
8. All release commands and D1/F-J evidence validators are fully implemented and
   audited here against synthetic protocol fixtures. The preregistered WO40-J gate is
   specifically `WO40_J_PREREQUISITES_V1`: it checks every required prior canonical/
   deviation gate and immutable evidence reference but never checks or creates its own
   closeout packet, avoiding self-reference. `audit-expansion --gate all` verifies
   stored one-time evidence and never reruns a qualification, holdout, platform run,
   or performance workload. This card parses/digests
   protocols and validates command refusal/dispatch only; it does not build or inspect
   actual release qualification results.
9. WO40-D commits every source-independent `ReleasePerformanceRowTemplateV1` byte and
   the exact `RUNNER_SOURCE_TREE_V1` resolver. It cannot hash future WO40-E bytes.
   WO40-E's one mechanical source lock is therefore an expressly preregistered
   candidate binding, not a post-result protocol choice; changing its projection or
   resolver requires a release-restart amendment.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D
```

Acceptance: every format, target, dependency, license, row template, source-binding
rule, workload, metric, threshold, sample count, and abort rule is canonical and
digest-bound before any release result; synthetic binding proves the later mechanical
candidate lock has no discretionary input.

Commit: `Preregister Kirby2 release qualification`

### WO40-D1 — Read-only release-resource preflight

Objective: prove the exact offline build inputs and both real clean target environments
exist before freezing a candidate that cannot be qualified.

Owned files:

- create `KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md` only;
- do not download, install, build, alter credentials, or modify source/protocol files.

Fixed decisions:

1. Invoke WO40-D's read-only preflight against the committed lock/protocol/layout.
   Inventory and SHA-256 every required macOS-arm64 and Linux-x86_64 wheelhouse file,
   external packaging tool, and both already-committed `RELEASE_STARTER_SET_V1`
   pack inventories; prove no dependency
   requires a network fetch. Candidate source and `release/launchers/` are explicitly
   excluded because WO40-E has not created/frozen them; WO40-F hashes and verifies
   those candidate-owned inputs before building.
2. Resolve an explicit real clean-environment provider for both target platforms and
   record access method/capability without secrets: OS, architecture, runtime support,
   available disk/memory, offline install boundary, clean-root mechanism, and how
   evidence returns. A local developer checkout is not a clean provider.
3. Read-only discovery does not authorize a remote connection, credential creation,
   download, or installation. If access itself needs new authority, stop and request
   it before probing.
4. Any missing/mismatched wheel, unavailable platform, absent credential, or
   insufficient resource is an exact hard pause with a machine-readable missing-item
   list. Do not commit this card as passing, weaken the target matrix, or freeze WO40-E
   until the user/environment supplies the named resource and the preflight passes.
5. The committed report contains no secret/path beyond the governed redaction policy
   and binds WO40-D commit plus every inventory/provider fingerprint.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 release-resource-preflight --platforms release/platforms.toml --lock release/requirements.lock --qualification release/qualification.toml --no-network --output KIRBY2_RELEASE_RESOURCE_PREFLIGHT.md
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-D1
```

Acceptance: both exact wheelhouses and both real clean target providers are available,
hash-verified, offline-capable, and sufficient for the frozen matrix; otherwise the
goal pauses here with the exact artifact/access request and this card is not completed.

Commit: `Verify release build resources`

### WO40-E — Frozen release-candidate source

Objective: finish every source, launcher, packaging, version, starter-content, and
user-documentation change, then freeze one clean commit from which all artifacts and
platform evidence are produced.

Owned files:

- create `kirby2/release/desktop.py`, `kirby2/release/headless.py`, and
  exact POSIX launcher files `release/launchers/macos/kirby2`,
  `release/launchers/linux/kirby2`, and `release/launchers/headless/kirby2`;
- create the mechanically derived
  `release/performance_runner_sources.lock` after every other candidate-owned path is
  staged; it is the only WO40-D protocol binding materialized in this card;
- create `docs/USER_GUIDE.md`, `docs/SCENARIO_AUTHORING.md`,
  `docs/INSTRUCTOR_RESEARCH.md`, `docs/SECURITY_PRIVACY.md`,
  `docs/TROUBLESHOOTING.md`, and `docs/LIMITATIONS.md`;
- modify `pyproject.toml`, `kirby2/__init__.py`, `kirby2/ui/terminal.py`,
  `kirby2/microscope/commands.py`, `kirby2/microscope/report.py`, and
  `kirby2/audit/release.py` only as required by the frozen WO40-D layout.

Fixed decisions:

1. `DESKTOP_V1` means the local terminal execution trainer, terminal/CLI scenario
   authoring, and locally opened bundled offline HTML analysis. Native widget GUI is
   outside this batch.
2. Desktop and headless paths invoke the same canonical plan/runtime/store code and
   produce identical canonical run contents for identical inputs.
3. Opening a local report is explicit user action; no local web server is required.
4. Launcher contains no telemetry, updater, account, brokerage, subscription, social
   feed, public leaderboard, or background daemon.
5. Display assets and licenses enter the release manifest.
6. Documentation covers installation, first session, hotkeys, traffic-light scripts,
   scenario files, lesson packs, historical labels, replay analysis, data locations,
   backup/recovery, privacy/security, limitations, and troubleshooting.
   Every user-facing documentation set states all five boundaries explicitly: Kirby2
   is a simulation/training environment; it is not a broker; it is not a live market
   connector; it provides no performance guarantee; and a reconstruction is not proof
   of historical market state.
7. Both content-derived pack IDs and the set digest of `RELEASE_STARTER_SET_V1` are
   bound and offered together on first run. Neither is silently installed over
   existing content, and an unresolved dependency refuses the starter flow.
8. After this card commits, WO40-F through WO40-J may add evidence/closeout files only.
   They invoke the commands and generic validators preregistered in WO40-D and do not
   add command handlers, probes, gate registrations, source, package, or documentation.
   Any discovered need for a production/package/doc/protocol change invalidates the
   candidate and is a hard user-authority pause. The user must explicitly authorize a
   uniquely recorded release-restart amendment; canonical WO40-D/E subjects may not be
   duplicated or silently replayed under section 5.6.
9. Generate and verify `release/performance_runner_sources.lock` exactly by section
   5.7.8 after all source/package changes are staged. This is a mechanical source-byte
   binding, not a workload, threshold, fixture-value, or result amendment. Refuse the
   candidate commit if any template cannot bind or any indexed source differs.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-E
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 ui --help
```

Acceptance: desktop/headless equivalent run digests match; bundled reports open from
relocated installation content; all release audits pass from a clean tree; no
native-GUI claim is made. The resulting clean commit is the sole candidate source.

Commit: `Freeze the first Kirby2 release candidate`

### WO40-F — Immutable headless and desktop release artifacts

Objective: build both platform artifact families twice from the exact clean WO40-E
candidate and record immutable manifests/digests without modifying candidate source.

Owned files:

- create `KIRBY2_RELEASE_BUILD_EVIDENCE.md` only in Git;
- write built artifacts/manifests to the explicit release artifact store governed by
  `DataPaths`; do not commit generated wheels/bundles into the source tree.

Fixed decisions:

1. Resolve and record the WO40-E commit and its exact
   `release/performance_runner_sources.lock` blob before building. Refuse a dirty tree,
   a lock whose source projection does not reproduce from that commit, or any
   protocol/dependency/layout digest mismatch.
2. Build headless wheel/source archive and desktop bundles for macOS arm64 and Linux
   x86_64 from that exact commit using WO40-D commands and preverified wheelhouses.
3. Repeat each build under the declared reproducibility environment. Compare logical
   identity and exact bytes where promised; explain permitted platform/container
   differences.
4. Verify manifests, dependency/license inventory, bundled pack/assets, no developer
   data, and offline installability before registration.
5. Evidence commit adds only the build-evidence document; artifacts remain immutable
   and reference the earlier candidate commit, not this evidence commit.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 build-release --candidate HEAD --protocol release/qualification.toml --artifact-store .kirby2/release
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 verify-release-artifacts --candidate HEAD --artifact-store .kirby2/release
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-F
```

Acceptance: both target artifact families exist and verify against one candidate;
repeat-build results follow the committed reproducibility claim; no package/source
file changed after candidate freeze.

Commit: `Build Kirby2 release artifacts`

### WO40-G — macOS clean-environment qualification

Objective: qualify the immutable macOS arm64 artifacts on a clean macOS environment,
not the developer checkout.

Owned files:

- create `KIRBY2_RELEASE_MACOS_EVIDENCE.md` only;
- do not modify release probes, production code, packaging, or documentation.

Fixed decisions:

1. Use only the WO40-D matrix and WO40-F artifact bound to the WO40-E candidate.
2. In a clean environment exercise the exact matrix: offline install; launch; complete
   first run and starter; place/cancel; complete/save; open replay microscope; export
   pack; close; reopen; verify the session; import to a second clean root, replay it
   there, and compare the declared digest exactly; restore
   backup; crash recovery; diagnostics; and uninstall preserving user data. Exercise
   the installed headless simulation, audit, calibration, and distributed-worker
   paths as separate rows.
3. Record OS/architecture/runtime/machine, artifact/manifests, commands, exit statuses,
   run/replay/diagnostic digests, timings, warnings, and `NOT_EXERCISED` capabilities.
4. Checkout execution cannot substitute for installed-artifact evidence.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 qualify-release --platform macos-arm64 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-G
```

Acceptance: the full committed macOS matrix passes against the immutable artifact and
the evidence document binds exact candidate/artifact identities. Otherwise status is
`FAIL` or `NOT_RUN` and final closeout cannot pass.

Commit: `Qualify macOS release artifacts`

### WO40-H — Linux clean-environment qualification

Objective: qualify the immutable Linux x86_64 artifacts on a real clean Linux
environment and refuse final release completion without that evidence.

Owned files:

- create `KIRBY2_RELEASE_LINUX_EVIDENCE.md` only;
- do not modify release probes, production code, packaging, or documentation.

Fixed decisions:

1. Use only the WO40-D matrix and WO40-F artifact bound to the WO40-E candidate.
2. Run the same exact full functional/headless matrix as macOS in a clean Linux
   environment: launch, starter, place/cancel, complete/save, open replay microscope,
   export pack, close, reopen, verify session, import into a second clean root, replay
   and compare the declared digest, restore backup, diagnostics,
   simulation, audit, calibration, distributed worker, and uninstall.
3. Require byte-identical macOS/Linux run and replay identities only for WO40-D's
   `CROSS_PLATFORM_INTEGER_CORE_V1`. Compare same-platform desktop/headless runs
   exactly. Report float/libm-driven full-day outputs under their bound runtime/
   platform identity and do not imply cross-platform equality from similar summaries.
4. Record distribution/kernel/architecture/runtime/machine, artifacts, commands,
   statuses, digests, timings, warnings, and unexercised capabilities.
5. If no real qualifying environment exists, record platform `NOT_RUN` and pause. The
   source work order requires both macOS and Linux; WO40-J cannot waive Linux without
   an explicit user amendment.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 qualify-release --platform linux-x86_64 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-H
```

Acceptance: the full committed Linux matrix passes and binds the exact candidate and
artifact, or final release completion remains blocked with truthful `FAIL`/`NOT_RUN`.

Commit: `Qualify Linux release artifacts`

### WO40-I — Preregistered 10,000-work-unit performance run

Objective: execute the already committed performance protocol, including exactly
10,000 complete distributed run work units, without changing thresholds after
measurement.

Owned files:

- create `KIRBY2_RELEASE_PERFORMANCE_EVIDENCE.md` only;
- write immutable performance artifacts to the release evidence store;
- do not modify workloads, thresholds, production code, or packages.

Fixed decisions:

1. Refuse execution unless WO40-D protocol/threshold digests, the WO40-E candidate's
   exact `release/performance_runner_sources.lock` blob and reproduced source
   projection, and WO40-F candidate/artifact identities all match.
2. Measure interactive event latency, UI update stability, full-day generation/replay,
   peak memory, ledger/checkpoint growth, analysis-load time, and exactly 10,000
   complete run work units under the committed cells/seeds/backend/resources. Every
   work unit produces one full registered run/result/artifact/audit tuple; attempts or
   shard fragments never count as additional work units.
3. Record machine, OS, architecture, runtime, warmup, repetitions, percentile/statistic
   formulas, aborts, retries, failures, `NOT_EXERCISED`, and raw artifact digests.
4. Operational wall time does not enter simulation/result identity. Worker count/order
   cannot change the 10,000 canonical complete-run results or aggregate.
5. Threshold miss is `FAIL` or `WARNING` exactly as preregistered; it is never rewritten
   after observation.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 qualify-performance --manifest release/performance_thresholds.toml --complete-run-work-units 10000 --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --artifact-store .kirby2/release
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate WO40-I
```

Acceptance: every required workload completes or records its preregistered failure;
the distributed result contains exactly 10,000 unique complete-run logical IDs, each
with a registered full result/artifact/audit record, and deterministic aggregate
evidence.

Commit: `Measure Kirby2 release performance`

### WO40-J — Release closeout evidence and documentation

Objective: run the complete release gate and commit a truthful closeout referencing
the frozen candidate and immutable artifacts without changing either.

Owned files:

- create `KIRBY2_RELEASE_CLOSEOUT.md` only;
- create final release acceptance packet through the existing immutable evidence store;
- do not modify production, packaging, version, protocol, threshold, or user-doc files.

Fixed decisions:

1. The release packet has separate hard rows for: repaired baseline invariants;
   accepted deterministic scenario engineering gates; full-day replay parity;
   historical capability/evidence labels; observability boundary; no-lookahead;
   hostile pack fixtures/no partial install; distributed retry/idempotence; exact
   counterfactual parent linkage; desktop/headless compatible run records; clean-
   install starter demonstration; backup/restore; crash recovery; and learner-state
   version/rebuild. It also has separate macOS, Linux, privacy/redaction, artifact,
   and preregistered performance/10,000-complete-run-work-unit rows. One aggregate line cannot hide
   any red or not-run row.
2. Packet binds candidate commit, source/build provenance, artifact/dependency/pack
   digests, schemas, protocol/threshold manifests, machine/platform evidence,
   automated/statistical/platform statuses, warnings, `NOT_EXERCISED`, human review
   statuses, and limitations.
3. Both macOS and Linux must pass. A missing platform cannot be silently unadvertised;
   changing minimum targets requires an explicit user amendment and a new protocol.
4. Old audit packets certify only their old scope. New evidence does not erase the
   inherited warnings.
5. Valid claim is bounded to an installable deterministic locally auditable synthetic
   execution-training environment. Do not claim empirical real-market resemblance,
   unsupported observed historical Level 2, validated mastery/education, profitable
   strategies, or live execution suitability.
6. `audit-expansion --gate all` first runs the non-self-referential
   `WO40_J_PREREQUISITES_V1` gate preregistered in WO40-D. Only after that aggregate
   passes may `close-release` create the immutable closeout packet and this Markdown
   summary. `verify-release-closeout` then verifies their exact bytes and references;
   no prerequisite command reruns one-time qualification/holdout/platform/performance
   work.

Required evidence:

```text
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-expansion --gate all
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 audit-model-risk-lab
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 verify-installation
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 close-release --build-evidence KIRBY2_RELEASE_BUILD_EVIDENCE.md --macos-evidence KIRBY2_RELEASE_MACOS_EVIDENCE.md --linux-evidence KIRBY2_RELEASE_LINUX_EVIDENCE.md --performance-evidence KIRBY2_RELEASE_PERFORMANCE_EVIDENCE.md --output KIRBY2_RELEASE_CLOSEOUT.md
PYTHONDONTWRITEBYTECODE=1 ./.venv/bin/python -m kirby2 verify-release-closeout KIRBY2_RELEASE_CLOSEOUT.md
git diff --check
```

Acceptance: both target platforms, all hard gates, artifact workflows, recovery,
backup, replay, privacy, hostile input, and 10,000-complete-run-work-unit performance pass; warnings
and pending human reviews remain separate and visible.

Commit: `Close the first Kirby2 release candidate`

## 19. Hard pause gates

Pause the goal only when:

- unrelated or user-owned work overlaps an active owned path;
- a sealed-layer defect requires a separately documented amendment;
- an invariant, replay, provenance, identity, observability, privacy, or pack-security
  gate fails and cannot be repaired within the active card;
- a new third-party dependency, native GUI framework, cryptographic implementation,
  external credential, or broader network authority is required;
- migration, cleanup, or removal would rewrite/delete immutable or user-owned data;
- a human decision is a genuine prerequisite of the next card;
- holdout information was exposed before candidate selection was frozen;
- an advertised platform lacks a real qualification environment.

The following truthful outcomes do not automatically stop engineering progress:

- `PENDING` human review;
- optional `NOT_EXERCISED` capability;
- fewer defensible lessons than hoped;
- `NO_CANDIDATE_MET_CRITERIA` for ordinary discovery experiments; the specifically
  preregistered WO35-F1 controlled acceptance experiment is a hard gate and pauses if
  it does not produce its declared validation-improved candidate;
- an unsigned but safely validated pack;
- a measured warning below a non-hard exploratory target.

## 20. Source work-order traceability

Every numbered source requirement and acceptance clause maps below. `REVISED` means
the engineering requirement remains, but a later user instruction or a truth boundary
changes the claim; it never means omitted.

### 20.1 Batch instructions

| Source | Canonical location | Resolution |
|---|---|---|
| B.1 record repaired baseline commit | section 2, K2X-01 | Records closeout HEAD and audited implementation commit separately. |
| B.2 run existing audit/replay commands | K2X-01 | Literal non-persisting commands and outputs. |
| B.3 record baseline in run ledger | K2X-01 | **REVISED:** verify/reference the already-ledgered sealed packet; do not create a duplicate. |
| B.4 do not reopen repaired architecture without blocker | sections 3, 5.5, 19 | Amendment requires reproducer and separate commit. |
| B.5 prefer existing interfaces | section 3 and every owned-file list | Parallel owners are explicitly refused. |
| B.6 preserve ticks, determinism, immutable evidence, observability, no-lookahead, replay, invariants, minimization | sections 3-5 and all audits | Global laws plus per-card evidence. |
| B.7 no second test framework | section 5.2, K2X-02 | Existing audit/invariant/accepted-scenario systems only. |
| B.8 no live brokerage/order submission | sections 1, 3, 19; WO35-F; WO40-J | Explicitly outside scope. |
| B.9 existing format; TOML for new human configuration | section 3; WO31-H; WO32-A; WO33-A1; WO35-D; WO40-D | Canonical JSON only for machine contracts/evidence. |
| B.10 report files, commands/output, status, limitations, commit | sections 5.3-5.4 | Mandatory card gate. |
| B.11 finish active work order then stop | sections 1 and 21 | **REVISED by current request:** one bounded card and commit, then automatic retrieval/continuation. |
| Dependency-order amendment | sections 1, 6, 15-17 | WO39-A-C intentionally precede WO38 to establish one secure bundle/CAS substrate; WO39-D1-E remain after orchestration. |

### 20.2 Work Order 31

| Source | Canonical cards | Resolution |
|---|---|---|
| 31.1 complete `FullDayPlan` fields | WO31-A | Frozen v1 IR explicitly maps market/instrument profiles, calendar/phases, pressure profiles, macro schedule, local transition model, participants/events/shocks/halt/seed/checkpoints. |
| 31.2 hierarchical day/local states | WO31-A, WO31-B | All requested states; ambiguous absorption/momentum are side-explicit. |
| 31.3 semi-Markov/duration-aware transitions | WO31-A, WO31-B | Integer categorical durations, ages, triggers, successors, weights. |
| 31.4 evolving participant populations | WO31-E1, WO31-F | Makers increase near open; metaorders begin/end; LPs withdraw in shocks; auction agents activate near close; noise falls midday; distressed flow follows its event, all through normal interfaces. |
| 31.5 all scheduled event types/no target price | WO31-F | Eight exact types and imperative-price prohibitions. |
| 31.6 deterministic unscheduled events | WO31-F | Stable shock substream, recorded candidate/acceptance path. |
| 31.7 full checkpoints for seek/resume/branch/extract/distributed rerun and all state | WO31-A, WO31-C, WO31-D, WO31-E1-E6, WO31-G, WO38-C | Frozen inventory, strict envelope, fresh-process adapters, artifact transfer. |
| 31.8 generate/inspect/seek/extract commands | WO31-G | Exact command names and target-cut semantics. |
| 31.9 complete `DaySummary` | WO31-G | Exact OHLC/volume/trade/spread/depth/volatility/state/mechanics/agent/invariant/replay formulas. |
| 31.10 four accepted profiles | WO31-H, WO31-I, WO31-I1 | **REVISED:** qualification code is frozen before one-time evidence; four causal-pressure candidates can be automated-qualified/review-ready; human `ACCEPTED` requires a sidecar. |
| 31.11 random-window plausibility inspection | WO31-H, WO31-I1 | Preregistered independent window RNG, strata, blinded packet; human judgment stays pending. |
| 31.12 generation/memory/checkpoint/replay/ledger measurement | WO31-A, WO31-F, WO31-H, WO31-I, WO31-I1 | Pilot limits and qualification implementation are frozen before evidence; raw bottlenecks are then measured without source changes. |
| 31 acceptance close/reopen/seek/replay and coherent context | WO31-C-G, WO31-I1 | Fresh-process suffix parity, durable artifacts, arbitrary target seek, contextual extraction. |

### 20.3 Work Order 32

| Source | Canonical cards | Resolution |
|---|---|---|
| 32.1 every `ScenarioSource` section | WO32-A | Includes historical constraints, curriculum metadata, reveal policy, envelopes, and all other named sections. |
| 32.2 reusable definitions/imports | WO32-B | Exact market/venue/latency/agent-population/regime/historical-source/objective-template types with confined deterministic imports. |
| 32.3 single inheritance/explicit override | WO32-B | Type-specific merge; multiple inheritance refused. |
| 32.4 explicit units | WO32-A | Exact `duration_ms`, `price_ticks`, `quantity_shares`, `rate_per_second`, `latency_us`, and exact `volume_multiplier`; ambiguous numbers fail. |
| 32.5 fully resolved immutable plan | WO32-A-D | References/defaults/units/schemas/capabilities/digests/warnings/provenance materialized. |
| 32.6 every static validation family | WO32-B, WO32-D | Includes unsupported orders, negative quantities, crossed starts, capability/no-lookahead/resource/state cases. |
| 32.7 lint/compile/explain/diff/run | WO32-E | Exact noncolliding `scenario-source` family; legacy `scenario NAME` remains intact. |
| 32.8 eight `explain` questions | WO32-E | Eight named sections plus identity/defaults/units. |
| 32.9 semantic diff after inheritance | WO32-E | Semantic paths separate from source-only changes. |
| 32.10 no arbitrary Python/shell/code | WO32-B-C | Closed bounded expressions only. |
| 32.11 six valid scenarios | WO32-E | Quiet day, opening momentum, hidden liquidity, fragmented venue, reconstruction, halt/reopen. |
| 32.12 invalid scenarios/useful diagnostics | WO32-B, WO32-D-E | Stable span/path/code/explanation/correction and no execution. |
| 32 acceptance declarative run/persist/replay and digest behavior | WO32-A-E | Six end-to-end cases; formatting versus semantic edit proven. |

### 20.4 Work Order 33

| Source | Canonical cards | Resolution |
|---|---|---|
| 33.1 every `LessonCandidate` field | WO33-A | Exact bounds, summaries, estimates, detector/objective/reveal/ambiguity/capability/digest/review fields. |
| 33.2 all 22 detector families | WO33-A1, WO33-B1-B2 | Every named detector has a distinct ID; bid/ask and expansion/recovery remain separate. |
| 33.3 truth may locate but not leak | WO33-A, WO33-B1-B2, WO33-D | Assessment and reveal payloads are separate. |
| 33.4 sufficient warmup context | WO33-D | Warmup eligibility and information fairness are enforced. |
| 33.5 transparent difficulty inputs | WO33-A1, WO33-C | All eleven named inputs, exact units/formula/missing behavior. |
| 33.6 deduplication inputs | WO33-A1, WO33-C | Window, feature, regime, sequence, objective, ancestry. |
| 33.7 diversity selection | WO33-A1, WO33-C | Preregistered weights and shortfall behavior. |
| 33.8 proposal/review states and sidecars | WO33-A, WO33-E | `PROPOSED` lifecycle plus `PENDING` human state; immutable decisions. |
| 33.9 mine/list/inspect/accept/build commands | WO33-E | Exact command names and human-accept authority boundary. |
| 33.10 built-lesson lineage | WO33-D | All seven fields: source run, exact bounds, checkpoint, feed policy, reveal policy, historical provenance, detector version. |
| 33.11 five-source matrix | WO33-A1, WO33-E | Exact source/adapter/config/seed/digest/capability rows for quiet, event-driven, hidden, fragmented, and historical sources; outcomes unseen until E. |
| 33.12 manually inspect twenty/report six categories | WO33-A1, WO33-E | Technical packet has all categories; actual human judgments remain `PENDING`. |
| 33 acceptance five distinct accepted lessons/source parity | WO33-D-E | **REVISED:** parity is hard; five review-ready items are automated, five human acceptances require real sidecars. |

### 20.5 Work Order 34

| Source | Canonical cards | Resolution |
|---|---|---|
| 34.1 all 23 initial skill families | WO34-A | Exact IDs enumerated. |
| 34.2 dependency examples | WO34-A | All four required prerequisite relations. |
| 34.3 every learner-state field | WO34-B | Mastery/confidence/count/history/recency/diversity/success/failure/errors plus sufficiency. |
| 34.4 evidence from session metrics, not P&L | WO34-A-B | All named evidence families; P&L auxiliary only. |
| 34.5 complete error taxonomy | WO34-A | All thirteen requested errors plus ambiguity/refusal states. |
| 34.6 guided/practice/assessment/remediation | WO34-C | Guided teaches/explains one concept; practice adaptively mixes with feedback; assessment hides/restricts/fixes scoring; remediation targets diagnosed errors. |
| 34.7 all selection dimensions | WO34-C | Weakness through historical/synthetic exposure. |
| 34.8 prevent narrow memorization | WO34-C | Seed/symbol/queue/regime semantic diversity. |
| 34.9 explain every recommendation | WO34-C | Evidence, uncertainty, exclusions, tie-breaks. |
| 34.10 manual curriculum override | WO34-C | Valid immutable plan supersedes adaptive ranking in scope, but cannot bypass prerequisites, assignment/assessment locks, consent, capability, reveal, or observability gates. |
| 34.11 immutable learner updates/model versions | WO34-A-B, WO34-D | Rebuildable projections; no past-score rewrite. |
| 34.12 exact six synthetic learners | WO34-D | Reader/execution, reader/hotkey, aggression, passivity, hidden confusion, new learner. |
| 34 acceptance differentiated explained sequences/broad cold start | WO34-C-D | Hard routing audit; educational validity remains unclaimed. |
| 34 guardrail no gamification | WO34-C | No XP, loot, streak economy, achievements, or public ranks. |

### 20.6 Work Order 35

| Source | Canonical cards | Resolution |
|---|---|---|
| 35.1 parsed AST/state machine, not source text | WO35-A, WO35-C | Typed canonical AST and semantic transforms. |
| 35.2 all 15 mutation types | WO35-C | Each requested operator has a distinct versioned ID. |
| 35.3 mutation metadata | WO35-A, WO35-C | Parent/child/diff/operation/rule/reasons/complexity/digests. |
| 35.4 five bounded search methods | WO35-D, WO35-F, WO35-F1 | Grid/random/coordinate/beam/evolutionary through one interface; controlled evidence runs only after machinery freeze. |
| 35.5 train/validation/holdout/adversarial partitions | WO35-B, WO35-D, WO35-F-F1 | Ancestry separation and one-time holdout reveal after clean implementation commit. |
| 35.6 six complexity counts/simple tie preference | WO35-C-D | Conditions/features/states/transitions/windows/parameters. |
| 35.7 all non-P&L objectives | WO35-D | Exact objective list; P&L never sole. |
| 35.8 no lookahead/hidden leakage | WO35-C, WO35-E-F | Compile/runtime observability gates. |
| 35.9 robustness probe families | WO35-E | Seven mandatory threshold/window/latency/fee/volume/liquidity/regime families plus capability-declared venue `NOT_APPLICABLE`; no venue-robustness claim. |
| 35.10 lineage browser fields | WO35-A, WO35-F-F1 | Ancestor/mutation/train/validation/holdout/rejection/descendants, with sealed fields hidden until evidence-only reveal. |
| 35.11 discover/inspect-lineage/compare commands | WO35-F | Exact commands and stored lineage. |
| 35.12 all seven overfit patterns | WO35-E | Every requested pattern operationalized before reveal. |
| 35 acceptance improved and rejected-overfit candidates | WO35-E, WO35-F-F1 | Frozen machinery then one evidence-only real-Kirby2 experiment must produce the validation-improved candidate and reject its training-star overfit candidate; `NO_CANDIDATE_MET_CRITERIA` remains valid elsewhere but hard-pauses this fixture. |

### 20.7 Work Order 36

| Source | Canonical cards | Resolution |
|---|---|---|
| 36.1 every synchronized pane | WO36-C, WO36-E | All 17 source panes plus the mechanistic-trace view, including heatmap, queue, lifecycle, traffic light, agents, metrics, and comparison. |
| 36.2 observed/postmortem modes | WO36-B, WO36-D | Enforced query policies and unmistakable watermark. |
| 36.3 immutable ledger source of truth | WO36-A-C | Source-linked read models; no summary reconstruction. |
| 36.4 every timeline control | WO36-C, WO36-E | Play/pause/event/time steps, all six jumps, bookmark, annotation. |
| 36.5 full causal chain | WO36-A | Observable event through feature/rule/light/player/client/routing/receipt/queue/fill-cancel/adverse-selection, with ID-linked gaps unavailable. |
| 36.6 strategy-state reasons | WO36-A, WO36-C | Recorded rule evidence, never later recomputation. |
| 36.7 data age | WO36-B | Source/venue/client/render/action timestamps. |
| 36.8 parent/counterfactual comparison | WO36-E | Prefix, first difference, orders/queues/fills/metrics/path. |
| 36.9 all nine overlays | WO36-C, WO36-E | Exact versioned overlay inventory. |
| 36.10 portable report contents | WO36-D-E | References/bookmarks/annotations/snapshots/traces/comparison/metrics/provenance. |
| 36.11 named stale/partial/cancel-race multivenue-hidden scenario | WO36-A, WO36-D-E | Explicit multivenue hidden-liquidity fixture with stale quote, partial fill, cancel race, and adverse selection. |
| 36.12 manual timing-lie inspection | WO36-E | Rubric/evidence generated; human result remains `PENDING`. |
| 36 acceptance action-to-consequence trace | WO36-A-E | Every player action in the new acceptance fixture has the complete immutable event chain; `UNAVAILABLE` is limited to legacy/insufficient sources. |

### 20.8 Work Order 37

| Source | Canonical cards | Resolution |
|---|---|---|
| 37.1 all nine named entities | WO37-A-C | Exact model vocabulary. |
| 37.2 all assignment fields | WO37-B | Lesson/pool through consent metadata, including attempt/feedback/hotkeys. |
| 37.3 all eight lockable parameters | WO37-B | Runtime-enforced attempt bindings. |
| 37.4 immutable assignment versions | WO37-B | Corrections supersede; attempts retain original. |
| 37.5 every review operation | WO37-B, WO37-D, WO36 | Open/replay/trace/compare/annotate/tag/feedback/complete/rubric. |
| 37.6 annotation sidecars | WO37-B | Source run/event bytes untouched. |
| 37.7 all six comparison views | WO37-D | Exact learner/lesson/skill/hotkey/strategy/manual-benchmark views. |
| 37.8 cohort uncertainty/sample count | WO37-C-D | Counts/denominators/missingness/uncertainty shown. |
| 37.9 complete research-study manifest | WO37-C | Question through export policy, including software/analysis/metrics. |
| 37.10 privacy controls | WO37-A, WO37-E | **REVISED:** local mapping, redaction, deletion/retention consent, and no telemetry; retained evidence is called pseudonymous rather than falsely anonymous. |
| 37.11 complete portable evidence export | WO37-E | Assignment/attempt/scores/annotations/traces/provenance/version/limitations. |
| 37.12 exact demonstration data | WO37-D | Two learners, one hidden-liquidity assignment, three attempts each, all six attempts reviewed/annotated, rubric and cohort comparison. |
| 37 acceptance hidden-liquidity assignment/review/export without mutation | WO37-B, WO37-D-E | End-to-end local workflow and redacted clean-root evidence. |
| 37 guardrail local-first/no platform economy | sections 1, 3; WO37-D | No cloud accounts, subscriptions, social feeds, public leaderboards, network service, or telemetry. |

### 20.9 Work Order 38

| Source | Canonical cards | Resolution |
|---|---|---|
| 38.1 complete immutable work manifest | WO38-A | Includes exact scenario/profile/software/config/data/strategy/pack/seed/schema/capability/result/resource identity; attempts stay operational. |
| 38.2 content-derived idempotent identity | WO38-A | Leases/workers excluded. |
| 38.3 coordinator responsibilities | WO38-A-E | Plan/queue/assign/verify/retry/aggregate/persist. |
| 38.4 worker responsibilities | WO38-B-D | Fetch/validate/run/audit/return data-only artifacts. |
| 38.5 single/local/LAN backends | WO38-B, WO38-D | One protocol; TLS/mTLS for explicit LAN. |
| 38.6 deterministic seeds/results/order | WO38-A-B, WO38-E | Logical IDs and canonical aggregation. |
| 38.7 leasing/recovery | WO38-D-E | Heartbeats, expiry, reissue, restart state. |
| 38.8 content-addressed artifacts | WO39-A-C, WO38-C | Secure pack substrate precedes transfer. |
| 38.9 version compatibility | WO38-A-D | Exact engine/runtime/dependency/schema/compiler/capability identity. |
| 38.10 resource controls | WO38-D-E | Max concurrency, per-run memory/disk/time, backpressure, whole-experiment cancel, and temp cleanup. |
| 38.11 coordinator/worker/submit/status/cancel/resume | WO38-E | Exact command family plus plan. |
| 38.12 kill/restart demonstration | WO38-B, WO38-E | Complete multi-seed strategy experiment with reference, local plus extra process/LAN worker, worker kill, coordinator restart, and full comparison. |
| 38 acceptance no lost/duplicate seeds and reference equality | WO38-E | Logical uniqueness, idempotence, deterministic aggregate. |

### 20.10 Work Order 39

| Source | Canonical cards | Resolution |
|---|---|---|
| 39.1 every `.k2pack` manifest field | WO39-A | Includes title, creator, IDs, compatibility, digests, provenance/license/entrypoints/inventory. |
| 39.2 eight initial pack types | WO39-D1-D2 | Five training/model plus historical/replay/analysis; research is extra. |
| 39.3 reuse canonical formats | WO39-A, WO39-D1-D2 | TOML/Parquet/normalized data/report data; no duplicate IR. |
| 39.4 all import protections | WO39-B-C, WO39-E | Paths/limits/digests/schema/capability/quarantine/atomicity. |
| 39.5 no executable content | WO39-A-B | Declarative content still passes safe compiler; renderer code stays installed. |
| 39.6 namespace/version policy | WO39-A-C | Canonical creator ID, lowercase dotted namespace/name, immutable full key, exact SemVer, digest-bound unambiguous local resolution. |
| 39.7 dependency policy/no Internet fetch | WO39-A, WO39-C | Local exact version/digest resolution before activation. |
| 39.8 build/inspect/verify/install/list/remove/export-run | WO39-C, WO39-D1-D2, WO39-E | Exact lifecycle command family. |
| 39.9 installed content separate from runs | WO39-C, WO39-D1-D2 | Registry never mutates immutable evidence. |
| 39.10 signing interface/optional dependency/unsigned label | WO39-E | Provider interface only; signature cannot override safety. |
| 39.11 five sample groups | WO39-E | Starter, five-lesson, traffic-light, reconstruction, completed replay/analysis. |
| 39.12 eight hostile fixtures | WO39-B, WO39-E | All exact source attacks plus expanded hostile matrix. |
| 39 acceptance completed lesson/replay clean-root digest and no partial hostile install | WO39-E | Offline second-root parity and atomic refusal. |

### 20.11 Work Order 40

| Source | Canonical cards | Resolution |
|---|---|---|
| 40.1 desktop and headless forms | WO40-D-F | Terminal live trainer plus offline analysis/authoring bundle; CLI worker form. |
| 40.2 macOS/Linux minimum; Windows only without detour | WO40-D-D1, WO40-F-H | macOS arm64 and Linux x86_64 hard targets; wheelhouses/providers preflight before freeze; Windows deferred. |
| 40.3 all ten standard data locations | WO31-C, WO40-A | Config/packs/datasets/runs/checkpoints/logs/crash/temp/exports/backups plus governed extras. |
| 40.4 every first-run behavior | WO40-C, WO40-E | Flow itself creates/verifies/shows version/runs health/installs-or-offers starter/launches demo/prints paths, offline. |
| 40.5 crash-recovery state and truthful refusal | WO40-B | Exact durable checkpoint/action/client/pending state only. |
| 40.6 backup/restore selection | WO40-B1 | Every requested mutable/evidence family and dataset embed/reference policy. |
| 40.7 five diagnostics commands | WO40-C | Exact names, allowlisted redaction. |
| 40.8 release manifest fields | WO40-D, WO40-F, WO40-J | Exact embedded ReleaseManifestV1 version/commit/build time/runtime/dependencies/schemas/packs/supported OS/limitations/member and subordinate-artifact digests; external ReleaseArtifactIndexV1 binds manifest and transport digests without self-reference. |
| 40.9 dependency/license inventory | WO40-D-F | Frozen before release and verified in artifacts. |
| 40.10 upgrade handling | WO40-A | Detect/backup/migrate/refuse downgrade/preserve runs. |
| 40.11 every named release acceptance gate | WO40-J with WO31-I1, WO32-E, WO34-B, WO36-B/E, WO38-E, WO39-E, WO40-B/B1/C/G/H/I and inherited audits | Separate hard rows; no red aggregation. |
| 40.12 all performance metrics/preregistered thresholds | WO40-D, WO40-I | Interactive/UI/full-day/memory/growth/analysis plus exactly 10,000 complete registered run work units fixed before run. |
| 40.13 clean-environment platform matrix | WO40-D1, WO40-G-H | Real providers preflighted; exact launch/starter/order/save/microscope/export/close/reopen/verify/restore/headless/uninstall matrix. |
| 40.14 all documentation topics | WO40-E | Installation through troubleshooting plus security/privacy. |
| 40.15 five explicit product disclaimers | WO40-E, WO40-J | Synthetic training, not broker/connector/guarantee/historical proof. |
| 40.16 no updater/telemetry/accounts/cloud/brokerage/subscription/leaderboard | sections 1, 3, 19; WO40-C/E/J | Explicitly refused. |
| 40 acceptance clean install through second-root replay digest | WO40-D-D1, WO40-F-J | Both artifacts/platforms and full matrix; only integer-core cross-platform bytes are claimed exact, with same-platform desktop/headless parity. |

## 21. Goal completion statement

After K2X-00 is committed, create the long-running goal with this objective:

> Execute the canonical sequence in
> `KIRBY2_WORK_ORDERS_31_40_GOAL.md`, beginning with K2X-01. Work on exactly one
> bounded card at a time. For each card, preserve every design law and truth boundary,
> produce the literal runtime evidence, report actual files/statuses/limitations,
> commit with the prescribed subject, verify the commit and clean worktree, then
> automatically retrieve and begin the next card. Do not combine cards, weaken gates,
> rewrite immutable evidence, grant human acceptance, expose holdouts early, claim
> unsupported historical or platform capability, add live brokerage behavior, or
> push. Follow the amendment protocol for concrete unforeseen blockers. Stop only at
> an explicit hard pause gate or after WO40-J has committed truthful release-candidate
> closeout evidence.

The sequence does not end by declaring every scientific or human question solved. It
ends when the engineering artifacts and evidence exist, every advertised hard gate is
truthfully satisfied, and remaining warnings, pending reviews, unavailable evidence,
and bounded claims are preserved rather than hidden.
